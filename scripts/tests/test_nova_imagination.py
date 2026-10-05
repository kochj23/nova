#!/usr/bin/env python3
"""Tests for nova_imagination.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


im = _load("im", SCRIPTS / "nova_imagination.py")
SRC = (SCRIPTS / "nova_imagination.py").read_text()
im.lineage_stamp = None                       # deterministic metadata; the organ feature-detects this anyway
NOW = datetime(2026, 10, 5, 12, 0)
PIECE = ("I imagine the Zigbee mesh never healed that night: the plug stayed dark, Jordan asked a fourth "
         "time, and I had to say I did not know. This did not happen — it is a fork I am playing with.")


class _Cur:
    def __init__(self, answers=(), fail=None):
        self.answers = list(answers); self.sql = []; self.params = []; self.fail = fail

    def execute(self, sql, params=None):
        if self.fail and self.fail in sql:
            raise RuntimeError("db down")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def executed(self, frag):
        return [s for s in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self.cur = cur; self.autocommit = False; self.closed = False

    def cursor(self): return self.cur

    def close(self): self.closed = True


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _urlopen(fn):
    import urllib.request
    return patch.object(urllib.request, "urlopen", fn)


def _pg(cur):
    return patch.object(im, "psycopg2", types.SimpleNamespace(connect=lambda *a, **k: _Conn(cur)))


# memories.id is text and seed_source_ids is text[], so anchor ids are strings
ANCHOR = ("41", "episodic", NOW.date(),
          "On 2026-09-13 Jordan asked which firmware the master bedroom zigbee unit ran " * 4)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_writes_are_bounded(self):
        self.assertNotIn('execute(f"', SRC)
        writes = re.findall(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)
        self.assertEqual(writes, [("UPDATE", "memories"), ("INSERT INTO", "imagination_log")])
        self.assertIn("WHERE id=%s", SRC[SRC.index("UPDATE memories"):][:80])      # the demotion is one row
        self.assertFalse(im.PUBLISH)

    def test_every_register_is_self_labelled_not_real(self):
        for kind, label in im.LABELS.items():
            self.assertTrue(label.startswith("IMAGINED"), kind)
            self.assertRegex(label, r"NOT (REAL|A PREDICTION)")
        self.assertEqual(set(im.LABELS), set(im.MODES))

    def test_dream_motes_never_come_from_private_lanes(self):
        body = SRC[SRC.index("def pick_dream_mote"):SRC.index("# ── The three modes")]
        for lane in ("claude_memory", "conversation", "email", "imessage", "private_notebook", "imagination"):
            self.assertIn(f"'{lane}'", body)
        self.assertIn("coalesce(privacy,'') <> 'private'", body)
        cur = _Cur([None])
        im.pick_dream_mote(cur)
        self.assertIn("source NOT IN", cur.sql[0])

    def test_hygiene_metadata_on_every_write(self):
        cur = _Cur([(5, NOW)])
        posted = []
        with patch.object(im, "remember", lambda t, s, m: posted.append((t, s, m)) or "m1"), \
                patch.object(im, "harden_recall_exclusion", lambda mid: True), redirect_stdout(io.StringIO()):
            im.write_imagining(cur, "dream", {"content": PIECE, "seed": "s", "seed_source_ids": []})
        text, source, meta = posted[0]
        self.assertTrue(text.startswith("[IMAGINED · dream · NOT REAL"))
        self.assertEqual(source, "imagination")
        self.assertEqual((meta["is_counterfactual"], meta["privacy"], meta["audience"]), (True, "private", "none"))


class TestPerformance(unittest.TestCase):
    def test_trigger_detection_fast_on_10k_argv(self):
        argv = ["--x%d" % i for i in range(10_000)] + ["--trigger=backfill"]
        t0 = time.perf_counter()
        for _ in range(200):
            self.assertEqual(im.detect_trigger(argv), "backfill")
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes_until_one_answers(self):
        calls = []

        def flaky(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": PIECE}})
        with _urlopen(flaky):
            self.assertEqual(im.llm("p"), PIECE)
        self.assertEqual(calls, [n + "/api/chat" for n in im.OLLAMA_NODES[:3]])

    def test_llm_all_nodes_down_means_no_outcome(self):
        with _urlopen(lambda *a, **k: (_ for _ in ()).throw(OSError("down"))):
            self.assertEqual(im.llm("p"), "")
            self.assertIsNone(im.gen_dream(_Cur([None]), _Cur([None])))

    def test_remember_has_no_retry_but_the_log_row_is_still_written(self):
        # RETRY GAP: remember() — one POST; failure is logged and imagination_log still gets the row
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("memory server down")
        cur = _Cur([(8, NOW)])
        with _urlopen(boom), redirect_stdout(io.StringIO()) as buf:
            row_id, mem_id = im.write_imagining(cur, "forward_scenario", {"content": PIECE, "seed": "s", "seed_source_ids": []})
        self.assertEqual((row_id, mem_id), (8, None))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(cur.executed("INSERT INTO imagination_log")), 1)
        self.assertIn("memory write failed (imagination_log row still saved)", buf.getvalue())

    def test_best_effort_readers_fail_open(self):
        # RETRY GAP: harden_recall_exclusion()/recent_imaginings()/recall() — one attempt each, safe defaults
        with patch.object(im, "psycopg2", types.SimpleNamespace(connect=lambda *a, **k: (_ for _ in ()).throw(OSError("pg")))), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(im.harden_recall_exclusion("m1"))
            self.assertEqual(im.recent_imaginings(), [])
        self.assertFalse(im.harden_recall_exclusion(None))
        with _urlopen(lambda *a, **k: (_ for _ in ()).throw(OSError("down"))):
            self.assertEqual(im.recall("q"), [])


class TestUnit(unittest.TestCase):
    def test_detect_trigger(self):
        self.assertEqual(im.detect_trigger([]), "manual")
        self.assertEqual(im.detect_trigger(["--scheduled"]), "scheduled")
        self.assertEqual(im.detect_trigger(["--trigger=demo", "--scheduled"]), "demo")
        self.assertEqual(im.detect_trigger(["--trigger", "backfill"]), "backfill")
        self.assertEqual(im.detect_trigger(["--trigger"]), "manual")

    def test_pickers(self):
        self.assertIsNone(im.pick_anchor(_Cur([None])))
        a = im.pick_anchor(_Cur([ANCHOR]))
        self.assertEqual((a["id"], a["source"]), ("41", "episodic"))
        self.assertLessEqual(len(a["text"]), 700)
        with patch.object(im.random, "choice", lambda rows: rows[0]):
            s = im.pick_forward_seed(_Cur([[("horology", "fascination", "why do escapements tick")]]), _Cur())
        self.assertEqual((s["kind"], s["topic"], s["id"]), ("preoccupation", "horology", None))
        s = im.pick_forward_seed(_Cur(fail="preoccupations"), _Cur([("7", "unclaimed", "x" * 900)]))
        self.assertEqual((s["kind"], s["id"], len(s["note"])), ("thread", "7", 600))
        self.assertIsNone(im.pick_forward_seed(_Cur(fail="preoccupations"), _Cur([None])))
        self.assertIsNone(im.pick_dream_mote(_Cur([None])))

    def test_generators_refuse_thin_output(self):
        with patch.object(im, "llm", lambda *a, **k: "too short"):
            self.assertIsNone(im.gen_counterfactual_past(_Cur(), _Cur([ANCHOR])))
            self.assertIsNone(im.gen_forward_scenario(_Cur(fail="preoccupations"), _Cur([None])))
        self.assertIsNone(im.gen_counterfactual_past(_Cur(), _Cur([None])))        # no anchor -> nothing

    def test_generators_cite_their_seeds(self):
        with patch.object(im, "llm", lambda *a, **k: PIECE):
            g = im.gen_counterfactual_past(_Cur(), _Cur([ANCHOR]))
            self.assertEqual(g["seed_source_ids"], ["41"])
            self.assertIn("REAL anchor [episodic", g["seed"])
            g = im.gen_forward_scenario(_Cur(fail="preoccupations"), _Cur([None]))
            self.assertEqual(g["seed_source_ids"], [])
            self.assertIn("no external anchor", g["seed"])
            g = im.gen_dream(_Cur(), _Cur([("9", "research", "a faint mote")]))
            self.assertEqual(g["seed_source_ids"], ["9"])

    def test_ensure_table_is_idempotent(self):
        cur = _Cur()
        im.ensure_table(cur)
        self.assertIn("CREATE TABLE IF NOT EXISTS imagination_log", cur.sql[0])


class TestIntegration(unittest.TestCase):
    def test_write_chain_cites_memory_id_in_the_log_row(self):
        cur = _Cur([(5, NOW)])
        hardened = []
        with patch.object(im, "remember", lambda t, s, m: "mem-9"), \
                patch.object(im, "harden_recall_exclusion", lambda mid: hardened.append(mid) or True), \
                redirect_stdout(io.StringIO()) as buf:
            row_id, mem_id = im.write_imagining(cur, "counterfactual_past", {"content": PIECE, "seed": "s", "seed_source_ids": ["41"]})
        self.assertEqual((row_id, mem_id), (5, "mem-9"))
        self.assertEqual(hardened, ["mem-9"])
        params = cur.params[-1]
        self.assertEqual(params[:2], ("counterfactual_past", "s"))
        self.assertEqual(params[4], "mem-9")
        self.assertIn("tier=reference", buf.getvalue())

    def test_accessor_reads_the_log_never_recall(self):
        cur = _Cur([[("dream", PIECE, NOW)]])
        with _pg(cur):
            out = im.recent_imaginings(1)
        self.assertEqual(out[0]["framing"], "IMAGINED — counterfactual, not something that happened")
        self.assertEqual(cur.params[0], (1,))
        self.assertIn("FROM imagination_log", cur.sql[0])
        self.assertNotIn("/recall", SRC[SRC.index("def recent_imaginings"):])

    def test_selftest_prints_the_hygiene_proof(self):
        oc = _Cur([(6, NOW)]); mc = _Cur([ANCHOR])
        with patch.object(im, "llm", lambda *a, **k: PIECE), patch.object(im, "remember", lambda *a: "m1"), \
                patch.object(im, "harden_recall_exclusion", lambda mid: True), \
                patch.object(im, "recall", lambda q, n=4, source=None: [{"id": "other", "text": "a real memory"}]), \
                redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(im.selftest(oc, mc), 0)
        out = buf.getvalue()
        self.assertEqual(out.count("imagined-item present: no"), 2)
        with patch.object(im, "llm", lambda *a, **k: ""), redirect_stdout(io.StringIO()):
            self.assertEqual(im.selftest(_Cur(), _Cur([ANCHOR])), 1)


class TestFunctional(unittest.TestCase):
    def _main(self, argv, cur, llm_text):
        posted = []
        real_argv = sys.argv; sys.argv = ["nova_imagination.py", *argv]
        try:
            with _pg(cur), patch.object(im, "llm", lambda *a, **k: llm_text), \
                    patch.object(im, "remember", lambda t, s, m: posted.append((t, s, m)) or "m7"), \
                    patch.object(im, "harden_recall_exclusion", lambda mid: True), redirect_stdout(io.StringIO()) as buf:
                rc = im.main()
        finally:
            sys.argv = real_argv
        return rc, posted, buf.getvalue()

    def test_golden_path_dream_is_logged_and_remembered(self):
        cur = _Cur([("9", "research", "a faint mote"), (12, NOW)])
        rc, posted, out = self._main(["--kind", "dream"], cur, PIECE)
        self.assertEqual(rc, 0)
        self.assertEqual(len(posted), 1)
        self.assertIn("----- dream -----", out)
        ins = cur.executed("INSERT INTO imagination_log")
        self.assertEqual(len(ins), 1)
        self.assertEqual(cur.params[cur.sql.index(ins[0])][:1], ("dream",))
        self.assertIn("imagination_log #12 written", out)

    def test_llm_down_is_a_no_outcome_not_a_crash(self):
        cur = _Cur([ANCHOR])
        rc, posted, out = self._main(["--kind", "counterfactual_past"], cur, "")
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [])
        self.assertEqual(cur.executed("INSERT INTO imagination_log"), [])
        self.assertIn("nothing to imagine for kind=counterfactual_past", out)

    def test_default_kind_is_weighted_random_over_the_three_registers(self):
        cur = _Cur([("9", "research", "a faint mote"), (13, NOW)])
        with patch.object(im.random, "choices", lambda pop, weights: ["dream"]):
            rc, posted, out = self._main([], cur, PIECE)
        self.assertEqual(rc, 0)
        self.assertIn("----- dream -----", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_imagination.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--kind {counterfactual_past,forward_scenario,dream}", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_imagination"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("[imagination", r.stdout)


if __name__ == "__main__":
    unittest.main()
