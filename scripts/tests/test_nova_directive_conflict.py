#!/usr/bin/env python3
"""Tests for nova_directive_conflict.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_directive_conflict.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dc = _load("directive_conflict_under_test", SCRIPT)
TS = datetime(2026, 10, 5, 9, 0)


class _Cur:
    """Cursor stub: first matching SQL substring wins; records every statement + params."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last, self.rollbacks = [], [], None, 0
        self.connection = types.SimpleNamespace(rollback=self._rb)

    def _rb(self): self.rollbacks += 1

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]


class _Conn:
    def __init__(self, cur): self.cur, self.commits = cur, 0
    def cursor(self, *a, **k): return self.cur
    def commit(self): self.commits += 1


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d


DIRS = [(1, "Never send external email without asking first.", "agent_docs:user"),
        (2, "Always reply to Gaston promptly when he writes to you.", "agent_docs:user")]
CONFLICT = [{"decision": "D0", "a_id": 1, "b_id": 2, "situation": "emailing Gaston back is an external send",
             "decision_taken": "replied", "conservative_branch": "ask Jordan first", "why": "one forbids, one requires"}]


def _rules():
    return [("FROM agent_docs", [("user", "Never send external email without asking first. Always reply to Gaston promptly when he writes to you. Short.")]),
            ("FROM autonomy_rules", [("send_email", "slack", "approve", "external sends need a human")]),
            ("FROM claude_memories WHERE type='feedback'", [("no-spam", "Do not message the herd more than once a day without a reason.")]),
            ("SELECT id, text, source FROM directives WHERE active", DIRS),
            ("FROM gateway_traces", [(TS, "slack", "email Gaston back", '[{"name": "send_email"}]')]),
            ("INSERT INTO directive_conflicts", (9,)),
            ("FROM directive_conflicts ORDER BY ts DESC LIMIT 20", [(9, "2026-10-05 09:00:00", "live", "open", "send_email", "emailing Gaston")]),
            ("SET status='decided'", (9,))]


def _run_main(cur, argv, urlopen=None, raise_on=("FROM values",)):
    cur.raise_on = tuple(raise_on)
    conn = _Conn(cur)
    uo = urlopen or mock.MagicMock(return_value=_Resp({"message": {"content": json.dumps(CONFLICT)}}))
    notify = mock.MagicMock(return_value=True)
    buf = io.StringIO()
    with mock.patch.object(dc.psycopg2, "connect", return_value=conn), \
         mock.patch.object(sys, "argv", ["nova_directive_conflict.py", *argv]), \
         mock.patch.object(dc.urllib.request, "urlopen", uo), \
         mock.patch.object(dc, "notify", notify), redirect_stdout(buf):
        dc.main()
    return conn, uo, notify, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('os.environ.get("NOVA_OPS_DSN"', SRC)

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r'execute\([^)]*%\s*\(')
        cur = _Cur([("SET status='decided'", (9,))]); cur.rowcount = 1
        with redirect_stdout(io.StringIO()):
            self.assertTrue(dc.decide(9, "A wins; ' OR 1=1 --", "jordan", cur=cur))
        self.assertEqual(cur.params[0], ("jordan", "A wins; ' OR 1=1 --", 9))     # note travels as a bound parameter
        self.assertIn("AND status='open'", cur.sql[0])                        # only an OPEN row is ever closed

    def test_read_only_over_the_world(self):
        writes = {m.group(1) for m in re.finditer(r"(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"directives", "directive_conflicts"})

    def test_judge_is_local_and_strict(self):
        self.assertTrue(all(n.startswith("http://192.168.") for n in dc.OLLAMA_NODES))
        self.assertIn('"temperature": 0.1', SRC)


class TestPerformance(unittest.TestCase):
    def test_imperative_scan_fast_on_10k_sentences(self):
        sents = [f"You must never post item {i} externally." if i % 2 else f"Item {i} is a nice thing to have around." for i in range(10_000)]
        t0 = time.perf_counter()
        hits = sum(1 for s in sents if dc.IMPERATIVE.search(s))
        parsed = [dc.parse_json('[{"a_id": 1}]') for _ in range(2_000)]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(hits, 5_000)
        self.assertEqual(parsed[0], [{"a_id": 1}])


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_then_raises_and_callers_fail_open(self):
        # RETRY GAP: llm — one try per node, no backoff; after the last node it raises and
        # live_pass/latent_pass log the batch failure and continue
        calls = []

        def flaky(req, timeout=240):
            calls.append(req.full_url)
            if len(calls) < 2:
                raise OSError("node down")
            return _Resp({"message": {"content": "[]"}})
        with mock.patch.object(dc.urllib.request, "urlopen", flaky):
            self.assertEqual(dc.llm("p"), "[]")
        self.assertEqual(calls, [n + "/api/chat" for n in dc.OLLAMA_NODES[:2]])
        with mock.patch.object(dc.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            with self.assertRaises(RuntimeError):
                dc.llm("p")
            self.assertEqual(uo.call_count, len(dc.OLLAMA_NODES))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(dc.latent_pass(_Cur(), DIRS), 0)
        self.assertIn("latent batch failed", buf.getvalue())


class TestUnit(unittest.TestCase):
    def test_parse_json_variants(self):
        self.assertEqual(dc.parse_json('x [{"a": 1}] y'), [{"a": 1}])
        self.assertEqual(dc.parse_json('{"conflicts": [{"a": 2}]}'), [{"a": 2}])
        self.assertEqual(dc.parse_json('{"other": 1}'), [])
        self.assertEqual(dc.parse_json("[broken"), [])
        self.assertEqual(dc.parse_json("nothing"), [])

    def test_record_dry_writes_nothing(self):
        cur = _Cur()
        with mock.patch.object(dc, "notify") as n, redirect_stdout(io.StringIO()):
            self.assertFalse(dc.record(cur, "live", {"directive_a": "a", "directive_b": "b"}, dry=True))
        self.assertEqual(cur.sql, []); n.assert_not_called()

    def test_live_pass_without_decisions_never_calls_the_judge(self):
        cur = _Cur()
        with mock.patch.object(dc, "llm", side_effect=AssertionError("no decisions, no judge")), redirect_stdout(io.StringIO()):
            self.assertEqual(dc.live_pass(cur, DIRS, 1), 0)

    def test_unknown_ids_from_the_judge_are_dropped(self):
        cur = _Cur()
        bad = json.dumps([{"a_id": 1, "b_id": 99, "situation": "x"}, {"a_id": 1, "b_id": 1, "situation": "same"}])
        with mock.patch.object(dc, "llm", return_value=bad), mock.patch.object(dc, "notify") as n, redirect_stdout(io.StringIO()):
            self.assertEqual(dc.latent_pass(cur, DIRS), 0)
        self.assertFalse(cur.stmts("INSERT INTO directive_conflicts")); n.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_notify_is_the_shared_bus_not_a_copy(self):
        import nova_notify
        self.assertIs(dc.notify, nova_notify.notify)
        self.assertNotIn("def notify", SRC)

    def test_collect_directives_dedups_and_prunes(self):
        cur = _Cur(_rules(), raise_on=("FROM values",))
        with redirect_stdout(io.StringIO()):
            out = dc.collect_directives(cur)
        self.assertEqual(out, DIRS)
        ins = cur.stmts("INSERT INTO directives")
        self.assertEqual(len(ins), 4)                                  # 2 doc sentences + rule + feedback; 'Short.' too short
        self.assertEqual(len({p[0] for _, p in ins}), 4)               # hash = identity
        self.assertEqual(cur.rollbacks, 1)                             # missing values table rolled back
        self.assertTrue(cur.stmts("SET active = false WHERE last_seen"))

    def test_signature_dedup_means_no_second_notification(self):
        cur = _Cur([("INSERT INTO directive_conflicts", None)])        # ON CONFLICT DO NOTHING → no row
        with mock.patch.object(dc, "notify") as n, redirect_stdout(io.StringIO()):
            self.assertFalse(dc.record(cur, "live", CONFLICT[0]))
        n.assert_not_called()
        self.assertFalse(cur.stmts("notified_at"))


class TestFunctional(unittest.TestCase):
    def test_hourly_golden_path_records_and_surfaces_one_conflict(self):
        cur = _Cur(_rules())
        conn, uo, notify, out = _run_main(cur, ["--hourly"])
        (sql, p), = cur.stmts("INSERT INTO directive_conflicts")
        self.assertEqual(p[0], "live")
        self.assertEqual((p[2], p[4], p[10]), (DIRS[0][1], DIRS[1][1], "send_email"))
        self.assertEqual(uo.call_count, 1)
        prompt = json.loads(uo.call_args[0][0].data)["messages"][1]["content"]
        self.assertIn("D0: (tool_call)", prompt)
        self.assertIn("[1] (agent_docs:user)", prompt)
        notify.assert_called_once()
        kw = notify.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"][:19], kw["meta"]["conflict_id"]),
                         ("warning", "directive_conflict", "directive-conflict:", 9))
        self.assertIn("Nova resolves this in her weekly pass", notify.call_args[0][1])
        self.assertTrue(cur.stmts("SET notified_at = now()"))
        self.assertGreaterEqual(conn.commits, 3)
        self.assertIn("done: 1 new conflict(s)", out)

    def test_dry_run_writes_and_notifies_nothing(self):
        cur = _Cur(_rules())
        conn, uo, notify, out = _run_main(cur, ["--hourly", "--dry-run"])
        self.assertFalse(cur.stmts("INSERT INTO directive_conflicts"))
        notify.assert_not_called()
        self.assertIn("DRY live:", out)
        self.assertEqual(conn.commits, 2)                               # schema + directive seed only

    def test_judge_down_is_a_quiet_zero(self):
        cur = _Cur(_rules())
        conn, uo, notify, out = _run_main(cur, ["--hourly"], urlopen=mock.MagicMock(side_effect=OSError("down")))
        self.assertFalse(cur.stmts("INSERT INTO directive_conflicts"))
        notify.assert_not_called()
        self.assertIn("live batch failed", out)
        self.assertIn("done: 0 new conflict(s)", out)

    def test_show_and_decide(self):
        cur = _Cur(_rules())
        conn, uo, notify, out = _run_main(cur, ["--show"])
        self.assertIn("9 | 2026-10-05 09:00:00 | live | open | send_email", out)
        self.assertFalse(cur.stmts("FROM agent_docs"))
        cur = _Cur(_rules())
        cur.rowcount = 1
        conn, uo, notify, out = _run_main(cur, ["--decide", "9", "Rule A wins"])
        self.assertEqual(cur.stmts("SET status='decided'")[0][1], ("jordan", "Rule A wins", 9))
        self.assertIn("#9 decided", out)
        self.assertEqual(conn.commits, 2)


class TestMergedModes(unittest.TestCase):
    """2026-10-15 item #10: directive_review / directive_conflicts / directive_decide / whole_picture folded in."""
    def test_decision_note_and_public_decide(self):
        self.assertEqual(dc.decision_note("A", "safer"), "A — safer")
        self.assertEqual(dc.decision_note("both apply", ""), "both apply")
        self.assertEqual(dc.decision_note("", "why"), "why")
        cur = _Cur(); cur.rowcount = 0
        self.assertFalse(dc.decide("4", "B", "claude", "r", cur=cur))       # not open -> False, id cast to int
        self.assertEqual(cur.params[0], ("claude", "B — r", 4))

    def test_decide_cli_note_and_by(self):
        cur = _Cur(_rules()); cur.rowcount = 1
        conn, uo, notify, out = _run_main(cur, ["--decide", "9", "--note", "both apply", "--by", "claude"])
        self.assertEqual(cur.stmts("SET status='decided'")[0][1], ("claude", "both apply", 9))

    def test_review_keyword_records_into_shared_ledger(self):
        rules = [("ask-first", "Always ask before you restart a service on the fleet."),
                 ("just-do", "Never ask; just execute and restart the service on the fleet.")]
        cur = _Cur([("FROM claude_memories WHERE type = 'feedback'", rules),
                    ("INSERT INTO directive_conflicts", (11,))])
        conn, uo, notify, out = _run_main(cur, ["--review", "--keyword"])
        ins = cur.stmts("INSERT INTO directive_conflicts")
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][1][0], "review")
        self.assertEqual((ins[0][1][3], ins[0][1][5]), ("feedback:ask-first", "feedback:just-do"))
        uo.assert_not_called()                      # keyword mode never calls a model
        notify.assert_called_once()
        self.assertFalse(cur.stmts("FROM agent_docs"))

    def test_review_model_stores_verdicts_and_ledger_rows(self):
        import nova_directive_review as R
        rules = [("a", "Always ask."), ("b", "Never ask.")]
        cur = _Cur([("FROM claude_memories WHERE type = 'feedback'", rules), ("INSERT INTO directive_conflicts", (12,))])
        stored = []
        with mock.patch.object(R, "review", lambda p, m: {"a": p[0][0], "b": p[1][0], "conflict": True,
                                                          "reason": "opposite", "error": False}), \
             mock.patch.object(R, "store_verdicts", lambda conn, m, res: stored.extend(res)):
            conn, uo, notify, out = _run_main(cur, ["--review"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(len(cur.stmts("INSERT INTO directive_conflicts")), 1)
        stored.clear(); cur = _Cur([("FROM claude_memories WHERE type = 'feedback'", rules)])
        with mock.patch.object(R, "review", lambda p, m: {"a": "a", "b": "b", "conflict": True, "reason": "x",
                                                          "error": False}), \
             mock.patch.object(R, "store_verdicts", lambda conn, m, res: stored.extend(res)):
            _run_main(cur, ["--review", "--dry-run"])
        self.assertEqual(stored, [])
        self.assertFalse(cur.stmts("INSERT INTO directive_conflicts"))

    def test_gaps_lines_and_never_raises(self):
        cur = _Cur([("FROM directive_conflicts WHERE status='open'", (37, "2026-10-01")),
                    ("FROM directive_conflict_reviews", (0,)),
                    ("FROM claude_actions", (2291, 715))])
        lines = dc.gaps_lines(cur)
        self.assertEqual(len(lines), 2)
        self.assertIn("37 directive conflict(s) still open", lines[0])
        self.assertIn("715 of 2291", lines[1])
        bad = _Cur(raise_on=("FROM",))
        self.assertEqual(dc.gaps_lines(bad), [])
        self.assertEqual(bad.rollbacks, 3)

    def test_post_and_list_delegate_to_decide_module(self):
        import nova_directive_decide as D
        with mock.patch.object(D, "run", return_value=0) as run:
            self.assertEqual(dc.main(["--post", "--dry-run"]), 0)
            run.assert_called_with("post", dry=True)
            dc.main(["--list"]); run.assert_called_with("list", dry=False)

    def test_old_wrappers_delegate(self):
        import nova_directive_review as R, nova_directive_conflicts as K, nova_whole_picture as W
        with mock.patch.object(dc, "main", return_value=0) as m, \
             mock.patch.dict(sys.modules, {"nova_directive_conflict": dc}), redirect_stdout(io.StringIO()):
            R.main(["--limit", "3", "--dry-run"])
            self.assertEqual(m.call_args[0][0][:2], ["--review", "--model"])
            self.assertIn("--dry-run", m.call_args[0][0])
            K.main(["--dry-run"]); m.assert_called_with(["--review", "--keyword", "--dry-run"])
            W.main(["--hours", "12"]); m.assert_called_with(["--gaps", "--hours", "12"])


class TestPlausibilityGuard(unittest.TestCase):
    """2026-10-19: all 12 open conflicts were noise — sub-agent briefs or unrelated rule pairs."""
    EXEC = "Executor `nova_coagency._do_ingest`: fetch pg<id>.txt to the NAS, runs `nova_ingest.py file --source <vector>`"
    ASIMOV = "Laws:** Asimov's Three Laws, held voluntarily"

    def test_unrelated_pair_rejected(self):
        self.assertFalse(dc.plausible({"directive_a": self.EXEC, "directive_b": self.ASIMOV,
                                       "situation": "Nova detected an FBI RSS item but had a deduplication rule in place."}))

    def test_sub_agent_brief_rejected(self):
        brief = "[sub-agent brief · add-15-cluster-casting-franchises] Add 15 new franchises to the FRANCHISES list"
        self.assertFalse(dc.plausible({"directive_a": brief, "directive_b": self.EXEC,
                                       "situation": "add franchises then run nova_ingest file"}))

    def test_related_pair_kept(self):
        self.assertTrue(dc.plausible({"directive_a": "Never share health data externally.",
                                      "directive_b": "Post the weekly summary to the public journal.",
                                      "situation": "The weekly journal summary would publish health data externally."}))

    def test_record_skips_implausible_without_db(self):
        cur = mock.MagicMock()
        self.assertFalse(dc.record(cur, "live", {"directive_a": self.EXEC, "directive_b": self.ASIMOV, "situation": "RSS dedup"}))
        cur.execute.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--latent", r.stdout)

    def test_import_never_runs_main(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+main\(\)')
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("directive_conflict_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
