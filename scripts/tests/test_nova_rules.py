#!/usr/bin/env python3
"""Tests for nova_rules.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_rules.py"
SRC = SCRIPT.read_text()
SEP = "\x1f"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nr = _load("nr", SCRIPT)
nr.log = MagicMock()                 # nova_logger.log would append to ~/.openclaw/logs/nova.jsonl


class _Psql:
    """subprocess.run stand-in for the psql CLI: records every argv, answers `rows` on -tA queries."""
    def __init__(self, rows=None, rc=0, exc=None):
        self.rows = rows or []; self.rc = rc; self.exc = exc; self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        out = "\n".join(SEP.join(r) for r in self.rows) if "-tA" in argv else ""
        return subprocess.CompletedProcess(argv, self.rc, stdout=out, stderr="boom" if self.rc else "")

    def sql(self, i=-1):
        return self.calls[i][-1]


def _row(i, rule="be nicer", topic="global", conf="1.0", applied="0"):
    return [f"id{i}", rule, topic, conf, applied, "2026-10-05"]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_rule_text_topic_and_context_are_escaped_before_psql(self):
        ps = _Psql()
        with patch("subprocess.run", ps):
            rid = nr.add_rule("x'; DROP TABLE rules; --", topic="t'", context="c'", original_correction={"a": "it's"})
        self.assertIsNotNone(rid)
        sql = ps.sql()
        self.assertIn("'x''; DROP TABLE rules; --'", sql)       # the quote is doubled: still one literal
        self.assertIn("'t'''", sql)
        self.assertIn("it''s", sql)
        self.assertNotIn("; DROP TABLE rules; --'" + "\n", sql.replace("''", ""))
        self.assertEqual(ps.calls[0][:4], ["psql", "-U", "kochj", "-d"])   # argv list, never a shell string

    def test_identifiers_from_callers_are_escaped_too(self):
        ps = _Psql()
        with patch("subprocess.run", ps):
            nr.retire_rule("abc' OR '1'='1")
            nr.record_application("r'1", context="ctx")
            nr.add_rule("r", source_type="pref'x", expires_at="2026-01-01' --")
        self.assertIn("WHERE id = 'abc'' OR ''1''=''1'", ps.sql(0))
        self.assertIn("'r''1'", ps.sql(1))
        self.assertIn("'pref''x'", ps.sql(2))
        self.assertIn("'2026-01-01'' --'", ps.sql(2))

    def test_confidence_is_coerced_to_a_number(self):
        ps = _Psql()
        with patch("subprocess.run", ps), self.assertRaises(ValueError):
            nr.add_rule("r", confidence="1; DROP TABLE rules")
        self.assertEqual(ps.calls, [])

    def test_topic_filter_is_escaped(self):
        ps = _Psql()
        with patch("subprocess.run", ps):
            nr.get_active_rules(topic="people' OR 1=1 --")
        self.assertIn("topic = 'people'' OR 1=1 --'", ps.sql())


class TestPerformance(unittest.TestCase):
    def test_prompt_formatting_on_10k_rules_is_fast(self):
        ps = _Psql(rows=[_row(i, topic="people" if i % 2 else "global") for i in range(10_000)])
        with patch("subprocess.run", ps):
            t0 = time.perf_counter()
            text = nr.format_rules_for_prompt("people")
            dt = time.perf_counter() - t0
        self.assertLess(dt, 1.0)
        self.assertEqual(text.count("\n- "), 10_000)
        self.assertEqual(text.count("[people] "), 5_000)

    def test_correction_to_rule_on_10k_items(self):
        items = [{"nova_response": f"n{i}", "jordan_correction": f"j{i}"} for i in range(10_000)]
        t0 = time.perf_counter()
        out = [nr._correction_to_rule(c) for c in items]
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(out), 10_000)


class TestRetry(unittest.TestCase):
    def test_query_fails_open_on_psql_error(self):
        # RETRY GAP: _query — one psql attempt; a non-zero exit returns [] and is logged
        ps = _Psql(rc=1)
        with patch("subprocess.run", ps):
            self.assertEqual(nr.get_active_rules(), [])
        self.assertEqual(len(ps.calls), 1)
        nr.log.assert_called()

    def test_query_fails_open_on_timeout(self):
        # RETRY GAP: _query — a raised TimeoutExpired is swallowed, [] returned, no second attempt
        ps = _Psql(exc=subprocess.TimeoutExpired("psql", 10))
        with patch("subprocess.run", ps):
            self.assertEqual(nr.get_all_rules(), [])
        self.assertEqual(len(ps.calls), 1)

    def test_exec_returns_false_on_psql_error_but_a_raise_escapes(self):
        # RETRY GAP: _exec — no retry; non-zero exit -> False (add_rule -> None), but a timeout escapes
        with patch("subprocess.run", _Psql(rc=1)):
            self.assertIsNone(nr.add_rule("r"))
            self.assertFalse(nr.retire_rule("id1"))
        with patch("subprocess.run", _Psql(exc=subprocess.TimeoutExpired("psql", 10))), self.assertRaises(subprocess.TimeoutExpired):
            nr.add_rule("r")


class TestUnit(unittest.TestCase):
    def test_escape_edges(self):
        self.assertEqual(nr._escape(None), "")
        self.assertEqual(nr._escape(""), "")
        self.assertEqual(nr._escape("it's"), "it''s")
        self.assertEqual(nr._escape("a\\b"), "a\\\\b")

    def test_correction_to_rule_edges(self):
        self.assertIsNone(nr._correction_to_rule({}))
        self.assertIsNone(nr._correction_to_rule({"nova_response": "x"}))
        self.assertEqual(nr._correction_to_rule({"jordan_correction": "use metric"}), "use metric")
        self.assertEqual(nr._correction_to_rule({"nova_response": "5 ft", "jordan_correction": "1.5 m"}),
                         "Do NOT say: '5 ft'. Correct answer: 1.5 m")

    def test_active_rules_parsing_skips_short_rows_and_defaults_blanks(self):
        rows = [_row(1, conf="", applied=""), ["short", "row"], _row(2, conf="0.5", applied="3")]
        with patch("subprocess.run", _Psql(rows=rows)):
            rules = nr.get_active_rules()
        self.assertEqual([r["id"] for r in rules], ["id1", "id2"])
        self.assertEqual((rules[0]["confidence"], rules[0]["times_applied"]), (1.0, 0))
        self.assertEqual((rules[1]["confidence"], rules[1]["times_applied"]), (0.5, 3))

    def test_all_rules_parsing_carries_status_and_source_type(self):
        rows = [["id1", "r", "global", "retired", "1.0", "2", "preference"]]
        with patch("subprocess.run", _Psql(rows=rows)):
            rules = nr.get_all_rules()
        self.assertEqual(rules[0]["status"], "retired")
        self.assertEqual(rules[0]["source_type"], "preference")

    def test_format_rules_for_prompt_empty_and_topic_tags(self):
        with patch("subprocess.run", _Psql(rows=[])):
            self.assertEqual(nr.format_rules_for_prompt(), "")
        with patch("subprocess.run", _Psql(rows=[_row(1), _row(2, rule="ask first", topic="email")])):
            text = nr.format_rules_for_prompt()
        self.assertTrue(text.startswith("## Active Rules"))
        self.assertIn("\n- be nicer", text)
        self.assertIn("\n- [email] ask first", text)

    def test_promote_corrections_without_a_file_is_zero(self):
        with tempfile.TemporaryDirectory() as td, patch.object(nr, "CORRECTIONS_FILE", Path(td) / "none.json"):
            self.assertEqual(nr.promote_corrections(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_logger_is_imported_not_reimplemented(self):
        self.assertIn("from nova_logger import log", SRC)
        self.assertNotIn("def log(", SRC)
        self.assertEqual(nr.DB, "nova_ops")

    def test_schema_targets_the_rules_tables(self):
        ps = _Psql()
        with patch("subprocess.run", ps):
            nr.ensure_schema()
        sql = ps.sql()
        self.assertIn("CREATE TABLE IF NOT EXISTS rules", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS rule_applications", sql)
        self.assertEqual(ps.calls[0][3:5], ["-d", "nova_ops"])

    def test_ingest_correction_chains_file_history_and_rule_insert(self):
        ps = _Psql()
        with tempfile.TemporaryDirectory() as td, patch.object(nr, "CORRECTIONS_FILE", Path(td) / "corrections.json"), \
             patch("subprocess.run", ps):
            rid = nr.ingest_correction("Amy is 40", "Amy is 41", topic="people")
            saved = json.loads((Path(td) / "corrections.json").read_text())
        self.assertEqual(len(rid), 8)
        self.assertEqual(saved[0]["jordan_correction"], "Amy is 41")
        self.assertIn("Do NOT say: ''Amy is 40''. Correct answer: Amy is 41", ps.sql())
        self.assertIn("INSERT INTO rules", ps.sql())
        self.assertIn("'people'", ps.sql())

    def test_promote_skips_rules_that_already_exist(self):
        existing = [["id1", "use metric", "global", "active", "1.0", "0", "correction"]]
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "-tA" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="\n".join(SEP.join(r) for r in existing), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        corrections = [{"jordan_correction": "use metric"}, {"jordan_correction": "spell Koch"}, {"nova_response": "x"}]
        with tempfile.TemporaryDirectory() as td, patch.object(nr, "CORRECTIONS_FILE", Path(td) / "c.json"), patch("subprocess.run", run):
            (Path(td) / "c.json").write_text(json.dumps(corrections))
            self.assertEqual(nr.promote_corrections(), 1)
        inserts = [a[-1] for a in calls if "INSERT INTO rules" in a[-1]]
        self.assertEqual(len(inserts), 1)
        self.assertIn("'spell Koch'", inserts[0])


def _main(argv, ps):
    out = io.StringIO()
    with patch.object(sys, "argv", ["nova_rules.py", *argv]), patch("subprocess.run", ps), redirect_stdout(out):
        nr.main()
    return out.getvalue()


class TestFunctional(unittest.TestCase):
    def test_prompt_command_prints_the_rule_block(self):
        out = _main(["prompt", "--topic", "people"], _Psql(rows=[_row(1, rule="Amy is 41", topic="people")]))
        self.assertIn("## Active Rules", out)
        self.assertIn("- [people] Amy is 41", out)

    def test_correct_command_records_and_promotes(self):
        ps = _Psql()
        with tempfile.TemporaryDirectory() as td, patch.object(nr, "CORRECTIONS_FILE", Path(td) / "c.json"):
            out = _main(["correct", "--nova", "wrong", "--jordan", "right"], ps)
            self.assertTrue((Path(td) / "c.json").exists())
        self.assertRegex(out, r"Correction recorded and promoted to rule \[[0-9a-f]{8}\]")
        self.assertIn("INSERT INTO rules", ps.sql())

    def test_list_all_and_init(self):
        rows = [["id1", "r", "global", "retired", "1.0", "2", "preference"]]
        out = _main(["list", "--all"], _Psql(rows=rows))
        self.assertIn("[id1] (global) r (retired) [applied 2x]", out)
        self.assertIn("Schema ready.", _main(["init"], _Psql()))

    def test_add_prints_nothing_when_psql_fails(self):
        out = _main(["add", "be terse"], _Psql(rc=1))
        self.assertNotIn("Rule [", out)
        self.assertIn("Rule [", _main(["add", "be terse", "--topic", "style"], _Psql()))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        for cmd in ("init", "add", "retire", "list", "prompt", "promote", "correct"):
            self.assertIn(cmd, r.stdout)
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_rules"], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
