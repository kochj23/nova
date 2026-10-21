"""
test_githooks_active.py — the secret scanner must never be silently inert.

Why this file exists
--------------------
On 2026-10-21 the git hooks stopped running on the macOS nodes while *looking*
perfectly healthy. `ls -l` showed `-rwx------`, `test -x` returned true, bash could
execute them — and yet `git commit` printed

    hint: The 'githooks/pre-commit' hook was ignored because it's not set as executable.

and committed a planted secret anyway.

Root cause (measured, not guessed): the working tree is an SMB share
(//192.168.1.69/nas/nova-fs — /Volumes/nas/nova-fs on the Macs, /nova on the Linux
nodes). On macOS, `access(path, X_OK)` on that mount is driven by the server's ACL
mapping rather than the mode bits, so it disagrees with them: sampling the three hook
files 20 times in a row returned mode 0700 with X_OK False for one of them, and which
file is affected drifts over time. The Linux clients see the same files as 0755 /
X_OK True. git checks exactly access(X_OK), so on the Macs the hooks went inert at
random — with nothing but a hint to say so.

The fix: core.hooksPath points at /usr/local/share/nova-githooks, a LOCAL directory on
each node holding tiny executable stubs. Local disk honours the exec bit. Each stub runs
the real, shared hook body as an ARGUMENT to bash (`exec bash "$ROOT/githooks/<name>"`),
which needs only READ permission — so git's access(X_OK) check never touches the SMB
share. One shared body on the NAS (untracked and gitignored, as intended), ten tiny
local stubs that essentially never change.

This test exists because that whole arrangement fails *quietly*. A scanner that has
stopped scanning reports nothing at all, which is the worst failure mode for security
tooling, so it is turned into a red test here.
"""
import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BLOCKING_HOOKS = ("pre-commit", "pre-push")   # these must be able to fail a commit/push
ALL_HOOKS = ("pre-commit", "pre-push", "post-commit")


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True).stdout.strip()


class TestGitHooksActive(unittest.TestCase):
    def setUp(self):
        self.hooks_path = _git("config", "--get", "core.hooksPath")
        if not self.hooks_path:
            self.skipTest("core.hooksPath is not set in this clone")
        self.stub_dir = Path(self.hooks_path) if os.path.isabs(self.hooks_path) \
            else (REPO / self.hooks_path)
        self.shared_dir = REPO / "githooks"

    def test_stub_directory_is_installed_on_this_node(self):
        """core.hooksPath lives in .git/config, which is ONE file shared by all ten nodes.
        An absolute path is therefore only safe if it exists on every node — if this node
        is missing it, git silently runs no hooks at all here."""
        self.assertTrue(
            self.stub_dir.is_dir(),
            f"core.hooksPath={self.hooks_path!r} but {self.stub_dir} does not exist on "
            f"{os.uname().nodename}. git is running NO hooks on this node. Install the "
            f"stubs here (see this module's docstring).",
        )

    def test_stubs_are_executable(self):
        """The exact check git makes. These live on local disk precisely so this is
        reliable; if it ever fails here, the stub was installed wrong."""
        for name in BLOCKING_HOOKS:
            stub = self.stub_dir / name
            with self.subTest(hook=name):
                self.assertTrue(stub.is_file(), f"{stub} is missing — scanner absent on this node")
                self.assertTrue(
                    os.access(stub, os.X_OK),
                    f"{stub} is not executable, so git will ignore it with only a hint and "
                    f"commit unscanned. Fix: sudo chmod 755 {stub}",
                )

    def test_stubs_delegate_to_the_shared_body(self):
        """A stub that stopped delegating would freeze hook logic at install time and drift
        away from the shared body every other node runs."""
        for name in BLOCKING_HOOKS:
            stub = self.stub_dir / name
            if not stub.is_file():
                continue
            with self.subTest(hook=name):
                self.assertIn("githooks/" + name, stub.read_text(),
                              f"{stub} no longer delegates to the shared githooks/{name}")

    def test_shared_hook_bodies_are_readable(self):
        """The stubs run these with `bash <file>`, so READ is all that is required — and
        read is the one thing the SMB mount reports reliably."""
        for name in BLOCKING_HOOKS:
            body = self.shared_dir / name
            with self.subTest(hook=name):
                self.assertTrue(body.is_file(), f"{body} is missing — the scanner has no body")
                self.assertTrue(os.access(body, os.R_OK), f"{body} is not readable")

    def test_shared_hook_bodies_are_valid_shell(self):
        """A syntax error can make a hook fail open; catch it here rather than at a commit
        that needed blocking."""
        for name in BLOCKING_HOOKS:
            body = self.shared_dir / name
            if not body.is_file():
                continue
            with self.subTest(hook=name):
                r = subprocess.run(["bash", "-n", str(body)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{body} is not valid bash: {r.stderr.strip()}")

    def test_hooks_load_patterns_from_the_store(self):
        """Sensitive patterns must be fetched at runtime, never inlined. Hardcoding them is
        exactly what leaked the credential in the first place."""
        for name in BLOCKING_HOOKS:
            body = self.shared_dir / name
            if not body.is_file():
                continue
            with self.subTest(hook=name):
                self.assertIn(
                    "nova_secret_patterns", body.read_text(),
                    f"{body} does not load patterns from the secret store — it may have been "
                    f"reverted to inline literals, which re-leaks the secret it detects.",
                )

    def test_hook_files_contain_no_known_credentials(self):
        """Belt and braces. Skips loudly if the pattern store is unreachable."""
        import sys
        sys.path.insert(0, str(REPO / "scripts"))
        from nova_secret_patterns import assert_no_known_credentials
        for d in (self.shared_dir, self.stub_dir):
            for name in ALL_HOOKS:
                f = d / name
                if not f.is_file():
                    continue
                with self.subTest(hook=str(f)):
                    assert_no_known_credentials(self, f.read_text(), str(f))


if __name__ == "__main__":
    unittest.main()
