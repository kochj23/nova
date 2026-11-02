#!/usr/bin/env python3
"""Tests for nova_cinc_orchestrate.py fleet baseline (2026-11-01) — the 7 house categories (Security,
Performance, Retry, Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Covers the pure helpers behind the baseline checks (snmp views/community, sshd drop-in, known_hosts,
zshrc colors line, fstab, symlinks, wazuh address, macOS snmpd) and proves the new recipes (nova_wazuh,
nova_zsh, nova_macos) dispatch and stay read-only in a drift check. Offline: every ssh_exec / subprocess
call is mocked; LOG_FILE is a tempdir."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_cinc_orchestrate.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="cinc_base_"))
# write verbs only: read-only probes such as `systemctl is-active` / `launchctl print` are allowed
MUTATING = re.compile(r"apt-get install|systemctl (restart|reload|enable|start)|\btee\b|sed -i|chsh|git clone|"
                      r"ln -s|launchctl (bootstrap|kickstart|enable)|installer -pkg|timedatectl set|apt-mark hold|"
                      r"socketfilterfw --(add|unblock)|cat >")


def _load():
    spec = importlib.util.spec_from_file_location("nova_cinc_orchestrate_base_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.nova_notify = MagicMock()
    mod.LOG_FILE = TMP / "orch.log"
    mod.print = lambda *a, **k: None
    mod._LOCAL_CACHE.clear()
    mod.is_local = lambda ip: False
    return mod


co = _load()

UBUNTU_SNMP = """agentaddress udp:161,udp6:[::1]:161
view   systemonly  included   .1.3.6.1.2.1.1
view   systemonly  included   .1.3.6.1.2.1.2
view   systemonly  included   .1.3.6.1.2.1.25
view   systemonly  included   .1.3.6.1.4.1
rocommunity nova_readonly 192.168.1.6
rocommunity public 192.168.1.0/24
rocommunity  public 192.168.1.0/24
"""
GOOD_FSTAB = """# comment
//192.168.1.19/nas/nova-fs /nova cifs credentials=/etc/cifs-unas.creds 0 0
//192.168.1.19/nas /mnt/nas cifs credentials=/etc/cifs-unas.creds 0 0
//192.168.1.19/External /mnt/nas-external cifs credentials=/etc/cifs-unas.creds 0 0
192.168.1.19:/var/nfs/shared/nas/Data /mnt/data nfs rw,hard 0 0
"""


class _Ssh:
    """ssh_exec double: answers every probe with `answer`; records every command."""
    def __init__(self, answer="drift missing inactive"):
        self.answer, self.cmds = answer, []

    def __call__(self, user, host, command, timeout=60):
        self.cmds.append(command)
        if command == "echo ok":
            return 0, "ok", ""
        return 0, self.answer, ""


class TestSecurity(unittest.TestCase):
    def test_drift_check_of_new_recipes_is_read_only(self):
        ssh = _Ssh()
        with patch.object(co, "ssh_exec", side_effect=ssh), patch.object(co.subprocess, "run") as run:
            for os_family in ("linux", "macos"):
                for r in ("nova_base", "nova_monitoring", "nova_linux", "nova_wazuh", "nova_zsh", "nova_macos"):
                    co._apply_recipe_shell("u", "10.0.0.9", os_family, r, True, node_name="n", run_list=[])
        self.assertTrue(ssh.cmds)
        self.assertEqual([c for c in ssh.cmds if MUTATING.search(c)], [])
        run.assert_not_called()                       # no _push (stdin writes) during a drift check

    def test_fstab_and_creds_never_written(self):
        self.assertIsNone(re.search(r"tee[^\"']*/etc/fstab|>\s*/etc/fstab|cifs-unas\.creds[^\"']*tee", SRC))

    def test_check_only_mac_wazuh_nodes_never_reinstalled(self):
        self.assertNotIn("192.168.1.6", co.WAZUH_MAC_AGENT_NAMES)
        self.assertNotIn("192.168.1.7", co.WAZUH_MAC_AGENT_NAMES)
        ssh = _Ssh(answer="")
        with patch.object(co, "ssh_exec", side_effect=ssh):
            out = co._check_wazuh_macos("u", "192.168.1.7", False)
        self.assertIn("check-only", out[0])
        self.assertEqual([c for c in ssh.cmds if MUTATING.search(c)], [])


class TestPerformance(unittest.TestCase):
    def test_snmp_fix_on_large_conf(self):
        big = "\n".join(f"# filler line {i}" for i in range(10000)) + "\n" + UBUNTU_SNMP
        t0 = time.perf_counter()
        fixed = co.snmp_fix_linux(big)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(co.snmp_missing_views(fixed), [])
        self.assertEqual(co.snmp_community_count(fixed), 1)


class TestRetry(unittest.TestCase):
    def test_push_and_local_run_fail_open(self):
        # RETRY GAP: _push() / _run() — one attempt; timeout -> (-1, "", msg), never raises
        with patch.object(co.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 5)) as run:
            self.assertEqual(co._push("u", "10.0.0.9", "x", "cat"), (-1, "", "push timed out"))
            with patch.object(co, "_local", return_value=True):
                self.assertEqual(co._run("u", "127.0.0.1", "true"), (-1, "", "local command timed out"))
        self.assertEqual(run.call_count, 2)

    def test_sshd_t_failure_reports_failed_not_changed(self):
        with patch.object(co, "ssh_exec", side_effect=_Ssh(answer="")), \
                patch.object(co, "_push", return_value=(0, "NOVA_SSHD_T_FAILED", "")) as push:
            out = co._check_sshd_dropin("u", "10.0.0.9", "linux", False)
        self.assertTrue(out[0].startswith("[failed]"))
        fix = push.call_args[0][3]
        self.assertIn("if sudo -n /usr/sbin/sshd -t; then", fix)          # reload only after sshd -t passes
        self.assertLess(fix.index("sshd -t"), fix.index("systemctl reload ssh"))
        self.assertIn(".nova-prev", fix)                                    # rollback copy


class TestUnit(unittest.TestCase):
    def test_snmp_missing_views_whitespace_tolerant(self):
        self.assertEqual(co.snmp_missing_views(UBUNTU_SNMP), [".1.3.6.1.2.1.25.1"])
        mint = "view systemonly included .1.3.6.1.2.1.1\nview systemonly included .1.3.6.1.2.1.25.1\n"
        self.assertEqual(co.snmp_missing_views(mint), [".1.3.6.1.2.1.2", ".1.3.6.1.2.1.25", ".1.3.6.1.4.1"])
        self.assertEqual(co.snmp_missing_views(""), co.SNMP_VIEW_OIDS)

    def test_snmp_fix_inserts_after_25_1_and_dedupes_community(self):
        conf = ("view   systemonly  included   .1.3.6.1.2.1.1\nview   systemonly  included   .1.3.6.1.2.1.25.1\n"
                "rocommunity public 192.168.1.0/24\nrocommunity public 192.168.1.0/24\n")
        fixed = co.snmp_fix_linux(conf).splitlines()
        self.assertEqual(fixed[2:5], [f"view   systemonly  included   {o}"
                                      for o in (".1.3.6.1.2.1.2", ".1.3.6.1.2.1.25", ".1.3.6.1.4.1")])
        self.assertEqual(co.snmp_community_count("\n".join(fixed)), 1)
        self.assertEqual(co.snmp_fix_linux(co.snmp_fix_linux(conf)), co.snmp_fix_linux(conf))   # idempotent
        # .25.1 itself missing -> goes after the last systemonly view; other lines untouched
        f2 = co.snmp_fix_linux(UBUNTU_SNMP)
        self.assertIn(".1.3.6.1.4.1\nview   systemonly  included   .1.3.6.1.2.1.25.1\n", f2)
        self.assertIn("rocommunity nova_readonly 192.168.1.6", f2)
        self.assertTrue(co.snmp_fix_linux("agentaddress udp:161\n").endswith(co.SNMP_COMMUNITY_LINE + "\n"))

    def test_zshrc_colors_line(self):
        self.assertTrue(co.zshrc_has_colors_line(co.ZSH_COLORS_LINE))
        self.assertTrue(co.zshrc_has_colors_line("source $HOME/.nova-term-colors.zsh"))
        self.assertFalse(co.zshrc_has_colors_line("# " + co.ZSH_COLORS_LINE))
        self.assertFalse(co.zshrc_has_colors_line(""))
        self.assertFalse(co.zshrc_has_colors_line("export X=~/.nova-term-colors.zsh"))

    def test_known_hosts_compare(self):
        h = co.sha256_hex(b"host key\n")
        self.assertTrue(co.known_hosts_in_sync(h, f"{h}  /etc/ssh/ssh_known_hosts\n"))
        self.assertFalse(co.known_hosts_in_sync(h, "0" * 64 + "  /etc/ssh/ssh_known_hosts"))
        self.assertFalse(co.known_hosts_in_sync(h, ""))
        self.assertFalse(co.known_hosts_in_sync("", ""))

    def test_sshd_dropin_and_order(self):
        self.assertTrue(co.sshd_dropin_ok(co.SSHD_DROPIN_CONTENT))
        self.assertTrue(co.sshd_dropin_ok("# other comment\nPasswordAuthentication  no\n"
                                          "KbdInteractiveAuthentication no\nPermitRootLogin no\n"))
        self.assertFalse(co.sshd_dropin_ok("PasswordAuthentication no\nPermitRootLogin no\n"))
        self.assertTrue(co.dropin_sorts_first(["50-cloud-init.conf", "01-nova-nopassword.conf"]))
        self.assertTrue(co.dropin_sorts_first(["100-macos.conf", "01-nova-nopassword.conf", "x.bak-20261101"]))
        self.assertFalse(co.dropin_sorts_first(["00-early.conf", "01-nova-nopassword.conf"]))
        self.assertFalse(co.dropin_sorts_first([]))

    def test_sshd_effective(self):
        ok = "permitrootlogin no\npasswordauthentication no\nkbdinteractiveauthentication no\n"
        self.assertEqual(co.sshd_effective_bad(ok), [])
        self.assertEqual(co.sshd_effective_bad(ok.replace("kbdinteractiveauthentication no",
                                                          "kbdinteractiveauthentication yes")),
                         ["kbdinteractiveauthentication=yes"])
        self.assertEqual(len(co.sshd_effective_bad("")), 3)

    def test_fstab_and_symlinks(self):
        self.assertEqual(co.fstab_missing_mounts(GOOD_FSTAB), [])
        commented = GOOD_FSTAB.replace("//192.168.1.19/External", "#//192.168.1.19/External")
        self.assertEqual(co.fstab_missing_mounts(commented), ["/mnt/nas-external"])
        old_nas = GOOD_FSTAB.replace("//192.168.1.19/nas /mnt/nas", "//192.168.1.11/nas /mnt/nas")
        self.assertEqual(co.fstab_missing_mounts(old_nas), ["/mnt/nas"])
        self.assertEqual(co.symlink_state("/mnt/data\n", "/mnt/data"), "ok")
        self.assertEqual(co.symlink_state("ABSENT", "/mnt/data"), "fix")
        self.assertEqual(co.symlink_state("/elsewhere", "/mnt/data"), "fix")
        self.assertEqual(co.symlink_state("REALPATH", "/nova/scripts"), "blocked")

    def test_wazuh_mac_snmpd_os_helpers(self):
        self.assertTrue(co.wazuh_address_ok("<address>192.168.1.2</address>"))
        self.assertFalse(co.wazuh_address_ok("<address>192.168.1.7</address>"))
        self.assertFalse(co.wazuh_address_ok("<address>192.168.1.2</address><address>10.0.0.1</address>"))
        self.assertFalse(co.wazuh_address_ok(""))
        conf = co.mac_snmpd_conf("nova-core4")
        self.assertEqual(co.missing_lines(conf, conf), [])
        self.assertEqual(co.missing_lines(conf.replace("sysservices 72\n", ""), conf), ["sysservices 72"])
        self.assertIn("syslocation Nova cluster - nova-core4", conf)
        self.assertTrue(co.firewall_blocks_app("Firewall is enabled. (State = 1)", "is blocked"))
        self.assertFalse(co.firewall_blocks_app("Firewall is disabled. (State = 0)", ""))
        self.assertFalse(co.firewall_blocks_app("Firewall is enabled. (State = 1)",
                                                "Incoming connection to /usr/sbin/snmpd is permitted."))
        self.assertEqual(co.detect_os("Darwin\n", "linux"), "macos")
        self.assertEqual(co.detect_os("", "linux"), "linux")
        self.assertEqual(co.split_sections(f"a{co._SEP}b", 3), ["a", "b", ""])

    def test_backup_snippet_never_clobbers(self):
        s = co.backup_snippet("/etc/x")
        self.assertRegex(s, r"\[ ! -e /etc/x\.bak-\d{8} \]")
        self.assertIn("sudo -n cp -p", s)
        self.assertNotIn("sudo", co.backup_snippet("~/.zshrc", sudo=False))


class TestIntegration(unittest.TestCase):
    def test_new_recipes_dispatch(self):
        with patch.object(co, "ssh_exec", side_effect=_Ssh()):
            for r in ("nova_wazuh", "nova_zsh"):
                out = co._apply_recipe_shell("u", "10.0.0.9", "linux", r, True, node_name="n")[1]
                self.assertNotIn("no shell handler", out)
                self.assertIn("[drift]", out)

    def test_os_detection_overrides_registry(self):
        node = {"node_name": "nova-core4", "node_ip": "10.0.0.9", "os_family": "linux", "run_list": ["nova_linux"]}

        def ssh(user, host, command, timeout=60):
            return (0, "Darwin\n", "") if command == "uname -s" else (0, "", "")
        with patch.object(co, "ssh_exec", side_effect=ssh):
            res, lines = co._run_recipes(node, True)
        self.assertIn("host is macos", lines[0])
        self.assertTrue(any("nova_linux: 0 changes" in l for l in lines))   # linux recipe skipped on a Mac

    def test_registry_mismatch_is_drift_only_even_on_converge(self):
        node = {"node_name": "m", "node_ip": "10.0.0.9", "os_family": "linux", "run_list": ["nova_zsh", "nova_base"]}
        cmds = []

        def ssh(user, host, command, timeout=60):
            cmds.append(command)
            return (0, "Darwin\n", "") if command == "uname -s" else (0, "", "")
        with patch.object(co, "ssh_exec", side_effect=ssh), patch.object(co, "_push") as push:
            co._run_recipes(node, False)
        push.assert_not_called()
        self.assertEqual([c for c in cmds if MUTATING.search(c)], [])

    def test_known_hosts_source_is_repo_config(self):
        self.assertEqual(co.KNOWN_HOSTS_SRC, SCRIPTS.parent / "config" / "ssh_known_hosts")
        self.assertEqual(co.MACOS_CFG_DIR, SCRIPTS.parent / "config" / "macos")


class TestFunctional(unittest.TestCase):
    def test_zsh_converge_appends_line_without_overwriting_zshrc(self):
        sep = co._SEP
        probe_out = f"/bin/bash{sep}OMZ P10K_THEME P10K ZSHRC ZSH_BIN{sep}{'a' * 64}  f{sep}"
        with patch.object(co, "ssh_exec", side_effect=_Ssh(answer=probe_out)), \
                patch.object(co, "_read_source", return_value=None), \
                patch.object(co, "_push", return_value=(0, "", "")) as push:
            out = co._check_zsh("u", "10.0.0.9", "linux", False)
        appended = [c for c in push.call_args_list if "cat >> ~/.zshrc" in c[0][3]]
        self.assertEqual(len(appended), 1)
        self.assertIn(co.ZSH_COLORS_LINE, appended[0][0][2])
        self.assertNotIn("cat > ~/.zshrc", " ".join(c[0][3] for c in push.call_args_list))
        self.assertTrue(any(l.startswith("[changed] ~/.zshrc") for l in out))
        self.assertTrue(any(l.startswith("[changed] login shell") for l in out))

    def test_known_hosts_push_when_differs(self):
        src = TMP / "kh"
        src.write_bytes(b"k\n")
        with patch.object(co, "KNOWN_HOSTS_SRC", src), patch.object(co, "ssh_exec", side_effect=_Ssh(answer="")), \
                patch.object(co, "_push", return_value=(0, "", "")) as push:
            self.assertTrue(co._check_known_hosts("u", "10.0.0.9", "macos", True)[0].startswith("[drift]"))
            push.assert_not_called()
            out = co._check_known_hosts("u", "10.0.0.9", "macos", False)
        self.assertTrue(out[0].startswith("[changed]"))
        self.assertEqual(push.call_args[0][2], b"k\n")
        self.assertIn("sudo -n chmod 644", push.call_args[0][3])

    def test_error_path_unreadable_snmpd_conf_is_not_rewritten(self):
        with patch.object(co, "ssh_exec", return_value=(1, "", "sudo: a password is required")), \
                patch.object(co, "_push") as push:
            out = co._check_linux_snmp("u", "10.0.0.9", False)
        self.assertIn("not rewriting", out[0])
        push.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_and_import(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, "-c", "import nova_cinc_orchestrate"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
