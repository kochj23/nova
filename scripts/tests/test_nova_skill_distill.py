#!/usr/bin/env python3
"""Tests for nova_skill_distill.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_skill_distill.py"
SRC = PATH.read_text()

import nova_coagency  # noqa: E402 — import-clean; file_proposal patched per test
import nova_config  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("skill_distill_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sk = _load()
_PATCHES = []


def setUpModule():
    for p in (patch.object(sk.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(psycopg2, "connect", side_effect=AssertionError("unmocked PG")),
              patch.object(nova_config, "post_both"),
              patch.object(nova_coagency, "file_proposal", side_effect=AssertionError("unmocked proposal"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


CARD = {"title": "Draft a Status Check-In", "trigger": "when a goal goes quiet", "summary": "Write a short check-in.",
        "steps": ["read goal", "draft", "post"], "inputs": ["goal"], "success_check": "posted", "rollback": "delete draft",
        "risk": "low"}
USES_SQL = "UPDATE nova_skills SET uses"


class _Cur:
    """Routes SELECTs by table to canned rows; records every statement."""
    def __init__(self, tables=None, slug_taken=False):
        self.t = tables or {}; self.sql = []; self.slug_taken = slug_taken; self._last = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        for key, rows in self.t.items():
            if key in sql and sql.lstrip().upper().startswith("SELECT"):
                self._last = rows; return
        self._last = []

    def fetchall(self):
        return self._last

    def fetchone(self):
        return (1,) if self.slug_taken else None


def _resp(content):
    r = MagicMock(); r.__enter__.return_value = r
    r.read.return_value = json.dumps({"message": {"content": content}}).encode()
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_writes_skills_and_proposals(self):
        writes = set(re.findall(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)", SRC))
        self.assertEqual(writes, {"nova_skills"})        # proposals go through nova_coagency.file_proposal

    def test_redline_strips_model_words_from_rationale(self):
        cur = _Cur()
        with patch.object(nova_coagency, "redline_ok", return_value=False), \
                patch.object(nova_coagency, "file_proposal", return_value={"id": 7}) as fp:
            self.assertEqual(sk.file_proposal(cur, "s", {**CARD, "summary": "delete all backups"},
                                              {"count": 4, "signal": "coagency"}), 7)
        self.assertNotIn("delete all backups", fp.call_args.kwargs["rationale"])

    def test_interpolated_sql_only_uses_module_constants(self):
        found = re.findall(r'" % (\(?[\w, \["\]]+\)?)', SRC)
        self.assertTrue(found)
        for g in found:
            self.assertRegex(g, r"WINDOW_DAYS|MIN_REPEATS")


class TestPerformance(unittest.TestCase):
    def test_normalize_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            sk.normalize(f"Execute approved co-agency proposal #{i}: retire goal 'G{i}' (6f7261a0): untouched {i}d")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_llm_falls_through_nodes_until_one_answers(self):
        seq = [OSError("down"), OSError("down"), _resp('{"title":"x"}')]
        with patch.object(sk.urllib.request, "urlopen", side_effect=seq) as u:
            self.assertEqual(sk.llm("p"), '{"title":"x"}')
        self.assertEqual(u.call_count, 3)
        self.assertTrue(u.call_args_list[2].args[0].full_url.startswith(sk.OLLAMA_NODES[2]))

    def test_all_nodes_down_returns_empty(self):
        with patch.object(sk.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertEqual(sk.llm("p"), "")
        self.assertEqual(u.call_count, len(sk.OLLAMA_NODES))

    def test_proposal_failure_is_fail_open(self):
        with patch.object(nova_coagency, "file_proposal", side_effect=RuntimeError("pg")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(sk.file_proposal(_Cur(), "s", CARD, {"count": 3, "signal": "x"}))
        self.assertIn("file_proposal failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sk.selftest(), 0)
        self.assertIn("selftest ok", out.getvalue())

    def test_parse_card_clamps(self):
        c = sk.parse_card("prefix " + json.dumps({**CARD, "inputs": [str(i) for i in range(20)], "risk": "HIGH"}) + " tail")
        self.assertEqual((len(c["inputs"]), c["risk"]), (8, "high"))
        self.assertIsNone(sk.parse_card(json.dumps({**CARD, "steps": ["a"] * 8})))
        self.assertIsNone(sk.parse_card("{not json}"))
        self.assertEqual(sk.slugify("!!!"), "skill")
        self.assertEqual(len(sk.trigger_key("x")), 12)


class TestIntegration(unittest.TestCase):
    def test_gather_thresholds_per_signal(self):
        cur = _Cur({"coagency_proposals": [("Draft check-in for goal 1",)] * 3 + [("rare thing happening",)],
                    "claude_queue": [("Execute approved co-agency proposal #5: Draft check-in for goal 9",)],
                    "pursuit_threads": [("tides", "topic", 4)],
                    "autonomy_ledger": [("restart_ha", 6)]})
        got = {c["key"]: (c["signal"], c["count"]) for c in sk.gather(cur)}
        self.assertEqual(got["draft check-in for goal N"], ("coagency", 4))   # handoff folds into same key
        self.assertIn("pursue topic: tides", got)
        self.assertIn("autonomous restart_ha", got)
        self.assertNotIn("rare thing happening", got)

    def test_known_keys_and_store_slug_collision(self):
        cur = _Cur({"nova_skills": [("When X happens", "coagency:draft check-in for goal N"), ("t", None)]})
        self.assertEqual(sk.known_trigger_keys(cur), {"when x happens", "draft check-in for goal N", "t"})
        cur = _Cur(slug_taken=True)
        slug = sk.store_skill(cur, {"key": "k", "signal": "coagency", "count": 4}, CARD, 9)
        self.assertTrue(slug.startswith("draft-a-status-check-in-"))
        params = cur.sql[-1][1]
        self.assertEqual((params[9], params[10], params[11]), ("coagency:k", 4, 9))


class TestFunctional(unittest.TestCase):
    def _run(self, dry, card_json, pid=11):
        cur = _Cur({"slug, source_signal, created_at": [("old-skill", "coagency:draft check-in for goal N", "t0")],
                    "coagency_proposals": [("Draft check-in for goal 1",)] * 4})
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(psycopg2, "connect", return_value=conn), patch.object(sk, "llm", return_value=card_json), \
                patch.object(nova_coagency, "file_proposal", return_value=pid) as fp, \
                patch.object(nova_config, "post_both") as post, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sk.run(dry, 2), 0)
        return cur, fp, post, out.getvalue()

    def test_golden_path_files_stores_and_announces(self):
        cur, fp, post, out = self._run(False, json.dumps(CARD))
        self.assertTrue(any("INSERT INTO nova_skills" in s for s, _ in cur.sql))
        self.assertIn("adopt skill 'draft-a-status-check-in'", fp.call_args.kwargs["action"])
        self.assertIn("co-agency #11", post.call_args.args[0])
        uses = [p for s, p in cur.sql if USES_SQL in s]
        self.assertEqual(uses, [(4, "old-skill")])       # recount of the trigger recurring since the skill

    def test_dry_run_and_bad_card_write_nothing(self):
        cur, fp, post, out = self._run(True, json.dumps(CARD))
        fp.assert_not_called(); post.assert_not_called()
        self.assertIn("DRY coagency x4", out)
        cur, fp, post, out = self._run(False, "garbage")
        self.assertFalse(any("INSERT" in s for s, _ in cur.sql))
        self.assertIn("no usable card", out)


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_skill_distill"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
