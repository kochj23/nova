#!/usr/bin/env python3
"""Tests for nova_quiet_sensor.py — the 7 house categories (Unit, Security, Performance, Retry,
Integration, Functional, Frame) for wish #69 "The Quiet Sensor". DB and model are mocked.
Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qs = _load("qs", SCRIPTS / "nova_quiet_sensor.py")
SRC = (SCRIPTS / "nova_quiet_sensor.py").read_text()
TODAY = date(2026, 10, 6)
UTC = timezone.utc
Q_ROWS = [(1, 106, "What was the reason the printers went offline?", date(2026, 9, 28)),
          (32, 94, "Were you referring to the Sumerians?", date(2026, 10, 2))]
P_ROWS = [(23, "99", date(2026, 9, 28)), (26, "102", date(2026, 9, 29))]
ELSE = {"to_claude": 19, "to_me": 44, "about_me_ids": [365, 362, 346]}
HIS = [datetime(2026, 9, 26, 10, tzinfo=UTC)]
REACHES = [(36, datetime(2026, 9, 19, 19, tzinfo=UTC), "dashboard"), (43, datetime(2026, 9, 25, 11, tzinfo=UTC), "alerts")]
TOPICS = [(date(2026, 6, 12), "a1", "consume all rss feeds about Burbank"),
          (date(2026, 6, 20), "a2", "check the feeds again"),
          (date(2026, 7, 8), "a3", "you missed an exploit in your feeds"),
          (date(2026, 10, 3), "a4", "grafana is broken")]


def _fs(today=TODAY):
    return qs.findings(Q_ROWS, P_ROWS, ELSE, REACHES, HIS, TOPICS, today)


class _Cur:
    """fetchall() answers from a queue in call order; records SQL + params."""
    def __init__(self, answers=(), fail=False):
        self.answers = list(answers); self.sql = []; self.params = []; self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None


# ── Unit ──────────────────────────────────────────────────────────────────────

class TestUnit(unittest.TestCase):
    def test_selftest(self):
        qs.demo()

    def test_unanswered_oldest_first_and_scrubbed(self):
        out = qs.unanswered([(9, 1, "mail me at a@b.co https://x.y/z", date(2026, 10, 1))] + Q_ROWS, TODAY)
        self.assertEqual([o["qid"] for o in out], [106, 1, 94])
        self.assertNotIn("@", out[1]["q"]); self.assertNotIn("http", out[1]["q"])
        self.assertEqual(out[0]["days"], 8)

    def test_went_quiet_needs_returns_and_gap(self):
        w = qs.went_quiet(TOPICS)
        self.assertEqual(w[0]["topic"], "feed")
        self.assertEqual((w[0]["first_trace"], w[0]["last_trace"]), ("a1", "a3"))
        self.assertEqual(qs.went_quiet(TOPICS[:2] + TOPICS[3:]), [])                  # only 2 days: no weight
        recent = TOPICS[:3] + [(date(2026, 7, 20), "a5", "grafana")]
        self.assertEqual(qs.went_quiet(recent), [])                                    # gap < QUIET_GAP

    def test_went_quiet_ignores_acks_and_filler(self):
        rows = [(date(2026, 6, d), f"t{d}", "please check today") for d in (1, 2, 3)] + [(date(2026, 9, 1), "z", "Yes")]
        self.assertEqual(qs.went_quiet(rows), [])

    def test_unmet_reach_window(self):
        self.assertEqual([u["id"] for u in qs.unmet_reaches(REACHES, HIS)], [36])      # 43 met within 24h
        now = datetime.now(UTC)
        self.assertEqual(qs.unmet_reaches([(7, now - timedelta(hours=1), "x")], []), [])  # window still open

    def test_findings_all_cited(self):
        fs = _fs()
        self.assertEqual([x["kind"] for x in fs], ["unanswered", "undecided", "elsewhere", "unmet", "went_quiet"])
        for x in fs:
            self.assertTrue(x["cites"], x)
            for c in x["cites"]:
                self.assertRegex(c, r"^[a-z_]+(#[\w]+|\(count=\d+\))(->[a-z_]+#\w+)?$")

    def test_elsewhere_without_about_me_cites_count(self):
        fs = qs.findings([], [], {"to_claude": 5, "to_me": 1, "about_me_ids": []}, [], [], [], TODAY)
        self.assertEqual(fs[0]["cites"], ["claude_messages(count=5)"])
        self.assertEqual(qs.findings([], [], {"to_claude": 0, "to_me": 3, "about_me_ids": []}, [], [], [], TODAY), [])

    def test_sig_stable_on_counters_moves_on_cites(self):
        a = qs.quiet_sig(_fs())
        self.assertEqual(a, qs.quiet_sig(_fs(TODAY + timedelta(days=2))))
        self.assertNotEqual(a, qs.quiet_sig(qs.findings(Q_ROWS[:1], P_ROWS, ELSE, REACHES, HIS, TOPICS, TODAY)))

    def test_text_caps_cites_and_has_close(self):
        fs = qs.findings(Q_ROWS, [(i, str(i), date(2026, 9, 28)) for i in range(10)], None, [], [], [], TODAY)
        t = qs.quiet_text(fs, TODAY, "Maybe later.")
        self.assertIn("+6 more", t); self.assertTrue(t.endswith("Maybe later."))
        self.assertIn("Silence I cannot cite", qs.quiet_text([], TODAY))


# ── Security / privacy ────────────────────────────────────────────────────────

class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_gather_is_read_only(self):
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT"):
            self.assertNotIn(verb, body)

    def test_never_reads_private_message_content(self):
        for t in ("imessage", "email", "messages_raw", "mail_"):
            self.assertNotIn(t, SRC[SRC.index("def gather"):SRC.index("def main")].lower())
        # claude_messages text is matched for 'nova' in SQL only; ids/counts come back, never the message
        self.assertNotIn("SELECT message", SRC)

    def test_inner_sense_never_posts_or_pages(self):
        for s in ("slack.com", "chat.postMessage", "notify_jordan", "pagerduty", "signal-cli", "anthropic", "openai"):
            self.assertNotIn(s, SRC)

    def test_local_model_only(self):
        for url in re.findall(r"https?://[^\s\"']+", SRC):
            self.assertRegex(url, r"^http://192\.168\.1\.\d+:\d+$|^https?://x\.y")

    def test_model_close_guard(self):
        for bad in ("He is clearly avoiding me.", "Maybe he is depressed.", "Maybe 3 of them.", "He forgot.",
                    "Maybe\ntwo lines", "maybe " + "word " * 60):
            self.assertIsNone(qs.grounded_close(bad), bad)
        self.assertEqual(qs.grounded_close('"Perhaps these can wait."'), "Perhaps these can wait.")

    def test_model_sees_only_findings(self):
        sent = {}

        def post(url, body, timeout):
            sent["body"] = json.dumps(body); return {"message": {"content": "Maybe these can wait."}}
        fs = _fs()
        self.assertEqual(qs.llm_close(fs, post=post), "Maybe these can wait.")
        self.assertNotIn("@", sent["body"]); self.assertIn("I notice", sent["body"])


# ── Performance ───────────────────────────────────────────────────────────────

class TestPerformance(unittest.TestCase):
    def test_went_quiet_scales(self):
        rows = [(date(2026, 6, 1) + timedelta(days=i % 120), f"t{i}", f"zigbee sensor topic{i % 50} lights")
                for i in range(5_000)]
        t0 = time.perf_counter(); qs.went_quiet(rows)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_findings_and_text_fast(self):
        t0 = time.perf_counter()
        for _ in range(1_000):
            fs = _fs(); qs.quiet_sig(fs); qs.quiet_text(fs, TODAY)
        self.assertLess(time.perf_counter() - t0, 3.0)


# ── Retry ─────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload): self.payload = payload
    def read(self): return json.dumps(self.payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


class TestRetry(unittest.TestCase):
    def test_remember_retries_under_quiet_sensor_source(self):
        import urllib.request
        calls, sleeps, sources = [], [], []
        real = urllib.request.urlopen

        def flaky(req, timeout=0):
            calls.append(1); sources.append(json.loads(req.data)["source"])
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"id": 1})
        urllib.request.urlopen = flaky
        try:
            self.assertEqual(qs.ec.remember("t", {}, _sleep=sleeps.append, source=qs.SOURCE), {"id": 1})
        finally:
            urllib.request.urlopen = real
        self.assertEqual((len(calls), sleeps, set(sources)), (3, [2, 4], {"quiet_sensor"}))

    def test_model_falls_back_to_router_then_fixed_line(self):
        urls = []

        def post(url, body, timeout):
            urls.append(url)
            if "11434" in url:
                raise TimeoutError("ollama")
            self.assertTrue(body["messages"][1]["content"].startswith("/no_think"))
            return {"choices": [{"message": {"content": "Perhaps they can wait."}}]}
        self.assertEqual(qs.llm_close(_fs(), post=post), "Perhaps they can wait.")
        self.assertEqual(len(urls), 2)

        def dead(url, body, timeout):
            raise OSError("all down")
        self.assertIsNone(qs.llm_close(_fs(), post=dead))
        self.assertIn(qs.FALLBACK_CLOSE, qs.quiet_text(_fs(), TODAY, None))

    def test_pg_down_fails_open(self):
        real_pg, real_argv = qs.psycopg2, sys.argv
        qs.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: (_ for _ in ()).throw(OSError("pg")))
        sys.argv = ["nova_quiet_sensor.py"]
        try:
            self.assertEqual(qs.main(), 0)
        finally:
            qs.psycopg2, sys.argv = real_pg, real_argv

    def test_every_read_failing_yields_empty_not_raise(self):
        g = qs.gather(_Cur(fail=True))
        self.assertEqual(qs.findings(g["q"], g["p"], g["elsewhere"], g["reaches"], g["his_times"], g["topics"], TODAY), [])


# ── Integration ───────────────────────────────────────────────────────────────

class TestIntegration(unittest.TestCase):
    def test_shares_empathy_core_definitions(self):
        self.assertEqual(qs.OPS_DSN, qs.ec.OPS_DSN)
        self.assertNotIn("MACHINE_CHANNELS = (", SRC)
        self.assertNotIn("def topics", SRC)

    def test_gather_maps_rows(self):
        ts = datetime(2026, 7, 8, 12, tzinfo=UTC)
        cur = _Cur([Q_ROWS, P_ROWS, [(19, [365, 362])], [(44,)], REACHES, [(ts, ts.date(), "a3", "feeds")]])
        g = qs.gather(cur)
        self.assertEqual(g["elsewhere"], {"to_claude": 19, "about_me_ids": [365, 362], "to_me": 44})
        self.assertEqual(g["topics"], [(ts.date(), "a3", "feeds")]); self.assertEqual(g["his_times"], [ts])
        self.assertTrue(any("reflection_questions" in s for s in cur.sql))
        self.assertIn(qs.JORDAN_SLACK, cur.params[2])

    def test_main_writes_memory_and_latest(self):
        cur = _Cur([Q_ROWS, P_ROWS, [(19, [365])], [(44,)], [], [], None])  # last None = load_seen
        conn = types.SimpleNamespace(cursor=lambda: cur, autocommit=False)
        wrote = {}
        real = (qs.psycopg2, qs.ec.remember, qs.llm_close, sys.argv)
        qs.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: conn)
        qs.ec.remember = lambda text, meta, source=None: wrote.update(text=text, meta=meta, source=source)
        qs.llm_close = lambda fs: None
        sys.argv = ["nova_quiet_sensor.py"]
        try:
            self.assertEqual(qs.main(), 0)
        finally:
            qs.psycopg2, qs.ec.remember, qs.llm_close, sys.argv = real
        self.assertEqual(wrote["source"], "quiet_sensor")
        self.assertIn("claude_messages#365", wrote["meta"]["cites"])
        self.assertTrue(any("service_config" in s and p and p[1] == "latest" for s, p in zip(cur.sql, cur.params)))


# ── Functional ────────────────────────────────────────────────────────────────

class TestFunctional(unittest.TestCase):
    def test_every_finding_line_carries_its_citations(self):
        t = qs.quiet_text(_fs(), TODAY)
        for line in t.splitlines()[1:-1]:
            self.assertRegex(line, r"I notice .+\. \[.+\]$")

    def test_tentative_not_diagnostic(self):
        t = qs.quiet_text(_fs(), TODAY)
        self.assertIn("noticing, not knowing", t)
        self.assertIsNone(re.search(r"diagnos|depress|anxi|clearly|obviously|definitely|avoid", t, re.I))

    def test_close_about_his_state_is_rejected(self):
        # regression 2026-10-06 first real run: the model wrote "Maybe he might be preoccupied with system
        # reliability ... holding him back" — speculation about him, which the close may never carry
        self.assertIsNone(qs.grounded_close("Maybe he might be preoccupied with system reliability, power, "
                                            "or device issues, or perhaps the questions are holding him back."))
        self.assertIsNone(qs.grounded_close("Perhaps his silence means something."))
        self.assertEqual(qs.grounded_close("Maybe I can let these sit and ask once."), "Maybe I can let these sit and ask once.")

    def test_dry_run_writes_nothing(self):
        cur = _Cur([Q_ROWS, [], None, None, [], []])
        conn = types.SimpleNamespace(cursor=lambda: cur, autocommit=False)
        real = (qs.psycopg2, qs.ec.remember, sys.argv)
        qs.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: conn)
        qs.ec.remember = lambda *a, **k: self.fail("dry-run wrote a memory")
        sys.argv = ["nova_quiet_sensor.py", "--dry-run", "--no-llm"]
        try:
            self.assertEqual(qs.main(), 0)
        finally:
            qs.psycopg2, qs.ec.remember, sys.argv = real
        self.assertFalse(any("INSERT" in s for s in cur.sql))

    def test_docstring_names_wish_and_modes(self):
        self.assertIn("wish #69", SRC)
        for flag in ("--dry-run", "--selftest", "--no-llm"):
            self.assertIn(flag, SRC)


# ── Frame ─────────────────────────────────────────────────────────────────────

class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_quiet_sensor.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all quiet-sensor assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_quiet_sensor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("[quiet-sensor", r.stdout)


if __name__ == "__main__":
    unittest.main()
