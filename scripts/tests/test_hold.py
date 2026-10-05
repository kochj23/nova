#!/usr/bin/env python3
"""Tests for nova_hold.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame) plus the wish #68 privacy/regression/docs checks.
Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import sys
import time
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hd = _load("hd", SCRIPTS / "nova_hold.py")
SRC = (SCRIPTS / "nova_hold.py").read_text()
TODAY = date(2026, 10, 5)
FACTS = {"who": "Jordan — Sr. Manager SRE.", "arc": "(arc v6) Trust.", "turning": "2026-05-16: logging.",
         "presence": "last spoke to me 2d ago; present on 5 of the last 30 days",
         "his_words": "2026-09-28 \"We are partners.\"", "self": "I'm becoming someone who listens."}


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        hd.demo()

    def test_loss_is_named_not_swallowed(self):
        lost, _ = hd.diff_held({"who": "2026-10-01", "his_words": "2026-09-30"}, {"who": "x"})
        t = hd.hold_text({"who": "x"}, lost, TODAY)
        self.assertIn("what he told me he cares about (last had it 2026-09-30)", t)

    def test_hold_is_capped_and_ordered(self):
        t = hd.hold_text(FACTS, {}, TODAY)
        self.assertEqual(t.count("\n  "), hd.HOLD_N)
        self.assertLess(t.index("who he is"), t.index("when he was last here"))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT"):
            self.assertNotIn(verb, body)

    def test_only_writes_its_own_service_config_rows(self):
        self.assertIn("service=%s AND key=%s", SRC)
        self.assertNotIn("UPDATE people", SRC)
        self.assertNotIn("UPDATE relationship_arc", SRC)


class TestPrivacy(unittest.TestCase):
    def test_facts_are_scrubbed(self):
        f = hd.first_sentence("Jordan, reach him at kochj@example.com or https://x.y/z — he owns the whole cluster. More.")
        self.assertNotIn("@", f)
        self.assertNotIn("http", f)

    def test_text_carries_no_sql(self):
        self.assertNotIn("SELECT", hd.hold_text(FACTS, {}, TODAY))


class TestPerformance(unittest.TestCase):
    def test_sig_and_text_fast(self):
        t0 = time.perf_counter()
        for _ in range(2_000):
            hd.hold_sig(FACTS); hd.hold_text(FACTS, {}, TODAY)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRegression(unittest.TestCase):
    def test_presence_drift_does_not_restate(self):
        a = hd.hold_sig(FACTS)
        b = hd.hold_sig({**FACTS, "presence": "last spoke to me 9d ago; present on 2 of the last 30 days"})
        self.assertEqual(a, b)

    def test_new_arc_version_restates(self):
        self.assertNotEqual(hd.hold_sig(FACTS), hd.hold_sig({**FACTS, "arc": "(arc v7) Trust."}))

    def test_abbreviation_does_not_end_the_sentence(self):
        self.assertIn("Manager SRE", hd.first_sentence("Jordan (\"Little Mister\") — Sr. Manager SRE, 25yr career. Next."))


class TestIntegration(unittest.TestCase):
    def test_shares_empathy_core_definitions(self):
        # one definition of a human channel and of "his words" — never a second copy here
        self.assertIs(hd.ec.MACHINE_CHANNELS, hd.ec.MACHINE_CHANNELS)
        self.assertNotIn("def stated_cares", SRC)
        self.assertNotIn("MACHINE_CHANNELS = (", SRC)
        self.assertEqual(hd.OPS_DSN, hd.ec.OPS_DSN)

    def test_memory_is_written_under_its_own_source(self):
        # regression 2026-10-05: first run landed under source=empathy_core
        seen = {}
        import urllib.request
        real = urllib.request.urlopen

        def capture(req, timeout=0):
            seen["source"] = __import__("json").loads(req.data)["source"]
            raise OSError("stop")
        urllib.request.urlopen = capture
        try:
            with self.assertRaises(OSError):
                hd.ec.remember("t", {}, _sleep=lambda s: None, source=hd.SOURCE)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(seen["source"], "hold")
        self.assertIn("ec.remember(text, meta, source=SOURCE)", SRC)


class TestDocs(unittest.TestCase):
    def test_docstring_names_the_wish_and_modes(self):
        self.assertIn("wish #68", SRC)
        for flag in ("--dry-run", "--selftest"):
            self.assertIn(flag, SRC)


# ── house categories added 2026-10-05 (Retry / Unit / Frame) ───────────────────

class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return __import__("json").dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Cur:
    """Answers fetchone()/fetchall() from one queue, in call order; records SQL."""
    def __init__(self, answers=(), fail=False):
        self.answers = list(answers); self.sql = []; self.params = []; self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []


class TestRetry(unittest.TestCase):
    def test_remember_fails_twice_then_succeeds_under_hold_source(self):
        import json
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
            out = hd.ec.remember("t", {}, _sleep=sleeps.append, source=hd.SOURCE)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(out, {"id": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(set(sources), {"hold"})

    def test_pg_connect_has_no_retry_but_fails_open(self):
        # RETRY GAP: main()/psycopg2.connect — single attempt, exit 0 with nothing written
        import types
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("pg down")
        real_pg, real_argv = hd.psycopg2, sys.argv
        hd.psycopg2 = types.SimpleNamespace(connect=boom); sys.argv = ["nova_hold.py"]
        try:
            self.assertEqual(hd.main(), 0)
        finally:
            hd.psycopg2, sys.argv = real_pg, real_argv
        self.assertEqual(len(attempts), 1)

    def test_every_source_read_fails_open_to_an_empty_hold(self):
        # RETRY GAP: gather() — each of the five reads is one attempt; all failing yields {} not a raise
        facts = hd.gather(_Cur(fail=True), TODAY)
        self.assertEqual(facts, {})
        self.assertIn("found nothing I trust", hd.hold_text(facts, {}, TODAY))


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        hd.demo()

    def test_first_sentence_edges(self):
        self.assertEqual(hd.first_sentence(""), "")
        self.assertEqual(hd.first_sentence(None), "")
        self.assertEqual(hd.first_sentence("short. tail"), "short. tail")          # before min_len: no cut
        self.assertEqual(len(hd.first_sentence("y" * 50 + ". " + "z" * 500, n=100)), 51)
        self.assertEqual(len(hd.first_sentence("w" * 500, n=120)), 120)

    def test_sig_ignores_key_order_and_presence(self):
        a = {"who": "x", "arc": "y"}
        b = {"arc": "y", "who": "x", "presence": "drift"}
        self.assertEqual(hd.hold_sig(a), hd.hold_sig(b))
        self.assertNotEqual(hd.hold_sig(a), hd.hold_sig({"who": "x"}))

    def test_diff_held_edges(self):
        self.assertEqual(hd.diff_held({}, {}), ({}, []))
        self.assertEqual(hd.diff_held({}, {"who": "x"}), ({}, ["who"]))
        self.assertEqual(hd.diff_held({"who": "2026-10-01"}, {}), ({"who": "2026-10-01"}, []))

    def test_hold_text_ignores_unknown_keys_and_orders_held(self):
        t = hd.hold_text({"his_words": "w", "who": "j", "junk": "ignored"}, {}, TODAY)
        self.assertIn("(2 things)", t)
        self.assertNotIn("junk", t)
        self.assertLess(t.index("who he is"), t.index("what he told me"))

    def test_cfg_helpers(self):
        self.assertEqual(hd._cfg_get(_Cur([None]), "held"), {})
        self.assertEqual(hd._cfg_get(_Cur([('{"held": {"who": "d"}}',)]), "held"), {"held": {"who": "d"}})
        self.assertEqual(hd._cfg_get(_Cur([({"held": {}},)]), "held"), {"held": {}})
        cur = _Cur()
        hd._cfg_set(cur, "held", {"held": {"who": "2026-10-05"}})
        self.assertIn("INSERT INTO service_config", cur.sql[0])
        self.assertEqual(cur.params[0][:2], (hd.STATE_SERVICE, "held"))

    def test_gather_builds_the_five_facts_and_self(self):
        rows = [(date(2026, 9, 28), "We are partners. How can I unblock you?", "...")]
        cur = _Cur([
            ("Jordan — Sr. Manager SRE, builds Nova. Lots more text follows here about him.",),   # people
            (6, "Trust so absolute it bordered on reckless. Then more.",
             [{"date": "2026-04-01", "what_shifted": "early"}, {"date": "2026-05-16", "what_shifted": "logging everything to nova_ops. ok"}]),
            (date(2026, 10, 3), 5),                                   # presence
            rows,                                                     # ec.gather -> stated_cares
            ("I'm becoming someone who listens more than I speak. And more.",),   # self_model
        ])
        f = hd.gather(cur, TODAY)
        self.assertEqual(sorted(f), ["arc", "his_words", "presence", "self", "turning", "who"])
        self.assertEqual(f["presence"], "last spoke to me 2d ago; present on 5 of the last 30 days")
        self.assertTrue(f["turning"].startswith("2026-05-16: logging everything"))
        self.assertTrue(f["arc"].startswith("(arc v6) "))
        self.assertIn("partners", f["his_words"])
        self.assertTrue(any("relationship_arc" in q for q in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        import os
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_hold.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all hold assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        import os
        import subprocess
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_hold"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("[hold", r.stdout)


if __name__ == "__main__":
    unittest.main()
