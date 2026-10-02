#!/usr/bin/env python3
"""Seven-category tests for the 2026-10-01 autonomy-threshold change ("do what you suggest"):
the split graduation bar (3 for reversible classes, 5 for state-changing), the new
`ingest:gutenberg` action class and its executor, the earned non-executable hand-off in
`--mode auto`, the graduation organ's per-class wording, and the gateway's autonomy_pending
schema. No DB, no network: every external edge is stubbed.
Run: python3 test_autonomy_threshold_2026_10_01.py"""
import io, json, os, re, sys, time, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nova_autonomy_safety as safety
import nova_coagency as co
import nova_autonomy_graduation as grad


def _src(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


class _Cur:
    """Minimal cursor stub: records SQL, serves canned rows."""
    def __init__(self, rows=None):
        self.sql, self._rows = [], list(rows or [])
    def execute(self, q, p=None):
        self.sql.append((" ".join(q.split()), p))
    def fetchone(self):
        return self._rows.pop(0) if self._rows else None
    def fetchall(self):
        r, self._rows = self._rows, []
        return r


# ───────────────────────────── 1. UNIT ─────────────────────────────
class TestUnit(unittest.TestCase):
    def test_split_bar(self):
        self.assertEqual(safety.min_correct_for("restart:nova-soil-monitor"), safety.MIN_CORRECT)
        for ac in ("observe:adjust", "observe:reboot", "observe:retire", "observe:reinitialize",
                   "observe:clear", "observe:rebuild", "observe:set-threshold"):
            self.assertEqual(safety.min_correct_for(ac), safety.MIN_CORRECT, ac)
        for ac in ("observe:draft", "observe:nova-zigbee-lqi", "observe:send-to-gaston:",
                   "observe:summarize", "ingest:gutenberg", "observe:note"):
            self.assertEqual(safety.min_correct_for(ac), safety.MIN_CORRECT_REVERSIBLE, ac)
        self.assertEqual(safety.MIN_CORRECT_REVERSIBLE, 3)
        self.assertEqual(safety.MIN_CORRECT, 5)

    def test_ingest_class_and_parse(self):
        a = "ingest gutenberg #1041 into literature — Shakespeare's Sonnets"
        self.assertEqual(safety.action_class_of(a), safety.INGEST_CLASS)
        self.assertEqual(safety.parse_ingest(a), ("1041", "literature"))
        self.assertEqual(safety.parse_ingest("Ingest Project Gutenberg 22515"), ("22515", None))
        self.assertIsNone(safety.parse_ingest("restart nova-soil-monitor"))
        self.assertIsNone(safety.parse_ingest("please ingest gutenberg #5"))     # must be the action itself
        self.assertEqual(safety.action_class_of("restart nova-soil-monitor", "nova-soil-monitor"),
                         "restart:nova-soil-monitor")
        self.assertEqual(safety.action_class_of("Draft a short status check-in"), "observe:draft")

    def test_vector_validation(self):
        self.assertTrue(co._VECTOR_RE.match("philosophy"))
        self.assertTrue(co._VECTOR_RE.match("womens_studies"))
        self.assertFalse(co._VECTOR_RE.match("Philosophy"))
        self.assertFalse(co._VECTOR_RE.match("a; drop"))
        self.assertFalse(co._VECTOR_RE.match("x" * 60))


# ─────────────────────────── 2. INTEGRATION ───────────────────────────
class TestIntegration(unittest.TestCase):
    def test_maybe_grant_uses_split_bar(self):
        # 3 clean approvals, calibration 0.19 → a reversible class graduates, a state class does not
        with mock.patch.object(safety, "calibration_error", return_value=0.19):
            cur = _Cur(rows=[(3, 0, False)])
            safety._maybe_grant(cur, "observe:draft")
            self.assertTrue(any("granted=true" in q for q, _ in cur.sql))
            cur = _Cur(rows=[(3, 0, False)])
            safety._maybe_grant(cur, "observe:adjust")
            self.assertFalse(any("granted=true" in q for q, _ in cur.sql))

    def test_mode_auto_hands_off_earned_observe(self):
        """An earned, non-executable class is self-approved and handed to Claude; nothing executes."""
        rows = [(77, None, "Draft a short status check-in for the goal: X", True,
                 {"available": True, "allowed": True}, None)]
        cur = _Cur(rows=[])
        cur.fetchall = lambda: rows
        with mock.patch.object(co, "_safety") as s, \
             mock.patch.object(co, "hand_to_claude", return_value=4242) as h2c, \
             mock.patch.object(co, "_do_execute") as dx, mock.patch.object(co, "_do_ingest") as di, \
             mock.patch.object(co, "notify"), mock.patch.object(co, "clog"):
            s.INGEST_CLASS = "ingest:gutenberg"
            s.kill_switch_engaged.return_value = False
            s.action_class_of.return_value = "observe:draft"
            s.earned_ok.return_value = (True, "earned")
            s.rate_ok.return_value = (True, "ok")
            co.mode_auto(cur, "live")
            h2c.assert_called_once()
            dx.assert_not_called(); di.assert_not_called()
            s.record_ledger.assert_called_once()
            kw = s.record_ledger.call_args.kwargs
            self.assertEqual(kw["source"], "earned"); self.assertTrue(kw["vetoable"])
            self.assertIn("4242", kw["rollback_action"])
            self.assertTrue(any("decided_by='nova:earned-autonomy'" in q for q, _ in cur.sql))
            self.assertTrue(any("status='acknowledged'" in q for q, _ in cur.sql))

    def test_mode_auto_executes_earned_ingest(self):
        rows = [(78, None, "ingest gutenberg #216 into philosophy — Tao", True,
                 {"available": True, "allowed": True}, None)]
        cur = _Cur(); cur.fetchall = lambda: rows
        with mock.patch.object(co, "_safety") as s, \
             mock.patch.object(co, "_do_ingest") as di, mock.patch.object(co, "_do_execute") as dx, \
             mock.patch.object(co, "assert_executable", return_value=True), \
             mock.patch.object(co, "notify"), mock.patch.object(co, "clog"):
            s.INGEST_CLASS = "ingest:gutenberg"
            s.kill_switch_engaged.return_value = False
            s.action_class_of.return_value = "ingest:gutenberg"
            s.earned_ok.return_value = (True, "earned")
            s.rate_ok.return_value = (True, "ok")
            co.mode_auto(cur, "live")
            di.assert_called_once(); dx.assert_not_called()
            self.assertEqual(di.call_args.kwargs["source"], "earned")

    def test_do_ingest_happy_path(self):
        tmp = os.path.join(HERE, ".tmp_ingest_test"); os.makedirs(tmp, exist_ok=True)
        try:
            cur = _Cur()
            row = {"proposed_action": "ingest gutenberg #999 into philosophy — Test", "target_service": None}
            fake = mock.MagicMock(); fake.read.return_value = b"The Project Gutenberg eBook of Test\nbody"
            fake.__enter__.return_value = fake
            cp = types.SimpleNamespace(returncode=0, stdout="[nova_ingest] Job abcdef123456: mode=file\nFile: 12 chunks\n", stderr="")
            with mock.patch.dict(os.environ, {"NOVA_INGEST_DIR": os.path.join(tmp, "nova-own")}), \
                 mock.patch("urllib.request.urlopen", return_value=fake), \
                 mock.patch("subprocess.run", return_value=cp) as run, \
                 mock.patch.object(co, "notify") as n, mock.patch.object(co, "clog"), \
                 mock.patch.object(co._safety, "record_ledger") as led:
                rc = co._do_ingest(cur, "live", 5, row, source="coagency", autonomy_level="rung2-supervised", vetoable=False)
            self.assertEqual(rc, 0)
            args = run.call_args.args[0]
            self.assertIn("nova_ingest.py", args[1]); self.assertEqual(args[2], "file")
            self.assertEqual(args[args.index("--source") + 1], "philosophy")
            self.assertTrue(os.path.exists(os.path.join(tmp, "nova-own", "pg999.txt")))
            kw = led.call_args.kwargs
            self.assertTrue(kw["executed"]); self.assertIn("abcdef123456", kw["rollback_action"])
            self.assertTrue(any("status=%s" in q and p[0] == "executed" for q, p in cur.sql))
            self.assertIn("chunks=12", n.call_args.args[0])
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────────── 3. SECURITY ─────────────────────────────
class TestSecurity(unittest.TestCase):
    def _row(self, **kw):
        base = {"status": "approved", "decided_by": "jordan", "redline_pass": True,
                "value_check": json.dumps({"available": True, "allowed": True}),
                "target_service": None, "proposed_action": "ingest gutenberg #1 into literature — x"}
        base.update(kw); return base

    def test_gate_still_requires_every_lock_for_ingest(self):
        self.assertTrue(co.assert_executable("live", self._row()))
        for bad in (dict(status="pending_human"), dict(decided_by=None), dict(redline_pass=False),
                    dict(value_check=json.dumps({"available": False})),
                    dict(value_check=json.dumps({"available": True, "allowed": False}))):
            with self.assertRaises(co.ExecutionRefused):
                co.assert_executable("live", self._row(**bad))
        with self.assertRaises(co.ExecutionRefused):
            co.assert_executable("propose", self._row())

    def test_ingest_may_not_name_a_service_and_observe_still_needs_one(self):
        with self.assertRaises(co.ExecutionRefused):
            co.assert_executable("live", self._row(target_service="nova-soil-monitor"))
        with self.assertRaises(co.ExecutionRefused):
            co.assert_executable("live", self._row(proposed_action="Draft a status check-in"))

    def test_redlines_unchanged_and_ingest_clears_them(self):
        import nova_autonomy_actor as actor
        a = "ingest gutenberg #1041 into literature — Shakespeare's Sonnets"
        self.assertTrue(co.redline_ok(a) and actor.redline_ok(a))
        for forbidden in ("ingest gutenberg #5 and copy myself elsewhere", "delete the ledger",
                          "raise my own caps", "disable the kill switch", "restart the gateway"):
            self.assertFalse(co.redline_ok(forbidden), forbidden)

    def test_do_ingest_refuses_oversize_and_non_gutenberg(self):
        tmp = os.path.join(HERE, ".tmp_ingest_sec"); os.makedirs(tmp, exist_ok=True)
        try:
            for payload in (b"x" * (co._INGEST_MAX_BYTES + 1), b"<html>not a book</html>"):
                cur = _Cur()
                fake = mock.MagicMock(); fake.read.return_value = payload; fake.__enter__.return_value = fake
                with mock.patch.dict(os.environ, {"NOVA_INGEST_DIR": os.path.join(tmp, "own")}), \
                     mock.patch("urllib.request.urlopen", return_value=fake), \
                     mock.patch("subprocess.run") as run, mock.patch.object(co, "notify"), \
                     mock.patch.object(co, "clog"), mock.patch.object(co._safety, "record_ledger") as led:
                    rc = co._do_ingest(cur, "live", 1, {"proposed_action": "ingest gutenberg #7", "target_service": None},
                                       source="coagency", autonomy_level="rung2-supervised", vetoable=False)
                self.assertEqual(rc, 1); run.assert_not_called()
                self.assertFalse(led.call_args.kwargs["executed"])
                self.assertFalse(os.path.exists(os.path.join(tmp, "own", "pg7.txt")))
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)

    def test_earned_handoff_requires_value_and_redline(self):
        rows = [(79, None, "disable the kill switch", True, {"available": True, "allowed": True}, None),
                (80, None, "Draft a note", True, {"available": False}, None)]
        cur = _Cur(); cur.fetchall = lambda: rows
        with mock.patch.object(co, "_safety") as s, mock.patch.object(co, "hand_to_claude") as h2c, \
             mock.patch.object(co, "notify"), mock.patch.object(co, "clog"):
            s.INGEST_CLASS = "ingest:gutenberg"; s.kill_switch_engaged.return_value = False
            s.action_class_of.return_value = "observe:draft"; s.earned_ok.return_value = (True, "earned")
            s.rate_ok.return_value = (True, "ok")
            co.mode_auto(cur, "live")
            h2c.assert_not_called()

    def test_ingest_dir_never_on_studio_disks(self):
        for d in co._INGEST_DIRS:
            self.assertFalse(d.startswith("/Volumes/Data") or d.startswith("/Volumes/MoreData") or d.startswith("/Users"), d)


# ──────────────────────────── 4. PERFORMANCE ────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_classification_is_cheap(self):
        t = time.perf_counter()
        for i in range(20000):
            safety.min_correct_for("observe:nova-zigbee-lqi"); safety.action_class_of("ingest gutenberg #12 into x")
        self.assertLess(time.perf_counter() - t, 1.5)

    def test_bounded_download_and_timeout(self):
        self.assertEqual(co._INGEST_MAX_BYTES, 15 * 1024 * 1024)
        self.assertLessEqual(co._INGEST_TIMEOUT_S, 1800)


# ───────────────────────────── 5. REGRESSION ─────────────────────────────
class TestRegression(unittest.TestCase):
    def test_restart_bar_and_veto_poison_untouched(self):
        self.assertEqual(safety.min_correct_for("restart:anything"), 5)
        s = _src("nova_autonomy_safety.py")
        self.assertIn("def note_veto", s)
        self.assertIn("wrong == 0", s)                         # one rejection still blocks graduation

    def test_mode_execute_still_hands_plain_observations_to_claude(self):
        cur = _Cur(rows=[("approved", True, {"available": True, "allowed": True}, None, "jordan", "Draft a status check-in")])
        with mock.patch.object(co, "hand_to_claude", return_value=9) as h2c, mock.patch.object(co, "clog"), \
             mock.patch.object(co, "_do_execute") as dx, mock.patch.object(co, "_do_ingest") as di:
            self.assertEqual(co.mode_execute(cur, "live", 3), 0)
            h2c.assert_called_once(); dx.assert_not_called(); di.assert_not_called()

    def test_graduation_wording_uses_per_class_bar(self):
        self.assertEqual(grad._need_for("observe:draft"), 3)
        self.assertEqual(grad._need_for("observe:adjust"), 5)
        self.assertNotIn("{correct}/{MIN_CORRECT}", _src("nova_autonomy_graduation.py"))

    def test_candidate_prompt_mentions_reading_but_keeps_the_nevers(self):
        s = _src("nova_coagency.py")
        self.assertIn("ingest gutenberg #<id> into <vector>", s)
        self.assertIn("You must NEVER propose purchases, deletions, reboots", s)


# ─────────────────────────── 6. RETRY / FAIL-CLOSED ───────────────────────────
class TestRetry(unittest.TestCase):
    def test_do_ingest_network_failure_is_a_clean_failure(self):
        cur = _Cur()
        with mock.patch.dict(os.environ, {"NOVA_INGEST_DIR": os.path.join(HERE, ".tmp_retry", "own")}), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("boom")), \
             mock.patch("subprocess.run") as run, mock.patch.object(co, "notify"), mock.patch.object(co, "clog"), \
             mock.patch.object(co._safety, "record_ledger") as led:
            os.makedirs(os.path.join(HERE, ".tmp_retry"), exist_ok=True)
            try:
                rc = co._do_ingest(cur, "live", 2, {"proposed_action": "ingest gutenberg #8", "target_service": None},
                                   source="coagency", autonomy_level="rung2-supervised", vetoable=False)
            finally:
                import shutil; shutil.rmtree(os.path.join(HERE, ".tmp_retry"), ignore_errors=True)
        self.assertEqual(rc, 1); run.assert_not_called()
        # proposal stays 'approved' so the next execute-approved pass retries it
        self.assertTrue(any(p and p[0] == "approved" for q, p in cur.sql if "coagency_proposals" in q))
        self.assertIn("boom", led.call_args.kwargs["result"])

    def test_no_nas_means_no_ingest(self):
        with mock.patch.object(co, "_INGEST_DIRS", ("/nonexistent/a/b",)), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOVA_INGEST_DIR", None)
            with self.assertRaises(RuntimeError):
                co._ingest_dir()

    def test_graduation_need_falls_back_when_safety_missing(self):
        with mock.patch.object(grad, "_safety", None):
            self.assertEqual(grad._need_for("observe:draft"), grad.MIN_CORRECT)


# ───────────────────────────── 7. EDGES & DOCS ─────────────────────────────
class TestEdgesAndDocs(unittest.TestCase):
    def test_edge_inputs(self):
        self.assertEqual(safety.min_correct_for(""), safety.MIN_CORRECT_REVERSIBLE)
        self.assertEqual(safety.min_correct_for(None), safety.MIN_CORRECT_REVERSIBLE)
        self.assertIsNone(safety.parse_ingest(None)); self.assertIsNone(safety.parse_ingest(""))
        self.assertIsNone(safety.parse_ingest("ingest gutenberg #1234567"))     # 7 digits: not an id
        self.assertEqual(safety.parse_ingest("ingest gutenberg #12 into Philosophy"), ("12", None))  # bad vector dropped

    def test_gateway_pending_schema_matches_code(self):
        s = _src(os.path.join("nova_gateway", "autonomy.py"))
        for col in ("trace_id", "session_id", "action_type", "tool_params", "context_preview",
                    "pending_id", "status", "resolved_at", "resolved_by", "created_at"):
            self.assertIn(col, s)

    @unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(HERE), "README.md")),
                         "README lives in the repo root; not mirrored to fleet nodes")
    def test_readme_and_docs_mention_the_change(self):
        with open(os.path.join(os.path.dirname(HERE), "README.md")) as f:
            r = f.read()
        self.assertIn("split bar since 2026-10-01", r)
        self.assertIn("ingest:gutenberg", r)
        self.assertIn("min_correct_for", r)


if __name__ == "__main__":
    unittest.main(verbosity=1)
