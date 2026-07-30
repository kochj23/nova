"""
test_nova_expectations_http_contains.py — All 7 test categories for the
http_contains kind in nova_expectations.py
Written by Jordan Koch.

WHY THIS KIND EXISTS. Every other check in this file can be satisfied by something
that is not the thing you actually care about. A directory mtime stays fresh from
yesterday's file. An HTTP 200 is returned by a site that is serving last week's
index. On 2026-07-30 eight ops articles were committed onto a detached HEAD and
never pushed — the repo directory looked recent, the site answered 200, and nobody
noticed for three hours. http_contains asks the only question that cannot be faked:
is today's article ACTUALLY on the published page?

HARD SAFETY (nothing here may touch the real world):
  * subprocess is stubbed for every measure() test — curl is never executed and no
    HTTP request is ever made.
  * conn() is patched for the check() tests — psycopg2 is never imported or
    connected, and no row in job_expectations is ever written.
  * nova_config is a MagicMock, so the Slack/alert path is captured, never posted.
"""

import ast
import re
import subprocess as _real_subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_expectations.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.SLACK_ALERTS = "#nova-alerts"
_nova_cfg.post_both = MagicMock(return_value=None)
sys.modules["nova_config"] = _nova_cfg
sys.modules.setdefault("psycopg2", MagicMock())

_mod = load_script_compat(_SCRIPT, "nova_expectations")
_SRC = _SCRIPT.read_text()
_TREE = ast.parse(_SRC)

measure = _mod.measure
TODAY = datetime.now().date().isoformat()
URL = "https://journal.example.invalid/operations/"


def _fake_subprocess(stdout="", returncode=0, raises=None):
    """A subprocess stand-in whose run() never spawns anything."""
    sp = MagicMock(name="subprocess")
    if raises is not None:
        sp.run.side_effect = raises
    else:
        sp.run.return_value = SimpleNamespace(returncode=returncode,
                                              stdout=stdout, stderr="")
    sp.TimeoutExpired = _real_subprocess.TimeoutExpired
    return sp


def _e(target, **kw):
    e = {"name": "journal_ops_published", "kind": "http_contains", "target": target,
         "dsn": None, "host": None, "max_silence_h": 26, "min_units": 1, "note": ""}
    e.update(kw)
    return e


def _measure(target, **fake):
    sp = _fake_subprocess(**fake)
    with patch.object(_mod, "subprocess", sp):
        result = measure(_e(target))
    return result, sp


def _fn(name):
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        for pat in (r"xox[baprs]-\d{5,}", r"\bsk-[A-Za-z0-9]{20,}",
                    r"\bghp_[A-Za-z0-9]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
                    r"(?i)password\s*=\s*['\"][^'\"]{4,}"):
            self.assertIsNone(re.search(pat, _SRC),
                              f"possible hardcoded credential matching {pat!r}")

    def test_no_hardcoded_home_path(self):
        self.assertNotIn(str(Path.home()) + "/", _SRC)

    def test_curl_is_never_run_through_a_shell(self):
        self.assertNotIn("shell=True", _SRC)
        self.assertNotIn("os.system", _SRC)

    def test_curl_is_invoked_as_an_argv_list(self):
        (_a, _u, _d), sp = _measure(f"{URL}||{TODAY}", stdout="page")
        argv = sp.run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "curl")

    def test_tls_verification_is_never_disabled(self):
        (_a, _u, _d), sp = _measure(f"{URL}||{TODAY}", stdout="page")
        argv = sp.run.call_args.args[0]
        for danger in ("-k", "--insecure", "--proxy-insecure"):
            self.assertNotIn(danger, argv, "http_contains must not skip TLS checks")

    def test_the_needle_is_never_handed_to_curl(self):
        """A crafted substring must not be able to become a curl option."""
        (_a, _u, _d), sp = _measure(f"{URL}||-o /tmp/pwned", stdout="page")
        argv = sp.run.call_args.args[0]
        self.assertEqual(argv, ["curl", "-sL", "-m", "20", URL])
        self.assertNotIn("-o /tmp/pwned", argv)

    def test_the_fetched_page_never_leaks_into_the_alert_detail(self):
        secret_page = "<html>internal roadmap and password=hunter2hunter2</html>"
        (age, units, detail) = _measure(f"{URL}||{TODAY}", stdout=secret_page)[0]
        self.assertIsNone(age)
        self.assertNotIn("hunter2hunter2", detail)
        self.assertNotIn("<html>", detail)

    def test_a_malformed_target_is_never_reported_as_satisfied(self):
        """No '||' means no substring — that must be MISSING, not a silent pass."""
        for bad in (URL, f"{URL}||", "   "):
            age, units, detail = _measure(bad, stdout="a whole page of html")[0]
            self.assertIsNone(age, f"{bad!r} reported satisfied with no substring "
                                   f"to look for — a misconfigured expectation "
                                   f"would be permanently green")
            self.assertEqual(units, 0)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_curl_carries_a_bounded_m_timeout(self):
        (_a, _u, _d), sp = _measure(f"{URL}||{TODAY}", stdout="page")
        argv = sp.run.call_args.args[0]
        self.assertIn("-m", argv, "curl must carry its own transfer timeout")
        secs = int(argv[argv.index("-m") + 1])
        self.assertGreater(secs, 0)
        self.assertLessEqual(secs, 60)

    def test_subprocess_timeout_is_finite_and_outlives_the_curl_timeout(self):
        (_a, _u, _d), sp = _measure(f"{URL}||{TODAY}", stdout="page")
        argv = sp.run.call_args.args[0]
        kwargs = sp.run.call_args.kwargs
        self.assertIn("timeout", kwargs, "a hung curl must not hang the sweep")
        self.assertGreater(kwargs["timeout"], int(argv[argv.index("-m") + 1]),
                           "the outer timeout must be the backstop, not the trigger")
        self.assertLessEqual(kwargs["timeout"], 120)

    def test_exactly_one_fetch_per_measure(self):
        (_a, _u, _d), sp = _measure(f"{URL}||{TODAY}", stdout="page " + TODAY)
        self.assertEqual(sp.run.call_count, 1,
                         "one expectation must cost exactly one HTTP fetch")

    def test_output_is_captured_not_streamed(self):
        (_a, _u, _d), sp = _measure(f"{URL}||{TODAY}", stdout="page")
        self.assertTrue(sp.run.call_args.kwargs.get("capture_output"))
        self.assertTrue(sp.run.call_args.kwargs.get("text"))

    def test_counting_a_large_page_is_a_single_pass(self):
        big = ("filler " * 50_000) + TODAY
        age, units, _d = _measure(f"{URL}||{TODAY}", stdout=big)[0]
        self.assertEqual(age, 0.0)
        self.assertEqual(units, 1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_curl_failure_returns_missing_and_never_raises(self):
        age, units, detail = _measure(f"{URL}||{TODAY}", returncode=6)[0]
        self.assertIsNone(age)
        self.assertEqual(units, 0)
        self.assertIn("fetch failed", detail)
        self.assertIn("6", detail)

    def test_every_curl_exit_code_is_handled(self):
        for rc in (1, 6, 7, 28, 35, 60):
            age, units, detail = _measure(f"{URL}||{TODAY}", returncode=rc)[0]
            self.assertIsNone(age, f"rc={rc}")
            self.assertIn(f"rc={rc}", detail)

    def test_a_timeout_is_caught_not_propagated(self):
        exc = _real_subprocess.TimeoutExpired(cmd="curl", timeout=40)
        age, units, detail = _measure(f"{URL}||{TODAY}", raises=exc)[0]
        self.assertIsNone(age)
        self.assertEqual(units, 0)
        self.assertIn("check error", detail)

    def test_curl_missing_from_the_box_is_caught(self):
        age, units, detail = _measure(f"{URL}||{TODAY}",
                                      raises=FileNotFoundError("curl"))[0]
        self.assertIsNone(age)
        self.assertIn("check error", detail)

    def test_a_failed_fetch_does_not_poison_the_next_one(self):
        first = _measure(f"{URL}||{TODAY}", returncode=7)[0]
        second = _measure(f"{URL}||{TODAY}", stdout=f"<h2>{TODAY}</h2>")[0]
        self.assertIsNone(first[0])
        self.assertEqual(second[0], 0.0)

    def test_the_error_detail_is_length_capped(self):
        exc = RuntimeError("x" * 5000)
        age, units, detail = _measure(f"{URL}||{TODAY}", raises=exc)[0]
        self.assertLess(len(detail), 200, "an alert line must stay readable")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnitParsing(unittest.TestCase):

    def test_target_splits_on_the_double_pipe(self):
        (_a, _u, _d), sp = _measure(f"{URL}||some-slug", stdout="page")
        self.assertEqual(sp.run.call_args.args[0][-1], URL)

    def test_only_the_first_double_pipe_separates(self):
        (_a, _u, _d), sp = _measure(f"{URL}||a||b", stdout="xx a||b xx")
        self.assertEqual(sp.run.call_args.args[0][-1], URL)
        age, units, _d2 = _measure(f"{URL}||a||b", stdout="xx a||b xx")[0]
        self.assertEqual(units, 1, "the needle keeps everything after the first ||")

    def test_a_single_pipe_in_the_needle_is_preserved(self):
        age, units, _d = _measure(f"{URL}||Ops | {TODAY}",
                                  stdout=f"<title>Ops | {TODAY}</title>")[0]
        self.assertEqual(age, 0.0)
        self.assertEqual(units, 1)

    def test_today_expands_to_the_iso_date(self):
        age, units, detail = _measure(f"{URL}||{{today}}", stdout=f"posted {TODAY}")[0]
        self.assertEqual(age, 0.0)
        self.assertEqual(units, 1)
        self.assertIn(TODAY, detail)

    def test_today_expands_inside_a_longer_needle(self):
        needle = "/operations/{today}-ops-column/"
        page = f'<a href="/operations/{TODAY}-ops-column/">today</a>'
        age, units, _d = _measure(f"{URL}||{needle}", stdout=page)[0]
        self.assertEqual(age, 0.0)
        self.assertEqual(units, 1)

    def test_yesterdays_date_does_not_satisfy_todays_needle(self):
        page = "<a href='/operations/2026-07-29-ops-column/'>yesterday</a>"
        age, units, detail = _measure(f"{URL}||{{today}}", stdout=page)[0]
        self.assertIsNone(age)
        self.assertEqual(units, 0)
        self.assertIn("NOT on page", detail)


class TestUnitResult(unittest.TestCase):

    def test_a_found_substring_returns_zero_age_and_positive_hits(self):
        age, units, detail = _measure(f"{URL}||{TODAY}", stdout=f"a {TODAY} b")[0]
        self.assertEqual(age, 0.0)
        self.assertGreater(units, 0)
        self.assertIn("found", detail)

    def test_hits_counts_every_occurrence(self):
        page = " ".join([TODAY] * 4)
        age, units, _d = _measure(f"{URL}||{TODAY}", stdout=page)[0]
        self.assertEqual(units, 4)

    def test_an_absent_substring_returns_none_age_and_zero_hits(self):
        age, units, detail = _measure(f"{URL}||{TODAY}",
                                      stdout="<html>nothing here</html>")[0]
        self.assertIsNone(age, "None age is what makes check() call it MISSING")
        self.assertEqual(units, 0)
        self.assertIn("NOT on page", detail)

    def test_an_empty_page_is_missing(self):
        age, units, _d = _measure(f"{URL}||{TODAY}", stdout="")[0]
        self.assertIsNone(age)
        self.assertEqual(units, 0)

    def test_the_result_is_always_a_three_tuple(self):
        for kw in ({"stdout": TODAY}, {"stdout": ""}, {"returncode": 7}):
            r = _measure(f"{URL}||{TODAY}", **kw)[0]
            self.assertIsInstance(r, tuple)
            self.assertEqual(len(r), 3)

    def test_an_unknown_kind_still_falls_through(self):
        sp = _fake_subprocess(stdout="page")
        with patch.object(_mod, "subprocess", sp):
            age, units, detail = measure(_e("whatever", kind="http_contains_typo"))
        self.assertIsNone(age)
        self.assertIn("unknown kind", detail)
        sp.run.assert_not_called()


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class _FakeCursor:
    def __init__(self, rows, cols):
        self._rows, self.description = rows, [(c,) for c in cols]
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def close(self):
        pass


class TestIntegration(unittest.TestCase):
    COLS = ["name", "kind", "target", "dsn", "host", "max_silence_h",
            "min_units", "note"]

    def _run_check(self, target, **fake):
        row = ("journal_ops_published", "http_contains", target, None, None,
               26, 1, "today's ops column must be on the site")
        cur = _FakeCursor([row], self.COLS)
        sp = _fake_subprocess(**fake)
        _nova_cfg.post_both.reset_mock()
        with patch.object(_mod, "conn", lambda dsn=None: _FakeConn(cur)), \
             patch.object(_mod, "subprocess", sp):
            rc = _mod.check(SimpleNamespace(quiet=True))
        return rc, cur

    def test_a_missing_article_makes_the_sweep_fail(self):
        rc, cur = self._run_check(f"{URL}||{{today}}", stdout="<html>stale</html>")
        self.assertEqual(rc, 1, "a missing article must exit non-zero")

    def test_a_missing_article_alerts_the_operator(self):
        self._run_check(f"{URL}||{{today}}", stdout="<html>stale</html>")
        _nova_cfg.post_both.assert_called_once()
        msg = _nova_cfg.post_both.call_args.args[0]
        self.assertIn("journal_ops_published", msg)
        self.assertIn("MISSING", msg)

    def test_a_missing_article_is_routed_to_the_alerts_channel(self):
        self._run_check(f"{URL}||{{today}}", stdout="<html>stale</html>")
        self.assertEqual(_nova_cfg.post_both.call_args.kwargs["slack_channel"],
                         _nova_cfg.SLACK_ALERTS)

    def test_a_published_article_satisfies_the_expectation(self):
        rc, cur = self._run_check(f"{URL}||{{today}}",
                                  stdout=f"<a href='/ops/{TODAY}/'>today</a>")
        self.assertEqual(rc, 0)
        _nova_cfg.post_both.assert_not_called()

    def test_a_satisfied_expectation_records_last_ok(self):
        rc, cur = self._run_check(f"{URL}||{{today}}", stdout=f"posted {TODAY}")
        updates = [s for s, _p in cur.executed if "UPDATE job_expectations" in s]
        self.assertTrue(updates)
        self.assertIn("last_ok=now()", updates[-1])

    def test_a_missing_expectation_does_not_record_last_ok(self):
        rc, cur = self._run_check(f"{URL}||{{today}}", stdout="nothing")
        updates = [s for s, _p in cur.executed if "UPDATE job_expectations" in s]
        self.assertTrue(updates)
        self.assertNotIn("last_ok=now()", updates[-1])

    def test_a_fetch_failure_is_reported_as_missing_not_as_ok(self):
        rc, cur = self._run_check(f"{URL}||{{today}}", returncode=7)
        self.assertEqual(rc, 1)
        self.assertIn("fetch failed", _nova_cfg.post_both.call_args.args[0])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):
    """The 2026-07-30 outage, as this check would have seen it."""

    INDEX = ("<html><body><h1>Operations</h1>"
             "<a href='/operations/2026-07-29-ops-column/'>Yesterday</a>"
             "{today_link}</body></html>")

    def test_stranded_articles_are_caught_the_same_morning(self):
        """Eight commits on a detached HEAD: the page never gained today's link."""
        page = self.INDEX.format(today_link="")
        age, units, detail = _measure(f"{URL}||{{today}}", stdout=page)[0]
        self.assertIsNone(age, "this is the outage the check exists to catch")
        self.assertEqual(units, 0)
        self.assertIn("NOT on page", detail)

    def test_a_successful_publish_is_silent(self):
        page = self.INDEX.format(
            today_link=f"<a href='/operations/{TODAY}-ops-column/'>Today</a>")
        age, units, detail = _measure(f"{URL}||{{today}}", stdout=page)[0]
        self.assertEqual(age, 0.0)
        self.assertEqual(units, 1)
        self.assertIn("found", detail)

    def test_a_site_that_answers_200_with_stale_content_still_fails(self):
        """The blind spot of the plain `http` kind — a live site serving yesterday."""
        page = self.INDEX.format(today_link="")
        # The plain `http` kind is satisfied by any 2xx — it would call this ok.
        sp = _fake_subprocess(stdout="200")
        with patch.object(_mod, "subprocess", sp):
            plain_age, plain_units, _d = measure(_e(URL, kind="http"))
        self.assertEqual(plain_age, 0.0, "the `http` kind sees a healthy site")
        self.assertEqual(plain_units, 1)
        # http_contains looks at the artifact instead, and correctly fails.
        age, units, _d2 = _measure(f"{URL}||{{today}}", stdout=page)[0]
        self.assertIsNone(age, "200 OK proves the server is up, not that work happened")

    def test_the_check_never_raises_whatever_the_site_returns(self):
        for kw in ({"stdout": ""}, {"stdout": "\x00\x01binary"},
                   {"stdout": "x" * 200_000}, {"returncode": 52},
                   {"raises": OSError("boom")}):
            try:
                _measure(f"{URL}||{{today}}", **kw)
            except Exception as ex:                     # pragma: no cover
                self.fail(f"measure raised on {kw}: {ex}")

    def test_the_kind_is_registerable_from_the_command_line(self):
        """A check you cannot add with --add is a check that never runs."""
        kinds = None
        for node in ast.walk(_fn("main")):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") ==
                    "add_argument" and node.args and
                    getattr(node.args[0], "value", "") == "--kind"):
                for kw in node.keywords:
                    if kw.arg == "choices":
                        kinds = [c.value for c in kw.value.elts]
        self.assertIsNotNone(kinds, "--kind should offer an explicit choices list")
        self.assertIn("http_contains", kinds,
                      "http_contains cannot be registered with --add")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_expectations.py has syntax errors: {e}")

    def test_shebang_and_docstring(self):
        self.assertTrue(_SRC.startswith("#!/usr/bin/env python3"))
        self.assertIn("nova_expectations.py", ast.get_docstring(_TREE))

    def test_module_imports_cleanly(self):
        self.assertEqual(_mod.__name__, "nova_expectations")

    def test_measure_and_check_are_callable(self):
        for name in ("measure", "check", "add", "main", "conn"):
            self.assertTrue(callable(getattr(_mod, name, None)), f"missing: {name}")

    def test_http_contains_is_a_recognised_kind(self):
        self.assertIn('kind == "http_contains"', _SRC)

    def test_the_schema_comment_lists_the_new_kind(self):
        kind_line = next(l for l in _mod.SCHEMA.splitlines() if "kind " in l)
        self.assertIn("http_contains", kind_line,
                      "the schema comment is the registry's documentation")

    def test_main_does_not_run_on_import(self):
        self.assertIn('if __name__ == "__main__":', _SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
