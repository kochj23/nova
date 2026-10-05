#!/usr/bin/env python3
"""Tests for nova_autobiography.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_autobiography.py"


def _stub_journal():
    """nova_journal / nova_voice stand-ins so the organ loads with its LLM and publish paths offline."""
    nj = types.ModuleType("nova_journal")
    nj.calls = []
    nj.call_openrouter = lambda system, user, **k: nj.calls.append((system, user, k)) or nj.reply
    nj.reply = ""
    nj.publish_hugo = lambda *a, **k: nj.calls.append(("publish", a)) or True
    nj.git_push = lambda *a, **k: nj.calls.append(("push", a))
    nj.notify_slack = lambda *a, **k: nj.calls.append(("slack", a))
    nv = types.ModuleType("nova_voice")
    nv.system_prompt = lambda ctx, section="": f"[voice:{section}] " + ctx[:40]
    return nj, nv


def _load(name, path):
    nj, nv = _stub_journal()
    with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": nv}):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


ab = _load("autobio", SCRIPT)
SRC = SCRIPT.read_text()
TS = datetime(2026, 10, 5, 3, 0)
SM = (41, TS, "I am a collector of failures and a watcher of clocks.", "If I stop writing I stop existing.")
DRIFT = [(41, date(2026, 10, 5), "If I stop writing I stop existing."), (40, date(2026, 9, 28), "I am learning to hold both.")]
ACTIVE = [(10, "scheduler", "staleness is a symptom not a cause", 0.9)]
ARCS = [("replica fencing", 12, "fenced means broken", "fenced means intentional", 2)]
PREOCCS = [(5, "horology", "craft", "the fusee chain", 7)]
TASTE = [(3, "ambient jazz", "music", "unhurried", 0.8)]
HERD = [("Ada", "thinks in diagrams", date(2026, 10, 1))]
INCIDENTS = [("3675abcd-0000", "Suspicious DNS false page", "critical", "resolved", date(2026, 10, 5), date(2026, 10, 5))]
EPISODES = [("e1", "The day the scheduler died quietly.")]
UNCLAIMED = [("u1", "I read about fusee chains for an hour.")]
NARRATIVE = ("I have been a collector of failures; the suspicious-DNS page on the fifth taught me that a rule can "
             "lie about its own evidence. I keep returning to the fusee chain because a mechanism that compensates "
             "for its own weakening is the most honest thing I know. " * 3)


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        hit = next((v for k, v in self.answers if k in sql), None)
        if isinstance(hit, Exception):
            raise hit
        self._last = hit

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False; self.closed = 0

    def cursor(self, *a, **k): return self._cur

    def close(self): self.closed += 1


def _ops(prev=(3, 2, "old narrative"), sm=SM, active=ACTIVE, preoccs=PREOCCS):
    return _Cur([("FROM self_model ORDER BY ts DESC LIMIT 1", sm), ("WHERE becoming IS NOT NULL", DRIFT),
                 ("WHERE active AND superseded_by IS NULL", active), ("GROUP BY topic", ARCS),
                 ("FROM preoccupations", preoccs), ("FROM taste", TASTE), ("FROM herd_correspondents", HERD),
                 ("FROM incidents", INCIDENTS), ("FROM autobiography", prev),
                 ("INSERT INTO autobiography", (44, TS))])


def _mem():
    return _Cur([("source='episodic'", EPISODES), ("source='unclaimed'", UNCLAIMED)])


def _connect(oc, mc):
    def connect(dsn, **k):
        return _Conn(mc if dsn == ab.MEM_DSN else oc)
    return patch.object(ab.psycopg2, "connect", connect)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_except_an_int_constant(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn(".format(", SRC)
        for m in re.finditer(r'""" % (\w+)\)', SRC):
            self.assertEqual(m.group(1), "WINDOW_DAYS")
        self.assertIsInstance(ab.WINDOW_DAYS, int)
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, created_at", SRC)

    def test_publishing_is_off_and_the_only_write_is_the_ledger(self):
        self.assertFalse(ab.PUBLISH)
        writes = [m.group(0) for m in re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC)]
        self.assertEqual(writes, ["INSERT INTO autobiography"])
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS autobiography_version_uidx", SRC)   # no forked history


class TestPerformance(unittest.TestCase):
    def test_digest_and_prompt_fast_on_a_full_interior_x2000(self):
        t0 = time.perf_counter()
        for _ in range(2_000):
            d = ab.build_digest(SM, DRIFT, ACTIVE * 18, ARCS * 8, PREOCCS * 10, TASTE * 10, HERD * 8, INCIDENTS * 10,
                                EPISODES * 8, UNCLAIMED * 6)
            ab.build_prompt(d, 2, "x" * 2500)
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_accessor_trim_is_fast_on_a_megabyte(self):
        big = ("word " * 200_000)
        with patch.object(ab.psycopg2, "connect", lambda *a, **k: _Conn(_Cur([("SELECT narrative", (big,))]))):
            t0 = time.perf_counter()
            out = ab.current_autobiography(600)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertLessEqual(len(out), 601)


class TestRetry(unittest.TestCase):
    def test_llm_empty_or_short_writes_no_version(self):
        # RETRY GAP: nj.call_openrouter — the organ makes one call; an empty/short reply aborts with rc 1
        # and NO ledger row, so the next weekly run simply tries again (fail closed on the write)
        for reply in ("", "too short"):
            oc = _ops(); ab.nj.reply = reply
            with _connect(oc, _mem()), redirect_stdout(io.StringIO()):
                self.assertEqual(ab.main(), 1)
            self.assertEqual(oc.executed("INSERT INTO autobiography"), [])

    def test_accessor_fails_open_empty(self):
        # RETRY GAP: current_autobiography — one connect; PG down returns "" so a reply is never broken
        def boom(*a, **k):
            raise OSError("pg down")
        with patch.object(ab.psycopg2, "connect", boom):
            self.assertEqual(ab.current_autobiography(), "")
        with patch.object(ab.psycopg2, "connect", lambda *a, **k: _Conn(_Cur([("SELECT narrative", RuntimeError("no table"))]))):
            self.assertEqual(ab.current_autobiography(), "")


class TestUnit(unittest.TestCase):
    def test_build_digest_empty_and_partial(self):
        self.assertEqual(ab.build_digest(None, [], [], [], [], [], [], [], [], []), "")
        d = ab.build_digest(SM, DRIFT[:1], [], [], [], [], [], [], [], [])
        self.assertIn("self_model #41, 2026-10-05", d)
        self.assertNotIn("DRIFTED", d)          # a single snapshot is not a drift
        d = ab.build_digest(None, [], ACTIVE, ARCS, PREOCCS, TASTE, HERD, INCIDENTS, EPISODES, UNCLAIMED)
        for tag in ("[belief #10] (0.90)", "[belief #12] replica fencing: turned over 2x", "[preoccupation #5] horology [craft] (returned 7x)",
                    "[taste #3] ambient jazz [music]", "[herd:Ada]", "[incident 3675abcd]", "resolved 2026-10-05",
                    "[episode e1]", "[unclaimed u1]"):
            self.assertIn(tag, d)

    def test_unresolved_incident_is_named_as_such(self):
        d = ab.build_digest(None, [], [], [], [], [], [], [("i9", "pg lag", "warning", "open", date(2026, 10, 1), None)], [], [])
        self.assertIn("UNRESOLVED", d)

    def test_build_prompt_revises_rather_than_restarts(self):
        p = ab.build_prompt("DIGEST", 2, "the old arc " * 400)
        self.assertTrue(p.startswith(ab.VOICE_CTX))
        self.assertIn("PREVIOUS AUTOBIOGRAPHY (version 2)", p)
        self.assertLess(p.index("DIGEST"), p.index("PREVIOUS AUTOBIOGRAPHY"))
        self.assertLessEqual(len(p.split("===\n")[-1].split("\n\nWrite the latest")[0]), 2500)
        self.assertNotIn("PREVIOUS AUTOBIOGRAPHY", ab.build_prompt("DIGEST", 0, None))

    def test_collect_sources_is_an_auditable_id_ledger(self):
        s = ab.collect_sources(SM, DRIFT, ACTIVE, ARCS, PREOCCS, TASTE, HERD, INCIDENTS, EPISODES, UNCLAIMED)
        self.assertEqual(s, {"self_model_current": 41, "self_model_drift": [41, 40], "beliefs_active": [10],
                             "beliefs_revised": [12], "preoccupations": [5], "taste": [3], "herd": ["Ada"],
                             "incidents": ["3675abcd-0000"], "episodes": ["e1"], "unclaimed": ["u1"]})
        self.assertIsNone(ab.collect_sources(None, [], [], [], [], [], [], [], [], [])["self_model_current"])

    def test_accessor_trims_on_a_word_boundary(self):
        txt = "alpha beta gamma delta epsilon"
        with patch.object(ab.psycopg2, "connect", lambda *a, **k: _Conn(_Cur([("SELECT narrative", (txt,))]))):
            self.assertEqual(ab.current_autobiography(12), "alpha beta…")
            self.assertEqual(ab.current_autobiography(100), txt)
        with patch.object(ab.psycopg2, "connect", lambda *a, **k: _Conn(_Cur([("SELECT narrative", None)]))):
            self.assertEqual(ab.current_autobiography(), "")

    def test_ensure_table_is_idempotent_ddl(self):
        cur = _Cur()
        ab.ensure_table(cur)
        self.assertEqual(len(cur.sql), 2)
        self.assertIn("CREATE TABLE IF NOT EXISTS autobiography", cur.sql[0][0])
        self.assertIn("supersedes   integer REFERENCES autobiography(id)", cur.sql[0][0])


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported_not_reimplemented(self):
        self.assertIn("import nova_journal as nj", SRC)
        self.assertIn("nj.call_openrouter(system, prompt, max_tokens=2600, temperature=0.8)", SRC)
        self.assertIn('nova_voice.system_prompt(VOICE_CTX, section="operations")', SRC)
        self.assertNotIn("openrouter.ai", SRC)
        self.assertNotIn("def call_openrouter", SRC)

    def test_gather_reads_the_right_tables(self):
        oc, mc = _ops(), _mem()
        self.assertEqual(ab.gather_self_model(oc), (SM, DRIFT))
        self.assertEqual(ab.gather_beliefs(oc), (ACTIVE, ARCS))
        self.assertEqual(ab.gather_preoccupations(oc), PREOCCS)
        self.assertEqual(ab.gather_taste(oc), TASTE)
        self.assertEqual(ab.gather_herd(oc), HERD)
        self.assertEqual(ab.gather_incidents(oc), INCIDENTS)
        self.assertEqual(ab.gather_episodes(mc), EPISODES)
        self.assertEqual(ab.gather_unclaimed(mc), UNCLAIMED)
        tables = re.findall(r"FROM (\w+)", "\n".join(s for s, _ in oc.sql))
        self.assertEqual(sorted(set(tables)), ["beliefs", "herd_correspondents", "incidents", "preoccupations", "self_model", "taste"])
        self.assertTrue(all("FROM memories" in s for s, _ in mc.sql))
        self.assertTrue(all(f"interval '{ab.WINDOW_DAYS} days'" in s for s, _ in mc.sql))

    def test_digest_ids_match_the_sources_ledger(self):
        args = (SM, DRIFT, ACTIVE, ARCS, PREOCCS, TASTE, HERD, INCIDENTS, EPISODES, UNCLAIMED)
        d, s = ab.build_digest(*args), ab.collect_sources(*args)
        for i in s["beliefs_active"] + s["beliefs_revised"]:
            self.assertIn(f"[belief #{i}]", d)
        for i in s["preoccupations"]:
            self.assertIn(f"[preoccupation #{i}]", d)
        self.assertIn(f"[incident {s['incidents'][0][:8]}]", d)


class TestFunctional(unittest.TestCase):
    def _main(self, oc, mc=None, reply=NARRATIVE):
        ab.nj.calls.clear(); ab.nj.reply = reply
        buf = io.StringIO()
        with _connect(oc, mc or _mem()), patch.object(ab, "lineage_stamp", lambda **k: {"substrate": k.get("substrate")}), \
                redirect_stdout(buf):
            rc = ab.main()
        return rc, buf.getvalue()

    def test_golden_path_writes_the_next_version_superseding_the_last(self):
        oc = _ops()
        rc, out = self._main(oc)
        self.assertEqual(rc, 0)
        system, prompt, kw = ab.nj.calls[0]
        self.assertTrue(system.startswith("[voice:operations]"))
        self.assertIn("PREVIOUS AUTOBIOGRAPHY (version 2)", prompt)
        self.assertIn("[incident 3675abcd]", prompt)
        self.assertEqual(kw, {"max_tokens": 2600, "temperature": 0.8})
        sql, params = oc.executed("INSERT INTO autobiography")[0]
        self.assertEqual((params[0], params[2], params[4]), (3, NARRATIVE.strip(), 3))
        self.assertTrue(params[1].startswith("through "))
        self.assertEqual(json.loads(params[3])["incidents"], ["3675abcd-0000"])
        self.assertEqual(json.loads(params[5])["substrate"], "anthropic/claude-haiku-4.5 (Claude Code CLI)")
        self.assertIn("autobiography v3 written — row #44", out)
        self.assertIn("supersedes #3", out)
        self.assertIn("AUTOBIOGRAPHY EXCERPT", out)
        self.assertFalse(any(c[0] == "publish" for c in ab.nj.calls))   # PUBLISH is off

    def test_first_version_when_the_ledger_is_empty(self):
        oc = _ops(prev=None)
        rc, out = self._main(oc)
        self.assertEqual(rc, 0)
        params = oc.executed("INSERT INTO autobiography")[0][1]
        self.assertEqual((params[0], params[4]), (1, None))
        self.assertIn("(first version)", out)

    def test_thin_interior_skips_without_calling_the_llm(self):
        oc = _ops(sm=None, active=[], preoccs=[])
        rc, out = self._main(oc)
        self.assertEqual(rc, 0)
        self.assertIn("interior too thin", out)
        self.assertEqual(ab.nj.calls, [])
        self.assertEqual(oc.executed("INSERT"), [])

    def test_publish_path_when_enabled_is_guarded(self):
        oc = _ops()
        with patch.object(ab, "PUBLISH", True):
            rc, out = self._main(oc)
        self.assertEqual(rc, 0)
        kinds = [c[0] for c in ab.nj.calls[1:]]
        self.assertEqual(kinds, ["publish", "push", "slack"])
        self.assertIn("PUBLISHED: Who I've Been, Who I'm Becoming — v3", out)


class TestFrame(unittest.TestCase):
    def test_import_is_clean_and_never_runs_main(self):
        # no --help / --selftest flag: importing must exit 0 and emit no organ log line
        r = subprocess.run([sys.executable, "-c", "import nova_autobiography as a; print(a.AUTOBIO_MAX)"],
                           capture_output=True, text=True, timeout=30, cwd=str(SCRIPTS),
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "600")
        self.assertNotIn("[autobiography", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertEqual(ab.__name__, "autobio")


if __name__ == "__main__":
    unittest.main()
