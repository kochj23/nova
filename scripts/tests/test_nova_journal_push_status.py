#!/usr/bin/env python3
"""git_push failure classification + return status (2026-10-05 nova-core incident: an ssh timeout was
reported as a rebase conflict and the caller logged PUBLISHED). Written by Jordan Koch (via Claude).
All git is faked through subprocess.run; nothing touches the network, PG or Slack."""
import importlib.util
import io
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401  (real module locked in before any stubbing)

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
TMP = Path(tempfile.mkdtemp(prefix="nj_push_status_"))

SSH_TIMEOUT = ("ssh: connect to host github.com port 22: Connection timed out\n"
               "fatal: Could not read from remote repository.\n\n"
               "Please make sure you have the correct access rights\nand the repository exists.\n")


def _stubs():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    iu = types.ModuleType("nova_image_utils"); iu.generate_image = MagicMock(return_value=None)
    oc = types.ModuleType("nova_ops_context")
    oc.get_full_context = lambda hours=24: {}; oc.format_security_brief = lambda c: ""; oc.format_infra_brief = lambda c: ""
    rs = types.ModuleType("nova_resolve"); rs.resolve_url = lambda svc, path="": f"http://127.0.0.1:0{path}"
    return {"nova_notify": nn, "nova_image_utils": iu, "nova_ops_context": oc, "nova_resolve": rs}


def _load():
    spec = importlib.util.spec_from_file_location("nj_push_status", SCRIPTS / "nova_journal.py")
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs()), patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")), patch("subprocess.Popen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


nj = _load()
nj.LOG_FILE = TMP / "nova_journal.log"
nj.HUGO_ROOT = TMP / "nova-journal"
(nj.HUGO_ROOT / ".git").mkdir(parents=True, exist_ok=True)


def _runner(push=(), pull=None):
    """push: list of (rc, stderr) or an exception per push attempt; pull: (rc, stderr) or exception."""
    calls, pushes = [], list(push)

    def run(argv, **kw):
        calls.append(list(argv))
        if argv[:2] == ["git", "push"]:
            r = pushes.pop(0) if pushes else (0, "")
            if isinstance(r, BaseException):
                raise r
            return subprocess.CompletedProcess(argv, r[0], stdout="", stderr=r[1])
        if argv[:2] == ["git", "pull"]:
            if isinstance(pull, BaseException):
                raise pull
            rc, err = pull or (0, "")
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr=err)
        if argv[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(argv, 0, stdout="1\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return run, calls


class TestClassifier(unittest.TestCase):
    def test_real_network_strings(self):
        for s in (SSH_TIMEOUT,
                  "ssh: connect to host github.com port 22: Connection timed out",
                  "fatal: unable to access 'https://github.com/kochj23/nova-journal.git/': Could not resolve host: github.com",
                  "ssh: connect to host github.com port 22: Connection refused",
                  "Connection reset by 140.82.112.4 port 22",
                  "fatal: could not read Username for 'https://github.com': Device not configured",
                  "git push timed out after 180s"):
            self.assertEqual(nj.classify_git_failure(s), "network", s)

    def test_non_ff_and_conflict(self):
        nff = (" ! [rejected]        main -> main (fetch first)\nerror: failed to push some refs\n"
               "hint: Updates were rejected because the remote contains work that you do not have locally.")
        self.assertEqual(nj.classify_git_failure(nff), "non_fast_forward")
        self.assertEqual(nj.classify_git_failure(
            "CONFLICT (content): Merge conflict in content/operations/x.md\nerror: could not apply abc123"), "conflict")
        self.assertEqual(nj.classify_git_failure("rejected"), "other")
        self.assertEqual(nj.classify_git_failure(""), "other")

    def test_published_label(self):
        self.assertEqual(nj.published_label(nj.PUSH_PUSHED), "PUBLISHED")
        self.assertEqual(nj.published_label(nj.PUSH_NOT_PUSHED), "COMMITTED (not yet pushed)")
        self.assertEqual(nj.published_label(None), "PUBLISHED")


class TestGitPushStatus(unittest.TestCase):
    def _push(self, run, resolve_rv=False):
        notify = types.ModuleType("nova_notify"); notify.notify = MagicMock(return_value=True)
        out = io.StringIO()
        with patch.dict(sys.modules, {"nova_notify": notify}), patch.object(nj, "_acquire_push_lock", return_value=None), \
             patch.object(nj, "_resolve_rolling_conflicts", return_value=resolve_rv) as resolve, patch.object(nj, "_unwedge") as unwedge, \
             patch("subprocess.run", side_effect=run), redirect_stdout(out):
            st = nj.git_push("operations", "The Heartbeat")
        return st, out.getvalue(), resolve, unwedge, notify.notify

    def test_network_push_failure_skips_rebase_and_reports_not_pushed(self):
        run, calls = _runner(push=[(128, SSH_TIMEOUT)])
        st, out, resolve, unwedge, notify = self._push(run)
        self.assertEqual(st, nj.PUSH_NOT_PUSHED)
        self.assertNotIn(["git", "pull", "--rebase"], calls)
        resolve.assert_not_called(); unwedge.assert_not_called()
        self.assertIn("push failed (network)", out)
        self.assertIn("NOT published", out)
        self.assertNotIn("conflict", out.lower())
        self.assertEqual(notify.call_args.kwargs["dedup_key"], "journal-push-failing")

    def test_push_timeout_is_network_not_generic_git_error(self):
        run, calls = _runner(push=[subprocess.TimeoutExpired(["git", "push"], 180)])
        st, out, resolve, unwedge, notify = self._push(run)
        self.assertEqual(st, nj.PUSH_NOT_PUSHED)
        self.assertNotIn("Git error", out)
        self.assertIn("push failed (network)", out)
        resolve.assert_not_called()
        notify.assert_called_once()

    def test_non_ff_then_network_pull_is_not_called_a_conflict(self):
        # the exact 2026-10-05 sequence, but with the first push looking like a rejection
        run, calls = _runner(push=[(1, "rejected")], pull=(1, SSH_TIMEOUT))
        st, out, resolve, unwedge, notify = self._push(run)
        self.assertEqual(st, nj.PUSH_NOT_PUSHED)
        resolve.assert_not_called(); unwedge.assert_not_called()
        self.assertNotIn("conflict needing a human", out)
        self.assertIn("push failed (network)", out)

    def test_success_and_rebase_retry_return_pushed(self):
        st, out, *_ = self._push(_runner()[0])
        self.assertEqual(st, nj.PUSH_PUSHED)
        st, out, *_ = self._push(_runner(push=[(1, "! [rejected] main -> main (fetch first)"), (0, "")])[0])
        self.assertEqual(st, nj.PUSH_PUSHED)
        self.assertIn("Pushed to GitHub after rebase", out)

    def test_real_conflict_still_goes_through_resolution(self):
        run, calls = _runner(push=[(1, "rejected")], pull=(1, "CONFLICT (content): Merge conflict in content/x.md"))
        # clean at entry; mid-rebase once the pull has stopped on the conflict
        with patch.object(nj, "_repo_wedged", side_effect=["", "rebase in progress", "rebase in progress", "rebase in progress"]):
            st, out, resolve, unwedge, notify = self._push(run)
        self.assertEqual(st, nj.PUSH_NOT_PUSHED)
        resolve.assert_called_once()
        unwedge.assert_called_once_with("pull --rebase conflict needing a human")

    def test_nothing_to_commit(self):
        def run(argv, **kw):
            if argv[:2] == ["git", "commit"]:
                return subprocess.CompletedProcess(argv, 1, stdout="nothing to commit, working tree clean", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        st, *_ = self._push(run)
        self.assertEqual(st, nj.PUSH_NOTHING)


if __name__ == "__main__":
    unittest.main()
