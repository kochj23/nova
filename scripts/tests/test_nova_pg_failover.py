#!/usr/bin/env python3
"""Tests for nova_pg_failover.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). This tool promotes a Postgres standby and rewrites DNS/pgbouncer,
so EVERY ssh/subprocess/nsupdate/Slack call is mocked and the fence marker is redirected to a
tempdir; no host, DB, or DNS record is ever touched, and the refusal/abort paths are proven.
Written by Jordan Koch (via Claude)."""
import argparse
import importlib.util
import io
import re
import subprocess
import sys
import types
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fo = _load("nova_pg_failover_t", SCRIPTS / "nova_pg_failover.py")
SRC = (SCRIPTS / "nova_pg_failover.py").read_text()
# neutralize the only outbound at load — nothing can post to Slack
fo.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), JORDAN_DM="D_TEST")


def _args(**kw):
    return argparse.Namespace(**kw)


class TestSecurity(unittest.TestCase):

    def test_federal_hill_failover_probe_is_detached_and_non_fatal(self):
        src = (Path(__file__).resolve().parent.parent / "nova_pg_failover.py").read_text()
        i = src.index("nova_federal_hill_lights.py")
        block = src[src.rindex("try:", 0, i):src.index("except Exception", i) + 120]
        self.assertIn("start_new_session=True", block)      # never blocks the failover
        self.assertIn("non-fatal", block)
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_tsig_key_from_keychain_not_source(self):
        self.assertIn('"security", "find-generic-password"', SRC)
        self.assertIn("nova-bind-tsig-key", SRC)
        self.assertNotRegex(SRC, r"hmac-sha256:[^\"']*:[A-Za-z0-9+/=]{20,}")

    def test_promote_refuses_without_confirm(self):
        with mock.patch.object(fo, "ssh") as ssh, mock.patch.object(fo, "log"):
            rc = fo.cmd_promote(_args(confirm=False))
        self.assertEqual(rc, 1)
        ssh.assert_not_called()                       # never touched a host
        fo.nova_config.post_both.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_pgbouncer_rewrite_large_file_fast(self):
        text = ("host=192.168.1.2 port=5434 db=x\n" * 10_000)
        t0 = time.perf_counter()
        out = text.replace("host=192.168.1.2 port=5434", "host=192.168.1.10 port=5432")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertNotIn("192.168.1.2", out)


class TestRetry(unittest.TestCase):
    def test_promote_aborts_if_primary_still_answers(self):
        # the split-brain guard: a live primary => abort, no promotion, nothing mutated
        with mock.patch.object(fo, "check_primary_alive", return_value=(True, True)), \
             mock.patch.object(fo, "ssh") as ssh, mock.patch.object(fo, "log"):
            rc = fo.cmd_promote(_args(confirm=True))
        self.assertEqual(rc, 1)
        ssh.assert_not_called()
        fo.nova_config.post_both.assert_not_called()

    def test_promote_aborts_if_promotion_command_fails(self):
        with mock.patch.object(fo, "check_primary_alive", return_value=(False, False)), \
             mock.patch.object(fo, "load_failover_cfg", return_value={}), \
             mock.patch.object(fo, "run_gates", return_value=(True, [("witness", True, "ok")])), \
             mock.patch.object(fo, "ssh", return_value=(1, "", "err")) as ssh, mock.patch.object(fo, "log"):
            rc = fo.cmd_promote(_args(confirm=True))
        self.assertEqual(rc, 1)
        # one ssh (the failed promote), then abort — never reached pg_is_in_recovery re-check
        self.assertEqual(ssh.call_count, 1)
        fo.nova_config.post_both.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_check_primary_alive_two_signals(self):
        def ssh(host, cmd, timeout=15):
            if "pg_is_in_recovery" in cmd:
                return (0, "f", "")
            return (0, "", "")
        with mock.patch.object(fo, "ssh", side_effect=ssh):
            self.assertEqual(fo.check_primary_alive(), (True, True))

    def test_check_primary_dead(self):
        with mock.patch.object(fo, "ssh", return_value=(255, "", "timeout")):
            self.assertEqual(fo.check_primary_alive(), (False, False))

    def test_standby_health(self):
        def ssh(host, cmd, timeout=15):
            if host == fo.STANDBY_IP:
                return (0, "t", "")
            return (0, "2ms", "")
        with mock.patch.object(fo, "ssh", side_effect=ssh):
            in_rec, lag, err = fo.check_standby_health()
        self.assertTrue(in_rec)
        self.assertEqual(lag, "2ms")


class TestIntegration(unittest.TestCase):
    def test_unfence_requires_confirm_and_marker(self):
        fo.nova_config.post_both.reset_mock()
        with mock.patch.object(fo, "FENCE_MARKER", Path("/nonexistent/fence.json")), mock.patch.object(fo, "log"):
            self.assertEqual(fo.cmd_unfence(_args(confirm=False)), 0)   # no marker => nothing to do
        fo.nova_config.post_both.assert_not_called()

    def test_unfence_without_confirm_refuses_when_marker_present(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            m = Path(td) / "fence.json"; m.write_text("fenced")
            with mock.patch.object(fo, "FENCE_MARKER", m), mock.patch.object(fo, "log"), \
                 redirect_stdout(io.StringIO()):
                rc = fo.cmd_unfence(_args(confirm=False))
            self.assertEqual(rc, 1)
            self.assertTrue(m.exists())              # marker NOT cleared without --confirm


class TestFunctional(unittest.TestCase):
    def test_check_reports_status_readonly(self):
        with mock.patch.object(fo, "check_primary_alive", return_value=(True, True)), \
             mock.patch.object(fo, "check_standby_health", return_value=(True, "1ms", "")), \
             mock.patch.object(fo, "FENCE_MARKER", Path("/nonexistent/fence.json")), \
             redirect_stdout(io.StringIO()) as out:
            rc = fo.cmd_check(_args())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("All healthy", text)
        self.assertIn("Postgres responding: True", text)

    def test_unfence_clears_marker_with_confirm(self):
        import tempfile
        fo.nova_config.post_both.reset_mock()
        with tempfile.TemporaryDirectory() as td:
            m = Path(td) / "fence.json"; m.write_text("fenced")
            with mock.patch.object(fo, "FENCE_MARKER", m), mock.patch.object(fo, "log"):
                rc = fo.cmd_unfence(_args(confirm=True))
            self.assertEqual(rc, 0)
            self.assertFalse(m.exists())
        fo.nova_config.post_both.assert_called_once()

    def test_full_promote_golden_path_writes_fence_and_alerts(self):
        import tempfile
        fo.nova_config.post_both.reset_mock()
        calls = {"n": 0}
        def ssh(host, cmd, timeout=15):
            calls["n"] += 1
            if "pg_promote" in cmd:
                return (0, "t", "")
            if "pg_is_in_recovery" in cmd:
                return (0, "f", "")        # promoted standby reports writable
            return (0, "", "")
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "state" / "fence.json"
            ini = Path(td) / "pgbouncer.ini"; ini.write_text("[databases]\nnova_ops = host=192.168.1.2 port=5434\n")
            with mock.patch.object(fo, "check_primary_alive", return_value=(False, False)), \
                 mock.patch.object(fo, "load_failover_cfg", return_value={}), \
                 mock.patch.object(fo, "run_gates", return_value=(True, [("lsn", True, "past checkpoint")])), \
                 mock.patch.object(fo, "ssh", side_effect=ssh), \
                 mock.patch.object(fo, "FENCE_MARKER", marker), \
                 mock.patch.object(fo, "PGBOUNCER_INI", str(ini)), \
                 mock.patch.object(fo.subprocess, "run") as run, mock.patch.object(fo, "log"), \
                 mock.patch.object(fo.time, "sleep"):
                run.return_value = types.SimpleNamespace(returncode=0, stdout="tsigsecret\n", stderr="")
                rc = fo.cmd_promote(_args(confirm=True))
            self.assertEqual(rc, 0)
            self.assertTrue(marker.exists())
            self.assertIn("Gates: lsn: past checkpoint", marker.read_text())
            self.assertIn("host=192.168.1.10 port=5432", ini.read_text())
            self.assertNotIn("192.168.1.2", ini.read_text())
        fo.nova_config.post_both.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_check_cli_exits_zero(self):
        import os
        r = subprocess.run([sys.executable, "-c",
                            "import sys,unittest.mock as m; "
                            "import importlib.util as u; "
                            "s=u.spec_from_file_location('f', r'" + str(SCRIPTS / 'nova_pg_failover.py') + "'); "
                            "mod=u.module_from_spec(s); s.loader.exec_module(mod); "
                            "mod.nova_config=m.MagicMock(); "
                            "mod.check_primary_alive=lambda: (True, True); "
                            "mod.check_standby_health=lambda: (True,'1ms',''); "
                            "from pathlib import Path; mod.FENCE_MARKER=Path('/nonexistent/x.json'); "
                            "sys.argv=['x','check']; sys.exit(mod.main())"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("All healthy", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


# ── #43 hard gates ──────────────────────────────────────────────────────────

WITNESS_CFG = {"witness": {"name": "w", "host": "witness.example", "pg_ports": [5432, 5434],
                           "power_independent": True}}


class FakeSSH:
    """Scripted ssh: list of (substring, (rc, out, err)); first match wins. Records every call."""

    def __init__(self, rules, default=(0, "", "")):
        self.rules, self.default, self.calls = rules, default, []

    def __call__(self, host, cmd, timeout=15):
        self.calls.append((host, cmd))
        for sub, res in self.rules:
            if sub in cmd or sub == host:
                return res
        return self.default


class TestGateWitness(unittest.TestCase):
    def test_no_witness_configured_refuses(self):
        ok, d = fo.witness_gate({})
        self.assertFalse(ok); self.assertIn("no witness", d)

    def test_unreachable_witness_refuses(self):
        f = FakeSSH([("true", (255, "", "timeout"))])
        with mock.patch.object(fo, "ssh", f):
            ok, d = fo.witness_gate(WITNESS_CFG)
        self.assertFalse(ok); self.assertIn("unreachable", d)
        self.assertEqual(len(f.calls), 1)

    def test_witness_still_reaching_primary_is_a_partition(self):
        f = FakeSSH([("/dev/tcp/", (0, "open", "")), ("true", (0, "", ""))])
        with mock.patch.object(fo, "ssh", f):
            ok, d = fo.witness_gate(WITNESS_CFG)
        self.assertFalse(ok); self.assertIn("partition", d)

    def test_witness_agrees_primary_down(self):
        f = FakeSSH([("/dev/tcp/", (0, "closed", "")), ("true", (0, "", ""))])
        with mock.patch.object(fo, "ssh", f):
            ok, d = fo.witness_gate(WITNESS_CFG)
        self.assertTrue(ok, d)
        self.assertEqual({c[0] for c in f.calls}, {"witness.example"})   # probes run FROM the witness

    def test_unverified_power_independence_refuses(self):
        cfg = {"witness": {**WITNESS_CFG["witness"], "power_independent": False}}
        f = FakeSSH([("/dev/tcp/", (0, "closed", "")), ("true", (0, "", ""))])
        with mock.patch.object(fo, "ssh", f):
            ok, d = fo.witness_gate(cfg)
        self.assertFalse(ok); self.assertIn("power independence", d)

    def test_hostile_host_value_never_reaches_a_shell(self):
        cfg = {"witness": {**WITNESS_CFG["witness"], "host": "w; rm -rf /"}}
        with mock.patch.object(fo, "ssh") as ssh:
            ok, d = fo.witness_gate(cfg)
        self.assertFalse(ok); ssh.assert_not_called()


class TestGateFence(unittest.TestCase):
    def test_ssh_stop_verified_passes_without_plug(self):
        f = FakeSSH([("systemctl stop", (0, "", "")), ("is-active", (0, "", ""))])
        with mock.patch.object(fo, "ssh", f):
            ok, d, method = fo.fence_gate({})
        self.assertTrue(ok, d); self.assertEqual(method, "ssh")

    def test_unverified_stop_and_no_plug_refuses(self):
        f = FakeSSH([("systemctl stop", (255, "", "no route")), ("is-active", (255, "", ""))])
        with mock.patch.object(fo, "ssh", f):
            ok, d, method = fo.fence_gate({})
        self.assertFalse(ok); self.assertIn("NOT fenced", d); self.assertEqual(method, "none")

    def test_plug_used_only_when_ssh_fails_and_through_power_cut(self):
        power = mock.MagicMock(return_value=(True, "plug switch.pg state=off"))
        f = FakeSSH([("systemctl stop", (255, "", "")), ("is-active", (255, "", ""))])
        with mock.patch.object(fo, "ssh", f):
            ok, d, method = fo.fence_gate({"fence": {"plug_entity": "switch.pg"}}, confirmation_id=7, _power=power)
        self.assertTrue(ok); self.assertEqual(method, "plug")
        power.assert_called_once_with("switch.pg", 7)
        power.reset_mock()
        f = FakeSSH([("systemctl stop", (0, "", "")), ("is-active", (0, "", ""))])
        with mock.patch.object(fo, "ssh", f):
            fo.fence_gate({"fence": {"plug_entity": "switch.pg"}}, _power=power)
        power.assert_not_called()                                         # ssh worked: no power cut

    def test_refused_plug_with_failed_ssh_refuses(self):
        power = mock.MagicMock(return_value=(False, "physical_guard refused: x"))
        f = FakeSSH([("is-active", (1, "", ""))], default=(255, "", ""))
        with mock.patch.object(fo, "ssh", f):
            ok, d, _ = fo.fence_gate({"fence": {"plug_entity": "switch.pg"}}, _power=power)
        self.assertFalse(ok); self.assertIn("physical_guard refused", d)

    def test_dry_run_never_stops_anything(self):
        f = FakeSSH([])
        with mock.patch.object(fo, "ssh", f):
            ok, d, method = fo.fence_gate({}, dry_run=True)
        self.assertEqual(method, "plan"); self.assertIn("WOULD run", d)
        self.assertFalse(any("systemctl stop" in c[1] for c in f.calls))

    def test_power_cut_goes_through_proteus(self):
        G = types.SimpleNamespace(kill_engaged=lambda: False, report_block=mock.MagicMock(),
                                  physical_guard=mock.MagicMock(return_value=(False, "needs Jordan")))
        plug = mock.MagicMock()
        ok, d = fo._power_cut("switch.pg", None, _guards=G, _plug_off=plug)
        self.assertFalse(ok); plug.assert_not_called()                    # guard said no => no actuation
        G.report_block.assert_called_once()
        self.assertEqual(G.physical_guard.call_args[1]["entity_ids"], ["switch.pg"])
        G.physical_guard.return_value = (True, "ok")
        plug.return_value = (True, "off")
        self.assertEqual(fo._power_cut("switch.pg", 3, _guards=G, _plug_off=plug), (True, "off"))
        G.kill_engaged = lambda: True
        plug.reset_mock()
        ok, d = fo._power_cut("switch.pg", 3, _guards=G, _plug_off=plug)
        self.assertFalse(ok); self.assertIn("kill switch", d); plug.assert_not_called()


class TestGateLSN(unittest.TestCase):
    def test_lsn_int(self):
        self.assertEqual(fo.lsn_int("0/10"), 16)
        self.assertEqual(fo.lsn_int("1/0"), 1 << 32)
        self.assertIsNone(fo.lsn_int("garbage")); self.assertIsNone(fo.lsn_int(None))

    def test_strictly_past_checkpoint_required(self):
        self.assertTrue(fo.evaluate_lsn("307/7D01AA51", "307/7D01AA50", "checkpoint", 0)[0])
        ok, d = fo.evaluate_lsn("307/7D01AA50", "307/7D01AA50", "checkpoint", 1 << 30)
        self.assertFalse(ok); self.assertIn("fork the timeline", d)       # equal is NOT enough
        self.assertTrue(fo.evaluate_lsn("307/0", "307/7D01AA50", "checkpoint", 0, accept_loss=True)[0])

    def test_unknown_window_needs_accept_loss(self):
        self.assertFalse(fo.evaluate_lsn("1/0", None, None, 0)[0])
        self.assertTrue(fo.evaluate_lsn("1/0", None, None, 0, accept_loss=True)[0])
        self.assertFalse(fo.evaluate_lsn(None, "1/0", "checkpoint", 0)[0])

    def test_peer_ahead_refuses(self):
        ok, d = fo.evaluate_lsn("1/0", "0/10", "checkpoint", 1024, peer_best=("core7", (1 << 32) + 4096))
        self.assertFalse(ok); self.assertIn("promote core7 instead", d)
        self.assertTrue(fo.evaluate_lsn("1/0", "0/10", "checkpoint", 8192, peer_best=("core7", (1 << 32) + 4096))[0])

    def test_lsn_gate_reads_controldata_and_waits_for_record_end(self):
        seq = iter(["1/0|1/0", "1/0|1/0", "1/100|1/0"])
        f = FakeSSH([("pg_controldata", (0, "Latest checkpoint location:           1/80\n", ""))])
        sleeps = []
        with mock.patch.object(fo, "ssh", f), \
                mock.patch.object(fo, "node_sql", side_effect=lambda h, q, **k: (True, next(seq))):
            ok, d = fo.lsn_gate({"lsn": {"controldata_cmd": "pg_controldata -D x"}}, fenced_by_ssh=True,
                                _sleep=sleeps.append)
        self.assertTrue(ok, d); self.assertEqual(sleeps, [5, 5])

    def test_lsn_gate_without_ssh_fence_has_unknown_window(self):
        with mock.patch.object(fo, "node_sql", return_value=(True, "1/0|1/0")), mock.patch.object(fo, "ssh") as ssh:
            ok, d = fo.lsn_gate({"lsn": {"controldata_cmd": "pg_controldata"}}, fenced_by_ssh=False, _sleep=lambda s: None)
        self.assertFalse(ok); self.assertIn("unknown", d); ssh.assert_not_called()


class TestGateOrchestration(unittest.TestCase):
    def test_live_order_stops_before_destructive_fence(self):
        args = _args(accept_loss=False, plug_confirmation=None)
        with mock.patch.object(fo, "witness_gate", return_value=(False, "partition")), \
                mock.patch.object(fo, "fence_gate") as fence, mock.patch.object(fo, "lsn_gate") as lsn:
            ok, res = fo.run_gates(args, {})
        self.assertFalse(ok); fence.assert_not_called(); lsn.assert_not_called()

    def test_promote_refuses_and_never_promotes_when_a_gate_fails(self):
        fo.nova_config.post_both.reset_mock()
        f = FakeSSH([])
        with mock.patch.object(fo, "check_primary_alive", return_value=(False, False)), \
                mock.patch.object(fo, "load_failover_cfg", return_value={}), \
                mock.patch.object(fo, "ssh", f), mock.patch.object(fo, "log"):
            rc = fo.cmd_promote(_args(confirm=True, dry_run=False, accept_loss=False, plug_confirmation=None))
        self.assertEqual(rc, 1)
        self.assertFalse(any("pg_promote" in c[1] for c in f.calls))
        fo.nova_config.post_both.assert_not_called()

    def test_dry_run_plan_is_read_only_and_reports_settings(self):
        fo.nova_config.post_both.reset_mock()
        f = FakeSSH([("/dev/tcp/", (0, "closed", "")), ("wal_log_hints", (0, "off|-1|off|false", "")),
                     ("pg_last_wal_receive_lsn", (0, "1/100|1/100", "")),
                     ("pg_controldata", (0, "Latest checkpoint location: 1/80", ""))])
        cfg = {**WITNESS_CFG, "lsn": {"controldata_cmd": "pg_controldata"},
               "nodes": [{"name": "nova-core", "host": "core.example"}]}
        with mock.patch.object(fo, "check_primary_alive", return_value=(False, True)), \
                mock.patch.object(fo, "load_failover_cfg", return_value=cfg), \
                mock.patch.object(fo, "ssh", f), redirect_stdout(io.StringIO()) as out:
            rc = fo.cmd_promote(_args(confirm=False, dry_run=True, accept_loss=False, plug_confirmation=None))
        self.assertEqual(rc, 0)
        text = out.getvalue()
        for needle in ("gate 1  witness  PASS", "gate 2  fence", "gate 3  lsn      PASS",
                       "wal_log_hints=off", "max_slot_wal_keep_size=-1", "pg_rewind impossible"):
            self.assertIn(needle, text)
        mutating = ("pg_promote", "systemctl stop", "nsupdate", "brew")
        self.assertFalse(any(m in c[1] for c in f.calls for m in mutating))
        fo.nova_config.post_both.assert_not_called()

    def test_config_falls_back_to_standby_copy(self):
        def boom():
            raise OSError("primary down")
        sql = mock.MagicMock(return_value=(True, '{"witness": {"host": "w"}}'))
        cfg = fo.load_failover_cfg(_connect=boom, _ssh_sql=sql)
        self.assertEqual(cfg["witness"]["host"], "w")
        self.assertEqual(sql.call_args[0][0], fo.STANDBY_IP)
        self.assertEqual(fo.load_failover_cfg(_connect=boom, _ssh_sql=lambda *a: (False, "x")), {})

    def test_new_gate_code_adds_no_hardcoded_ips(self):
        gate_src = SRC[SRC.index("# ── #43 hard gates"):SRC.index("def cmd_check")].replace("127.0.0.1", "")
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", gate_src))


if __name__ == "__main__":
    unittest.main()
