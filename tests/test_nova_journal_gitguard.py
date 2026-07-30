"""
test_nova_journal_gitguard.py — All 7 test categories for nova_journal.py
Written by Jordan Koch.

Scope: the git-safety layer added on 2026-07-30 — ROLLING_PATHS, _git,
_repo_wedged, _unwedge, _resolve_rolling_conflicts and the rewritten git_push.

THE REGRESSION THIS PINS DOWN. nova-core's nova-journal clone was left mid-rebase
by an unattended `git pull --rebase` that hit a conflict on the rolling fishbowl
file. HEAD went detached and stayed there. The next eight article jobs each ran
`git add -A` (happily staging conflict markers) and committed onto that detached
HEAD, every one of them exiting 0, for about three hours. Nothing reached the site
and nothing complained. So the tests below care about three things above all:
  * a wedged repo is detected BEFORE anything is committed,
  * repair NEVER discards a commit (rescue branch first, abort second),
  * a conflict on a real article is refused, not auto-resolved.

HARD SAFETY (nothing here may touch the real world):
  * nova_voice / nova_config / nova_notify / nova_image_utils / nova_resolve /
    nova_ops_context / psycopg2 are stubbed BEFORE load — no DB, no network, no
    Keychain, no LLM, no Slack.
  * HUGO_ROOT is repointed at a throwaway temp clone for every single test, so the
    real ~/nova-journal repo is never opened, let alone committed to or pushed.
  * log() is replaced with a recorder — the real journal log is never written.
  * notify() is a MagicMock — the alert bus is never touched.

WHY REAL GIT. The conflict paths are exercised against genuine temporary repos
(a bare origin, a seed clone, and the clone under test) built with subprocess +
tempfile. Mocking git here would only prove the mock agrees with itself; the whole
bug was about what git actually does to HEAD, the index and refs/heads/main when a
rebase stops. Repos are created with an empty --template and a hooks-free
core.hooksPath so the user's global git hooks never fire.
"""

import ast
import inspect
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

# ---------------------------------------------------------------------------
# Stub every heavy dependency before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_journal.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_notify_stub = MagicMock()
_notify_stub.notify = MagicMock(return_value=None)
sys.modules["nova_notify"] = _notify_stub

for _name in ("nova_voice", "nova_config", "nova_image_utils", "nova_resolve",
              "nova_ops_context", "psycopg2"):
    sys.modules.setdefault(_name, MagicMock())
sys.modules["nova_config"] = MagicMock()

_mod = load_script_compat(_SCRIPT, "nova_journal")

_SRC = _SCRIPT.read_text()
_TREE = ast.parse(_SRC)

ROLLING_MD = "content/fishbowl/the-fishbowl.md"
ROLLING_IMG = "static/images/fishbowl/the-fishbowl.webp"
ARTICLE = "content/operations/2026-07-29-ops-column.md"

NEW_FUNCS = ("_git", "_repo_wedged", "_unwedge", "_resolve_rolling_conflicts",
             "git_push")


def _fn(name):
    """Return the ast.FunctionDef for a top-level function."""
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in nova_journal.py")


# ---------------------------------------------------------------------------
# Base case — a real bare origin + a real clone, per test
# ---------------------------------------------------------------------------
class _JournalRepoCase(unittest.TestCase):
    """Builds origin.git / seed / clone in a temp dir and points HUGO_ROOT at clone."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nova-journal-gitguard-"))
        self.template = self.root / "empty-template"   # defeats global git hooks
        self.template.mkdir()

        self.origin = self.root / "origin.git"
        self._raw(["git", "init", "--bare", "-b", "main",
                   f"--template={self.template}", str(self.origin)], self.root)

        self.seed = self._make_clone("seed")
        self._write(self.seed, ROLLING_MD, "BASE fishbowl\n")
        self._write(self.seed, ROLLING_IMG, "BASE image bytes\n")
        self._write(self.seed, ARTICLE, "BASE ops column\n")
        self._raw(["git", "add", "-A"], self.seed)
        self._raw(["git", "commit", "-m", "base"], self.seed)
        self._raw(["git", "push", "-u", "origin", "main"], self.seed)

        self.clone = self._make_clone("clone")

        self.logs = []
        self._patches = [
            patch.object(_mod, "HUGO_ROOT", self.clone),
            patch.object(_mod, "log", lambda m: self.logs.append(str(m))),
        ]
        for p in self._patches:
            p.start()
        _notify_stub.notify.reset_mock()
        _notify_stub.notify.side_effect = None

    def tearDown(self):
        for p in self._patches:
            p.stop()
        _notify_stub.notify.side_effect = None
        shutil.rmtree(self.root, ignore_errors=True)

    # -- plumbing ----------------------------------------------------------
    def _raw(self, args, cwd, check=True):
        r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                           timeout=90)
        if check and r.returncode != 0:
            raise AssertionError(f"{args} in {cwd} -> {r.returncode}\n"
                                 f"{r.stdout}\n{r.stderr}")
        return r

    def _make_clone(self, name):
        dest = self.root / name
        self._raw(["git", "clone", f"--template={self.template}",
                   str(self.origin), str(dest)], self.root)
        for k, v in (("user.email", "tests@example.invalid"),
                     ("user.name", "Nova Test"),
                     ("commit.gpgsign", "false"),
                     ("core.hooksPath", str(self.template))):
            self._raw(["git", "config", k, v], dest)
        return dest

    @staticmethod
    def _write(repo, rel, text):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    # -- test-facing helpers ----------------------------------------------
    def git(self, *args, repo=None, check=True):
        return self._raw(["git", *args], repo or self.clone, check=check)

    def write(self, rel, text, repo=None):
        return self._write(repo or self.clone, rel, text)

    def read(self, rel, repo=None):
        return (repo or self.clone).joinpath(rel).read_text()

    def head(self, ref="HEAD", repo=None):
        return self.git("rev-parse", ref, repo=repo).stdout.strip()

    def detached(self, repo=None):
        return self.git("symbolic-ref", "-q", "HEAD", repo=repo,
                        check=False).returncode != 0

    def branch(self, repo=None):
        return self.git("rev-parse", "--abbrev-ref", "HEAD",
                        repo=repo).stdout.strip()

    def rescue_branches(self):
        out = self.git("for-each-ref", "--format=%(refname:short)",
                       "refs/heads/").stdout.split()
        return [b for b in out if b.startswith("rescue-")]

    def diverge(self, files):
        """files = {relpath: (origin_text, local_text)} — commit both sides."""
        for rel, (origin_text, _) in files.items():
            self._write(self.seed, rel, origin_text)
        self._raw(["git", "add", "-A"], self.seed)
        self._raw(["git", "commit", "-m", "origin side"], self.seed)
        self._raw(["git", "push"], self.seed)
        for rel, (_, local_text) in files.items():
            self._write(self.clone, rel, local_text)
        self.git("add", "-A")
        self.git("commit", "-m", "local side")

    def start_conflict(self, files):
        """Diverge, then really run `git pull --rebase` so the repo really wedges."""
        self.diverge(files)
        pull = self.git("pull", "--rebase", check=False)
        self.assertNotEqual(pull.returncode, 0,
                            "test setup expected a real rebase conflict")
        return pull

    def rolling_conflict(self):
        return self.start_conflict(
            {ROLLING_MD: ("ORIGIN OLDER COPY\n", "LOCAL FRESH REGENERATION\n")})

    def stranded_commits(self, n=3):
        """Reproduce production: wedge, then commit n times onto the detached HEAD."""
        self.rolling_conflict()
        shas = []
        for i in range(n):
            self.write(f"content/operations/stranded-{i}.md", f"stranded {i}\n")
            self.git("add", "-A")                       # stages conflict markers too
            self.git("commit", "-m", f"operations: stranded {i}")
            shas.append(self.head())
        return shas


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurityUnwedgeNeverLosesCommits(_JournalRepoCase):
    """Repair is only acceptable if it cannot destroy work. Rescue branch FIRST."""

    def test_rescue_branch_pins_the_exact_pre_repair_head(self):
        self.stranded_commits(3)
        before = self.head()
        self.assertTrue(_mod._unwedge("rebase in progress"))
        rescues = self.rescue_branches()
        self.assertEqual(len(rescues), 1, f"expected one rescue branch, got {rescues}")
        self.assertEqual(self.head(rescues[0]), before,
                         "rescue branch must point at the pre-repair HEAD")

    def test_rescue_branch_is_named_for_the_day_and_the_sha(self):
        self.stranded_commits(1)
        before = self.head()
        _mod._unwedge("rebase in progress")
        self.assertIn(f"rescue-{_mod.today_str()}-{before[:12]}", self.rescue_branches())

    def test_every_stranded_commit_survives_the_abort(self):
        shas = self.stranded_commits(4)
        _mod._unwedge("rebase in progress")
        rescue = self.rescue_branches()[0]
        reachable = set(self.git("rev-list", rescue).stdout.split())
        for sha in shas:
            self.assertIn(sha, reachable,
                          "a commit was discarded by the repair — unacceptable")
            self.assertEqual(
                self.git("cat-file", "-e", sha, check=False).returncode, 0,
                "commit object no longer exists after repair")

    def test_stranded_commits_are_counted_for_the_operator(self):
        self.stranded_commits(3)
        _mod._unwedge("rebase in progress")
        rescue = self.rescue_branches()[0]
        count = self.git("rev-list", "--count", f"origin/main..{rescue}").stdout.strip()
        self.assertEqual(count, "3",
                         "the rescue branch must account for all 3 commits that were "
                         "made onto the detached HEAD and would otherwise vanish")

    def test_pin_happens_before_any_abort(self):
        """Source order matters: `git branch` must precede every --abort call."""
        src = ast.get_source_segment(_SRC, _fn("_unwedge"))
        self.assertLess(src.index('"branch"'), src.index("--abort"),
                        "the rescue branch must be created BEFORE anything is aborted")

    def test_unwedge_returns_to_a_branch_and_clears_the_wedge(self):
        self.stranded_commits(2)
        self.assertTrue(self.detached())
        self.assertTrue(_mod._unwedge("rebase in progress"))
        self.assertFalse(self.detached())
        self.assertEqual(self.branch(), "main")
        self.assertEqual(_mod._repo_wedged(), "")

    def test_unwedge_reports_failure_when_the_repo_cannot_be_repaired(self):
        """No branch to return to: repair must report False, not pretend success."""
        self.rolling_conflict()
        self.git("rebase", "--abort")
        self.git("checkout", "--detach", "HEAD")
        self.git("branch", "-D", "main")
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertFalse(_mod._unwedge("detached HEAD"))
        self.assertTrue(self.detached())

    def test_failed_repair_still_pinned_the_commits(self):
        self.rolling_conflict()
        self.git("rebase", "--abort")
        self.git("checkout", "--detach", "HEAD")
        before = self.head()
        self.git("branch", "-D", "main")
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        _mod._unwedge("detached HEAD")
        self.assertEqual(len(self.rescue_branches()), 1)
        self.assertEqual(self.head(self.rescue_branches()[0]), before)

    def test_unwedge_notifies_a_human_at_warning_level(self):
        self.stranded_commits(2)
        _mod._unwedge("rebase in progress")
        _notify_stub.notify.assert_called()
        kwargs = _notify_stub.notify.call_args.kwargs
        self.assertEqual(kwargs["level"], "warning")
        self.assertEqual(kwargs["category"], "journal")
        self.assertEqual(kwargs["source"], "nova_journal.py")
        self.assertIn("rescue-", kwargs["body"])
        self.assertIn("nothing discarded", kwargs["body"])


class TestSecurityConflictResolution(_JournalRepoCase):
    """Auto-resolution is allowed for rolling files only, and never leaks markers."""

    def test_resolved_file_keeps_the_local_fresh_copy(self):
        self.rolling_conflict()
        self.assertTrue(_mod._resolve_rolling_conflicts())
        self.assertEqual(self.read(ROLLING_MD), "LOCAL FRESH REGENERATION\n",
                         "--theirs during a rebase is the LOCAL commit; taking "
                         "origin's older copy would discard the fresh article")

    def test_resolved_file_is_never_origins_stale_copy(self):
        self.rolling_conflict()
        _mod._resolve_rolling_conflicts()
        self.assertNotIn("ORIGIN OLDER COPY", self.read(ROLLING_MD))

    def test_no_conflict_markers_survive_in_a_resolved_markdown_file(self):
        self.rolling_conflict()
        self.assertIn("<<<<<<<", self.read(ROLLING_MD), "setup sanity")
        self.assertTrue(_mod._resolve_rolling_conflicts())
        text = self.read(ROLLING_MD)
        for marker in ("<<<<<<<", "=======", ">>>>>>>", "|||||||"):
            self.assertNotIn(marker, text, f"{marker} survived into a published file")

    def test_resolver_bails_out_when_markers_would_survive(self):
        """If the checkout silently no-ops, the marker assertion must catch it."""
        self.rolling_conflict()
        real_git = _mod._git

        def crippled(args, timeout=60):
            if list(args[:2]) == ["checkout", "--theirs"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_git(args, timeout=timeout)

        with patch.object(_mod, "_git", crippled):
            self.assertFalse(_mod._resolve_rolling_conflicts(),
                             "markers still in the file — must not report success")

    def test_a_real_article_conflict_is_refused(self):
        self.start_conflict({ARTICLE: ("ORIGIN column\n", "LOCAL column\n")})
        self.assertFalse(_mod._resolve_rolling_conflicts(),
                         "a genuine article must never be auto-resolved")
        self.assertIn("<<<<<<<", self.read(ARTICLE),
                      "the refused conflict must be left untouched for a human")

    def test_mixed_conflict_is_refused_even_though_a_rolling_file_is_involved(self):
        self.start_conflict({
            ROLLING_MD: ("ORIGIN fishbowl\n", "LOCAL fishbowl\n"),
            ARTICLE: ("ORIGIN column\n", "LOCAL column\n"),
        })
        self.assertFalse(_mod._resolve_rolling_conflicts())
        self.assertIn("<<<<<<<", self.read(ARTICLE))

    def test_refusal_is_logged_with_the_offending_paths(self):
        self.start_conflict({ARTICLE: ("ORIGIN column\n", "LOCAL column\n")})
        _mod._resolve_rolling_conflicts()
        self.assertTrue(any("not auto-resolving" in m for m in self.logs), self.logs)
        self.assertTrue(any(ARTICLE in m for m in self.logs), self.logs)


class TestSecuritySource(unittest.TestCase):
    """Static properties of the new code."""

    def test_no_hardcoded_credentials(self):
        for pat in (r"xox[baprs]-\d{5,}", r"\bsk-[A-Za-z0-9]{20,}",
                    r"\bghp_[A-Za-z0-9]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
                    r"(?i)password\s*=\s*['\"][^'\"]{4,}",
                    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"):
            self.assertIsNone(re.search(pat, _SRC),
                              f"possible hardcoded credential matching {pat!r}")

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC,
                         "use Path.home(), never a literal home path")

    def test_new_code_never_uses_a_shell(self):
        for name in NEW_FUNCS:
            src = ast.get_source_segment(_SRC, _fn(name))
            self.assertNotIn("shell=True", src, f"{name} must not use a shell")
            self.assertNotIn("os.system", src, f"{name} must not use os.system")

    def test_git_is_always_invoked_as_an_argv_list(self):
        node = _fn("_git")
        call = next(n for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "run")
        self.assertIsInstance(call.args[0], ast.List,
                              "_git must pass an argv list, never a string")

    def test_force_push_and_hard_reset_are_never_used(self):
        for name in NEW_FUNCS:
            src = ast.get_source_segment(_SRC, _fn(name))
            for danger in ("--force", "-f", "--hard", "reflog expire", "gc --prune"):
                self.assertNotIn(f'"{danger}"', src,
                                 f"{name} must not use {danger} — it destroys commits")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(_JournalRepoCase):

    def test_repo_wedged_makes_at_most_one_git_subprocess(self):
        calls = []
        real_git = _mod._git

        def counting(args, timeout=60):
            calls.append(list(args))
            return real_git(args, timeout=timeout)

        with patch.object(_mod, "_git", counting):
            self.assertEqual(_mod._repo_wedged(), "")
        self.assertLessEqual(len(calls), 1,
                             f"the fast path must be filesystem checks, got {calls}")

    def test_repo_wedged_short_circuits_before_spawning_git(self):
        (self.clone / ".git" / "rebase-merge").mkdir()
        calls = []
        with patch.object(_mod, "_git", lambda a, timeout=60: calls.append(a)):
            self.assertEqual(_mod._repo_wedged(), "rebase in progress")
        self.assertEqual(calls, [], "a marker directory needs no subprocess at all")

    def test_repo_wedged_does_no_network(self):
        src = ast.get_source_segment(_SRC, _fn("_repo_wedged"))
        for verb in ("fetch", "pull", "push", "ls-remote", "clone", "urlopen",
                     "requests", "http"):
            self.assertNotIn(verb, src,
                             f"_repo_wedged must never talk to a remote ({verb})")

    def test_repo_wedged_is_fast_on_a_repo_with_many_files(self):
        big = self.clone / "content" / "bulk"
        big.mkdir(parents=True)
        for i in range(400):
            (big / f"post-{i}.md").write_text("x" * 200)
        started = time.time()
        self.assertEqual(_mod._repo_wedged(), "")
        self.assertLess(time.time() - started, 3.0,
                        "_repo_wedged must not scale with working-tree size")

    def test_rolling_paths_is_a_small_constant_set(self):
        self.assertIsInstance(_mod.ROLLING_PATHS, tuple)
        self.assertGreater(len(_mod.ROLLING_PATHS), 0)
        self.assertLessEqual(len(_mod.ROLLING_PATHS), 16,
                             "membership cost is O(len(ROLLING_PATHS)) — keep it tiny")

    def test_rolling_paths_membership_is_independent_of_repo_size(self):
        """The allow-list is checked against conflicted paths only, never the tree."""
        started = time.time()
        for _ in range(200_000):
            _ = ARTICLE in _mod.ROLLING_PATHS
        self.assertLess(time.time() - started, 2.0)

    def test_resolver_only_asks_git_for_conflicted_paths(self):
        calls = []
        with patch.object(_mod, "_git",
                          lambda a, timeout=60: calls.append(list(a)) or
                          SimpleNamespace(returncode=0, stdout="", stderr="")):
            _mod._resolve_rolling_conflicts()
        # Assert the REQUIREMENT (ask git only for unmerged paths) rather than a
        # literal argv, so a safe refinement like -z/NUL-splitting doesn't fail a
        # test whose intent it satisfies.
        self.assertEqual(calls[0][:3], ["diff", "--name-only", "--diff-filter=U"],
                         "must ask only for unmerged paths, not walk the tree")
        self.assertTrue(set(calls[0][3:]) <= {"-z"},
                        f"unexpected extra git args: {calls[0][3:]}")
        self.assertEqual(len(calls), 1, "no conflicts means no further git work")

    def test_git_helper_has_a_bounded_default_timeout(self):
        default = inspect.signature(_mod._git).parameters["timeout"].default
        self.assertIsInstance(default, (int, float))
        self.assertGreater(default, 0)
        self.assertLessEqual(default, 300)

    def test_every_git_subprocess_in_the_new_code_passes_a_timeout(self):
        for name in NEW_FUNCS:
            for node in ast.walk(_fn(name)):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "run"):
                    kws = {k.arg for k in node.keywords}
                    self.assertIn("timeout", kws,
                                  f"{name} line {node.lineno}: subprocess.run with "
                                  f"no timeout can hang the whole job forever")

    def test_declared_timeouts_are_finite_and_sane(self):
        for name in NEW_FUNCS:
            for node in ast.walk(_fn(name)):
                if isinstance(node, ast.keyword) and node.arg == "timeout":
                    if isinstance(node.value, ast.Constant):
                        self.assertGreater(node.value.value, 0)
                        self.assertLessEqual(node.value.value, 600,
                                             f"{name}: timeout is effectively infinite")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(_JournalRepoCase):

    def _stage_an_article(self):
        self.write("content/operations/2026-07-30-new.md", "fresh column\n")

    def test_push_is_retried_exactly_once_after_a_rebase(self):
        self._stage_an_article()
        real_git = _mod._git
        calls = []

        def flaky(args, timeout=60):
            calls.append(list(args))
            if list(args)[:1] == ["push"]:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="! [rejected] non-fast-forward")
            return real_git(args, timeout=timeout)

        with patch.object(_mod, "_git", flaky):
            _mod.git_push("operations", "A title")
        pushes = [c for c in calls if c[:1] == ["push"]]
        pulls = [c for c in calls if c[:2] == ["pull", "--rebase"]]
        self.assertEqual(len(pushes), 2, "exactly one retry — never an unbounded loop")
        self.assertEqual(len(pulls), 1, "exactly one rebase between the two pushes")

    def test_a_push_that_fails_twice_notifies_instead_of_returning_quietly(self):
        self._stage_an_article()
        real_git = _mod._git

        def always_reject(args, timeout=60):
            if list(args)[:1] == ["push"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="rejected")
            return real_git(args, timeout=timeout)

        with patch.object(_mod, "_git", always_reject):
            _mod.git_push("operations", "A title")
        _notify_stub.notify.assert_called()
        kwargs = _notify_stub.notify.call_args.kwargs
        self.assertEqual(kwargs["level"], "warning")
        self.assertEqual(kwargs["category"], "journal")
        self.assertEqual(kwargs["dedup_key"], "journal-push-failing")
        self.assertIn("not reaching the site",
                      _notify_stub.notify.call_args.args[0])

    def test_the_false_reassurance_is_gone(self):
        self.assertNotIn("commit is safe, ships next run", _SRC,
                         "a failed push is not 'safe' — that line hid the outage")
        self._stage_an_article()
        real_git = _mod._git

        def always_reject(args, timeout=60):
            if list(args)[:1] == ["push"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="rejected")
            return real_git(args, timeout=timeout)

        with patch.object(_mod, "_git", always_reject):
            _mod.git_push("operations", "A title")
        self.assertNotIn("ships next run", "\n".join(self.logs))

    def test_pull_rebase_return_code_is_actually_checked(self):
        """The root cause: the rebase result used to be discarded."""
        src = ast.get_source_segment(_SRC, _fn("git_push"))
        self.assertRegex(src, r"pull\s*=\s*_git\(\[\"pull\", \"--rebase\"\]")
        self.assertIn("pull.returncode != 0", src)

    def test_a_stopped_rebase_is_retried_via_resolve_then_continue(self):
        # diverge FIRST — it commits everything — then leave a genuinely new article.
        self.diverge({ROLLING_MD: ("ORIGIN older\n", "LOCAL fresh\n")})
        self._stage_an_article()
        _mod.git_push("operations", "Retry after conflict")
        self.assertEqual(_mod._repo_wedged(), "")
        self.assertEqual(self.read(ROLLING_MD), "LOCAL fresh\n")
        self.assertTrue(any("Pushed to GitHub after rebase" in m for m in self.logs),
                        self.logs)

    def test_rebase_continue_failure_backs_all_the_way_out(self):
        self.diverge({ROLLING_MD: ("ORIGIN older\n", "LOCAL fresh\n")})
        self._stage_an_article()
        real_run = _mod.subprocess.run

        def broken_continue(args, **kw):
            if list(args)[:2] == ["git", "rebase"] and "--continue" in args:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return real_run(args, **kw)

        with patch.object(_mod.subprocess, "run", broken_continue):
            _mod.git_push("operations", "Retry after conflict")
        self.assertEqual(_mod._repo_wedged(), "",
                         "a failed --continue must still leave a usable repo")
        self.assertTrue(any("rebase --continue failed" in m for m in self.logs),
                        self.logs)

    def test_git_timeouts_are_finite(self):
        started = time.time()
        r = _mod._git(["rev-parse", "HEAD"], timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertLess(time.time() - started, 30)

    def test_notify_failure_during_repair_is_swallowed(self):
        self.stranded_commits(1)
        _notify_stub.notify.side_effect = RuntimeError("alert bus down")
        try:
            self.assertTrue(_mod._unwedge("rebase in progress"),
                            "repair must succeed even if the alert bus is down")
        finally:
            _notify_stub.notify.side_effect = None


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnitRepoWedged(_JournalRepoCase):
    """Each wedge trigger, independently."""

    def test_clean_repo_on_a_branch_is_not_wedged(self):
        self.assertEqual(_mod._repo_wedged(), "")

    def test_dirty_but_unwedged_repo_is_not_wedged(self):
        self.write("content/operations/draft.md", "draft\n")
        self.assertEqual(_mod._repo_wedged(), "")

    def test_rebase_merge_directory(self):
        (self.clone / ".git" / "rebase-merge").mkdir()
        self.assertEqual(_mod._repo_wedged(), "rebase in progress")

    def test_rebase_apply_directory(self):
        (self.clone / ".git" / "rebase-apply").mkdir()
        self.assertEqual(_mod._repo_wedged(), "rebase in progress")

    def test_merge_head_file(self):
        (self.clone / ".git" / "MERGE_HEAD").write_text(self.head() + "\n")
        self.assertEqual(_mod._repo_wedged(), "merge in progress")

    def test_cherry_pick_head_file(self):
        (self.clone / ".git" / "CHERRY_PICK_HEAD").write_text(self.head() + "\n")
        self.assertEqual(_mod._repo_wedged(), "cherry-pick in progress")

    def test_detached_head(self):
        self.git("checkout", "--detach", self.head())
        self.assertEqual(_mod._repo_wedged(), "detached HEAD")

    def test_a_real_stopped_rebase_reports_rebase_first(self):
        self.rolling_conflict()
        self.assertTrue(self.detached(), "a stopped rebase detaches HEAD")
        self.assertEqual(_mod._repo_wedged(), "rebase in progress",
                         "the more specific reason must win over 'detached HEAD'")

    def test_reason_is_a_string_never_none(self):
        self.assertIsInstance(_mod._repo_wedged(), str)
        self.git("checkout", "--detach", self.head())
        self.assertIsInstance(_mod._repo_wedged(), str)


class TestUnitResolveRollingConflicts(_JournalRepoCase):

    def test_no_conflicts_at_all_returns_false(self):
        self.assertFalse(_mod._resolve_rolling_conflicts(),
                         "nothing to resolve is not a resolvable rebase")

    def test_single_rolling_markdown_conflict_resolves(self):
        self.rolling_conflict()
        self.assertTrue(_mod._resolve_rolling_conflicts())

    def test_both_rolling_paths_conflicting_resolve(self):
        self.start_conflict({
            ROLLING_MD: ("ORIGIN md\n", "LOCAL md\n"),
            ROLLING_IMG: ("ORIGIN img\n", "LOCAL img\n"),
        })
        self.assertTrue(_mod._resolve_rolling_conflicts())
        self.assertEqual(self.read(ROLLING_MD), "LOCAL md\n")
        self.assertEqual(self.read(ROLLING_IMG), "LOCAL img\n")

    def test_resolved_paths_are_staged(self):
        self.rolling_conflict()
        self.assertTrue(_mod._resolve_rolling_conflicts())
        left = self.git("diff", "--name-only", "--diff-filter=U").stdout.strip()
        self.assertEqual(left, "", "resolved paths must be added to the index")

    def test_conflict_outside_rolling_paths_returns_false(self):
        self.start_conflict({ARTICLE: ("ORIGIN\n", "LOCAL\n")})
        self.assertFalse(_mod._resolve_rolling_conflicts())

    def test_mixed_conflict_returns_false(self):
        self.start_conflict({
            ROLLING_MD: ("ORIGIN md\n", "LOCAL md\n"),
            ARTICLE: ("ORIGIN\n", "LOCAL\n"),
        })
        self.assertFalse(_mod._resolve_rolling_conflicts())

    def test_nothing_is_staged_when_the_resolve_is_refused(self):
        self.start_conflict({ARTICLE: ("ORIGIN\n", "LOCAL\n")})
        _mod._resolve_rolling_conflicts()
        left = self.git("diff", "--name-only", "--diff-filter=U").stdout.split()
        self.assertIn(ARTICLE, left, "a refused conflict must stay unmerged")

    def test_success_is_logged_with_a_count(self):
        self.rolling_conflict()
        _mod._resolve_rolling_conflicts()
        self.assertTrue(any("Auto-resolved 1" in m for m in self.logs), self.logs)


class TestUnitRollingPaths(unittest.TestCase):

    def test_rolling_paths_are_repo_relative_posix_paths(self):
        for p in _mod.ROLLING_PATHS:
            self.assertIsInstance(p, str)
            self.assertFalse(p.startswith("/"), f"{p} must be repo-relative")
            self.assertNotIn("\\", p)

    def test_the_fishbowl_rolling_document_is_covered(self):
        self.assertIn(ROLLING_MD, _mod.ROLLING_PATHS)

    def test_no_dated_article_path_is_ever_auto_resolvable(self):
        for p in _mod.ROLLING_PATHS:
            self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", p),
                              f"{p} looks like a dated article — never auto-resolve one")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(_JournalRepoCase):
    """Real git, end to end."""

    def test_full_rolling_conflict_cycle_completes_the_rebase(self):
        self.rolling_conflict()
        self.assertEqual(_mod._repo_wedged(), "rebase in progress")

        self.assertTrue(_mod._resolve_rolling_conflicts())
        self.assertEqual(self.read(ROLLING_MD), "LOCAL FRESH REGENERATION\n",
                         "the fresh local regeneration must win, not origin's copy")

        cont = subprocess.run(["git", "rebase", "--continue"], cwd=str(self.clone),
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "GIT_EDITOR": "true"})
        self.assertEqual(cont.returncode, 0, cont.stderr)

        self.assertEqual(_mod._repo_wedged(), "")
        self.assertEqual(self.branch(), "main")
        self.assertEqual(self.read(ROLLING_MD), "LOCAL FRESH REGENERATION\n")

    def test_the_rebased_history_contains_both_sides(self):
        self.rolling_conflict()
        _mod._resolve_rolling_conflicts()
        subprocess.run(["git", "rebase", "--continue"], cwd=str(self.clone),
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "GIT_EDITOR": "true"})
        subjects = self.git("log", "--format=%s").stdout.split("\n")
        self.assertIn("local side", subjects)
        self.assertIn("origin side", subjects)

    def test_git_push_publishes_the_article_to_origin_through_a_conflict(self):
        self.diverge({ROLLING_MD: ("ORIGIN older\n", "LOCAL fresh\n")})
        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _mod.git_push("operations", "Todays column")

        self.assertEqual(_mod._repo_wedged(), "")
        published = subprocess.run(
            ["git", "show", "main:content/operations/2026-07-30-column.md"],
            cwd=str(self.origin), capture_output=True, text=True, timeout=30)
        self.assertEqual(published.returncode, 0,
                         "the article never reached origin — this is the outage")
        self.assertEqual(published.stdout, "todays column\n")

    def test_origin_receives_the_fresh_rolling_file_not_its_own_old_one(self):
        self.diverge({ROLLING_MD: ("ORIGIN older\n", "LOCAL fresh\n")})
        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _mod.git_push("operations", "Todays column")
        shown = subprocess.run(["git", "show", f"main:{ROLLING_MD}"],
                               cwd=str(self.origin), capture_output=True,
                               text=True, timeout=30)
        self.assertEqual(shown.stdout, "LOCAL fresh\n")

    def test_no_conflict_marker_ever_reaches_origin(self):
        self.diverge({ROLLING_MD: ("ORIGIN older\n", "LOCAL fresh\n")})
        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _mod.git_push("operations", "Todays column")
        markers = subprocess.run(["git", "log", "--all", "-S<<<<<<<", "--format=%H"],
                                 cwd=str(self.origin), capture_output=True,
                                 text=True, timeout=30)
        self.assertEqual(markers.stdout.strip(), "",
                         "a commit containing conflict markers reached origin")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(_JournalRepoCase):
    """The exact 2026-07-30 production regression."""

    def test_wedged_repo_never_gets_a_commit_on_a_detached_head(self):
        self.stranded_commits(1)
        self.assertTrue(self.detached(), "setup sanity: production was detached here")

        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _mod.git_push("operations", "Todays column")

        self.assertFalse(self.detached(),
                         "git_push left HEAD detached — the 3-hour silent outage")
        self.assertEqual(self.branch(), "main")

    def test_the_new_article_lands_on_main_not_on_a_dangling_head(self):
        self.stranded_commits(1)
        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _mod.git_push("operations", "Todays column")
        listed = self.git("ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertIn("content/operations/2026-07-30-column.md", listed,
                      "the article must be reachable from main")

    def test_wedged_repo_never_stages_conflict_markers(self):
        self.stranded_commits(1)
        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _mod.git_push("operations", "Todays column")

        for rel in self.git("ls-tree", "-r", "--name-only", "HEAD").stdout.split():
            blob = self.git("show", f"HEAD:{rel}").stdout
            self.assertNotIn("<<<<<<<", blob,
                             f"{rel} was committed with conflict markers in it")

    def test_a_wedge_is_reported_not_silently_repaired(self):
        self.stranded_commits(1)
        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _notify_stub.notify.reset_mock()
        _mod.git_push("operations", "Todays column")
        levels = [c.kwargs.get("level") for c in _notify_stub.notify.call_args_list]
        self.assertIn("warning", levels,
                      "an eight-article outage must not be repaired in silence")

    def test_unrepairable_wedge_refuses_to_commit_at_all(self):
        self.rolling_conflict()
        self.git("rebase", "--abort")
        self.git("checkout", "--detach", "HEAD")
        self.git("branch", "-D", "main")
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        before = self.git("rev-list", "--count", "HEAD").stdout.strip()

        self.write("content/operations/2026-07-30-column.md", "todays column\n")
        _mod.git_push("operations", "Todays column")

        after = self.git("rev-list", "--count", "HEAD").stdout.strip()
        self.assertEqual(before, after,
                         "a repo that could not be repaired must not be committed to")
        self.assertTrue(any("refusing to commit" in m for m in self.logs), self.logs)

    def test_three_sequential_jobs_all_land_on_main_after_a_wedge(self):
        """Production ran eight jobs into the void. Every one must land now."""
        self.stranded_commits(1)
        for i in range(3):
            self.write(f"content/operations/2026-07-30-job-{i}.md", f"job {i}\n")
            _mod.git_push("operations", f"Job {i}")
            self.assertFalse(self.detached(), f"job {i} detached the repo")
        listed = self.git("ls-tree", "-r", "--name-only", "main").stdout.split()
        for i in range(3):
            self.assertIn(f"content/operations/2026-07-30-job-{i}.md", listed)

    def test_a_healthy_repo_still_publishes_normally(self):
        self.write("content/operations/2026-07-30-happy.md", "happy path\n")
        _mod.git_push("operations", "Happy path")
        self.assertEqual(_mod._repo_wedged(), "")
        shown = subprocess.run(
            ["git", "show", "main:content/operations/2026-07-30-happy.md"],
            cwd=str(self.origin), capture_output=True, text=True, timeout=30)
        self.assertEqual(shown.stdout, "happy path\n")
        self.assertTrue(any("Pushed to GitHub" in m for m in self.logs), self.logs)

    def test_nothing_to_commit_is_still_a_quiet_no_op(self):
        _mod.git_push("operations", "Nothing new")
        self.assertTrue(any("Nothing to commit" in m for m in self.logs), self.logs)

    def test_git_push_never_raises(self):
        self.stranded_commits(2)
        shutil.rmtree(self.clone / ".git" / "refs" / "remotes", ignore_errors=True)
        self.write("content/operations/x.md", "x\n")
        _mod.git_push("operations", "Title")   # must not raise


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_journal.py has syntax errors: {e}")

    def test_module_imports_cleanly(self):
        self.assertEqual(_mod.__name__, "nova_journal")

    def test_shebang_and_docstring(self):
        self.assertTrue(_SRC.startswith("#!/usr/bin/env python3"))
        self.assertIn("nova_journal.py", ast.get_docstring(_TREE))

    def test_all_four_new_helpers_exist_and_are_callable(self):
        for name in ("_git", "_repo_wedged", "_unwedge", "_resolve_rolling_conflicts"):
            self.assertTrue(callable(getattr(_mod, name, None)), f"missing: {name}")

    def test_git_push_is_still_callable(self):
        self.assertTrue(callable(_mod.git_push))

    def test_rolling_paths_is_a_non_empty_tuple(self):
        self.assertIsInstance(_mod.ROLLING_PATHS, tuple)
        self.assertGreater(len(_mod.ROLLING_PATHS), 0)

    def test_hugo_root_is_derived_not_hardcoded(self):
        self.assertIn("HUGO_ROOT = (Path.home()", _SRC)

    def test_main_does_not_run_on_import(self):
        self.assertIn('if __name__ == "__main__":', _SRC)

    def test_the_guard_runs_first_in_git_push(self):
        src = ast.get_source_segment(_SRC, _fn("git_push"))
        self.assertLess(src.index("_repo_wedged()"), src.index('"add", "-A"'),
                        "the wedge check must precede `git add -A`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
