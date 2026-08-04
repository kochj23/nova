#!/usr/bin/env python3
"""Tests for nova_nas_manifest_sync.py — all 7 categories (Jordan's rule).

Everything runs on LOCAL temp dirs / fakes; nothing touches the real NAS.
The PG integration test runs inside a rolled-back transaction (skipped if PG
is unreachable) so it never pollutes real telemetry.
"""
import importlib
import os
import shutil
import subprocess

import pytest

m = importlib.import_module("nova_nas_manifest_sync")


# ── shared helpers ────────────────────────────────────────────────────────────
def tree_manifest(root):
    """Build {relpath: size} from a real dir tree, applying the same excludes."""
    out = {}
    for dp, _, files in os.walk(root):
        for fn in files:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, root)
            if m.is_excluded(rel):
                continue
            out[rel] = os.path.getsize(full)
    return out


def mkfile(root, rel, data=b"x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


# ═══════════════════════════════ 1. UNIT ═════════════════════════════════════
def test_unit_parse_manifest_line():
    assert m.parse_manifest_line("dir/file.txt\t42") == ("dir/file.txt", 42)
    assert m.parse_manifest_line("a\tb\t99") == ("a\tb", 99)   # split on LAST tab
    assert m.parse_manifest_line("nodelim") is None
    assert m.parse_manifest_line("file\tNaN") is None
    assert m.parse_manifest_line("") is None


def test_unit_exclude_filtering():
    assert m.is_excluded("x/@eaDir/thumb.jpg")
    assert m.is_excluded("#recycle/old")
    assert m.is_excluded("a/.DS_Store")
    assert m.is_excluded(".nova-trash/nas/2026-08-04/f")
    assert m.is_excluded("p/.Spotlight-V100/q")
    assert not m.is_excluded("normal/file.txt")
    assert not m.is_excluded("eaDirNotSegment/x")   # substring, not a segment


def test_unit_diff_add_changed_orphan():
    src = {"keep": 10, "changed": 20, "added": 30}
    dst = {"keep": 10, "changed": 999, "orphan": 5}
    to_copy, orphans = m.diff_manifests(src, dst)
    assert sorted(to_copy) == [("added", 30), ("changed", 20)]   # missing + size-diff
    assert orphans == [("orphan", 5)]                            # dest-only
    assert ("keep", 10) not in to_copy                           # identical -> skipped


def test_unit_uuid_glob_resolution():
    good = "/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17/.srv/.unifi-drive\n"
    assert m.parse_uuid_root(good) == good.strip()
    # unexpanded glob (no volume present) must NOT resolve -> caller aborts
    assert m.parse_uuid_root("/volume/*/.srv/.unifi-drive") is None
    assert m.parse_uuid_root("") is None


# ═══════════════════════════ 2. INTEGRATION ══════════════════════════════════
def test_integration_diff_over_two_temp_trees(tmp_path):
    src = tmp_path / "src"; dst = tmp_path / "dst"
    mkfile(str(src), "a/keep.txt", b"same")
    mkfile(str(src), "a/changed.txt", b"NEWCONTENT")
    mkfile(str(src), "b/added.txt", b"brand new")
    mkfile(str(dst), "a/keep.txt", b"same")
    mkfile(str(dst), "a/changed.txt", b"old")           # different size
    mkfile(str(dst), "z/orphan.txt", b"remove me")
    mkfile(str(dst), "@eaDir/meta", b"ignore")          # excluded both sides

    to_copy, orphans = m.diff_manifests(tree_manifest(str(src)), tree_manifest(str(dst)))
    copy_paths = {p for p, _ in to_copy}
    orphan_paths = {p for p, _ in orphans}
    assert copy_paths == {"a/changed.txt", "b/added.txt"}
    assert orphan_paths == {"z/orphan.txt"}
    assert "@eaDir/meta" not in copy_paths and "@eaDir/meta" not in orphan_paths


def test_integration_pg_staging_upsert_roundtrip(tmp_path):
    try:
        import psycopg2
        conn = psycopg2.connect(m.DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PG unavailable: {e}")
    import csv
    share = "_pytest_share"

    def write_csv(p, rows):
        # matches clean_to_file's CSV output (QUOTE_MINIMAL, \n terminator)
        with open(p, "w", newline="") as f:
            csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n").writerows(rows)

    # REGRESSION: a filename with an embedded tab, newline, comma and quote — the
    # weird-char class that used to corrupt the tab/newline-delimited load and
    # collide on the manifest PK. It must survive COPY and round-trip exactly.
    weird = 'w/ta\tb\nnl,q".jpg'
    try:
        s1 = tmp_path / "s1.csv"
        write_csv(s1, [["a/x", 100], ["b/y", 200], [weird, 55]])
        prev = m.pg_replace_manifest(conn, "syno", share, str(s1))
        assert prev == 0                                   # first load, no prior rows
        # dest manifest: b/y size differs, plus an orphan c/z
        d1 = tmp_path / "d1.csv"
        write_csv(d1, [["b/y", 999], ["c/z", 7]])
        m.pg_replace_manifest(conn, "unas", share, str(d1))
        to_copy, orphans = m.sql_diff(conn, share)
        assert sorted(to_copy) == sorted([("a/x", 100), ("b/y", 200), (weird, 55)])
        assert orphans == [("c/z", 7)]
        # the weird path round-tripped through Postgres byte-for-byte
        assert (weird, 55) in to_copy
        # replace source (upsert semantics) -> prev now reflects prior 3 rows
        s2 = tmp_path / "s2.csv"
        write_csv(s2, [["a/x", 100]])
        prev2 = m.pg_replace_manifest(conn, "syno", share, str(s2))
        assert prev2 == 3
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {m.MANIFEST_TABLE} WHERE box='syno' AND share=%s",
                        (share,))
            assert cur.fetchone()[0] == 1                  # replaced, not appended
    finally:
        conn.rollback()   # nothing persists to real telemetry
        conn.close()


# ════════════════════════════ 3. FUNCTIONAL ══════════════════════════════════
def test_functional_dry_run_plan(tmp_path):
    src = tmp_path / "src"; dst = tmp_path / "dst"
    mkfile(str(src), "keep", b"aaa"); mkfile(str(dst), "keep", b"aaa")
    mkfile(str(src), "new", b"bbbb")
    mkfile(str(dst), "gone", b"cc")
    to_copy, orphans = m.diff_manifests(tree_manifest(str(src)), tree_manifest(str(dst)))
    assert {p for p, _ in to_copy} == {"new"}
    assert {p for p, _ in orphans} == {"gone"}


def test_functional_real_copy_and_quarantine_local(tmp_path):
    """Apply the plan locally (simulating rsync copy + ssh-local quarantine mv):
    dest must end == src, and orphans must land in the dated trash dir."""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    trash_root = str(tmp_path / ".nova-trash")
    mkfile(str(src), "a/keep", b"same"); mkfile(str(dst), "a/keep", b"same")
    mkfile(str(src), "a/changed", b"NEWBIGGER"); mkfile(str(dst), "a/changed", b"old")
    mkfile(str(src), "b/added", b"added file")
    mkfile(str(dst), "junk/orphan", b"delete me")

    to_copy, orphans = m.diff_manifests(tree_manifest(str(src)), tree_manifest(str(dst)))

    # copy (rsync stand-in)
    for rel, _ in to_copy:
        sp = os.path.join(str(src), rel); dpth = os.path.join(str(dst), rel)
        os.makedirs(os.path.dirname(dpth), exist_ok=True)
        shutil.copy2(sp, dpth)
    # quarantine (ssh-local mv stand-in), using the real path builder
    day = "2026-08-04"
    for rel, _ in orphans:
        qp = m.quarantine_dest(trash_root, "nas", day, rel)
        os.makedirs(os.path.dirname(qp), exist_ok=True)
        shutil.move(os.path.join(str(dst), rel), qp)

    assert tree_manifest(str(dst)) == tree_manifest(str(src))          # dest == src
    assert os.path.exists(os.path.join(trash_root, "nas", day, "junk/orphan"))
    assert not os.path.exists(os.path.join(str(dst), "junk/orphan"))    # orphan gone from dest


# ═════════════════════════════ 4. SECURITY ═══════════════════════════════════
@pytest.mark.parametrize("nasty", [
    "dir with spaces/file.txt",
    "weird/$(rm -rf ~).txt",
    "back`whoami`tick.txt",
    "semi;colon&pipe|amp.txt",
    "quote'and\"quote.txt",
])
def test_security_metachars_parsed_literally(nasty):
    # parses as data, never executes; size preserved exactly
    line = f"{nasty}\t123"
    assert m.parse_manifest_line(line) == (nasty, 123)
    assert not m.is_excluded(nasty)


def test_security_nul_list_rejects_traversal_and_absolute(tmp_path):
    out = str(tmp_path / "list.nul")
    n = m.write_nul_list(["ok/file.txt", "../escape", "/abs/path", "a/../../b"], out)
    assert n == 1                                        # only the safe one written
    with open(out, "rb") as f:
        payload = f.read()
    assert payload == b"ok/file.txt\x00"
    assert b"escape" not in payload and b"/abs/path" not in payload


def test_security_quarantine_cannot_escape_trash():
    tr = "/vol/.nova-trash"
    assert m.quarantine_dest(tr, "nas", "2026-08-04", "a/b/c").startswith(tr + "/nas/2026-08-04/")
    for bad in ["../../etc/passwd", "/etc/passwd", "a/../../../x"]:
        with pytest.raises(ValueError):
            m.quarantine_dest(tr, "nas", "2026-08-04", bad)


def test_security_shquote_neutralizes_metachars():
    q = m.shquote("$(rm -rf ~); echo pwned")
    assert q.startswith("'") and q.endswith("'")        # fully single-quoted -> inert


# ═══════════════════════════ 5. PERFORMANCE ══════════════════════════════════
def test_performance_100k_diff_is_setbased(tmp_path):
    import time
    n = 100_000
    src = {f"path/{i}/file{i}": i for i in range(n)}
    dst = dict(src)
    del dst["path/0/file0"]                # 1 add
    dst["path/1/file1"] = 999_999          # 1 size change
    dst["extra/orphan"] = 1                # 1 orphan
    t0 = time.time()
    to_copy, orphans = m.diff_manifests(src, dst)
    elapsed = time.time() - t0
    assert {p for p, _ in to_copy} == {"path/0/file0", "path/1/file1"}
    assert {p for p, _ in orphans} == {"extra/orphan"}
    assert elapsed < 2.0                   # O(n) — a quadratic diff would blow past this


# ═════════════════════════════ 6. RETRY ══════════════════════════════════════
def test_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class R:
        def __init__(self, rc): self.returncode = rc; self.stdout = ""; self.stderr = "boom"

    def fake_run(argv, **kw):
        calls["n"] += 1
        return R(1) if calls["n"] < 3 else R(0)   # fail twice, then succeed

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    r = m.run_with_retry(["true"], timeout=5, retries=3, backoff=1)
    assert r.returncode == 0 and calls["n"] == 3


def test_retry_persistent_failure_raises(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)

    def always_fail(argv, **kw):
        raise OSError("ssh down")

    monkeypatch.setattr(m.subprocess, "run", always_fail)
    with pytest.raises(RuntimeError):
        m.run_with_retry(["ssh"], timeout=5, retries=3, backoff=1)


def test_retry_persistent_failure_records_notok_and_alerts(monkeypatch):
    """A share that blows up must record ok=false AND alert — never a silent pass."""
    alerts = []
    monkeypatch.setattr(m, "alert", lambda subj, body, critical=False: alerts.append(subj))
    monkeypatch.setattr(m, "resolve_uroot", lambda: "/volume/UUID/.srv/.unifi-drive")
    monkeypatch.setattr(m, "sync_share",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rsync exhausted retries")))

    recorded = []

    class FakeCur:
        def execute(self, q, p=None):
            if "backup_runs" in q:
                recorded.append(p)
        def __enter__(self): return self
        def __exit__(self, *a): pass

    class FakeConn:
        def cursor(self): return FakeCur()
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    import types
    fake_pg = types.SimpleNamespace(connect=lambda *a, **k: FakeConn())
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_pg)

    args = m.argparse.Namespace(dry_run=False, share="nas", max_delete_pct=5.0,
                                max_delete_abs=500, min_src_ratio=0.5)
    rc = m.run(args)
    assert rc == 1                                   # non-zero exit, not false-green
    assert alerts and any("EXCEPTION" in s for s in alerts)
    assert recorded and recorded[-1][-1] is False    # ok=False recorded


# ══════════════════════════════ 7. FRAME ═════════════════════════════════════
def test_frame_selftest_touches_no_nas():
    assert m.selftest() == 0


def test_frame_import_and_help_run():
    r = subprocess.run(
        ["/opt/homebrew/bin/python3",
         os.path.join(os.path.dirname(__file__), "nova_nas_manifest_sync.py"), "--help"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "manifest-diff" in r.stdout


def test_frame_source_guard_blocks_empty(tmp_path):
    # the critical anti-wipe guard: empty source -> no copy, no delete
    allow_copy, allow_delete, reasons = m.evaluate_guards(
        nsrc=0, ndst=3_260_000, n_orphans=3_260_000, prev_src=3_260_000,
        max_pct=5.0, max_abs=500, min_ratio=0.5)
    assert allow_copy is False and allow_delete is False
    assert any("EMPTY" in r for r in reasons)


def test_frame_source_shrink_guard_blocks(tmp_path):
    allow_copy, allow_delete, _ = m.evaluate_guards(
        nsrc=100, ndst=3_260_000, n_orphans=3_259_900, prev_src=3_260_000,
        max_pct=5.0, max_abs=500, min_ratio=0.5)
    assert allow_copy is False and allow_delete is False


def test_frame_orphan_threshold_skips_delete_only():
    allow_copy, allow_delete, reasons = m.evaluate_guards(
        nsrc=3_260_000, ndst=3_260_000, n_orphans=200_000, prev_src=3_260_000,
        max_pct=5.0, max_abs=500, min_ratio=0.5)
    assert allow_copy is True and allow_delete is False   # copy safe, delete blocked
    assert any("exceeds delete guard" in r for r in reasons)
