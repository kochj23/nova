#!/usr/bin/env python3
"""Tests for nova_proactive_digest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame) plus the silence gate + feeds (2026-09-28 fix).
Written by Jordan Koch (via Claude)."""
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
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SRC = (SCRIPTS / "nova_proactive_digest.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("pd", SCRIPTS / "nova_proactive_digest.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class TestSilenceGate(unittest.TestCase):
    def test_bold_and_dressed_nothing_is_silence(self):          # regression: 5 mornings of "**Nothing**"
        pd = _load()
        for y in ("NOTHING", "**NOTHING**", "**Nothing.**", '"Nothing"', "_nothing_", "Nothing!\nmore"):
            self.assertTrue(pd.is_silence(y), y)

    def test_real_note_is_not_silence(self):                     # functional
        pd = _load()
        for n in ("Here's what I noticed", "**Nothing is wrong with the NAS**, but", "Nothing much, except the probe"):
            self.assertFalse(pd.is_silence(n), n)


class TestFeeds(unittest.TestCase):
    def test_reads_live_incident_table(self):                    # integration: legacy public.incidents had no opened_at
        self.assertIn("FROM telemetry.incidents", SRC)
        self.assertNotIn('FROM incidents "', SRC)

    def test_organ_noticings_are_candidates(self):
        for src in ("attention_focus", "pattern_sense", "human_insight"):
            self.assertIn(f'"{src}"', SRC)

    def test_no_stale_mini_ip_and_no_secrets(self):              # security / regression
        self.assertNotIn("192.168.1.251", SRC)
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_docstring_keeps_silence_as_valid(self):             # docs: the contract did not change
        self.assertIn("silence is a valid outcome", SRC)


# ── house categories added 2026-10-05 ──────────────────────────────────────────

PD = _load()
SCRIPT = SCRIPTS / "nova_proactive_digest.py"


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Cur:
    """Cursor stand-in: answers by SQL needle; records (sql, params)."""
    def __init__(self, answers=None, fail_on=None):
        self.answers = answers or {}; self.sql = []; self.fail_on = fail_on; self._last = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("relation missing")
        key = params[0] if params and "FROM memories" in sql else next((k for k in self.answers if k in sql), None)
        self._last = list(self.answers.get(key, []))

    def fetchall(self):
        return self._last


def _llm_reply(text):
    return lambda req, timeout=None: _Resp(json.dumps({"message": {"content": text}}).encode())


NOTE = "*KEV on your NAS.* Patch it before Friday — CISA says ransomware crews like it."
MEMS = {"research": [(1, "research", "a finding", datetime(2026, 1, 1))],
        "reflection_questions": [(7, "why do sparks cluster at 3am?")],
        "kev_matches": [("CVE-2026-1", "DSM", "nas", "storage", "Known", None)],
        "telemetry.incidents": [("critical", "nova-core2", "disk full")]}


def _main(argv, net):
    mc, oc = _Cur(MEMS), _Cur(MEMS)
    conns = iter([MagicMock(cursor=lambda: mc), MagicMock(cursor=lambda: oc)])
    with patch.object(PD.psycopg2, "connect", side_effect=lambda dsn: next(conns)), \
         patch.object(PD.urllib.request, "urlopen", net), patch.object(PD.nova_config, "post_both") as pb, \
         patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as out:
        PD.main()
    return oc, pb, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_private_sources_filtered(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        mc = _Cur({"research": [(1, "research", "ok", None)]})
        with patch.object(PD.nova_config, "filter_private_memories", side_effect=lambda rows: []) as flt:
            self.assertEqual(PD._mem_rows(mc, "research"), [])
        flt.assert_called_once()
        self.assertEqual(mc.sql[0][1], ("research", 24, 15))

    def test_communication_only_never_acts(self):
        for verb in ("subprocess", "os.system", "requests.post"):
            self.assertNotIn(verb, SRC)


class TestPerformance(unittest.TestCase):
    def test_gather_and_prompt_10k_candidates(self):
        big = {"reflection_questions": [(i, f"q{i}") for i in range(10_000)]}
        t0 = time.perf_counter()
        cands = PD.gather(_Cur(), _Cur(big))
        with patch.object(PD, "llm", return_value="NOTHING") as llm:
            PD.curate_and_write(cands)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(cands), 10_000)
        self.assertIn("10000. [open_question] q9999", llm.call_args.args[0])


class TestRetry(unittest.TestCase):
    def test_llm_falls_through_the_fleet(self):
        seen = []

        def net(req, timeout=None):
            seen.append(req.full_url)
            if len(seen) < 3:
                raise OSError("node down")
            return _Resp(json.dumps({"message": {"content": "third node answers"}}).encode())
        with patch.object(PD.urllib.request, "urlopen", net):
            self.assertEqual(PD.llm("p"), "third node answers")
        self.assertEqual(seen, [n + "/api/chat" for n in PD.OLLAMA_NODES])

    def test_total_llm_failure_posts_nothing(self):
        with patch.object(PD.urllib.request, "urlopen", side_effect=OSError("all down")):
            self.assertEqual(PD.llm("p"), "")
            self.assertEqual(PD.curate_and_write([{"kind": "x", "text": "y"}]), ("", []))

    def test_incident_table_failure_is_swallowed(self):
        with redirect_stdout(io.StringIO()) as out:
            cands = PD.gather(_Cur(), _Cur(MEMS, fail_on="telemetry.incidents"))
        self.assertIn("incidents read failed", out.getvalue())
        self.assertTrue(any(c["kind"] == "kev_match" for c in cands))


class TestUnit(unittest.TestCase):
    def test_demo_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            PD.demo()
        self.assertIn("all proactive-digest assertions passed", out.getvalue())

    def test_empty_candidates_skip_llm(self):
        with patch.object(PD, "llm") as llm:
            self.assertEqual(PD.curate_and_write([]), ("", []))
        llm.assert_not_called()

    def test_kev_text_flags_ransomware(self):
        c = [x for x in PD.gather(_Cur(), _Cur(MEMS)) if x["kind"] == "kev_match"][0]
        self.assertEqual(c["text"], "CVE-2026-1: DSM affecting your nas (ransomware-linked). CISA due n/a.")


class TestIntegration(unittest.TestCase):
    def test_gather_reads_memories_and_ops_tables(self):
        mc, oc = _Cur(MEMS), _Cur(MEMS)
        cands = PD.gather(mc, oc)
        self.assertEqual([p[0] for _, p in mc.sql][:3], ["research", "association", "episodic"])
        kinds = {c["kind"] for c in cands}
        self.assertEqual(kinds, {"research_finding", "open_question", "kev_match", "incident"})
        self.assertEqual(PD.DIGEST_CHANNEL, PD.nova_config.SLACK_CHAN)


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_and_logs_run(self):
        oc, pb, out = _main(["x"], _llm_reply(NOTE))
        self.assertTrue(pb.call_args.args[0].startswith("🌿 *Nova — a few things I noticed today*"))
        self.assertIn(NOTE, out)
        sql, params = oc.sql[-1]
        self.assertIn("INSERT INTO proactive_digest_log", sql)
        self.assertTrue(params[1])

    def test_silence_logs_unposted_and_dry_run_writes_nothing(self):
        oc, pb, _ = _main(["x"], _llm_reply("**NOTHING**"))
        pb.assert_not_called()
        self.assertFalse(oc.sql[-1][1][1])
        oc, pb, out = _main(["x", "--dry-run"], _llm_reply(NOTE))
        pb.assert_not_called()
        self.assertIn("DRY RUN — would post", out)
        self.assertFalse(any("INSERT" in s for s, _ in oc.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all proactive-digest assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    if "--selftest" in sys.argv:', SRC)


if __name__ == "__main__":
    unittest.main()
