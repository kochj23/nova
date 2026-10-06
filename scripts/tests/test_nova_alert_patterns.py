#!/usr/bin/env python3
"""Tests for nova_alert_patterns.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_alert_patterns.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config"); cfg.SLACK_ALERTS = "C_ALERTS"; cfg.post_both = MagicMock()
    nj = types.ModuleType("nova_journal")
    nj.call_openrouter = MagicMock(return_value="body"); nj.publish_hugo = MagicMock(); nj.git_push = MagicMock()
    voice = types.ModuleType("nova_voice"); voice.system_prompt = MagicMock(side_effect=lambda ctx, section="": f"SYS[{section}] {ctx}")
    img = types.ModuleType("nova_image_utils"); img.generate_image = MagicMock(return_value=None)
    sentinel = types.ModuleType("nova_rogue_ap_sentinel"); sentinel.get_current_flags = MagicMock(return_value=[])
    return {"nova_config": cfg, "nova_journal": nj, "nova_voice": voice, "nova_image_utils": img,
            "nova_rogue_ap_sentinel": sentinel}


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _stubbed(_stub_modules()):
        spec.loader.exec_module(mod)
    return mod


ap = _load("ap", SCRIPT)
# Offline guard: the module's own psycopg2 binding is replaced (never the real module in sys.modules).
ap.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("offline")),
                                    extras=types.SimpleNamespace(RealDictCursor=object))


class _Conn:
    """Fake connection: answers every query with `rows` and records executed SQL + params."""
    def __init__(self, rows=None, fail=None):
        self.rows = rows or []; self.fail = fail; self.sql = []; self.closed = False

    def cursor(self, cursor_factory=None):
        return self

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))
        if self.fail:
            raise self.fail

    def fetchall(self):
        return [dict(r) for r in self.rows]

    def close(self):
        self.closed = True


def _use(conn):
    ap.psycopg2 = types.SimpleNamespace(connect=MagicMock(return_value=conn),
                                        extras=types.SimpleNamespace(RealDictCursor=object))
    return conn


def _reset_stubs():
    ap.nova_config.post_both = MagicMock()
    ap.nj.call_openrouter = MagicMock(side_effect=["body " * 50, '"A Title"'])
    ap.nj.publish_hugo = MagicMock(); ap.nj.git_push = MagicMock()
    ap.generate_image = MagicMock(return_value=None)
    ap.sentinel.get_current_flags = MagicMock(return_value=[])


def _p(**over):
    p = {"recurring": [{"sig": "backup-stale:nova-core3", "this_week": 40, "last_week": 20, "total": 60},
                       {"sig": "disk:192.168.1.6", "this_week": 1, "last_week": 300, "total": 301}],
         "volume": {"this_week": 50, "last_week": 25},
         "incidents": {"open_now": 1, "opened_7d": 3, "resolved_7d": 2, "avg_mttr_min": 12},
         "incident_recurrence": [{"recurrence_key": "unas-low", "n": 3, "sev": "warning"}],
         "red": [{"scan_type": "nmap", "status": "ok", "n": 4, "last": "x"}],
         "blue": {"events": 9, "last": "10-04"},
         "purple": {"title": "purple run ok", "body": "b", "d": "10-03"},
         "new_devices": [{"client_name": "Amys-iPhone", "ip": "192.168.1.43"}],
         "rogue_flags": [{"kind": "open", "ssid": "FreeWifi", "bssid": "aa"}]}
    p.update(over)
    return p


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ap.DSN)

    def test_sql_is_read_only_and_parameterized(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP|TRUNCATE)\b", SRC))
        self.assertIsNone(re.search(r'_q\(\s*f"', SRC))
        c = _use(_Conn())
        with redirect_stdout(io.StringIO()):
            ap.gather_patterns()
        ilike = [(s, p) for s, p in c.sql if "ILIKE %s" in s]
        self.assertEqual(sorted(p for _, p in ilike), [("%purple%",), ("%wazuh%",)])   # patterns ride as params

    def test_public_brief_carries_no_ips_hostnames_or_device_names(self):
        brief = ap._sanitized_brief(_p())
        self.assertNotIn("192.168", brief)
        self.assertNotIn("nova-core3", brief)
        self.assertNotIn("Amys-iPhone", brief)
        self.assertNotIn("FreeWifi", brief)
        self.assertIn("'backup' alert", brief)
        self.assertIn("'disk' alert", brief)

    def test_article_prompt_is_built_from_the_sanitized_brief_only(self):
        _reset_stubs()
        ap.generate_article(_p())
        user_msg = ap.nj.call_openrouter.call_args_list[0][0][1]
        self.assertIn(ap._sanitized_brief(_p()), user_msg)
        self.assertNotIn("192.168.1", user_msg)
        self.assertIn("PUBLIC", ap.nj.call_openrouter.call_args_list[0][0][0])


class TestPerformance(unittest.TestCase):
    def test_render_and_brief_on_10k_signatures(self):
        rec = [{"sig": f"sig-{i}:host{i}", "this_week": i % 7, "last_week": i % 5, "total": i} for i in range(10_000)]
        p = _p(recurring=rec, new_devices=[{"client_name": f"d{i}", "ip": f"10.0.{i // 255}.{i % 255}"} for i in range(10_000)])
        t0 = time.perf_counter()
        s = ap.render_slack(p); b = ap._sanitized_brief(p)
        for i in range(10_000):
            ap._delta(i, i % 9)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(s.count("•"), 10)                      # slack digest is capped at 10 signatures
        self.assertEqual(b.count("a recurring"), 8)              # public brief at 8


class TestRetry(unittest.TestCase):
    def test_query_fails_open_on_connect_error(self):
        # RETRY GAP: _q — a single connect attempt; failure logs and returns the empty shape
        ap.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("pg down")),
                                            extras=types.SimpleNamespace(RealDictCursor=object))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ap._q("SELECT 1"), [])
            self.assertEqual(ap._q("SELECT 1", one=True), {})
        self.assertEqual(ap.psycopg2.connect.call_count, 2)
        self.assertIn("query failed: pg down", out.getvalue())

    def test_query_fails_open_on_execute_error(self):
        c = _use(_Conn(fail=RuntimeError("bad sql")))
        with redirect_stdout(io.StringIO()):
            p = ap.gather_patterns()
        self.assertEqual(p["recurring"], [])
        self.assertEqual(p["volume"], {})
        self.assertEqual(len(c.sql), 8)                          # every PG section still attempted once

    def test_main_survives_slack_and_article_failures(self):
        # RETRY GAP: main()/post_both, generate_article — each is one attempt, logged, never retried
        _use(_Conn())
        _reset_stubs()
        ap.nova_config.post_both = MagicMock(side_effect=RuntimeError("slack 500"))
        ap.nj.call_openrouter = MagicMock(side_effect=RuntimeError("openrouter 502"))
        with redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(ap.main())
        self.assertEqual(ap.nova_config.post_both.call_count, 1)
        self.assertEqual(ap.nj.call_openrouter.call_count, 1)
        ap.nj.publish_hugo.assert_not_called()
        self.assertIn("slack post failed: slack 500", out.getvalue())
        self.assertIn("article publish failed: openrouter 502", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_delta_edges(self):
        self.assertEqual(ap._delta(0, 0), "—")
        self.assertEqual(ap._delta(5, 0), "new")
        self.assertEqual(ap._delta(30, 20), "▲10 (+50%)")
        self.assertEqual(ap._delta(10, 20), "▼10 (-50%)")
        self.assertEqual(ap._delta(20, 20), "▬0 (+0%)")

    def test_render_slack_on_empty_patterns(self):
        s = ap.render_slack({})
        self.assertIn("*Volume:* 0 warning+ this week vs 0 last (—)", s)
        self.assertIn("red (Strix): no scans", s)
        self.assertIn("purple: (no run) []", s)
        self.assertIn("*Rogue/open APs:* 0 flagged", s)

    def test_render_slack_full_detail(self):
        s = ap.render_slack(_p())
        self.assertIn("`backup-stale:nova-core3` — 60 in 14d [40 this wk, ▲20 (+100%)]", s)
        self.assertIn("recurring: unas-low×3", s)
        self.assertIn("Amys-iPhone(192.168.1.43)", s)
        self.assertIn("⚠️ open:FreeWifi", s)
        self.assertIn("avg MTTR 12 min", s)

    def test_brief_bands_and_trends(self):
        rec = [{"sig": "a", "this_week": 1, "last_week": 0, "total": 250},
               {"sig": "b", "this_week": 0, "last_week": 1, "total": 30},
               {"sig": "c", "this_week": 2, "last_week": 2, "total": 3}]
        b = ap._sanitized_brief(_p(recurring=rec))
        self.assertIn("'a' alert, hundreds of times, rising", b)
        self.assertIn("'b' alert, dozens of times, easing", b)
        self.assertIn("'c' alert, a handful of times, flat", b)
        self.assertIn("1 new device(s) joined this week; 1 open/rogue AP(s)", b)

    def test_generate_article_strips_quotes_from_title(self):
        _reset_stubs()
        ap.nj.call_openrouter = MagicMock(side_effect=["  the body  ", '"Same Alerts, "New" Week"\n'])
        title, body = ap.generate_article(_p())
        self.assertEqual(body, "the body")
        self.assertEqual(title, "Same Alerts, New Week")


class TestIntegration(unittest.TestCase):
    def test_gather_reads_the_telemetry_tables_and_the_sentinel(self):
        c = _use(_Conn(rows=[{"sig": "x", "this_week": 1, "last_week": 0, "total": 1}]))
        ap.sentinel.get_current_flags = MagicMock(return_value=[{"kind": "open", "ssid": "s", "bssid": "b"}])
        with redirect_stdout(io.StringIO()):
            p = ap.gather_patterns()
        tables = {t for s, _ in c.sql for t in re.findall(r"FROM\s+([\w.]+)", s)}
        self.assertEqual(tables, {"telemetry.events", "telemetry.incidents", "security_scan_results", "telemetry.known_devices"})
        self.assertEqual(set(p), {"recurring", "volume", "incidents", "incident_recurrence", "red", "blue", "purple",
                                  "new_devices", "rogue_flags"})
        self.assertEqual(p["rogue_flags"][0]["ssid"], "s")
        self.assertTrue(c.closed)

    def test_gather_then_render_then_brief_compose(self):
        _use(_Conn(rows=[{"sig": "dns-fail:10.0.0.1", "this_week": 2, "last_week": 1, "total": 3, "open_now": 0,
                          "opened_7d": 0, "resolved_7d": 0, "avg_mttr_min": None, "events": 1, "last": "x",
                          "title": "t", "d": "d", "client_name": "n", "ip": "10.0.0.9", "recurrence_key": "k",
                          "n": 1, "sev": "s", "scan_type": "st", "status": "ok"}]))
        ap.sentinel.get_current_flags = MagicMock(return_value=[])
        with redirect_stdout(io.StringIO()):
            p = ap.gather_patterns()
        self.assertIn("10.0.0.1", ap.render_slack(p))
        self.assertNotIn("10.0.0", ap._sanitized_brief(p))

    def test_article_uses_the_shared_voice_and_journal_helpers(self):
        _reset_stubs()
        ap.generate_article(_p())
        self.assertEqual(ap.nova_voice.system_prompt.call_args[1], {"section": "operations"})
        self.assertNotIn("def call_openrouter", SRC)
        self.assertNotIn("def publish_hugo", SRC)
        self.assertIn("nj.publish_hugo(", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_slack_and_publishes_sanitized_article(self):
        _use(_Conn())
        _reset_stubs()
        ap.generate_image = MagicMock(return_value="/tmp/x.png")
        with redirect_stdout(io.StringIO()) as out:
            ap.main()
        msg, kw = ap.nova_config.post_both.call_args[0][0], ap.nova_config.post_both.call_args[1]
        self.assertTrue(msg.startswith("*Alert Patterns — 14-day rolling*"))
        self.assertEqual(kw, {"slack_channel": "C_ALERTS", "discord_channel": None})
        args, kw = ap.nj.publish_hugo.call_args
        self.assertEqual((args[0], args[2]), ("A Title", "operations"))
        self.assertEqual(kw["image_path"], "/tmp/x.png")
        self.assertIn("alerts", args[3])
        ap.nj.git_push.assert_called_once_with("operations", "A Title")
        self.assertIn("published sanitized article to /operations (image: yes)", out.getvalue())

    def test_image_failure_still_publishes_without_image(self):
        _use(_Conn())
        _reset_stubs()
        ap.generate_image = MagicMock(side_effect=RuntimeError("swarm down"))
        with redirect_stdout(io.StringIO()) as out:
            ap.main()
        self.assertIsNone(ap.nj.publish_hugo.call_args[1]["image_path"])
        self.assertIn("image gen failed: swarm down", out.getvalue())

    def test_error_path_publish_failure_skips_push(self):
        _use(_Conn())
        _reset_stubs()
        ap.nj.publish_hugo = MagicMock(side_effect=RuntimeError("hugo"))
        with redirect_stdout(io.StringIO()) as out:
            ap.main()
        ap.nj.git_push.assert_not_called()
        self.assertIn("article publish failed: hugo", out.getvalue())
        self.assertIn("done", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        # Import with the heavy collaborators stubbed in the child process: a clean import prints nothing.
        code = ("import sys, types\n"
                "for m in ('nova_config','nova_journal','nova_voice','nova_image_utils','nova_rogue_ap_sentinel'):\n"
                "    sys.modules[m] = types.ModuleType(m)\n"
                "sys.modules['nova_image_utils'].generate_image = lambda *a, **k: None\n"
                "import nova_alert_patterns\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
