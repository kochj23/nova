#!/usr/bin/env python3
"""Tests for nova_autonomy_memory.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_autonomy_memory.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


am = _load("am_under_test", SCRIPT)
TS = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
GRANTED = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
LEDGER_COLS = ["id", "ts", "source", "autonomy_level", "action_class", "target",
               "executed", "verified", "result", "vetoed", "veto_note"]
TRUST_COLS = ["action_class", "correct_count", "wrong_count", "granted", "granted_at"]


class _Cur:
    """Cursor stub (context-manager capable, sets .description): first matching SQL substring wins.
    A rule value of {"cols": [...], "rows": [...]} sets description + fetchall; a tuple answers fetchone."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last, self.description = [], [], None, None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                if isinstance(val, dict) and "cols" in val:
                    self.description = [(c,) for c in val["cols"]]
                    self._last = list(val["rows"])
                else:
                    self._last = val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, cur): self.cur, self.commits, self.closed = cur, 0, False
    def cursor(self, *a, **k): return self.cur
    def commit(self): self.commits += 1
    def close(self): self.closed = True


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


LEDGER = [(6, TS, "actor", "rung1-selfheal", "restart:nova-soil-monitor", "nova-soil-monitor@nova-core", True, True, "ok", False, None),
          (7, TS, "coagency", "rung2-supervised", "observe:status-note", None, True, False, "", False, None),
          (8, TS, "earned", "rung3-earned", "restart:nova-zigbee-lqi", "nova-zigbee-lqi@nova-core", True, True, "", True, "not now")]
TRUST = [("restart:nova-soil-monitor", 5, 0, True, GRANTED)]


def _rules(wm=None, ledger=LEDGER, trust=TRUST):
    return [("SELECT value FROM service_config", (wm,) if wm is not None else None),
            ("FROM autonomy_ledger", {"cols": LEDGER_COLS, "rows": ledger}),
            ("FROM autonomy_trust", {"cols": TRUST_COLS, "rows": trust})]


def _run(cur, fn=lambda: am.run(), urlopen=None):
    conn = _Conn(cur)
    uo = urlopen or mock.MagicMock(return_value=_Resp({"id": 99, "status": "stored"}))
    buf = io.StringIO()
    with mock.patch.object(am.psycopg2, "connect", return_value=conn), \
         mock.patch.object(am.urllib.request, "urlopen", uo), \
         mock.patch.object(am, "_stamp", lambda: {"host": "test"}), redirect_stdout(buf):
        fn()
    return conn, uo, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_writes_only_its_watermark(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        writes = {m.group(1) for m in re.finditer(r"(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"service_config"})
        self.assertIn("VALUES (%s, %s, %s::jsonb, now(), %s)", SRC)

    def test_reflections_are_templated_not_prompted(self):
        # factual events about the self: deterministic text, no LLM, no shell
        self.assertNotIn("ollama", SRC.lower())
        self.assertNotIn("subprocess", SRC)


class TestPerformance(unittest.TestCase):
    def test_reflection_fast_on_10k_rows(self):
        rows = [dict(zip(LEDGER_COLS, LEDGER[i % 3])) for i in range(10_000)]
        t0 = time.perf_counter()
        out = [am.ledger_reflection(r) for r in rows]
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertTrue(all(o is not None for o in out))


class TestRetry(unittest.TestCase):
    def test_remember_has_no_retry_but_run_fails_open_per_row(self):
        # RETRY GAP: remember — one urlopen attempt; run() logs the error per row and still advances
        uo = mock.MagicMock(side_effect=OSError("memory server down"))
        with mock.patch.object(am.urllib.request, "urlopen", uo):
            with self.assertRaises(OSError):
                am.remember("t", "agency", {})
        cur = _Cur(_rules())
        conn, uo, out = _run(cur, urlopen=mock.MagicMock(side_effect=OSError("down")))
        self.assertEqual(uo.call_count, 4)
        self.assertEqual(out.count("ERROR emitting"), 4)
        self.assertEqual(len(cur.stmts("INSERT INTO service_config")), 1)


class TestUnit(unittest.TestCase):
    def test_friendly_and_verb(self):
        self.assertEqual(am._friendly("nova-soil-monitor@nova-core", "restart:x"), "nova-soil-monitor")
        self.assertEqual(am._friendly(None, "restart:nova-x"), "nova-x")
        self.assertEqual(am._friendly(None, "weird"), "weird")
        self.assertEqual(am._friendly(None, None), "something")
        self.assertEqual(am._verb("restart:x"), "restarted")
        self.assertEqual(am._verb("heal:x"), "healed")
        self.assertEqual(am._verb("observe:x"), "acted on")

    def test_ledger_reflection_kinds_and_insignificance(self):
        r = dict(zip(LEDGER_COLS, LEDGER[0]))
        self.assertEqual(am.ledger_reflection(r), ("I restarted nova-soil-monitor on my own today, and it held.", "verified_selfheal"))
        self.assertEqual(am.ledger_reflection(dict(zip(LEDGER_COLS, LEDGER[1])))[1], "earned_execution")
        text, kind = am.ledger_reflection(dict(zip(LEDGER_COLS, LEDGER[2])))
        self.assertEqual(kind, "veto"); self.assertIn("(not now)", text)
        quiet = {**r, "executed": False, "verified": False, "vetoed": False}
        self.assertIsNone(am.ledger_reflection(quiet))

    def test_trust_reflection_and_watermark_edges(self):
        text, kind = am.trust_reflection({"action_class": "restart:x", "correct_count": 5, "wrong_count": 0})
        self.assertEqual(kind, "trust_granted"); self.assertIn("5 of 5", text)
        self.assertEqual(am.load_watermark(_Conn(_Cur())), (0, None))
        self.assertEqual(am.load_watermark(_Conn(_Cur([("service_config", ({"ledger_id": "12"},))]))), (12, None))


class TestIntegration(unittest.TestCase):
    def test_watermark_uses_the_organ_state_key(self):
        cur = _Cur()
        am.save_watermark(_Conn(cur), 8, GRANTED.isoformat())
        (_, p), = cur.stmts("INSERT INTO service_config")
        self.assertEqual((p[0], p[1], p[3]), (am.STATE_SERVICE, am.STATE_KEY, am.STATE_SERVICE))
        self.assertEqual(json.loads(p[2])["ledger_id"], 8)
        am.load_watermark(_Conn(cur))
        self.assertEqual(cur.params[-1], (am.STATE_SERVICE, am.STATE_KEY))

    def test_scan_then_reflect_produces_memory_shape(self):
        rows = am.scan_ledger(_Conn(_Cur(_rules())), 5)
        self.assertEqual([r["id"] for r in rows], [6, 7, 8])
        kinds = [am.ledger_reflection(r)[1] for r in rows]
        self.assertEqual(kinds, ["verified_selfheal", "earned_execution", "veto"])
        trust = am.scan_trust(_Conn(_Cur(_rules())), None)
        self.assertEqual(trust[0]["granted_at"], GRANTED)


class TestFunctional(unittest.TestCase):
    def test_golden_path_emits_and_advances_high_water(self):
        cur = _Cur(_rules(wm={"ledger_id": 5, "trust_granted_at": None}))
        conn, uo, out = _run(cur)
        self.assertEqual(uo.call_count, 4)
        bodies = [json.loads(c[0][0].data) for c in uo.call_args_list]
        self.assertTrue(all(b["source"] == "agency" for b in bodies))
        self.assertEqual(bodies[0]["metadata"]["ledger_id"], 6)
        self.assertEqual(bodies[3]["metadata"]["kind"], "trust_granted")
        self.assertEqual(cur.params[1], (5,))                       # scanned from the watermark
        (_, p), = cur.stmts("INSERT INTO service_config")
        self.assertEqual(json.loads(p[2]), {"ledger_id": 8, "trust_granted_at": GRANTED.isoformat()})
        self.assertEqual(conn.commits, 1)
        self.assertTrue(conn.closed)
        self.assertIn("4 reflection(s)", out)

    def test_dry_run_posts_nothing_and_keeps_watermark(self):
        cur = _Cur(_rules())
        conn, uo, out = _run(cur, fn=lambda: am.run(dry_run=True))
        uo.assert_not_called()
        self.assertFalse(cur.stmts("INSERT INTO service_config"))
        self.assertEqual(out.count("WOULD emit"), 4)

    def test_main_test_mode_posts_one_tagged_probe(self):
        cur = _Cur()
        with mock.patch.object(sys, "argv", ["nova_autonomy_memory.py", "--test"]):
            conn, uo, out = _run(cur, fn=am.main)
        self.assertEqual(uo.call_count, 1)
        body = json.loads(uo.call_args[0][0].data)
        self.assertIn("[TEST nova_autonomy_memory", body["text"])
        self.assertTrue(body["metadata"]["test"])
        self.assertEqual(cur.sql, [])

    def test_ledger_read_failure_closes_connection(self):
        cur = _Cur(_rules(), raise_on=("FROM autonomy_ledger",))
        conn = _Conn(cur)
        with mock.patch.object(am.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                am.run()
        self.assertTrue(conn.closed)
        self.assertFalse(cur.stmts("INSERT INTO service_config"))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+sys\.exit\(main\(\)\)')
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("am_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
