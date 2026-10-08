#!/usr/bin/env python3
"""Tests for nova_jade_amulet.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_jade_amulet as J  # noqa: E402

SRC = (SCRIPTS / "nova_jade_amulet.py").read_text()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def tags_response(models):
    return io.BytesIO(json.dumps({"models": models}).encode())


MODEL = {"name": "qwen3:235b", "digest": "754a" * 16, "modified_at": "2026-10-01T00:00:00Z"}
PREV_ROUTES = {
    "to_regclass": [("jade_amulet_manifest",)],
    "FROM jade_amulet_manifest m": [(T0, "ollama_model", "qwen3:235b", "old" * 4),
                                    (T0, "ollama_model", "gone:1b", "bb" * 8)],
    "FROM claude_actions": [(5, "ollama rm gone:1b")],
    "FROM unexplained_events": [],
}


class TestSecurity(unittest.TestCase):

    def test_read_actions_never_explain_a_change(self):
        import inspect
        src = inspect.getsource(J.actions_since)
        self.assertIn("file_read", src)
        self.assertIn("CHANGE_VERBS", src)
        self.assertRegex(J.CHANGE_VERBS, "pull")
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b(?!127\.0\.0\.1)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_name_stays_a_parameter(self):
        cur = FakeCur()
        evil = "x'; DROP TABLE claude_actions; --"
        J.write(cur, "h", T0, [{"kind": "ollama_model", "name": evil, "version": None, "digest": "d", "origin": "o"}])
        ins = [(s, p) for s, p in cur.sql if "INSERT" in s][0]
        self.assertNotIn(evil, ins[0])
        self.assertIn(evil, ins[1])

    def test_expected_updaters_allowlist_suppresses(self):
        with mock.patch.object(J, "EXPECTED_UPDATERS", ("auto:*",)):
            ch = J.match([{"change": "changed", "kind": "ollama_model", "name": "auto:1", "old": "a", "new": "b"}], [])
            self.assertTrue(ch[0]["expected"])
            with mock.patch("nova_buick8_log.log_unexplained") as lu:
                self.assertEqual(J.report(FakeCur(), ch, T0), 0)
                lu.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_diff_and_match_10k(self):
        prev = [{"kind": "ollama_model", "name": f"m{i}:1", "digest": "a"} for i in range(10000)]
        cur = [dict(p, digest="b" if i % 100 == 0 else "a") for i, p in enumerate(prev)]
        t = time.monotonic()
        ch = J.match(J.diff(prev, cur), [(i, f"action {i}") for i in range(1000)])
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(len(ch), 100)


class TestRetry(unittest.TestCase):
    def test_ollama_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection refused")
            return tags_response([MODEL])
        with mock.patch("urllib.request.urlopen", side_effect=flaky), mock.patch("builtins.print"):
            items = J.ollama_models(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(items[0]["name"], "qwen3:235b")

    def test_ollama_down_fails_open(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), mock.patch("builtins.print"):
            self.assertIsNone(J.ollama_models(_sleep=lambda s: None))

    def test_query_failure_contained(self):
        with mock.patch("builtins.print"):
            self.assertEqual(J.previous(FakeCur(boom=True), "h"), (None, []))


class TestUnit(unittest.TestCase):
    def test_diff_kinds(self):
        a = [{"kind": "ollama_model", "name": "m", "digest": "1"}]
        self.assertEqual(J.diff([], []), [])
        self.assertEqual(J.diff(a, a), [])
        self.assertEqual(J.diff(a, []), [])   # no current items of that kind -> skipped, not "removed"
        self.assertEqual(J.diff([], a)[0]["change"], "added")

    def test_needle_uses_plist_label(self):
        self.assertEqual(J.needle({"kind": "launchd_plist", "name": "/L/net.digitalnoise.x.plist"}),
                         "net.digitalnoise.x")
        self.assertEqual(J.needle({"kind": "ollama_model", "name": "qwen3:235b"}), "qwen3:235b")

    def test_plists_hash_and_filter(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "net.digitalnoise.a.plist").write_bytes(b"<plist/>")
            (Path(d) / "com.apple.b.plist").write_bytes(b"x")
            items = J.plists((Path(d), Path(d) / "missing"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["digest"], hashlib.sha256(b"<plist/>").hexdigest())

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(J.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_buick8_log import log_unexplained", SRC)
        self.assertIn("W.retry(", SRC)

    def test_contract_table(self):
        for col in ("ts timestamptz NOT NULL DEFAULT now()", "host text", "kind text", "name text",
                    "version text", "digest text", "origin text"):
            self.assertIn(col, J.SCHEMA)
        self.assertIn("(kind, name, ts)", J.SCHEMA)

    def test_unmatched_goes_to_buick8_without_cause(self):
        ch = J.match(J.diff([{"kind": "ollama_model", "name": "m:1", "digest": "a"}],
                            [{"kind": "ollama_model", "name": "m:1", "digest": "b"}]), [])
        with mock.patch("nova_buick8_log.log_unexplained") as lu:
            self.assertEqual(J.report(FakeCur(), ch, T0), 1)
        args, kw = lu.call_args
        self.assertEqual(args[0], "substrate_change")
        self.assertEqual(kw["source"], "jade_amulet")
        self.assertNotIn("cause", kw)


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes):
        cur = FakeCur(routes)
        with mock.patch.object(J.W, "connect", return_value=fake_conn(cur)), \
                mock.patch("urllib.request.urlopen", return_value=tags_response([MODEL])), \
                mock.patch.object(J, "plists", return_value=[]), \
                mock.patch("nova_buick8_log.log_unexplained") as lu, mock.patch("builtins.print"):
            changes = J.run(dry=dry)
        return cur, changes, lu

    def test_run_writes_snapshot_and_reports_unmatched(self):
        cur, changes, lu = self._run(False, PREV_ROUTES)
        kinds = {(c["change"], c["name"]): c["action_id"] for c in changes}
        self.assertEqual(kinds, {("changed", "qwen3:235b"): None, ("removed", "gone:1b"): 5})
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS jade_amulet_manifest" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO jade_amulet_manifest" in s for s, _ in cur.sql), 1)
        self.assertEqual(lu.call_count, 1)                  # only the unmatched digest change

    def test_dry_run_writes_nothing(self):
        cur, changes, lu = self._run(True, PREV_ROUTES)
        self.assertEqual(len(changes), 2)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE")))
        lu.assert_not_called()

    def test_first_run_has_no_diff(self):
        cur, changes, lu = self._run(False, {"to_regclass": [(None,)]})
        self.assertEqual(changes, [])
        lu.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_jade_amulet.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_jade_amulet.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
