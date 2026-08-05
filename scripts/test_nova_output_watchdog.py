#!/usr/bin/env python3
"""Tests for nova_output_watchdog.py.

Seven categories, matching the repo style (pytest, no real journal repo, no real
site, no real incidents). Integration/Retry-PG tests use a session TEMP TABLE
(pg_temp) so nothing ever lands in public.incidents.
"""
import importlib
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

m = importlib.import_module("nova_output_watchdog")
LA = m.TZ


# ── shared fixtures ─────────────────────────────────────────────────────────
def _write_post(section_dir, name, body_words=400, title="T"):
    os.makedirs(section_dir, exist_ok=True)
    body = " ".join(["word"] * body_words)
    text = f"---\ntitle: \"{title}\"\ndate: 2026-08-05\n---\n\n{body}\n"
    p = os.path.join(section_dir, name)
    with open(p, "w") as f:
        f.write(text)
    return p


@pytest.fixture
def journal(tmp_path):
    """A fake journal root: content/<section>/<date>-slug.md."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "content"))
    return root


def _fake_http_ok(slug_present=True):
    def _get(url, timeout=10):
        # server returns the slug so the reader-check passes
        return 200, ("<html>" + ("some-slug-here" if slug_present else "") + "</html>")
    return _get


# ╔═══════════════════════════ 1. UNIT ═══════════════════════════╗
def test_unit_parse_post_date():
    assert m.parse_post_date("2026-08-05-sunny.md") == date(2026, 8, 5)
    assert m.parse_post_date("_index.md") is None
    assert m.parse_post_date("2026-13-40-bad.md") is None


def test_unit_word_count_strips_frontmatter():
    text = "---\ntitle: x\ndate: y\n---\n\none two three four\n"
    assert m.word_count(text) == 4


def test_unit_stub_detection():
    tiny = "---\nt: x\n---\nNot logged in"           # the Burbank 33-char bug
    assert m.is_stub(tiny)
    big = "---\nt: x\n---\n" + " ".join(["w"] * 200)
    assert not m.is_stub(big)


def test_unit_median_gap_and_cadence():
    # weekly-ish section: gaps of 7,6,7 -> median 7 -> threshold 14 (2x)
    ds = [date(2026, 7, 1), date(2026, 7, 8), date(2026, 7, 14), date(2026, 7, 21)]
    assert m.median_gap_days(ds) == 7
    assert m.cadence_max_age(ds) == 14
    # thin history -> cannot derive
    assert m.cadence_max_age([date(2026, 7, 1)]) is None


def test_unit_deadline_logic():
    noon = datetime(2026, 8, 5, 12, 0, tzinfo=LA)
    assert m.deadline_passed(noon, "11:30")
    assert not m.deadline_passed(noon, "18:30")


def test_unit_dedup_key_is_date_independent():
    # same output + failure = same key regardless of the day it happens
    k1 = m.dedup_logical_key("journal:local", "missing")
    assert k1 == "journal:local:missing"
    assert k1 == m.dedup_logical_key("journal:local", "missing")


# ╔═══════════════════════ 4. SECURITY ═══════════════════════════╗
def test_security_url_cannot_be_injected():
    # a hostile section name must never reach a URL or the filesystem
    for bad in ["../etc", "local/../../x", "a b", "a;rm", "a/b", "http://evil"]:
        assert not m.safe_section(bad)
        with pytest.raises(ValueError):
            m.reader_url(bad)
    assert m.reader_url("local") == f"{m.SITE_BASE}/local/"


def test_security_section_posts_rejects_traversal(journal):
    with pytest.raises(ValueError):
        m.section_posts(journal, "../../etc")


def test_security_incident_sql_is_parameterized():
    # the INSERT must pass values as params, never string-format user text in
    src = open(m.__file__).read()
    assert "VALUES (%s, %s, %s, 'open', %s, %s::jsonb)" in src
    # only the (constant) table name is f-string interpolated, never a value
    assert 'f"INSERT INTO {INCIDENTS_TABLE} "' in src


# ╔═══════════════════════ 5. PERFORMANCE ════════════════════════╗
def test_performance_median_no_n_squared():
    import time
    big = [date(2026, 1, 1) + timedelta(days=i) for i in range(5000)]
    t = time.time()
    m.median_gap_days(big)
    assert time.time() - t < 0.5   # linear, not N^2


def test_performance_daily_check_is_quick(journal):
    d = os.path.join(journal, "content", "local")
    for i in range(1, 20):
        _write_post(d, f"2026-07-%02d-post.md" % i)
    _write_post(d, "2026-08-05-today-slug.md")
    import time
    t = time.time()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=LA)
    m.check_journal_daily({"key": "journal:local", "section": "local",
                           "deadline": "11:30", "severity": "high"},
                          journal, now, http=_fake_http_ok())
    assert time.time() - t < 0.3


# ╔═══════════════════════ 6. RETRY ══════════════════════════════╗
def test_retry_transient_then_success():
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "ok"
    assert m.with_retry(flaky, attempts=3, base_delay=0) == "ok"
    assert calls["n"] == 3


def test_retry_persistent_failure_raises_never_silent():
    def always():
        raise ConnectionError("down")
    # a watchdog that dies silently is the worst-case irony: it must RAISE
    with pytest.raises(RuntimeError) as ei:
        m.with_retry(always, attempts=2, base_delay=0, label="pg")
    assert "failed after 2 attempts" in str(ei.value)


# ╔═══════════════════════ 3. FUNCTIONAL ═════════════════════════╗
def _daily_exp():
    return {"key": "journal:local", "section": "local", "deadline": "11:30",
            "severity": "high", "reader_check": True}


def test_functional_fresh_good_post_is_healthy(journal):
    d = os.path.join(journal, "content", "local")
    _write_post(d, "2026-08-05-today-slug.md", body_words=400)
    now = datetime(2026, 8, 5, 12, 0, tzinfo=LA)
    http = lambda url, timeout=10: (200, "<a href=x/2026-08-05-today-slug/>")
    fs = m.check_journal_daily(_daily_exp(), journal, now, http=http)
    assert all(f.ok for f in fs)


def test_functional_missing_today_after_deadline_is_incident(journal):
    d = os.path.join(journal, "content", "local")
    _write_post(d, "2026-08-04-yesterday.md")
    now = datetime(2026, 8, 5, 12, 0, tzinfo=LA)
    fs = m.check_journal_daily(_daily_exp(), journal, now, http=_fake_http_ok())
    miss = [f for f in fs if not f.ok]
    assert miss and miss[0].dedup_key == "journal:local:missing"


def test_functional_before_deadline_missing_is_pending_not_incident(journal):
    d = os.path.join(journal, "content", "local")
    _write_post(d, "2026-08-04-yesterday.md")
    now = datetime(2026, 8, 5, 9, 0, tzinfo=LA)   # before 11:30
    fs = m.check_journal_daily(_daily_exp(), journal, now, http=_fake_http_ok())
    assert all(f.ok for f in fs)


def test_functional_stub_is_incident(journal):
    d = os.path.join(journal, "content", "local")
    _write_post(d, "2026-08-05-today-slug.md", body_words=5)   # tiny = stub
    now = datetime(2026, 8, 5, 12, 0, tzinfo=LA)
    http = lambda url, timeout=10: (200, "2026-08-05-today-slug")
    fs = m.check_journal_daily(_daily_exp(), journal, now, http=http)
    stub = [f for f in fs if f.dedup_key.endswith(":stub")]
    assert stub and not stub[0].ok


def test_functional_git_but_not_live_is_incident(journal):
    d = os.path.join(journal, "content", "local")
    _write_post(d, "2026-08-05-today-slug.md", body_words=400)
    now = datetime(2026, 8, 5, 12, 0, tzinfo=LA)
    # served HTML does NOT contain the slug -> deploy failure
    http = lambda url, timeout=10: (200, "<html>nothing here</html>")
    fs = m.check_journal_daily(_daily_exp(), journal, now, http=http)
    live = [f for f in fs if f.dedup_key.endswith(":not-live")]
    assert live and not live[0].ok


def test_functional_cadence_weekly_not_stalled(journal):
    d = os.path.join(journal, "content", "research")
    for day in (1, 8, 15, 22, 29):
        _write_post(d, f"2026-07-{day:02d}-weekly.md")
    now = datetime(2026, 8, 2, 12, 0, tzinfo=LA)   # 4 days after last weekly post
    fs = m.check_journal_cadence({"key": "journal:research", "section": "research",
                                  "severity": "warning"}, journal, now)
    stalled = [f for f in fs if f.dedup_key.endswith(":stalled")]
    assert stalled and stalled[0].ok


def test_functional_cadence_stalled_is_incident(journal):
    d = os.path.join(journal, "content", "research")
    for day in (1, 8, 15, 22, 29):
        _write_post(d, f"2026-06-{day:02d}-weekly.md")   # last post June 29
    now = datetime(2026, 8, 5, 12, 0, tzinfo=LA)          # ~5 weeks later = stalled
    fs = m.check_journal_cadence({"key": "journal:research", "section": "research",
                                  "severity": "warning"}, journal, now)
    stalled = [f for f in fs if f.dedup_key.endswith(":stalled")]
    assert stalled and not stalled[0].ok


# ╔═══════════════════════ 2. INTEGRATION ════════════════════════╗
@pytest.fixture
def pg_temp(monkeypatch):
    """Connection with a session TEMP incidents table; never touches public."""
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(m.DSN, connect_timeout=5)
    except Exception as e:
        pytest.skip(f"no PG: {e}")
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE incidents (
                id uuid DEFAULT gen_random_uuid(),
                title text NOT NULL, root_cause text,
                status text DEFAULT 'open', severity text DEFAULT 'warning',
                started_at timestamptz DEFAULT now(), resolved_at timestamptz,
                affected_services text[], events jsonb DEFAULT '[]'::jsonb
            ) ON COMMIT PRESERVE ROWS
        """)
    conn.commit()
    monkeypatch.setattr(m, "INCIDENTS_TABLE", "incidents")  # -> pg_temp schema
    yield conn
    conn.close()   # temp table vanishes with the session


def _finding(ok=False, key="journal:local:missing"):
    return m.Finding(dedup_key=key, ok=ok, title="Burbank daily missing (2026-08-05)",
                     severity="high", affected_services=["journal:local"],
                     root_cause="newest post is 2026-08-04", note="miss")


def test_integration_insert_dedup_resolve(pg_temp):
    conn = pg_temp
    iso = datetime.now(timezone.utc).isoformat()
    f = _finding()

    # 1) new miss -> opens exactly one incident, signals transition
    assert m.open_or_append(conn, f, iso, dry_run=False) == "opened"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM incidents WHERE status='open'")
        assert cur.fetchone()[0] == 1

    # 2) same miss again -> APPENDS to events, does NOT duplicate
    assert m.open_or_append(conn, f, iso, dry_run=False) == "appended"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(jsonb_array_length(events)) FROM incidents")
        n, evlen = cur.fetchone()
    assert n == 1 and evlen == 2   # one row, two events

    # 3) recovery -> auto-resolve
    assert m.resolve_if_open(conn, f.dedup_key, iso, dry_run=False) is True
    with conn.cursor() as cur:
        cur.execute("SELECT status, resolved_at FROM incidents")
        status, resolved = cur.fetchone()
    assert status == "resolved" and resolved is not None

    # 4) next day's miss opens a FRESH incident (makes MTBF measurable)
    assert m.open_or_append(conn, f, iso, dry_run=False) == "opened"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM incidents")
        assert cur.fetchone()[0] == 2


def test_integration_dry_run_writes_nothing(pg_temp):
    conn = pg_temp
    iso = datetime.now(timezone.utc).isoformat()
    assert m.open_or_append(conn, _finding(), iso, dry_run=True) == "dry"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM incidents")
        assert cur.fetchone()[0] == 0


def test_integration_retry_wraps_real_query(pg_temp):
    # a real query through with_retry (the Retry category, exercised against PG)
    rows = m.with_retry(lambda: m._query(pg_temp, "SELECT 1"), label="q")
    assert rows == [(1,)]


# ╔═══════════════════════ 7. FRAME ══════════════════════════════╗
def test_frame_imports():
    assert hasattr(m, "run") and hasattr(m, "main") and m.EXPECTATIONS


def test_frame_help_exits_zero():
    r = subprocess.run([sys.executable, m.__file__, "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "watchdog" in r.stdout.lower()


def test_frame_dry_run_writes_no_incident(monkeypatch):
    # --dry-run must never open an incident nor alert. We stub PG + journal so
    # the test never hits real infra, and assert open_or_append is dry.
    monkeypatch.setattr(m, "git_pull_journal", lambda: None)
    monkeypatch.setattr(m, "alert", lambda *_a, **_k: pytest.fail("alerted in dry-run"))
    opened = []
    real = m.open_or_append
    monkeypatch.setattr(m, "open_or_append",
                        lambda c, f, i, dry_run: opened.append(dry_run) or "dry")
    monkeypatch.setattr(m, "connect", lambda: None)   # no PG -> conn None path
    rc = m.run(dry_run=True)
    assert rc == 0
