#!/usr/bin/env python3
"""7-category tests for the hub's Games 1.4 payload (2026-11-11, Nova Cluster 1.4.0, Jordan: "pretty-up the games
section of Nova Cluster. More stats about live game play"): live-session enrichment (elapsed, APM, last event,
GTNW DEFCON / turn / approval polls), the 60-minute activity timeline, the live feed (human one-liners + levels,
bursts collapsed), today per game, the 7-day roll-up, the hall of fame and the AI share.

Offline: the Games DB (psycopg2) and the relay are faked; nothing touches core3. Written by Jordan Koch (via Claude)."""
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_minion_hub as hub  # noqa: E402

NOW = datetime(2026, 11, 11, 19, 40, tzinfo=timezone.utc)
SID = "fe87e78b-3fba-405d-99fa-86e7648d2c8d"
GTNW_SUMMARY = {"leader": "Thomas Jefferson", "country_name": "United States", "scenario": "sandbox", "turns": 7,
                "defcon": 1, "difficulty": "Nightmare", "game_date_end": "May 20, 1801",
                "polls": [{"date": f"1801-{i:02d}-20", "popularity": 40 + i} for i in range(1, 16)]}
LIVE_ROW = (SID, "gtnw", "mac", "default", NOW - timedelta(minutes=4), NOW - timedelta(seconds=20), None, None, None, 8,
            GTNW_SUMMARY, NOW)
FEED_ROWS = [
    (SID, 30, NOW, "gtnw", "nuclear_strike", {"attacker": "USA", "target": "BLZ", "warheads": 13, "casualties": 104000000}),
    (SID, 29, NOW - timedelta(seconds=5), "gtnw", "world_claim", {"power": "PRT", "how": "claims", "land": "SLE"}),
    (SID, 28, NOW - timedelta(seconds=5.5), "gtnw", "world_claim", {"power": "NLD", "how": "claims", "land": "ZWE"}),
    (SID, 27, NOW - timedelta(seconds=6), "gtnw", "world_claim", {"power": "ESP", "how": "claims", "land": "GAB"}),
    (SID, 26, NOW - timedelta(seconds=60), "gtnw", "treaty_answered", {"from": "GBR", "type": "Trade Agreement", "accepted": True}),
]


class GamesCur:
    """Answers collect_games' queries by shape; `boom` names a query fragment that raises."""

    def __init__(self, boom: str | None = None, live=(LIVE_ROW,), feed=FEED_ROWS):
        self.boom, self.live, self.feed = boom, list(live), list(feed)
        self.sql, self.calls = "", []

    def execute(self, sql, params=None):
        self.sql = sql
        self.calls.append((sql, params))
        if self.boom and self.boom in sql:
            raise RuntimeError("permission denied for " + self.boom)

    def fetchone(self):
        if "count(*) FILTER (WHERE ts > now() - interval '1 hour')" in self.sql:
            return (659, 134)
        if "FILTER (WHERE outcome = 'victory'), count(*) FILTER (WHERE outcome = 'defeat'), count(*) " in self.sql:
            return (1, 13, 16)
        if "casting.ai_calls" in self.sql:
            return (2, 600, 8000)
        return (1,)

    def fetchall(self):
        q = self.sql
        if "WHERE ended_at IS NULL AND last_seen_at" in q:
            return self.live
        if "count(*) FILTER (WHERE ts > now() - interval '5 minutes')" in q:
            return [(SID, 40, 111)]
        if "DISTINCT ON (session_id)" in q:
            return [(SID, NOW, "defcon_change", {"from": 3, "to": 2, "game_date": "1801-08-20"})]
        if "floor(extract(epoch" in q:
            return [("gtnw", 0, 9), ("gtnw", 59, 4), ("blompie-mac", 3, 2), ("gtnw", 75, 99)]
        if "ORDER BY e.ts DESC" in q:
            return self.feed
        if "greatest(started_at" in q:
            return [("gtnw", 2, 15.0, 0, 1, 0, 1, None), ("blompie-mac", 1, 3.0, 0, 0, 1, 0, None)]
        if "WHERE e.ts >=" in q and "detail ? 'source'" not in q:
            return [("gtnw", 316), ("cartest", 1)]
        if "to_char(started_at" in q:
            return [("2026-11-10", "gtnw", 11, 35.6), ("2026-11-11", "gtnw", 2, 15.0)]
        if "generate_series" in q:
            return [(f"2026-11-{d:02d}",) for d in range(5, 12)]
        if "score > 0 ORDER BY score DESC" in q:
            return [("gtnw", "default", {"leader": "", "country_name": "United States", "turns": 198}, 9617.0, "defeat", NOW)]
        if "detail ? 'source'" in q:
            return [("gtnw", "template", 58), ("blompie-mac", "llm", 6), ("blompie-mac", "fallback", 2)]
        if "FROM play.sessions ORDER BY last_seen_at DESC" in q:
            return [LIVE_ROW[:-1]]
        if "GROUP BY game ORDER BY 4 DESC" in q:
            return [("gtnw", 40, 2, NOW)]
        return []


class FakeConn:
    def __init__(self, cur):
        self.cur, self.autocommit, self.closed = cur, False, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def run_collect(cur=None, relay=b'{"status": "ok", "ok": 3}'):
    cur = cur or GamesCur()
    conn = FakeConn(cur)
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = relay
    with mock.patch("psycopg2.connect", return_value=conn), mock.patch.object(hub, "_games_password", return_value="pw"), \
         mock.patch.object(hub.urllib.request, "urlopen", return_value=resp), mock.patch.object(hub, "log"):
        return hub.collect_games(), cur, conn


class TestSecurity(unittest.TestCase):
    def test_event_text_strips_control_and_bidi_and_caps(self):
        t = hub.event_text("ticker", {"text": "ok‮evil\x00\n" + "z" * 900})
        self.assertNotIn("‮", t)
        self.assertNotIn("\n", t)
        self.assertLessEqual(len(t), 140)

    def test_queries_take_session_ids_as_parameters(self):
        evil = "x'; DROP TABLE play.sessions; --"
        row = (evil,) + LIVE_ROW[1:]
        _, cur, _ = run_collect(GamesCur(live=[row]))
        for sql, params in cur.calls:
            self.assertNotIn("DROP TABLE", sql)
        self.assertTrue(any(p and evil in p[0] for _, p in cur.calls if p))

    def test_payload_has_no_ip_addresses(self):
        out, _, _ = run_collect()
        body = json.dumps({k: v for k, v in out.items() if k not in ("relay",)}, default=str)
        self.assertNotRegex(body, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_db_password_comes_from_keychain_not_source(self):
        src = (SCRIPTS / "nova_minion_hub.py").read_text()
        self.assertIn('"nova-games-play-reader"', src)
        self.assertNotIn("password=\"", src)


class TestPerformance(unittest.TestCase):
    def test_collect_is_a_bounded_number_of_queries(self):
        _, cur, _ = run_collect()
        self.assertLessEqual(len(cur.calls), 20)

    def test_every_event_scan_is_time_bounded_or_limited(self):
        _, cur, _ = run_collect()
        for sql, _ in cur.calls:
            if "FROM play.events" in sql:
                self.assertTrue("interval" in sql or "LIMIT" in sql or "ANY(" in sql or ">= date_trunc" in sql, sql)

    def test_feed_and_fame_are_capped(self):
        many = [(SID, i, NOW - timedelta(minutes=i), "gtnw", f"k{i % 2}x{i}", {}) for i in range(200)]
        out, _, _ = run_collect(GamesCur(feed=many))
        self.assertEqual(len(out["feed"]), hub.FEED_N)
        self.assertLessEqual(len(out["fame"]), hub.FAME_N)

    def test_collector_still_every_ten_seconds(self):
        self.assertIn(("games", hub.collect_games, 10), hub.COLLECTORS)


class TestRetry(unittest.TestCase):
    def test_one_failing_extra_keeps_the_rest(self):
        out, _, _ = run_collect(GamesCur(boom="floor(extract(epoch"))
        self.assertEqual(out["partial"], ["timeline"])
        self.assertNotIn("timeline", out)
        self.assertTrue(out["db"]["up"])
        for k in ("feed", "today", "week", "fame", "ai_share", "live"):
            self.assertIn(k, out)

    def test_failing_live_stats_keeps_live_sessions(self):
        out, _, _ = run_collect(GamesCur(boom="DISTINCT ON (session_id)"))
        self.assertIn("live_stats", out["partial"])
        self.assertEqual(out["live"][0]["who"], "Thomas Jefferson")
        self.assertNotIn("live_stats", out)

    def test_db_down_is_reported_and_relay_still_read(self):
        with mock.patch("psycopg2.connect", side_effect=Exception("timeout expired")), \
             mock.patch.object(hub, "_games_password", return_value="pw"), \
             mock.patch.object(hub.urllib.request, "urlopen", side_effect=OSError("refused")):
            out = hub.collect_games()
        self.assertEqual(out["db"], {"up": False, "error": "timeout expired"})
        self.assertEqual(out["relay"]["status"], "down")

    def test_connection_is_closed_even_when_a_core_query_fails(self):
        _, _, conn = run_collect(GamesCur(boom="GROUP BY game ORDER BY 4 DESC"))
        self.assertTrue(conn.closed)


class TestUnit(unittest.TestCase):
    def test_event_texts(self):
        self.assertEqual(hub.event_text("defcon_change", {"from": 3, "to": 2}), "DEFCON 3 → 2")
        self.assertEqual(hub.event_text("approval_swing", {"from": 66, "to": 4.6, "change": -61.4}), "Approval 66% → 4.6%")
        self.assertEqual(hub.event_text("nuclear_strike", {"attacker": "USA", "target": "BLZ", "warheads": 13, "casualties": 104000000}),
                         "☢ USA nukes BLZ: 13 warheads, 104M dead")
        self.assertEqual(hub.event_text("record_set", {"record": "lowestApproval", "value": 1.6, "previous": 2.7}),
                         "Record: lowest approval 1.6 (was 2.7)")
        self.assertEqual(hub.event_text("turn", {"turn": 4, "action": "Ask figure", "failed": True}), "Turn 4: Ask figure (failed)")
        self.assertTrue(hub.event_text("game_over", {"won": True, "turns": 48, "final_score": 9617}).startswith("Victory after 48 turns · score 9.6k"))
        self.assertEqual(hub.event_text("statehood", {"name": "Guam"}), "Statehood: Guam")

    def test_event_levels(self):
        self.assertEqual(hub.event_level("nuclear_strike", {}), "bad")
        self.assertEqual(hub.event_level("defcon_change", {"from": 2, "to": 3}), "good")
        self.assertEqual(hub.event_level("defcon_change", {"from": 5, "to": 4}), "warn")
        self.assertEqual(hub.event_level("treaty_answered", {"accepted": False}), "warn")
        self.assertEqual(hub.event_level("record_set", {}), "gold")
        self.assertEqual(hub.event_level("nova_commentary", {}), "nova")
        self.assertEqual(hub.event_level("ticker", {"severity": 3}), "warn")
        self.assertEqual(hub.event_level("game_over", {"won": False}), "bad")

    def test_gtnw_extras_keep_the_last_twelve_polls(self):
        x = hub._gtnw_extras(GTNW_SUMMARY, {"game_date": "1801-08-20"})
        self.assertEqual(len(x["polls"]), 12)
        self.assertEqual(x["polls"][-1], 55.0)
        self.assertEqual((x["defcon"], x["turn"], x["game_date"]), (1, 7, "1801-08-20"))

    def test_odd_shapes_never_raise(self):
        for d in (None, "x", 3, [], {"from": "a", "to": None}):
            for k in ("defcon_change", "approval_swing", "game_over", "nope", "", None):
                self.assertIsInstance(hub.event_text(k, d), str)
                self.assertIn(hub.event_level(k, d), {"bad", "warn", "good", "gold", "nova", "start", "info"})


class TestIntegration(unittest.TestCase):
    def test_full_payload_shape(self):
        out, _, _ = run_collect()
        live = out["live"][0]
        self.assertEqual((live["elapsed_s"], live["events_5m"], live["events"]), (240, 40, 111))
        self.assertEqual(live["apm"], 2.0)
        self.assertEqual(live["last_event"]["text"], "DEFCON 3 → 2")
        self.assertEqual(live["defcon"], 1)
        self.assertEqual(live["game_date"], "1801-08-20")
        self.assertEqual(out["timeline"]["series"][0]["game"], "gtnw")
        self.assertEqual(out["timeline"]["series"][0]["counts"][-1], 9)       # this minute is last
        self.assertEqual(out["timeline"]["series"][0]["counts"][0], 4)        # 59 min ago is first; 75 is dropped
        self.assertEqual(out["week"]["win_rate"], round(1 / 14, 3))
        self.assertEqual(len(out["week"]["days"]), 7)
        self.assertEqual(out["ai_share"]["ai"], 6)
        self.assertEqual(out["ai_share"]["other"], 60)
        self.assertEqual(out["fame"][0]["who"], "United States")
        cartest = [t for t in out["today"] if t["game"] == "cartest"][0]
        self.assertEqual((cartest["sessions"], cartest["events"]), (0, 1))     # events without a session row today

    def test_games_view_passes_the_new_parts_through(self):
        out, _, _ = run_collect()
        with hub._EXTRAS_LOCK:
            hub._EXTRAS["games"] = out
        v = hub.games_view(time.time())
        for k in ("timeline", "feed", "today", "week", "fame", "ai_share", "llm", "updated"):
            self.assertIn(k, v)
        json.dumps(v)


class TestFunctional(unittest.TestCase):
    def test_feed_collapses_a_burst_and_keeps_newest_first(self):
        out, _, _ = run_collect()
        f = out["feed"]
        self.assertEqual([e["kind"] for e in f], ["nuclear_strike", "world_claim", "treaty_answered"])
        self.assertEqual(f[1]["count"], 3)
        self.assertEqual(f[1]["text"], "PRT claims SLE")
        self.assertEqual(f[0]["level"], "bad")
        self.assertEqual(f[2]["level"], "good")
        self.assertEqual(f[0]["id"], f"{SID}:30")

    def test_nobody_playing_is_an_empty_live_list(self):
        out, _, _ = run_collect(GamesCur(live=[]))
        self.assertEqual(out["live"], [])
        self.assertNotIn("partial", out)

    def test_today_is_sorted_by_play_time(self):
        out, _, _ = run_collect()
        self.assertEqual([t["game"] for t in out["today"]], ["gtnw", "blompie-mac", "cartest"])


class TestFrame(unittest.TestCase):
    def test_selftest_passes(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_minion_hub.py"), "--selftest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_docstring_documents_the_new_payload(self):
        doc = hub.__doc__
        for k in ("timeline", "feed", "today", "week", "fame", "ai_share", "partial"):
            self.assertIn(k, doc)

    def test_days_are_burbank_days(self):
        self.assertEqual(hub.GAMES_TZ, "America/Los_Angeles")
        self.assertIn(hub.GAMES_TZ, hub._TODAY)


if __name__ == "__main__":
    unittest.main()
