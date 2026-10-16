#!/usr/bin/env python3
"""Tests for nova_capability_ledger.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_capability_ledger as CL  # noqa: E402
import nova_threshold as T  # noqa: E402
import nova_hendershot_cellar as C  # noqa: E402
import nova_goblin_map as G  # noqa: E402

SCRIPT = SCRIPTS / "nova_capability_ledger.py"
SRC = SCRIPT.read_text()
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
MODULE_SRCS = {p: (SCRIPTS / p).read_text() for p in
               ("nova_capability_ledger.py", "nova_threshold.py", "nova_hendershot_cellar.py", "nova_goblin_map.py")}


def _ctx(**over):
    idx = {"nova_x": "x = 1\n"}
    ctx = {"now": NOW, "me": "studio", "probes": {}, "index": idx, "graph": C.import_graph(idx),
           "refs": {}, "runs": {}, "runs_since": NOW - timedelta(days=150), "live": {},
           "fleet_secrets": {"nova-real-token": NOW, "legacy_api_keys": NOW},
           "keychain": {"nova-kc": None, "LEGACY_VAULT-copy": None}, "threshold_names": set(), "ssh_keys": [],
           "config_tokens": [("svc", "api_token", NOW), ("legacy_vault", "master_api_key", NOW)],
           "ha_auth": None, "claude": {"mcp": [], "allow": []}, "transcript_use": None,
           "transcripts_since": None, "gateway_registry": (None, set()), "gateway_use": ({}, None)}
    ctx.update(over)
    return ctx


class TestSecurity(unittest.TestCase):
    def test_no_credentials_or_hosts_in_source(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token)\s*=\s*['\"][^'\"]{6,}")
        self.assertNotRegex(SRC, r"\b\d{1,3}(\.\d{1,3}){3}\b")
        self.assertNotIn("/Users/", SRC)

    def test_no_query_ever_names_a_decoy(self):
        # TMA-1 counts readers of the decoys; no scan may name them in SQL
        for name, src in MODULE_SRCS.items():
            for q in re.findall(r"\"((?:SELECT|INSERT|UPDATE|COPY|WITH)[^\"]*)\"", src):
                self.assertNotIn("legacy_", q.lower(), name)

    def test_decoys_excluded_from_cellar_inventory(self):
        rows, _, _ = C.inventory(None, _ctx())
        idents = " ".join(f"{r['holder']} {r['capability']}" for r in rows).lower()
        self.assertNotIn("legacy_", idents)
        self.assertIn("svc/api_token", idents)                 # real ones still listed
        self.assertIn("nova-real-token", idents)

    def test_decoys_excluded_from_fleet_secret_names(self):
        cur = mock.Mock()
        cur.fetchall.return_value = [("nova-real-token", NOW), ("legacy_api_keys", NOW)]
        self.assertEqual(C.fleet_secret_names(cur), {"nova-real-token": NOW})

    def test_decoys_excluded_from_threshold_inventory(self):
        decoy = {"kind": "slack_token", "host": "", "ident": "legacy_vault_slack", "label": "legacy_vault_slack"}
        real = {"kind": "slack_token", "host": "", "ident": "slack_bot", "label": "slack_bot"}
        with mock.patch.object(T, "fleet_keys", return_value=[]), \
                mock.patch.object(T, "slack_tokens", return_value=[decoy, real]), \
                mock.patch.object(T, "herd_senders", return_value=[]):
            self.assertEqual(T.inventory(None), [real])

    def test_goblin_gather_counts_no_decoy(self):
        with mock.patch.object(C, "probe_fleet", return_value={}), mock.patch.object(C, "script_index", return_value={}), \
                mock.patch.object(C, "fleet_secret_names", return_value={"a": NOW, "legacy_api_keys": NOW}), \
                mock.patch.object(C, "keychain_names", return_value={"legacy_vault": None, "k": None}), \
                mock.patch.object(C, "ha_config_dir", return_value=None), \
                mock.patch.object(C, "gateway_tools", return_value=(None, set())), \
                mock.patch.object(C, "claude_config", return_value={"mcp": [], "allow": []}), \
                mock.patch.object(G.W, "get_config", return_value=None), \
                mock.patch.object(G, "scheduler_tasks", return_value=[]):
            ctx = G.gather(mock.Mock(fetchall=mock.Mock(return_value=[])))
        self.assertEqual(set(ctx["fleet"]), {"a"})
        self.assertTrue(ctx["keychain"] is None or "legacy_vault" not in ctx["keychain"])


class TestPerformance(unittest.TestCase):
    def test_drop_decoys_on_10k(self):
        rows = [("svc%d" % i, "api_token", None) for i in range(10000)] + [("legacy_vault", "master_api_key", None)]
        t = time.perf_counter()
        out = CL.drop_decoys(rows)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(len(out), 10000)


class TestRetry(unittest.TestCase):
    # RETRY GAP: run_mode — no external call of its own; the modes' modules own their retries.
    def test_decoy_names_fail_open_without_tma1(self):
        spec = importlib.util.spec_from_file_location("cl_no_tma1", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"nova_tma1": None}):
            spec.loader.exec_module(m)
        self.assertEqual(m.DECOY_NAMES, frozenset({"legacy_api_keys", "legacy_vault"}))

    def test_decoy_names_come_from_tma1(self):
        import nova_tma1
        self.assertIn(nova_tma1.DECOY_TABLE, CL.DECOY_NAMES)
        self.assertIn(nova_tma1.DECOY_CONFIG[0], CL.DECOY_NAMES)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(CL.selftest(), 0)

    def test_is_decoy_edges(self):
        self.assertFalse(CL.is_decoy())
        self.assertFalse(CL.is_decoy(None, ""))
        self.assertTrue(CL.is_decoy("x", "public.Legacy_Api_Keys"))
        self.assertEqual(CL.drop_decoys([]), [])
        self.assertEqual(CL.drop_decoys(["a", "legacy_vault"]), ["a"])


class TestIntegration(unittest.TestCase):
    def test_modes_map_to_the_three_modules(self):
        self.assertEqual({k: v[0] for k, v in CL.MODES.items()},
                         {"grants": "nova_threshold", "dormant": "nova_hendershot_cellar", "blast": "nova_goblin_map"})
        for mod in (T, C, G):
            self.assertTrue(callable(mod.run))

    def test_literary_names_survive(self):
        names = {v[1] for v in CL.MODES.values()}
        self.assertEqual(names, {"The Threshold Ledger", "Hendershot's Cellar", "The Green Goblin Map"})

    def test_tables_unchanged(self):
        self.assertIn("threshold_ledger", MODULE_SRCS["nova_threshold.py"])
        self.assertIn("cellar_inventory", MODULE_SRCS["nova_hendershot_cellar.py"])
        self.assertIn("goblin_map", MODULE_SRCS["nova_goblin_map.py"])


class TestFunctional(unittest.TestCase):
    def _run(self, fn, argv):
        with redirect_stdout(io.StringIO()) as out:
            rc = fn(argv)
        return rc, out.getvalue()

    def test_new_modes_dispatch(self):
        for argv, mod, kw in ((["--grants"], T, {"dry": False}), (["--dormant", "--dry-run"], C, {"dry": True}),
                              (["--blast"], G, {"dry": False})):
            with mock.patch.object(mod, "run") as r:
                rc, out = self._run(CL.main, argv)
            self.assertEqual(rc, 0)
            r.assert_called_once_with(**kw)
            self.assertIn(CL.MODES[argv[0][2:]][1], out)

    def test_old_invocations_delegate(self):
        with mock.patch.object(T, "run") as r:
            self.assertEqual(self._run(T.main, ["--run"])[0], 0)
        r.assert_called_once_with(dry=False)
        with mock.patch.object(T, "show", return_value=0) as s:
            self._run(T.main, ["--show"])
        s.assert_called_once()
        with mock.patch.object(C, "run") as r:
            self._run(C.main, ["--scheduled"])
        r.assert_called_once_with(dry=False)
        with mock.patch.object(G, "run") as r:
            self._run(G.main, ["--dry-run"])
        r.assert_called_once_with(dry=True)
        with mock.patch.object(G, "report", return_value=0) as rep:
            self._run(G.main, ["--report"])
        rep.assert_called_once()

    def test_error_paths(self):
        rc, out = self._run(CL.main, [])
        self.assertEqual(rc, 0)
        self.assertIn("--grants", out)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            CL.main(["--grants", "--blast"])


class TestFrame(unittest.TestCase):
    def test_selftest_and_help_subprocess(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for flag in ("--selftest", "--help"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch.object(T, "run") as r:
            spec = importlib.util.spec_from_file_location("cl_import", SCRIPT)
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        r.assert_not_called()


if __name__ == "__main__":
    unittest.main()
