#!/usr/bin/env python3
"""
test_nova_speaks_upload.py — tests for the Nova Speaks YouTube publisher (nova_speaks_upload.py),
the render sweep's upload hook (nova_speaks_sweep.py) and the frontmatter-cover fix (nova_speaks.py).

Categories: security, performance, retry, unit, integration, functional, frame.
Run: python3 -m pytest tests/test_nova_speaks_upload.py -v
"""
import os, re, subprocess, sys, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_speaks_upload as up  # noqa: E402

ARTICLE = '''---
title: "⚡ **Heat Dome <Leaves> Like Bad Roommate**"
date: 2026-10-05T10:00:00-07:00
draft: false
tags: ["burbank", "local-news", "burbank"]
description: "Nova's daily dispatch <b>from</b> Burbank."
cover:
  image: "/images/local/2026-10-05-heat-dome.webp"
---
Body.
'''


@pytest.fixture
def article(tmp_path):
    d = tmp_path / "content" / "local"; d.mkdir(parents=True)
    p = d / "2026-10-05-heat-dome.md"; p.write_text(ARTICLE); return p


# ── unit ────────────────────────────────────────────────────────────────────
def test_title_format_has_date_then_section(article):
    m = up.build("2026-10-05-heat-dome", str(article), "https://nova.digitalnoise.net/local/x/")
    assert m["title"].startswith("AI: Nova Speaks 10/5/26 - Local - Heat Dome")


def test_title_strips_emoji_markdown_and_brackets(article):
    m = up.build("s", str(article), "u")
    assert "⚡" not in m["title"] and "*" not in m["title"] and "<" not in m["title"] and ">" not in m["title"]
    assert "<" not in m["description"] and ">" not in m["description"]


def test_title_trimmed_at_word_boundary_under_100(tmp_path):
    d = tmp_path / "content" / "operations"; d.mkdir(parents=True)
    p = d / "long.md"; p.write_text(ARTICLE.replace("Heat Dome <Leaves> Like Bad Roommate", "Word " * 60))
    m = up.build("long", str(p), "u")
    assert len(m["title"]) <= 100 and not m["title"].endswith(" ") and m["title"].split(" - ", 2)[1] == "Operations"


def test_tags_include_section_and_dedupe(article):
    m = up.build("s", str(article), "u")
    assert m["tags"][:4] == ("Nova", "AI", "Nova Speaks", "local") and m["tags"].count("burbank") == 1


def test_description_has_article_url_boilerplate_and_disclaimer(article):
    m = up.build("s", str(article), "https://nova.digitalnoise.net/local/x/")
    assert "Article: https://nova.digitalnoise.net/local/x/" in m["description"]
    assert "start-here" in m["description"] and "AI voice" in m["description"]


# ── security ────────────────────────────────────────────────────────────────
def test_cookie_jar_keeps_only_youtube_and_redomains_whitelist(tmp_path, monkeypatch):
    raw = tmp_path / "raw.txt"
    raw.write_text("# Netscape HTTP Cookie File\n"
                   ".google.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsecret1\n"
                   ".google.com\tTRUE\t/\tTRUE\t0\tNID\tjunk\n"
                   ".pornhub.com\tTRUE\t/\tTRUE\t0\tsess\tnope\n"
                   ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tli\n"
                   "accounts.google.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tx\n")
    out = tmp_path / "jar.txt"; monkeypatch.setattr(up, "COOKIES", out)
    monkeypatch.setattr(up.tempfile, "mktemp", lambda suffix="": str(raw)) if hasattr(up, "tempfile") else None
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")), \
         patch("tempfile.mktemp", return_value=str(raw)):
        up.refresh_cookies()
    body = out.read_text()
    assert ".pornhub.com" not in body and "NID" not in body and "accounts.google.com" not in body
    assert ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsecret1" in body and "LOGIN_INFO" in body
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_no_hardcoded_secrets_in_source():
    src = (SCRIPTS / "nova_speaks_upload.py").read_text()
    assert not re.search(r"xox[bpoas]-|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36}", src)


# ── performance ─────────────────────────────────────────────────────────────
def test_build_is_fast_on_large_article(tmp_path):
    d = tmp_path / "content" / "essays"; d.mkdir(parents=True)
    p = d / "big.md"; p.write_text(ARTICLE + ("lorem ipsum " * 50000))
    t0 = time.time(); up.build("big", str(p), "u"); assert time.time() - t0 < 1.0


# ── retry ───────────────────────────────────────────────────────────────────
def test_sweep_upload_returns_none_on_failure_and_only_accepts_video_ids():
    import nova_speaks_sweep as sw
    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
        assert sw.upload("slug") is None
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="uploading\n", stderr="")):
        assert sw.upload("slug") is None                      # a claim marker is not a video id
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="[log]\nxDOTDX-xOd4\n", stderr="")):
        assert sw.upload("slug") == "xDOTDX-xOd4"


def test_sweep_retries_only_recent_unuploaded_rows():
    src = (SCRIPTS / "nova_speaks_sweep.py").read_text()
    assert "youtube_id IS NULL" in src and "interval '2 days'" in src and "LIMIT 1" in src


# ── integration ─────────────────────────────────────────────────────────────
def test_claim_happens_after_metadata_validation_and_is_released_on_failure():
    src = (SCRIPTS / "nova_speaks_upload.py").read_text()
    assert src.index("meta = Metadata(") < src.index("SET youtube_id='uploading'")
    assert "SET youtube_id=%s WHERE slug=%s AND youtube_id='uploading'" in src      # NULL, or the old id on a replacement


def test_renderer_and_sweep_honor_frontmatter_cover():
    pat = r"image:\\s"
    assert re.search(pat, (SCRIPTS / "nova_speaks.py").read_text())
    assert re.search(pat, (SCRIPTS / "nova_speaks_sweep.py").read_text())


def test_cinc_probes_local_host_without_ssh():
    import nova_cinc_daily as c
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ok", stderr="")) as r:
        c.ssh_cmd("127.0.0.1", "kochj", "true")
        assert r.call_args[0][0][0] == "bash"
        c.ssh_cmd("192.168.1.86", "kochj", "true")
        assert r.call_args[0][0][0] == "ssh"


# ── functional ──────────────────────────────────────────────────────────────
def test_dry_run_prints_metadata_without_uploading(article, monkeypatch, capsys):
    cur = MagicMock(); cur.fetchone.return_value = (str(article), "https://nova.digitalnoise.net/local/x/", "/tmp/x.mp4", None, None)
    conn = MagicMock(); conn.cursor.return_value = cur
    monkeypatch.setattr(up.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(sys, "argv", ["nova_speaks_upload.py", "--slug", "2026-10-05-heat-dome", "--dry-run"])
    assert up.main() == 0
    out = capsys.readouterr().out
    assert "AI: Nova Speaks 10/5/26 - Local - Heat Dome" in out and "Article: https://nova.digitalnoise.net/local/x/" in out
    assert not any("uploading" in str(c) for c in cur.execute.call_args_list)


def test_already_uploaded_row_is_skipped(article, monkeypatch):
    cur = MagicMock(); cur.fetchone.return_value = (str(article), "u", "/tmp/x.mp4", "xDOTDX-xOd4", None)
    conn = MagicMock(); conn.cursor.return_value = cur
    monkeypatch.setattr(up.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setattr(sys, "argv", ["nova_speaks_upload.py", "--slug", "s"])
    assert up.main() == 0


# ── frame ───────────────────────────────────────────────────────────────────
def test_script_help_runs():
    r = subprocess.run([sys.executable, str(SCRIPTS / "nova_speaks_upload.py"), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "--privacy" in r.stdout and "PUBLIC" in r.stdout


def test_default_privacy_is_public():
    assert 'default="PUBLIC"' in (SCRIPTS / "nova_speaks_upload.py").read_text()


# ── podcast index (Start Here grid) ─────────────────────────────────────────
def test_podcast_index_builds_json_newest_first_and_pushes_once(tmp_path, monkeypatch):
    import types, json
    import nova_speaks_sweep as sw
    j = tmp_path / "journal"; (j / "content" / "local").mkdir(parents=True); (j / "static" / "images" / "local").mkdir(parents=True)
    a = j / "content" / "local" / "a.md"; a.write_text(ARTICLE)
    b = j / "content" / "local" / "b.md"; b.write_text(ARTICLE.replace("2026-10-05", "2026-10-04").replace("cover:\n  image: \"/images/local/2026-10-05-heat-dome.webp\"\n", ""))
    (j / "static" / "images" / "local" / "2026-10-05-heat-dome.webp").write_bytes(b"x")
    monkeypatch.setattr(sw, "JOURNAL", j)
    pushes = []
    monkeypatch.setitem(sys.modules, "nova_journal", types.SimpleNamespace(git_push=lambda s, t: pushes.append(t)))
    cur = MagicMock(); cur.fetchall.return_value = [("b", "local", str(b), "ub", "BBBBBBBBBBB"), ("a", "local", str(a), "ua", "AAAAAAAAAAA")]
    assert sw.podcast_index(cur) == 2
    eps = json.loads((j / "data" / "nova_speaks.json").read_text())
    assert [e["youtube_id"] for e in eps] == ["AAAAAAAAAAA", "BBBBBBBBBBB"]          # newest first
    assert eps[0]["cover"] == "/images/local/2026-10-05-heat-dome.webp" and eps[0]["section"] == "Local"
    assert eps[1]["cover"].startswith("https://i.ytimg.com/vi/BBBBBBBBBBB/")        # no cover file -> YouTube thumb
    assert "<" not in eps[0]["title"] and "⚡" not in eps[0]["title"]
    assert pushes == ["Nova Speaks index: 2 episodes"]
    assert sw.podcast_index(cur) == 0 and len(pushes) == 1                           # unchanged -> no second push


def test_art_renders_use_only_the_artwork():
    src = (SCRIPTS / "nova_speaks.py").read_text()
    assert 'section == "art" and imgs' in src and src.count('section != "art"') == 2


# ── screenplay ingest helper ────────────────────────────────────────────────
def test_screenplay_reflow_joins_cues_and_raises_alpha_ratio():
    import nova_screenplay_ingest as sp
    raw = "          ANNIE\n\n     You dirty bird.\n\n\n          PAUL\n\n     What?\n\n     INT. BEDROOM - NIGHT\n"
    out = sp.reflow(raw)
    assert "ANNIE: You dirty bird." in out and "PAUL: What?" in out
    assert sum(c.isalpha() for c in out) / len(out) > 0.6


def test_screenplay_extract_skips_site_name_headings():
    import nova_screenplay_ingest as sp
    page = "<html><head><title>Jaws Script at IMSDb.</title></head><body><h1>The Internet Movie Script Database (IMSDb)</h1><h1>Jaws</h1><pre>FADE IN</pre></body></html>"
    title, body = sp.extract(page)
    assert title == "Jaws" and "FADE IN" in body
    t2, _ = sp.extract("<html><title>Misery - by William Goldman</title><pre>x</pre></html>")
    assert t2 == "Misery - by William Goldman"


def test_screenplay_pdf_link_and_og_title():
    import nova_screenplay_ingest as sp
    page = '<meta property="og:title" content="The Texas Chain Saw Massacre (1974) - Movie Script"><title></title><a href="https://assets.example.com/live/pdf/x.pdf?v=1">PDF</a>'
    assert sp.pdf_link("https://www.scriptslug.com/script/x", page) == "https://assets.example.com/live/pdf/x.pdf?v=1"
    assert sp.pdf_link("https://host/script.pdf", "") == "https://host/script.pdf"
    assert sp.pdf_link("https://imsdb.com/scripts/Jaws.html", "<pre>x</pre>") is None
    title, _ = sp.extract(page)
    assert title.startswith("The Texas Chain Saw Massacre (1974)")


def test_screenplay_alpha_ratio_gate_for_reocr():
    import nova_screenplay_ingest as sp
    assert sp.alpha_ratio("HOSTEI.: Wr~t:en & Cire~ted ty ~:.}:; r:ERVE - 'Soaz") < 0.62      # garbage text layer -> re-OCR
    assert sp.alpha_ratio("PAXTON: We should get out of here before it gets dark.") > 0.62


# ── the 7 house categories (unittest classes, added 2026-10-05) ─────────────
# Tests for nova_speaks_upload.py — the 7 house categories (Security, Performance, Retry, Unit,
# Integration, Functional, Frame). Written by Jordan Koch (via Claude).
import io
import tempfile
import types
import unittest
from contextlib import redirect_stdout

SCRIPT = SCRIPTS / "nova_speaks_upload.py"
SRC = SCRIPT.read_text()


def _article_file(body=ARTICLE, section="local"):
    d = Path(tempfile.mkdtemp()) / "content" / section
    d.mkdir(parents=True)
    p = d / "2026-10-05-heat-dome.md"
    p.write_text(body)
    return p


def _yt_stub(upload=None, exc=None, valid=True):
    """A youtube_up stand-in: records Metadata kwargs and the upload call; never touches the network."""
    mod = types.ModuleType("youtube_up")
    mod.calls = []

    class Metadata:
        def __init__(self, **kw):
            mod.calls.append(("Metadata", kw)); self.kw = kw

    class _Enum(dict):
        def __getattr__(self, k): return k
        def __getitem__(self, k): return k

    class YTUploaderSession:
        def __init__(self, jar): mod.calls.append(("session", jar))
        def has_valid_cookies(self): return valid
        def upload(self, mp4, meta, progress_callback=None):
            mod.calls.append(("upload", mp4, meta.kw))
            if progress_callback:
                progress_callback("upload", 0); progress_callback("upload", 50); progress_callback("upload", 100)
            if exc:
                raise exc
            return upload
    mod.Metadata, mod.PrivacyEnum, mod.CategoryEnum, mod.YTUploaderSession = Metadata, _Enum(), _Enum(), YTUploaderSession
    return mod


def _pg(row, rowcount=1):
    cur = MagicMock(); cur.fetchone.return_value = row; cur.rowcount = rowcount
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _main(argv, row, yt=None, rowcount=1):
    """Run main() with PG, youtube_up, the Safari cookie export and the cookie jar all stubbed.
    Returns (rc, cursor, stdout, youtube_up stub)."""
    conn, cur = _pg(row, rowcount)
    yt = yt or _yt_stub("xDOTDX-xOd4")
    jar = Path(tempfile.mkdtemp()) / "jar.txt"
    jar.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tli\n")
    out = io.StringIO()
    with patch.object(up.psycopg2, "connect", lambda dsn: conn), patch.object(sys, "argv", ["nova_speaks_upload.py"] + argv), \
         patch.dict(sys.modules, {"youtube_up": yt}), patch.object(up, "COOKIES", jar), \
         patch.object(up, "refresh_cookies", lambda: None), redirect_stdout(out):
        rc = up.main()
    return rc, cur, out.getvalue(), yt


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", up.DSN)

    def test_sql_is_parameterized_and_writes_are_scoped(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM|ALTER TABLE)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"nova_speaks_renders"})
        rc, cur, _, _ = _main(["--slug", "x'; SELECT pg_sleep(9); --", "--dry-run"], None)
        for call in cur.execute.call_args_list:
            self.assertNotIn("pg_sleep", call[0][0])                                   # the payload only ever travels as a parameter

    def test_cookie_values_never_reach_the_log(self):
        raw = Path(tempfile.mkdtemp()) / "raw.txt"
        raw.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tSUPERSECRETVALUE\n")
        out = Path(tempfile.mkdtemp()) / "jar.txt"
        buf = io.StringIO()
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")), patch("tempfile.mktemp", return_value=str(raw)), \
             patch.object(up, "COOKIES", out), redirect_stdout(buf):
            up.refresh_cookies()
        self.assertNotIn("SUPERSECRETVALUE", buf.getvalue())
        self.assertIn("cookies refreshed from Safari (2 lines)", buf.getvalue())
        self.assertFalse(raw.exists())                                                  # the raw export is unlinked

    def test_angle_brackets_are_stripped_from_every_field(self):
        m = up.build("s", str(_article_file()), "https://x/<y>")
        for v in (m["title"], m["description"], *m["tags"]):
            self.assertNotRegex(v, r"[<>]")


class TestPerformance(unittest.TestCase):
    def test_frontmatter_parse_10k_under_bound(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            up.fm(ARTICLE, "title"); up.fm(ARTICLE, "missing")
        self.assertLess(time.perf_counter() - t0, 1.5)

    def test_build_bounded_on_huge_tag_list(self):
        p = _article_file(ARTICLE.replace('["burbank", "local-news", "burbank"]', "[" + ", ".join(f'"t{i}"' for i in range(5000)) + "]"))
        t0 = time.perf_counter()
        m = up.build("s", str(p), "u")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(m["tags"]), 30)                                            # YouTube's cap is honoured


class TestRetry(unittest.TestCase):
    def test_cookie_export_failure_fails_open_and_keeps_the_last_jar(self):
        # refresh_cookies()/yt-dlp — 2 attempts (5 s apart); then the previous jar is reused and no exception escapes
        jar = Path(tempfile.mkdtemp()) / "jar.txt"; jar.write_text("old")
        buf = io.StringIO()
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="Safari: TCC denied")) as run, \
             patch("time.sleep"), patch.object(up, "COOKIES", jar), redirect_stdout(buf):
            up.refresh_cookies()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(jar.read_text(), "old")
        self.assertIn("reusing jar.txt", buf.getvalue())

    def test_upload_failure_releases_the_claim_and_reraises(self):
        # RETRY GAP: session().upload — one attempt; the 'uploading' claim is released so the sweep can retry later
        conn, cur = _pg((str(_article_file()), "u", "/tmp/x.mp4", None, None))
        yt = _yt_stub(exc=RuntimeError("quota"))
        with patch.object(up.psycopg2, "connect", lambda dsn: conn), patch.object(sys, "argv", ["x", "--slug", "s"]), \
             patch.dict(sys.modules, {"youtube_up": yt}), patch.object(up, "session", lambda: yt.YTUploaderSession(None)), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                up.main()
        sql = [c[0][0] for c in cur.execute.call_args_list]
        self.assertEqual(sum(1 for c in yt.calls if c[0] == "upload"), 1)
        self.assertTrue(any("youtube_id='uploading' WHERE slug=%s AND youtube_id IS NULL" in s for s in sql))
        self.assertTrue(sql[-1].startswith("UPDATE nova_speaks_renders SET youtube_id=%s WHERE slug=%s AND youtube_id='uploading'"))
        self.assertEqual(cur.execute.call_args_list[-1][0][1][0], None)


class TestUnit(unittest.TestCase):
    def test_fm_edges(self):
        self.assertEqual(up.fm("", "title"), "")
        self.assertEqual(up.fm("title: \"Quoted\"\n", "title"), "Quoted")
        self.assertEqual(up.fm("subtitle: no\ntitle:   spaced  \n", "title"), "spaced")
        self.assertEqual(up.fm("x: 1", "y"), "")

    def test_build_section_from_parent_dir_and_date_short_year(self):
        m = up.build("s", str(_article_file(section="random-thoughts")), "u")
        self.assertTrue(m["title"].startswith("AI: Nova Speaks 10/5/26 - Random Thoughts - "))
        self.assertEqual(str(m["recorded"]), "2026-10-05")
        self.assertIn("random-thoughts", m["tags"])

    def test_build_without_description_or_tags(self):
        body = ARTICLE.replace('description: "Nova\'s daily dispatch <b>from</b> Burbank."\n', "").replace('tags: ["burbank", "local-news", "burbank"]\n', "")
        m = up.build("s", str(_article_file(body)), "https://u/")
        self.assertTrue(m["description"].startswith("Article: https://u/\n\n"))
        self.assertEqual(m["tags"], ("Nova", "AI", "Nova Speaks", "local"))

    def test_build_rejects_a_missing_date(self):
        with self.assertRaises(ValueError):
            up.build("s", str(_article_file(ARTICLE.replace("date: 2026-10-05T10:00:00-07:00\n", ""))), "u")


class TestIntegration(unittest.TestCase):
    def test_sweep_invokes_this_script_by_slug_and_parses_its_last_line(self):
        sweep = (SCRIPTS / "nova_speaks_sweep.py").read_text()
        self.assertIn('"nova_speaks_upload.py"), "--slug", slug]', sweep)
        self.assertIn("print(vid)", SRC)                                                  # the id is the final stdout line

    def test_build_feeds_metadata_with_playlist_and_category(self):
        art = _article_file()
        rc, cur, _, yt = _main(["--slug", "2026-10-05-heat-dome"], (str(art), "https://u/", "/tmp/x.mp4", None, None), yt=_yt_stub("AAAAAAAAAAA"))
        kw = next(c[1] for c in yt.calls if c[0] == "Metadata")
        self.assertEqual(kw["playlist_ids"], [up.PLAYLIST])
        self.assertEqual((kw["privacy"], kw["category"], kw["made_for_kids"]), ("PUBLIC", "SCIENCE_TECH", False))
        self.assertEqual(kw["title"], up.build("s", str(art), "https://u/")["title"])

    def test_dsn_and_cookie_jar_match_the_fleet_conventions(self):
        self.assertEqual(up.DSN, "host=localhost dbname=nova_ops user=kochj")
        self.assertEqual(str(up.COOKIES).split("/.openclaw/")[1], "cache/yt_cookies_youtube.txt")


class TestFunctional(unittest.TestCase):
    def test_golden_path_claims_uploads_and_records_the_video_id(self):
        rc, cur, out, yt = _main(["--slug", "2026-10-05-heat-dome"], (str(_article_file()), "https://u/", "/tmp/x.mp4", None, None))
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in yt.calls], ["Metadata", "session", "upload"])
        self.assertEqual(yt.calls[-1][1], "/tmp/x.mp4")
        sql = [(c[0][0], c[0][1] if len(c[0]) > 1 else None) for c in cur.execute.call_args_list]
        self.assertEqual(sql[-1], ("UPDATE nova_speaks_renders SET youtube_id=%s, youtube_uploaded_at=now() WHERE slug=%s", ("xDOTDX-xOd4", "2026-10-05-heat-dome")))
        self.assertTrue(out.rstrip().endswith("xDOTDX-xOd4"))
        self.assertIn("DONE https://youtu.be/xDOTDX-xOd4 (PUBLIC)", out)
        self.assertIn("upload 0%", out); self.assertNotIn("upload 50%", out)

    def test_no_done_render_returns_one_without_touching_youtube(self):
        rc, cur, out, yt = _main(["--slug", "ghost"], None)
        self.assertEqual(rc, 1)
        self.assertIn("no done render for ghost", out)
        self.assertEqual(yt.calls, [])

    def test_lost_claim_race_returns_zero_without_uploading(self):
        rc, cur, out, yt = _main(["--slug", "s"], (str(_article_file()), "u", "/tmp/x.mp4", None, None), rowcount=0)
        self.assertEqual(rc, 0)
        self.assertIn("claimed by another uploader", out)
        self.assertFalse(any(c[0] == "upload" for c in yt.calls))

    def test_check_reports_cookie_validity_as_exit_code(self):
        for valid, rc in ((True, 0), (False, 1)):
            rc_got, _, out, _ = _main(["--check"], None, yt=_yt_stub(valid=valid))
            self.assertEqual(rc_got, rc)
            self.assertIn(f"cookies valid: {valid}", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_speaks_upload"], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
