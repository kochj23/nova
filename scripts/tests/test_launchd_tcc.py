#!/usr/bin/env python3
"""Studio launchd templates must not make a shell READ a script that lives on the NAS (2026-10-15).

Since the 2026-10-12 NAS cutover ~/.openclaw/scripts (and memory_server.py) resolve to /Volumes/nas,
and launchd's /bin/zsh|bash|sh cannot read files there under macOS TCC ("Operation not permitted",
exit 126/127). Python (which holds the grant) reading the file is fine, and so is a shell running an
inline `-c` command. nova-matview-refresh had been failing hourly since 10-12 this way.
"""
import plistlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "config" / "launchd" / "studio"
SHELLS = {"/bin/zsh", "/bin/bash", "/bin/sh", "/usr/bin/env"}
NAS_MARKERS = ("/.openclaw/scripts/", "/Volumes/nas/")
# Disabled and retired (old pre-scheduler boot orchestrator; 1000+ lines of zsh, hardcoded LAN IP).
# Re-enabling it would need a rewrite anyway, not a plist tweak.
EXEMPT = {"net.digitalnoise.nova-boot.plist"}


def _plists():
    return sorted(p for p in ROOT.glob("Launch*/*.plist") if p.name not in EXEMPT)


def shell_reads_nas_script(args):
    """True when launchd would hand a NAS-resident script FILE to a shell (or exec a NAS .sh directly)."""
    if not args:
        return False
    first = args[0]
    if first.endswith(".sh") and any(m in first for m in NAS_MARKERS):
        return True
    if first in SHELLS and len(args) > 1 and args[1] != "-c":
        return any(m in args[1] for m in NAS_MARKERS)
    return False


class TestUnit(unittest.TestCase):
    def test_detector(self):
        self.assertTrue(shell_reads_nas_script(["/bin/bash", "__HOME__/.openclaw/scripts/x.sh"]))
        self.assertTrue(shell_reads_nas_script(["__HOME__/.openclaw/scripts/postgres-wrapper.sh"]))
        self.assertFalse(shell_reads_nas_script(["/bin/zsh", "-c", "exec python3 __HOME__/.openclaw/scripts/x.py"]))
        self.assertFalse(shell_reads_nas_script(["/opt/homebrew/bin/python3", "__HOME__/.openclaw/memory_server.py"]))
        self.assertFalse(shell_reads_nas_script(["/bin/bash", "__HOME__/bin/local.sh"]))


class TestFunctional(unittest.TestCase):
    def test_templates_parse(self):
        for p in _plists():
            plistlib.loads(p.read_bytes())

    def test_no_template_has_a_shell_read_a_nas_script(self):
        bad = [p.name for p in _plists()
               if shell_reads_nas_script(plistlib.loads(p.read_bytes()).get("ProgramArguments", []))]
        self.assertEqual(bad, [])

    def test_matview_refresh_is_inline(self):
        d = plistlib.loads((ROOT / "LaunchAgents/net.digitalnoise.nova-matview-refresh.plist").read_bytes())
        self.assertEqual(d["ProgramArguments"][:2], ["/bin/sh", "-c"])
        self.assertIn("REFRESH MATERIALIZED VIEW", d["ProgramArguments"][2])

    def test_slack_preprocessor_token_not_in_plist(self):
        d = plistlib.loads((ROOT / "LaunchAgents/com.nova.slack-preprocessor.plist").read_bytes())
        self.assertNotIn("NOVA_SLACK_BOT_TOKEN", d.get("EnvironmentVariables", {}))
        self.assertIn("find-generic-password", d["ProgramArguments"][2])


if __name__ == "__main__":
    unittest.main()
