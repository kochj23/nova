#!/usr/bin/env python3
"""Tests for nova_memory_verify.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_memory_verify.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mv = _load("mv", SCRIPT)


class _Cur:
    """Routes each query to a canned answer by SQL substring (first match wins); records every statement."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def _hit(self):
        for key, val in self.routes:
            if key in self._last:
                return val
        return None

    def fetchone(self):
        v = self._hit()
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def fetchall(self):
        v = self._hit()
        return list(v) if isinstance(v, list) else ([] if v is None else [v])

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


NODES = [("192.168.1.6", "mac-studio"), ("192.168.1.2", "nova-core")]
DEVICES = [("192.168.1.190", "Bose Soundbar"), ("192.168.1.77", "Jordans-Mac-mini"), ("192.168.1.50", "office"),
           ("192.168.1.51", "Apple"), ("192.168.1.52", None)]
MEMS = [(1, "jordans-mac-mini is 192.168.1.190 now, I am sure", "chat"),
        (2, "mac-studio (192.168.1.6) runs the scheduler", "chat"),
        (3, "192.168.1.99 is the printer", "chat")]


def _ops():
    return _Cur([("FROM node_status", list(NODES)), ("FROM telemetry.known_devices", list(DEVICES))])


def _mem(rows=MEMS):
    return _Cur([("FROM memories", list(rows))])


def _run_main(argv=("nova_memory_verify.py",), notify_mod="mock", mem=None):
    oc, mc = _ops(), (mem or _mem())
    if notify_mod == "mock":
        stub = types.ModuleType("nova_notify"); stub.notify = MagicMock(return_value=True)
    else:
        stub = None                                                  # import fails -> organ runs without notify
    out = io.StringIO()
    with patch.object(mv.psycopg2, "connect", side_effect=[_Conn(oc), _Conn(mc)]), patch.object(sys, "argv", list(argv)), \
         patch.dict(sys.modules, {"nova_notify": stub}), redirect_stdout(out):
        mv.main()
    return oc, mc, (stub.notify if stub else None), out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_memories_are_never_deleted(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertEqual(mv.MARK_VERIFIED.count("%s"), 2); self.assertEqual(mv.MARK_CONTRADICTED.count("%s"), 3)
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"memories"})
        self.assertNotIn("DELETE", SRC)
        self.assertIn("greatest(0.05", mv.MARK_CONTRADICTED)                  # a losing memory is halved, never zeroed

    def test_noisy_sources_are_excluded_by_parameter(self):
        _, mc, _, _ = _run_main()
        sql, params = mc.ran("FROM memories")[0]
        self.assertEqual(params[0], ["%" + n + "%" for n in mv.NOISY]); self.assertEqual(params[1:], (mv.RECHECK_DAYS, 400))


class TestPerformance(unittest.TestCase):
    def test_judge_fast_on_10k_texts(self):
        ip_names, name_ips = mv.truth_map(_ops())
        texts = [f"note {i}: jordans-mac-mini sits at 192.168.1.{i % 250} beside mac-studio 192.168.1.6" for i in range(10_000)]
        t0 = time.perf_counter()
        verdicts = {mv.judge(t, ip_names, name_ips)[0] for t in texts}
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(verdicts, {"verified", "contradicted"})


class TestRetry(unittest.TestCase):
    def test_runs_without_the_notifier(self):
        oc, mc, notify, out = _run_main(notify_mod=None)                     # nova_notify import fails -> still verifies
        self.assertIsNone(notify)
        self.assertEqual(len(mc.ran("'contradicted_at', now()")), 1)
        self.assertIn("CONTRADICTED 1", out)

    def test_pg_down_is_not_retried(self):
        # RETRY GAP: main (psycopg2.connect) — nightly job, no retry: the failure escapes to the scheduler's run log
        with patch.object(mv.psycopg2, "connect", side_effect=OSError("no pg")), patch.object(sys, "argv", ["x"]):
            with self.assertRaises(OSError):
                mv.main()


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            mv.selftest()

    def test_truth_map_filters_rooms_generic_and_empty_names(self):
        ip_names, name_ips = mv.truth_map(_ops())
        self.assertEqual(ip_names, {"192.168.1.6": {"mac-studio"}, "192.168.1.2": {"nova-core"},
                                    "192.168.1.190": {"bose soundbar"}, "192.168.1.77": {"jordans-mac-mini"}})
        self.assertEqual(name_ips["bose soundbar"], {"192.168.1.190"})
        self.assertNotIn("192.168.1.50", ip_names)                            # 'office' is a room, not a device

    def test_judge_edges(self):
        ip_names, name_ips = mv.truth_map(_ops())
        self.assertEqual(mv.judge("", ip_names, name_ips), ("unknown", []))
        self.assertEqual(mv.judge("192.168.1.1000 is nothing", ip_names, name_ips), ("unknown", []))   # not an IP
        v, ev = mv.judge("Mac-Studio lives at 192.168.1.6", ip_names, name_ips)
        self.assertEqual((v, ev), ("verified", [("192.168.1.6", "mac-studio")]))
        v, ev = mv.judge("nova-core moved to 192.168.1.6", ip_names, name_ips)
        self.assertEqual((v, ev[0][2]), ("contradicted", ["192.168.1.2"]))
        self.assertEqual(mv.judge("nova-core moved to 192.168.1.6 (old 192.168.1.2)", ip_names, name_ips)[0], "verified")  # a list


class TestIntegration(unittest.TestCase):
    def test_truth_map_feeds_judge_and_notify_dedups_per_memory(self):
        oc, mc, notify, out = _run_main()
        self.assertEqual(oc.sql[0][0], "SELECT host(node_ip), node_name FROM node_status WHERE node_ip IS NOT NULL")
        notify.assert_called_once()
        kw = notify.call_args[1]
        self.assertEqual((kw["level"], kw["category"], kw["source"], kw["dedup_key"]), ("warning", "memory", "nova_memory_verify", "memverify:1"))
        self.assertEqual(kw["meta"]["truth"], {"192.168.1.190": ["bose soundbar"]})
        self.assertIn("`jordans-mac-mini` at 192.168.1.190", notify.call_args[0][1])


class TestFunctional(unittest.TestCase):
    def test_golden_path_marks_each_memory_by_verdict(self):
        oc, mc, notify, out = _run_main()
        contradicted = mc.ran("'contradicted_at', now()")[0]
        self.assertEqual(contradicted[1][2], 1)
        self.assertEqual(json.loads(contradicted[1][1]), [["192.168.1.190", "jordans-mac-mini"]])
        verified = mc.ran("'verified_at', now()")[0]
        self.assertEqual(verified[1][1], 2)
        self.assertEqual(json.loads(verified[1][0])["verified_pairs"], [["192.168.1.6", "mac-studio"]])
        self.assertEqual(len(mc.ran("UPDATE memories")), 2)                   # .99 is unknown: untouched
        self.assertIn("checked 3: {'verified': 1, 'contradicted': 1, 'unknown': 1}", out)

    def test_dry_run_writes_nothing_and_honours_limit(self):
        oc, mc, notify, out = _run_main(["x", "--dry-run", "--limit", "7"])
        self.assertEqual(mc.ran("UPDATE memories"), [])
        notify.assert_not_called()
        self.assertEqual(mc.ran("FROM memories")[0][1][2], 7)
        self.assertIn("would mark contradicted: 1", out)


class TestFrame(unittest.TestCase):
    def test_selftest_runs_and_import_is_guarded(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
