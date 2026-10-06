#!/usr/bin/env python3
"""Tests for nova_gateway_v2.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway_v2.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="gwv2-test-"))
(TMP / ".openclaw" / "logs").mkdir(parents=True)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # the package opens ~/.openclaw/logs/nova_gateway_v2.log at import; point HOME at a tempdir for the load
    with patch.dict(os.environ, {"HOME": str(TMP), "NOVA_TEST_QUIET": "1"}):
        spec.loader.exec_module(mod)
    return mod


_before = set(sys.modules)
gw = _load("gwv2_under_test", SCRIPT)
# Keep the scripts-dir nova_gateway package OUT of sys.modules after loading: tests/test_gateway.py
# imports a different nova_gateway (with a context/ sub-package) and collides with ours otherwise.
_GW_MODS = {k: m for k, m in sys.modules.items() if k == "nova_gateway" or k.startswith("nova_gateway.")}
for _k in [k for k in _GW_MODS if k not in _before]:
    sys.modules.pop(_k)   # only drop what THIS file added; leave a package another file loaded in place


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_wrapper_holds_no_secrets_or_sql(self):
        # the shim must stay a shim: no DSNs, tokens, URLs or SQL live here — they belong to the package
        for needle in ("xoxb-", "postgres", "psycopg2", "execute(", "http://", "https://"):
            self.assertNotIn(needle, SRC)

    def test_tokens_come_from_the_package_loader_not_source(self):
        cfg = _GW_MODS["nova_gateway.config"]
        self.assertTrue(callable(cfg.load_tokens))
        self.assertIn("find-generic-password", (SCRIPTS / "nova_gateway" / "config.py").read_text())


class TestPerformance(unittest.TestCase):
    def test_reload_is_fast_and_sys_path_growth_is_bounded(self):
        saved, mods = list(sys.path), set(sys.modules)
        try:
            t0 = time.perf_counter()
            for _ in range(20):
                _load("gwv2_reload", SCRIPT)
            dt = time.perf_counter() - t0
            grown = len(sys.path) - len(saved)
        finally:
            sys.path[:] = saved
            for k in [k for k in sys.modules if k not in mods and (k == "nova_gateway" or k.startswith("nova_gateway."))]:
                sys.modules.pop(k)   # keep the package out of the cache (see _GW_MODS)
        self.assertLess(dt, 5.0)
        self.assertLessEqual(grown, 21)      # one insert per load plus the one package import, never more


class TestRetry(unittest.TestCase):
    def test_wrapper_has_no_in_process_restart_failure_is_loud(self):
        # RETRY GAP: nova_gateway_v2 — asyncio.run(main()) runs once; a crash propagates so launchd's
        # KeepAlive restarts the process. The wrapper must never swallow the failure into a silent exit 0.
        async def boom():
            raise RuntimeError("gateway died")
        with patch.object(gw, "main", boom):
            with self.assertRaises(RuntimeError):
                asyncio.run(gw.main())
        self.assertNotIn("try:", SRC)
        self.assertNotIn("while True", SRC)

    def test_launchd_keepalive_is_the_retry(self):
        plist = Path.home() / "Library/LaunchAgents/net.digitalnoise.nova-gateway-v2.plist"
        if not plist.exists():
            self.skipTest("launchd plist not installed on this host")
        self.assertIn("KeepAlive", plist.read_text())


class TestUnit(unittest.TestCase):
    def test_exports_the_package_main_coroutine(self):
        self.assertTrue(asyncio.iscoroutinefunction(gw.main))
        self.assertEqual(gw.main.__module__, "nova_gateway.main")

    def test_scripts_dir_is_on_sys_path_after_load(self):
        self.assertIn(str(SCRIPTS), sys.path)
        self.assertIn("sys.path.insert(0, str(Path(__file__).parent))", SRC)

    def test_asyncio_entry_only_under_main_guard(self):
        self.assertIn('if __name__ == "__main__":\n    asyncio.run(main())', SRC)
        self.assertEqual(SRC.count("asyncio.run("), 1)


class TestIntegration(unittest.TestCase):
    def test_wrapper_delegates_to_the_same_object_the_package_exports(self):
        pkg = _GW_MODS["nova_gateway"]                  # the package re-exports main(), shadowing the submodule name
        self.assertIs(gw.main, _GW_MODS["nova_gateway.main"].main)
        self.assertIs(gw.main, pkg.main)
        self.assertEqual(pkg.__all__, ["main"])

    def test_package_main_wires_health_channels_and_pg(self):
        src = (SCRIPTS / "nova_gateway" / "main.py").read_text()
        for needle in ("health_server(ctx)", "ensure_pg_schema(ctx)", "run_slack(ctx)", "run_discord(ctx)",
                       "run_signal(ctx)", "run_claude_channel(ctx)", "ctx.shutdown.wait()"):
            self.assertIn(needle, src)


class TestFunctional(unittest.TestCase):
    def test_entrypoint_awaits_main_to_completion(self):
        seen = []

        async def fake_main():
            seen.append("ran")
            return "done"
        with patch.object(gw, "main", fake_main):
            self.assertEqual(asyncio.run(gw.main()), "done")
        self.assertEqual(seen, ["ran"])

    def test_package_main_runs_offline_through_startup_and_shutdown(self):
        """Golden path of the delegated main(): every I/O seam stubbed; shutdown is set by the health server."""
        m = _GW_MODS["nova_gateway.main"]
        calls = []

        async def health(ctx):
            calls.append("health"); ctx.shutdown.set()

        async def schema(ctx):
            calls.append("schema"); raise RuntimeError("pg down")   # error path: must be logged, not fatal

        async def chan(ctx):
            await asyncio.sleep(10)

        class _Client:
            def __init__(self, *a, **k): calls.append("http")
            async def aclose(self): calls.append("aclose")

        class _Loop:
            def add_signal_handler(self, *a, **k): calls.append("sig")

        with patch.object(m, "load_tokens", lambda: {"slack_bot": "", "openrouter": ""}), \
             patch.object(m, "ensure_pg_schema", schema), patch.object(m, "health_server", health), \
             patch.object(m, "run_slack", chan), patch.object(m, "run_discord", chan), \
             patch.object(m, "run_signal", chan), patch.object(m, "run_claude_channel", chan), \
             patch.object(m.httpx, "AsyncClient", _Client), patch.object(m.asyncio, "get_event_loop", lambda: _Loop()), \
             patch.object(m, "GW_STANDBY", False):
            asyncio.run(gw.main())
        self.assertEqual(calls[:2], ["http", "schema"])
        self.assertIn("health", calls)
        self.assertEqual(calls.count("sig"), 3)
        self.assertEqual(calls[-1], "aclose")


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_gateway(self):
        r = subprocess.run([sys.executable, "-c", "import nova_gateway_v2; print(type(nova_gateway_v2.main).__name__)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=60,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "function")
        self.assertNotIn("starting", r.stderr.lower())

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
