#!/usr/bin/env python3
"""Tests for nova_dashboard_look.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Grafana renders, the vision model, the memory server, PG and nova_notify are all mocked."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_dashboard_look.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("dashboard_look_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dl = _load()
# module-level stubs: no service registry lookup, no HTTP, no PG
dl.resolve_url = lambda service, path="": "http://grafana.test:3000" + path
# replace the module's own urllib binding (never the shared urllib.request module)
dl.urllib = types.SimpleNamespace(parse=dl.urllib.parse, request=types.SimpleNamespace(
    Request=dl.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))))
dl._pg = MagicMock(side_effect=OSError("offline: pg stubbed"))
dl.log = MagicMock()
PNG = b"\x89PNG\r\n\x1a\nfake"


class _Cur:
    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {}); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if sql.startswith("SELECT"):
            v = self.cfg.get(params[1])
            self._last = None if v is None else (json.dumps(v),)
        elif sql.lstrip().startswith("INSERT"):
            self.cfg[params[1]] = json.loads(params[2])

    def fetchone(self):
        return self._last


def _cm(body):
    r = MagicMock(); r.__enter__.return_value.read.return_value = body; return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", dl.OPS_DSN)

    def test_sql_is_parameterized_and_scoped_to_service(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        cur = _Cur()
        dl.cfg_set(cur, "last:x'; DROP", {"a": 1})
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], "dashboard_look")

    def test_render_url_encodes_query(self):
        u = dl.render_url("fleet-health")
        self.assertIn("/render/d/fleet-health?", u)
        self.assertNotIn(" ", u)


class TestPerformance(unittest.TestCase):
    def test_parse_look_10k(self):
        raw = json.dumps({"status": "watch", "summary": "s" * 1000, "findings": ["f"] * 50,
                          "numbers": {f"p{i}": i for i in range(50)}})
        t0 = time.perf_counter()
        for _ in range(10_000):
            d = dl.parse_look(raw)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual((len(d["findings"]), len(d["numbers"]), len(d["summary"])), (8, 12, 300))


class TestRetry(unittest.TestCase):
    def test_render_failure_skips_dashboard_and_continues(self):
        # RETRY GAP: render()/look_at — one attempt each; a failure skips that dashboard (fail open)
        calls = []
        def fake_render(uid):
            calls.append(uid)
            if uid == "a":
                raise OSError("renderer down")
            return PNG
        with patch.object(dl, "render", fake_render), \
             patch.object(dl, "look_at", return_value={"status": "ok", "summary": "", "findings": [], "numbers": {}}):
            rc = dl.run(["a", "b"], dry_run=True)
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(rc, 0)                       # one dashboard seen -> success

    def test_all_fail_returns_1_and_memory_fails_open(self):
        # RETRY GAP: remember() — single POST, False on failure
        self.assertEqual(dl.run(["a"], dry_run=True), 1)
        self.assertFalse(dl.remember("t", "u", "alarm"))


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        self.assertEqual(dl.selftest(), 0)

    def test_parse_look_edges(self):
        self.assertEqual(dl.parse_look(None)["findings"], ["empty reply from vision model"])
        self.assertEqual(dl.parse_look('{"status":"ok","numbers":[1,2]}')["numbers"], {})
        self.assertEqual(dl.parse_look('{"status":"ALARM ","findings":null}')["status"], "alarm")
        self.assertEqual(dl.parse_look('{broken')["status"], "watch")

    def test_should_alert_and_render_rejects_non_png(self):
        self.assertFalse(dl.should_alert("watch", None))
        with patch.object(dl.urllib.request, "urlopen", return_value=_cm(b"<html>login</html>")):
            with self.assertRaises(RuntimeError):
                dl.render("x")

    def test_targets_from_config_or_default(self):
        self.assertEqual(dl.targets(_Cur()), dl.DEFAULT_TARGETS)
        self.assertEqual(dl.targets(_Cur({"targets": [["u1", "T1"], "u2"]})), [("u1", "T1"), ("u2", "u2")])


class TestIntegration(unittest.TestCase):
    def test_look_at_sends_png_and_reads_thinking_fallback(self):
        reply = json.dumps({"response": "", "thinking": '{"status":"alarm","summary":"red"}'}).encode()
        with patch.object(dl.urllib.request, "urlopen", return_value=_cm(reply)) as m:
            look = dl.look_at("u", "T", PNG)
        self.assertEqual(look["status"], "alarm")
        body = json.loads(m.call_args[0][0].data)
        self.assertEqual(body["model"], dl.VISION_MODEL)
        self.assertEqual(len(body["images"]), 1)

    def test_alert_uses_shared_notify(self):
        nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
        with patch.dict(sys.modules, {"nova_notify": nn}):
            dl.alert("u", "Fleet", {"summary": "down", "findings": ["x"]})
        self.assertEqual(nn.notify.call_args[1]["category"], "dashboard_look")
        self.assertIn("/d/u", nn.notify.call_args[0][1])


class TestFunctional(unittest.TestCase):
    def test_alarm_stores_state_memory_and_alerts_once(self):
        cur = _Cur()
        alarm = {"status": "alarm", "summary": "red", "findings": ["p: 0"], "numbers": {}}
        pg = MagicMock(); pg.cursor.return_value = cur
        with patch.object(dl, "_pg", return_value=pg), patch.object(dl, "render", return_value=PNG), \
             patch.object(dl, "look_at", return_value=alarm), \
             patch.object(dl, "remember", return_value=True) as rem, patch.object(dl, "alert") as al:
            self.assertEqual(dl.run(["fleet-health"], dry_run=False), 0)
            self.assertEqual(dl.run(["fleet-health"], dry_run=False), 0)   # second look: deduped
        self.assertEqual(al.call_count, 1)
        self.assertEqual(rem.call_count, 2)
        self.assertEqual(cur.cfg["last:fleet-health"]["prev_status"], "alarm")

    def test_dry_run_writes_nothing(self):
        cur = _Cur(); pg = MagicMock(); pg.cursor.return_value = cur
        with patch.object(dl, "_pg", return_value=pg), patch.object(dl, "render", return_value=PNG), \
             patch.object(dl, "look_at", return_value={"status": "alarm", "summary": "", "findings": [], "numbers": {}}), \
             patch.object(dl, "remember") as rem, patch.object(dl, "alert") as al:
            dl.run(None, dry_run=True)
        rem.assert_not_called(); al.assert_not_called()
        self.assertFalse(any(s.lstrip().startswith("INSERT") for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        # --help, not --selftest: the selftest resolves Grafana through nova_resolve (service registry in PG)
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(dl.main))


if __name__ == "__main__":
    unittest.main()
