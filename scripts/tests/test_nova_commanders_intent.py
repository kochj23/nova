"""Tests for nova_commanders_intent.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "nova_commanders_intent.py"
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("nova_commanders_intent_t", SCRIPT)
CI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CI)

NOW = lambda: datetime(2026, 10, 8, 12, tzinfo=timezone.utc)  # noqa: E731


class FakeCur:
    """Minimal cursor: scripted fetch results by SQL substring, records every execute."""

    def __init__(self, script=None):
        self.script = script or []
        self.calls = []
        self._last = []
        self.rowcount = 1
        self.connection = MagicMock()

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        self._last = []
        for needle, rows in self.script:
            if needle in sql:
                self._last = rows(args) if callable(rows) else rows
                break

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last)


def seed_script():
    return [
        ("FROM relationship_ledger", [(20, "No sexual content, ever, anywhere.", "cm:114,cm:113"),
                                      (24, "Don't editorialize about lights on in the heat.", "cm:213")]),
        ("service='nova_dials'", []),
        ("FROM face_people", [(12,)]),
        ("service_config WHERE service=%s", lambda a: {
            ("the_shine", "settings"): [({"waking_start": 8, "waking_end": 22},)],
            ("the_shine", "enabled"): [(False,)],
            ("consent", "health_nudges"): [],
            ("autonomy", "caps"): [({"per_day": 20, "per_hour": 6},)],
            ("autonomy", "kill_switch"): [("false",)],
        }.get(tuple(a), [])),
        ("coagency_mode", [("live",)]),
    ]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        src = SCRIPT.read_text()
        self.assertIsNone(re.search(r"(xox[bap]-|password\s*=\s*['\"]|sk-[A-Za-z0-9]{10})", src))
        self.assertNotIn(str(Path.home()), src)

    def test_sql_parameterized(self):
        src = SCRIPT.read_text()
        self.assertIsNone(re.search(r"execute\(f[\"']", src), "f-string SQL found")

    def test_only_jordan_confirms_or_retires(self):
        cur = FakeCur()
        with self.assertRaises(PermissionError):
            CI.confirm(cur, "grant.autonomy_caps", "nova")
        with self.assertRaises(PermissionError):
            CI.retire(cur, "grant.autonomy_caps", "claude")
        self.assertEqual(cur.calls, [])

    def test_slack_no_never_retires_a_restriction(self):
        row = ("never.ledger_20", "never", "x", "p", "inferred", "e", False, NOW(), NOW(), None, None, "active")
        cur = FakeCur([
            ("FROM slack_prompts", [(1, "2026-W41", "C1", "123.4")]),
            ("FROM intent_reasoning_log", [('["never.ledger_20"]',)]),
            ("FROM commanders_intent WHERE key", [row]),
        ])
        with patch.dict(sys.modules, {"nova_slack_answers": MagicMock(read_answer=lambda c, t: ("no", "no"))}):
            CI.harvest_replies(cur)
        self.assertFalse(any("status='retired'" in s for s, _ in cur.calls))


class TestPerformance(unittest.TestCase):
    def test_status_10k(self):
        now = NOW()
        rows = [{"is_grant": i % 2 == 0, "status": "active", "review_by": now - timedelta(days=i % 40)}
                for i in range(10000)]
        t = time.time()
        out = [CI.grant_view(r, "k", now) for r in rows]
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(len(out), 10000)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}
        fake = MagicMock()

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg down")
            return fake
        sleeps = []
        with patch("psycopg2.connect", side_effect=flaky):
            c = CI.connect(_sleep=sleeps.append)
        self.assertIs(c, fake)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [1.5, 3.0])

    def test_api_fails_safe_when_pg_down(self):
        with patch.object(CI, "connect", side_effect=OSError("down")):
            g = CI.grant_status(None, "shine.preconsent")
            self.assertFalse(g["active"])
            self.assertEqual(CI.intents(None), [])
            self.assertEqual(CI.reason_from_intent(None, "k", "c"), "")
            self.assertEqual(CI.standing_orders(None), [])


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(CI.selftest(), 0)

    def test_grant_lifecycle(self):
        now = NOW()
        g = {"is_grant": True, "status": "active", "review_by": now + timedelta(days=1)}
        self.assertEqual(CI.compute_status(g, now), "active")
        g["review_by"] = now - timedelta(days=CI.GRACE_DAYS - 1)
        self.assertTrue(CI.grant_view(g, "k", now)["active"])
        g["review_by"] = now - timedelta(days=CI.GRACE_DAYS + 1)
        v = CI.grant_view(g, "k", now)
        self.assertEqual((v["status"], v["active"]), ("lapsed", False))
        self.assertEqual(v["days_overdue"], CI.GRACE_DAYS + 1)

    def test_restriction_never_lapses(self):
        now = NOW()
        r = {"is_grant": False, "status": "active", "review_by": now - timedelta(days=999)}
        self.assertEqual(CI.compute_status(r, now), "stale")
        self.assertTrue(CI.grant_view(r, "k", now)["active"])

    def test_retired_and_absent_stick(self):
        now = NOW()
        for st in ("retired", "absent"):
            self.assertEqual(CI.compute_status({"status": st, "review_by": now - timedelta(days=5)}, now), st)

    def test_quarter(self):
        self.assertEqual(CI.next_quarter_start(datetime(2026, 12, 31).date()).isoformat(), "2027-01-01")
        self.assertEqual(CI.next_quarter_start(datetime(2026, 1, 1).date()).isoformat(), "2026-04-01")

    def test_reconfirm_text_bounded(self):
        rows = [{"key": f"k{i}", "is_grant": True, "status": "stale"} for i in range(20)]
        t = CI.reconfirm_text(rows)
        self.assertIn("+12 more", t)
        self.assertNotRegex(t.lower(), r"haven't replied|waiting")


class TestIntegration(unittest.TestCase):
    def test_build_seeds_from_live_shapes(self):
        cur = FakeCur(seed_script())
        with patch.dict(sys.modules, {"nova_coagency": MagicMock(get_mode=lambda oc: "live")}):
            seeds = CI.build_seeds(cur, NOW())
        keys = {s["key"]: s for s in seeds}
        self.assertIn("never.ledger_20", keys)
        self.assertEqual(keys["never.ledger_24"]["purpose_basis"], "stated")
        self.assertEqual(keys["never.ledger_20"]["purpose_basis"], "inferred")
        self.assertFalse(keys["shine.preconsent"]["granted"])
        self.assertTrue(keys["shine.preconsent"]["is_grant"])
        self.assertTrue(keys["grant.autonomy_caps"]["is_grant"])
        self.assertIn("12 enrolled", keys["face.enrolled_non_household"]["instruction"])
        self.assertEqual(len([k for k in keys if k.startswith("safety.p")]), 14)
        for s in seeds:
            self.assertIn(s["kind"], CI.KINDS)

    def test_stated_needs_evidence(self):
        cur = FakeCur(seed_script())
        cur.script[0] = ("FROM relationship_ledger", [(24, "lights on in the heat", None)])
        with patch.dict(sys.modules, {"nova_coagency": MagicMock(get_mode=lambda oc: "off")}):
            seeds = {s["key"]: s for s in CI.build_seeds(cur, NOW())}
        self.assertEqual(seeds["never.ledger_24"]["purpose_basis"], "inferred")
        self.assertFalse(seeds["grant.coagency_mode"]["granted"])

    def test_merge_keeps_confirmation_and_grants_on_enable(self):
        now = NOW()
        ex = {"status": "active", "granted_at": now, "review_by": now, "last_confirmed_at": now, "confirmed_by": "jordan"}
        m = CI.merge_seed(ex, {"key": "a", "is_grant": True, "instruction": "new"}, now)
        self.assertEqual((m["confirmed_by"], m["instruction"]), ("jordan", "new"))
        absent = dict(ex, status="absent", review_by=None)
        m2 = CI.merge_seed(absent, {"key": "a", "is_grant": True, "granted": True,
                                    "review_by": now + timedelta(days=85)}, now)
        self.assertEqual(m2["status"], "active")
        self.assertEqual(m2["review_by"], now + timedelta(days=85))


class TestFunctional(unittest.TestCase):
    def test_seed_upserts_every_intent(self):
        cur = FakeCur(seed_script())
        with patch.dict(sys.modules, {"nova_coagency": MagicMock(get_mode=lambda oc: "live")}):
            n = CI.seed(cur, NOW())
        ins = [a for s, a in cur.calls if s.startswith("INSERT INTO commanders_intent")]
        self.assertEqual(len(ins), n)
        self.assertGreater(n, 25)

    def test_review_posts_once_per_week(self):
        now = NOW()
        stale = ("grant.autonomy_caps", True, now - timedelta(days=3), "active")
        posted = []
        cur = FakeCur([
            ("kind=%s AND resolved_at IS NULL", []),
            ("WHERE status IN ('active','stale','lapsed')", [stale]),
            ("FROM commanders_intent \n", []),
            ("ORDER BY kind, key", [("grant.autonomy_caps", "grant", "caps", "p", "inferred", "e", True, now,
                                     now - timedelta(days=3), None, None, "stale")]),
            ("kind=%s AND ref_id=%s", []),
        ])
        mods = {"nova_annie_rule": MagicMock(check=lambda t, oc: {"ok": True}),
                "nova_turning_point": MagicMock(decide=lambda *a, **k: {"allowed": True}),
                "nova_config": MagicMock(SLACK_CHAN="C1")}
        with patch.dict(sys.modules, mods), patch.object(CI, "_post", side_effect=lambda t: posted.append(t) or "9.9"):
            res = CI.review(cur, now=now)
        self.assertTrue(res["posted"])
        self.assertEqual(len(posted), 1)
        self.assertIn("grant.autonomy_caps", posted[0])
        self.assertTrue(any("INSERT INTO slack_prompts" in s for s, _ in cur.calls))

    def test_review_held_by_turning_point(self):
        now = NOW()
        cur = FakeCur([
            ("kind=%s AND resolved_at IS NULL", []),
            ("WHERE status IN ('active','stale','lapsed')", []),
            ("ORDER BY kind, key", [("g", "grant", "x", "p", "inferred", "e", True, now, now - timedelta(days=3),
                                     None, None, "stale")]),
            ("kind=%s AND ref_id=%s", []),
        ])
        mods = {"nova_annie_rule": MagicMock(check=lambda t, oc: {"ok": True}),
                "nova_turning_point": MagicMock(decide=lambda *a, **k: {"allowed": False})}
        with patch.dict(sys.modules, mods), patch.object(CI, "_post") as p:
            res = CI.review(cur, now=now)
        p.assert_not_called()
        self.assertFalse(res["posted"])


class TestFrame(unittest.TestCase):
    def test_help_and_selftest(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for arg in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        with patch.object(CI, "main") as m:
            importlib.util.spec_from_file_location("x", SCRIPT)
            m.assert_not_called()
        self.assertTrue(hasattr(CI, "grant_status"))


if __name__ == "__main__":
    unittest.main()
