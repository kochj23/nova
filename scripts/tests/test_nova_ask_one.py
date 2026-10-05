#!/usr/bin/env python3
"""Tests for nova_ask_one.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ask_one.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ask = _load("ask1", SCRIPT)
SRC = SCRIPT.read_text()
ROWS = [(5, "What was the reason the printers went offline?", "bambu", None),
        (4, "I predicted with 66% confidence that ... What did I misjudge?", "prediction_surprise", None),
        (3, "What were the motivations behind the founding of colonies?", "american_indian_wars", None)]


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = next((v for k, v in self.answers if k in sql), None)

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False

    def cursor(self, *a, **k): return self._cur


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _connect_with(cur):
    return patch.object(ask.psycopg2, "connect", lambda *a, **k: _Conn(cur))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)

    def test_slack_token_comes_from_keychain_or_fleet_store(self):
        self.assertIn('"security", "find-generic-password"', SRC)
        self.assertIn('"nova-slack-bot-token"', SRC)
        self.assertIn('get_secret("nova-slack-bot-token")', SRC)

    def test_sql_is_parameterized(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn(".format(", SRC)
        self.assertIn("(%s || ' days')::interval", SRC)
        self.assertIn("VALUES ('question', %s, %s, %s)", SRC)

    def test_self_directed_sources_never_posted_to_him(self):
        self.assertIsNone(ask.pick([ROWS[1]]))
        self.assertIn("prediction_surprise", ask.SKIP_SOURCES)


class TestPerformance(unittest.TestCase):
    def test_pick_fast_on_10k_rows(self):
        rows = [(i, f"What were the motivations behind event {i}?", "history", None) for i in range(10_000)]
        rows.append((0, "Jordan, did the garage door close?", "home", None))
        t0 = time.perf_counter()
        r = ask.pick(rows)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(r[0], 0)  # scanned the whole list and still preferred the one about him

    def test_compose_bounded(self):
        t = ask.compose(1, "q" * 5000)
        self.assertLess(len(t), 5200)


class TestRetry(unittest.TestCase):
    def test_slack_post_has_no_retry_and_main_writes_no_orphan_row(self):
        # RETRY GAP: slack_post — one POST, the error escapes; the safe default is that no slack_prompts
        # row is written for a message that never reached Slack (harvester would wait forever otherwise)
        def boom(*a, **k):
            raise OSError("slack down")
        cur = _Cur([("count(*) FROM slack_prompts", (0,)), ("FROM reflection_questions", ROWS)])
        with patch.object(subprocess, "check_output", lambda *a, **k: b"tok"), \
                patch.object(ask.urllib.request, "urlopen", boom), _connect_with(cur), \
                patch.object(sys, "argv", ["nova_ask_one.py"]), patch.object(ask.sys, "platform", "darwin"):
            with self.assertRaises(OSError):
                ask.main()
        self.assertEqual(cur.executed("INSERT INTO slack_prompts"), [])

    def test_slack_api_error_surfaces_as_runtime_error(self):
        with patch.object(subprocess, "check_output", lambda *a, **k: b"tok"), \
                patch.object(ask.urllib.request, "urlopen", lambda *a, **k: _Resp({"ok": False, "error": "not_in_channel"})), \
                patch.object(ask.sys, "platform", "darwin"):
            with self.assertRaises(RuntimeError) as cm:
                ask.slack_post("hi")
        self.assertIn("not_in_channel", str(cm.exception))


class TestUnit(unittest.TestCase):
    def test_pick_prefers_about_him_then_newest_and_skips_self_sources(self):
        self.assertEqual(ask.pick(ROWS)[0], 5)
        self.assertEqual(ask.pick(ROWS[1:])[0], 3)
        self.assertIsNone(ask.pick([]))
        self.assertIsNone(ask.pick([(9, "   ", "x", None)]))
        self.assertIsNone(ask.pick([(9, None, None, None)]))

    def test_about_him_regex(self):
        self.assertTrue(ask.ABOUT_HIM_RE.search("Jordan, were you referring to the Sumerians?"))
        self.assertTrue(ask.ABOUT_HIM_RE.search("is the zigbee hub in the garage?"))
        self.assertFalse(ask.ABOUT_HIM_RE.search("What was the 1911 train wreck?"))

    def test_compose_has_id_and_thread_hint(self):
        t = ask.compose(5, "  Why?  ")
        self.assertTrue(t.startswith("One question, no rush (Q#5): Why?\n"))
        self.assertIn("thread", t)

    def test_demo_selftest_passes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ask.demo()
        self.assertIn("passed", buf.getvalue())


class TestIntegration(unittest.TestCase):
    def test_slack_post_targets_nova_chat_with_bearer_from_keychain(self):
        seen = {}

        def fake_open(req, timeout=20):
            seen["url"] = req.full_url; seen["auth"] = req.get_header("Authorization")
            seen["body"] = json.loads(req.data.decode()); return _Resp({"ok": True, "ts": "1.2"})
        with patch.object(subprocess, "check_output", lambda *a, **k: b"xoxb-from-keychain\n"), \
                patch.object(ask.urllib.request, "urlopen", fake_open), patch.object(ask.sys, "platform", "darwin"):
            self.assertEqual(ask.slack_post("hello"), "1.2")
        self.assertEqual(seen["url"], "https://slack.com/api/chat.postMessage")
        self.assertEqual(seen["auth"], "Bearer xoxb-from-keychain")
        self.assertEqual(seen["body"], {"channel": ask.CHANNEL, "text": "hello"})

    def test_schema_and_prompt_row_shape_match_the_harvester(self):
        cur = _Cur()
        ask.ensure_schema(cur)
        ddl = cur.sql[0][0]
        for col in ("kind text", "ref_id text", "channel text", "ts text", "resolved_at", "UNIQUE (kind, ref_id)"):
            self.assertIn(col, ddl)

    def test_pick_and_compose_chain_from_cursor_rows(self):
        cur = _Cur([("FROM reflection_questions", ROWS)])
        cur.execute("SELECT id, question, memory_source, asked_at FROM reflection_questions")
        row = ask.pick(cur.fetchall())
        self.assertIn("Q#5", ask.compose(row[0], row[1]))


class TestFunctional(unittest.TestCase):
    def _main(self, cur, argv=(), post=None):
        buf = io.StringIO()
        with _connect_with(cur), patch.object(sys, "argv", ["nova_ask_one.py", *argv]), \
                patch.object(ask, "slack_post", post or (lambda text: "171.5")), redirect_stdout(buf):
            rc = ask.main()
        return rc, buf.getvalue()

    def test_golden_path_posts_and_records_prompt(self):
        cur = _Cur([("count(*) FROM slack_prompts", (0,)), ("FROM reflection_questions", ROWS)])
        rc, out = self._main(cur)
        self.assertEqual(rc, 0)
        ins = cur.executed("INSERT INTO slack_prompts")
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][1], ("5", ask.CHANNEL, "171.5"))
        self.assertIn("asked Q#5", out)

    def test_open_question_blocks_stacking(self):
        cur = _Cur([("count(*) FROM slack_prompts", (1,))])
        rc, out = self._main(cur, post=lambda text: self.fail("must not post"))
        self.assertEqual(rc, 0)
        self.assertIn("still open", out)
        self.assertEqual(cur.executed("INSERT"), [])

    def test_dry_run_prints_without_posting(self):
        cur = _Cur([("count(*) FROM slack_prompts", (0,)), ("FROM reflection_questions", ROWS)])
        rc, out = self._main(cur, ["--dry-run"], post=lambda text: self.fail("must not post"))
        self.assertEqual(rc, 0)
        self.assertIn("Q#5", out)
        self.assertEqual(cur.executed("INSERT"), [])

    def test_nothing_to_ask(self):
        cur = _Cur([("count(*) FROM slack_prompts", (0,)), ("FROM reflection_questions", [])])
        rc, out = self._main(cur)
        self.assertEqual(rc, 0)
        self.assertIn("nothing to ask", out)


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("sys.exit(main())", SRC)
        self.assertEqual(ask.__name__, "ask1")


if __name__ == "__main__":
    unittest.main()
