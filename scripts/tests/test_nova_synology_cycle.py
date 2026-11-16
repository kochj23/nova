"""Tests for nova_synology_cycle.py — no network, no PG: ssh/power/notify are patched."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_synology_cycle as c  # noqa: E402

MDSTAT_OK = "md2 : active raid6 sda5[0] sdb5[1]\n      [8/8] [UUUUUUUU]\n"
MDSTAT_SCRUB = MDSTAT_OK + "      [=>....]  resync = 7.1% (1/2) finish=9000min\n"


class MagicPacket(unittest.TestCase):
    def test_format(self):
        p = c.magic_packet("02-00-00-00-00-01".replace("-", ":"))
        self.assertEqual(len(p), 102)
        self.assertEqual(p[:6], b"\xff" * 6)
        self.assertEqual(p[6:12], bytes.fromhex("020000000001"))


class SafeToSleep(unittest.TestCase):
    def test_idle_ok(self):
        with patch.object(c, "ssh", return_value=(0, MDSTAT_OK)):
            self.assertEqual(c.safe_to_sleep(), (True, ""))

    def test_rsync_blocks(self):
        with patch.object(c, "ssh", return_value=(0, "RSYNC\n" + MDSTAT_OK)):
            self.assertFalse(c.safe_to_sleep()[0])

    def test_scrub_blocks(self):
        with patch.object(c, "ssh", return_value=(0, MDSTAT_SCRUB)):
            ok, why = c.safe_to_sleep()
            self.assertFalse(ok)
            self.assertIn("scrub", why)

    def test_unreachable_blocks(self):
        with patch.object(c, "ssh", return_value=(-1, "")):
            self.assertFalse(c.safe_to_sleep()[0])


class CloudState(unittest.TestCase):
    def test_parse(self):
        with patch.object(c, "ssh", return_value=(0, "-3\n1000\n1600\n")):
            self.assertEqual(c.cloud_state(), (-3, 600))

    def test_garbage_is_unknown(self):
        with patch.object(c, "ssh", return_value=(0, "x\n")):
            self.assertEqual(c.cloud_state(), (None, None))


class Flow(unittest.TestCase):
    def test_backup_failure_never_sleeps(self):
        with patch.object(c, "wake", return_value=True), \
             patch.object(c.power, "set_expected_off"), \
             patch.object(c, "backup", return_value=False), \
             patch.object(c, "notify"), \
             patch.object(c, "sleep_now") as sleep_now:
            self.assertEqual(c.main([]), 1)
            sleep_now.assert_not_called()

    def test_wake_failure_pages_critical(self):
        with patch.object(c, "wake", return_value=False), patch.object(c, "notify") as n:
            self.assertEqual(c.main([]), 1)
            self.assertEqual(n.call_args[0][2], "critical")

    def test_happy_path_sleeps(self):
        with patch.object(c, "wake", return_value=True), \
             patch.object(c.power, "set_expected_off"), \
             patch.object(c, "backup", return_value=True), \
             patch.object(c, "wait_cloud", return_value=True), \
             patch.object(c, "sleep_now") as sleep_now:
            self.assertEqual(c.main([]), 0)
            sleep_now.assert_called_once()

    def test_no_sleep_flag(self):
        with patch.object(c, "wake", return_value=True), \
             patch.object(c.power, "set_expected_off"), \
             patch.object(c, "backup", return_value=True), \
             patch.object(c, "wait_cloud", return_value=True), \
             patch.object(c, "sleep_now") as sleep_now:
            c.main(["--no-sleep"])
            sleep_now.assert_not_called()

    def test_sleep_refused_keeps_flag_clear(self):
        with patch.object(c, "safe_to_sleep", return_value=(False, "rsync still running")), \
             patch.object(c, "notify"), patch.object(c.power, "set_expected_off") as flag:
            self.assertFalse(c.sleep_now(False))
            flag.assert_not_called()


class Churn(unittest.TestCase):
    CFG = {"soft_deletes": True, "churn_prefixes": {"nas": ["frigate/"]}}

    def _cfg(self, k, d=None):
        return self.CFG.get(k, d)

    def test_backup_passes_churn_and_purges(self):
        run = MagicMock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
        with patch.object(c, "cfg", side_effect=self._cfg), patch.object(c.subprocess, "run", run), \
             patch.object(c, "ssh", return_value=(0, "")) as ssh:
            self.assertTrue(c.backup(False))
            nas_cmd = run.call_args_list[0][0][0]
            self.assertIn("--soft-deletes", nas_cmd)
            self.assertEqual(nas_cmd[nas_cmd.index("--churn-prefix") + 1], "frigate/")
            self.assertEqual(ssh.call_count, 1)                 # only nas has churn
            self.assertIn("/volume1/nas/#nova_deleted/", ssh.call_args[0][0])
            self.assertTrue(ssh.call_args[0][0].rstrip("'").endswith("/frigate"))

    def test_purge_rejects_traversal_and_empty(self):
        with patch.object(c, "ssh") as ssh:
            c.purge_churn_now("nas", ["../etc", "a/../b", "/", ""], False)
            ssh.assert_not_called()

    def test_failed_share_is_not_purged(self):
        run = MagicMock(return_value=Mock(returncode=1, stdout="", stderr="x"))
        with patch.object(c, "cfg", side_effect=self._cfg), patch.object(c.subprocess, "run", run), \
             patch.object(c, "ssh") as ssh:
            self.assertFalse(c.backup(False))
            ssh.assert_not_called()

    def test_soft_off_means_no_delete_flags(self):
        run = MagicMock(return_value=Mock(returncode=0, stdout="", stderr=""))
        with patch.object(c, "cfg", side_effect=lambda k, d=None: d), \
             patch.object(c.subprocess, "run", run), patch.object(c, "ssh") as ssh:
            c.backup(False)
            self.assertNotIn("--soft-deletes", run.call_args_list[0][0][0])
            ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
