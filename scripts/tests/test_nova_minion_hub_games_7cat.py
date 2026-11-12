#!/usr/bin/env python3
"""7-category tests for the hub's Games 1.4 payload (2026-11-11, Nova Cluster 1.4.0, Jordan: "pretty-up the games
section of Nova Cluster. More stats about live game play"): live-session enrichment (elapsed, APM, last event,
GTNW DEFCON / turn / approval polls), the 60-minute activity timeline, the live feed (human one-liners + levels,
bursts collapsed), today per game, the 7-day roll-up, the hall of fame and the AI share.
1.5 (2026-11-11): the GTNW Chess rivalry block (head-to-head per opponent, streaks, accuracy, recent games, newest
review link) and its privacy rule (a human opponent is "Challenger" unless play.consent names them).

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


def _side(name, kind="human", account=None, local=True, level=None, level_name=None):
    return {"name": name, "kind": kind, "account": account, "local": local, "level": level, "level_name": level_name}


def chess_summary(mode, white, black, you, result, winner, term="checkmate", acc=(88.4, 70.2), eco=("C50", "Italian Game"), moves=29):
    return {"mode": mode, "players": {"white": white, "black": black}, "you": you, "result": result, "winner": winner,
            "termination": term, "opening": {"eco": eco[0], "name": eco[1]}, "plies": moves * 2 - 1, "moves": moves,
            "analysis": None if acc is None else {"engine": "Stockfish 19", "depth": 12, "white": {"accuracy": acc[0]},
                                                  "black": {"accuracy": acc[1]}}}


LM_W = _side("Little Mister", account="default")
AMY_B = _side("Amy", account="amy", local=False)
# Newest first, as SQL_CHESS returns them. Little Mister vs Amy (chronologically): W D L W W; vs Nova: W.
# Amy vs Nova on her own Mac is a recent game but not part of his head-to-head; the abandoned game counts nowhere.
CHESS_ROWS = [
    ("c0", "default", NOW, "win", chess_summary("lan", LM_W, AMY_B, "white", "1-0", "white", acc=(91.0, 74.0))),
    ("c1", "default", NOW - timedelta(hours=1), "win", chess_summary("lan", LM_W, AMY_B, "white", "1-0", "white", "resignation")),
    ("c2", "amy", NOW - timedelta(hours=2), "win",                       # Amy hosted: her session, he was black (no account)
     chess_summary("lan", _side("Amy"), _side("Little Mister", local=False), "white", "1-0", "white", acc=(80.0, 60.0))),
    ("c3", "default", NOW - timedelta(hours=3), "win",
     chess_summary("vs_nova", _side("Nova", "nova", level=3, level_name="Cadet"), _side("Little Mister"), "black", "0-1", "black",
                   acc=(55.0, 77.0), eco=("B01", "Scandinavian Defense"))),
    ("c4", "amy", NOW - timedelta(hours=4), "loss",
     chess_summary("vs_nova", _side("Amy"), _side("Nova", "nova", level=5, level_name="Captain"), "white", "0-1", "black")),
    ("c5", "default", NOW - timedelta(hours=5), "draw",
     chess_summary("lan", LM_W, AMY_B, "white", "1/2-1/2", None, "agreement", acc=None)),
    ("c6", "default", NOW - timedelta(hours=6), "win", chess_summary("lan", LM_W, AMY_B, "white", "1-0", "white")),
    ("c7", "default", NOW - timedelta(hours=7), "abandoned", chess_summary("lan", LM_W, AMY_B, "white", "*", None, "abandoned")),
]
REVIEW = ("c0", {"at": "2026-11-11T19:50:00+00:00", "url": "https://nova.digitalnoise.net/nova/2026-11-11-the-italian-job/",
                 "game": "gtnwchess", "title": "The Italian Job", "status": "live", "published_at": "2026-11-11T19:49:00+00:00"})


class OpsCur:
    """nova_ops for the review link (service_config nova_game_report.reported)."""

    def __init__(self, row=REVIEW, boom=False):
        self.row, self.boom, self.calls = row, boom, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.boom:
            raise RuntimeError("connection to nova_ops lost")

    def fetchone(self):
        return self.row


class GamesCur:
    """Answers collect_games' queries by shape; `boom` names a query fragment that raises."""

    def __init__(self, boom: str | None = None, live=(LIVE_ROW,), feed=FEED_ROWS, chess=(), consent=None,
                 consent_table=True, recent=None):
        self.boom, self.live, self.feed = boom, list(live), list(feed)
        self.chess, self.consent, self.consent_table, self.recent = list(chess), list(consent or []), consent_table, recent
        self.sql, self.calls = "", []

    def execute(self, sql, params=None):
        self.sql = sql
        self.calls.append((sql, params))
        if self.boom and self.boom in sql:
            raise RuntimeError("permission denied for " + self.boom)

    def fetchone(self):
        if "to_regclass('play.consent')" in self.sql:
            return (self.consent_table,)
        if "count(*) FILTER (WHERE ts > now() - interval '1 hour')" in self.sql:
            return (659, 134)
        if "FILTER (WHERE outcome IN ('victory', 'win')), count(*) FILTER (WHERE outcome IN ('defeat', 'loss')), count(*) " in self.sql:
            return (1, 13, 16)
        if "casting.ai_calls" in self.sql:
            return (2, 600, 8000)
        return (1,)

    def fetchall(self):
        q = self.sql
        if "FROM play.consent" in q:
            return self.consent
        if "game = 'gtnwchess' AND ended_at IS NOT NULL" in q:
            return self.chess
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
        if "ORDER BY score DESC" in q:
            return [("gtnw", "default", {"leader": "", "country_name": "United States", "turns": 198}, 9617.0, "defeat", NOW)]
        if "detail ? 'source'" in q:
            return [("gtnw", "template", 58), ("blompie-mac", "llm", 6), ("blompie-mac", "fallback", 2)]
        if "FROM play.sessions ORDER BY last_seen_at DESC" in q:
            return self.recent if self.recent is not None else [LIVE_ROW[:-1]]
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


def run_collect(cur=None, relay=b'{"status": "ok", "ok": 3}', ops=None):
    cur = cur or GamesCur()
    conn = FakeConn(cur)
    resp = mock.MagicMock()
    resp.__enter__.return_value.read.return_value = relay
    with mock.patch("psycopg2.connect", return_value=conn), mock.patch.object(hub, "_games_password", return_value="pw"), \
         mock.patch.object(hub.urllib.request, "urlopen", return_value=resp), mock.patch.object(hub, "log"):
        return hub.collect_games(ops), cur, conn


def run_chess(consent=None, consent_table=True, ops=None, **kw):
    out, cur, _ = run_collect(GamesCur(chess=CHESS_ROWS, consent=consent, consent_table=consent_table, **kw), ops=ops or OpsCur())
    return out, cur


def rival(out, kind):
    return next(r for r in out["chess"]["rivals"] if r["kind"] == kind)


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


    def test_chess_opponent_is_challenger_without_consent_and_no_name_leaks(self):
        chess_live = ("c9", "gtnwchess", "mac", "amy", NOW - timedelta(minutes=3), NOW, None, None, None, 12,
                      chess_summary("lan", _side("Amy"), _side("Little Mister", local=False), "white", "*", None, None))
        out, _ = run_chess(recent=[chess_live[:11]])
        body = json.dumps(out, default=str)
        self.assertNotIn("Amy", body)
        self.assertNotIn('"amy"', body)
        self.assertEqual(rival(out, "human")["name"], "Challenger")
        self.assertEqual(out["recent"][0]["who"], "Challenger vs Little Mister")
        self.assertEqual(out["recent"][0]["player"], "challenger")

    def test_chess_consent_shows_only_the_display_name(self):
        out, _ = run_chess(consent=[("amy", True, "Ace\u202e\n")])
        self.assertEqual(rival(out, "human")["name"], "Ace")
        self.assertNotIn("Amy", json.dumps(out["chess"]))
        for consent in ([("amy", False, "Ace")], [("amy", True, "")], [("amy", True, "Ace"), ("amy", False, "Ace")],
                        [("someone-else", True, "Zed")]):
            out, _ = run_chess(consent=consent)
            self.assertEqual(rival(out, "human")["name"], "Challenger", consent)

    def test_chess_missing_consent_table_is_no_consent(self):
        out, cur = run_chess(consent=[("amy", True, "Ace")], consent_table=None)
        self.assertEqual(rival(out, "human")["name"], "Challenger")
        self.assertFalse(any("FROM play.consent" in sql for sql, _ in cur.calls))
        out, _ = run_chess(boom="FROM play.consent")           # no grant yet: still no consent, not a failure
        self.assertEqual(rival(out, "human")["name"], "Challenger")
        self.assertNotIn("partial", out)

    def test_chess_review_link_only_on_novas_site(self):
        for url in ("javascript:alert(1)", "https://evil.example/nova/", "https://nova.digitalnoise.net.evil.com/x",
                    'https://nova.digitalnoise.net/a"<script>'):
            row = ("c0", dict(REVIEW[1], url=url))
            self.assertIsNone(hub.chess_review(OpsCur(row)), url)
        ops = OpsCur(); hub.chess_review(ops)
        self.assertEqual(ops.calls[0][1], ("gtnwchess", "https://nova.digitalnoise.net/%"))


class TestPerformance(unittest.TestCase):
    def test_collect_is_a_bounded_number_of_queries(self):
        _, cur, _ = run_collect()
        self.assertLessEqual(len(cur.calls), 24)          # 1.5: + consent (2) + chess (1)

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


    def test_end_of_game_move_burst_stays_out_of_feed_and_timeline(self):
        for sql in (hub.SQL_FEED, hub.SQL_TIMELINE):
            self.assertIn("NOT (s.game = 'gtnwchess' AND e.kind = 'move')", sql)
        self.assertNotIn("gtnwchess", hub.SQL_TODAY_EVENTS, "the day's event count still includes them")

    def test_chess_reads_a_bounded_slice_without_pgn(self):
        self.assertIn(f"LIMIT {hub.CHESS_MAX}", hub.SQL_CHESS)
        self.assertIn("- 'pgn' - 'material_graph'", hub.SQL_CHESS)
        self.assertLessEqual(hub.CHESS_MAX, 1000)

    def test_chess_view_over_the_max_rows_is_fast_and_recent_is_capped(self):
        rows = [(f"x{i}",) + CHESS_ROWS[i % 7][1:] for i in range(hub.CHESS_MAX)]
        t0 = time.time()
        v = hub.chess_view(rows, {})
        self.assertLess(time.time() - t0, 0.5)
        self.assertEqual(len(v["recent"]), hub.CHESS_RECENT_N)
        self.assertEqual(v["games"], hub.CHESS_MAX)


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


    def test_failing_chess_query_keeps_the_rest(self):
        out, _ = run_chess(boom="game = 'gtnwchess' AND ended_at IS NOT NULL")
        self.assertEqual(out["partial"], ["chess"])
        self.assertNotIn("chess", out)
        for k in ("feed", "today", "week", "fame", "live"):
            self.assertIn(k, out)

    def test_review_failure_keeps_the_rivalry(self):
        out, _ = run_chess(ops=OpsCur(boom=True))
        self.assertEqual(out["partial"], ["chess_review"])
        self.assertIsNone(out["chess"]["review"])
        self.assertEqual(rival(out, "human")["games"], 5)

    def test_no_ops_cursor_or_no_review_is_none(self):
        self.assertIsNone(hub.chess_review(None))
        self.assertIsNone(hub.chess_review(OpsCur(row=None)))


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


    def test_chess_event_texts_and_levels(self):
        mv = {"ply": 32, "move_number": 16, "side": "black", "san": "Qxf2+", "classification": "blunder", "best_san": "Qd8"}
        self.assertEqual(hub.event_text("move", mv), "16… Qxf2+ — blunder (best Qd8)")
        self.assertEqual(hub.event_level("move", mv), "bad")
        self.assertEqual(hub.event_text("move", {"move_number": 3, "side": "white", "san": "Bc4", "classification": "best"}), "3. Bc4")
        self.assertEqual(hub.event_level("move", {"san": "Nf3", "classification": "mistake"}), "warn")
        self.assertEqual(hub.event_text("move", {"move_number": 20, "side": "white", "san": "Qh7#", "mate": True}), "20. Qh7# — checkmate")
        self.assertEqual(hub.event_level("move", {"san": "Qh7#", "mate": True}), "gold")
        self.assertEqual(hub.event_text("draw_offer", {"by": "black", "accepted": False}), "Draw offered by Black — declined")
        self.assertEqual(hub.event_level("draw_offer", {"accepted": True}), "good")
        self.assertEqual(hub.event_text("resign", {"by": "white"}), "White resigns")
        go = {"result": "1-0", "winner": "white", "termination": "checkmate", "opening": {"eco": "C50", "name": "Italian Game"},
              "white_accuracy": 87.4, "black_accuracy": 71.0}
        self.assertEqual(hub.event_text("game_over", go), "Chess: 1-0 · checkmate · Italian Game · accuracy 87/71")
        self.assertEqual(hub.event_level("game_over", go), "gold")
        self.assertEqual(hub.event_level("game_over", dict(go, winner=None, result="1/2-1/2")), "good")
        self.assertEqual(hub.event_text("game_start", {"mode": "vs_nova", "white": "Little Mister", "black": "Nova",
                                                       "white_kind": "human", "black_kind": "nova", "level": 3}),
                         "New chess game vs Nova · level 3")
        self.assertEqual(hub.event_text("game_start", {"mode": "lan", "white": "Amy", "white_kind": "human"}), "New chess game · LAN")
        # GTNW's game_over is untouched
        self.assertEqual(hub.event_level("game_over", {"won": False}), "bad")

    def test_live_ply_and_takeback_lines(self):
        self.assertEqual(hub.event_text("ply", {"ply": 3, "move_number": 2, "side": "white", "san": "Nf3", "capture": None,
                                                "check": False}), "2. ♘ Nf3")
        self.assertEqual(hub.event_text("ply", {"move_number": 7, "side": "black", "san": "exd4", "capture": "pawn"}),
                         "7… ♙ exd4 · takes pawn")
        self.assertEqual(hub.event_text("ply", {"move_number": 5, "side": "white", "san": "O-O"}), "5. ♔ O-O")
        self.assertEqual(hub.event_level("ply", {"san": "Bb5+", "check": True}), "warn")
        self.assertEqual(hub.event_level("ply", {"san": "e4"}), "info")
        self.assertEqual(hub.event_text("takeback", {"from_ply": 12, "to_ply": 10}), "Takeback: ply 12 → 10")
        self.assertEqual(hub.event_level("takeback", {}), "warn")

    def test_chess_sides_and_results(self):
        rows = {r[0]: r for r in CHESS_ROWS}
        self.assertEqual(hub.chess_lm_side("default", rows["c0"][4]), "white")       # account default
        self.assertEqual(hub.chess_lm_side("amy", rows["c2"][4]), "black")           # Amy hosted the LAN game
        self.assertEqual(hub.chess_lm_side("default", rows["c3"][4]), "black")       # vs Nova: the local side
        self.assertIsNone(hub.chess_lm_side("amy", rows["c4"][4]))                   # Amy vs Nova
        hot = chess_summary("hotseat", _side("Little Mister"), _side("Amy"), "both", "1-0", "white")
        self.assertEqual(hub.chess_lm_side("default", hot), "white")
        self.assertIsNone(hub.chess_lm_side("default", chess_summary("hotseat", _side("A"), _side("B"), "both", "1-0", "white")))
        self.assertIsNone(hub.chess_lm_side("default", chess_summary("lan", LM_W, dict(LM_W), "white", "1-0", "white")))
        self.assertEqual(hub.chess_lm_result("black", rows["c2"][4], "win", "amy"), "loss")
        self.assertEqual(hub.chess_lm_result("white", rows["c5"][4], "draw", "default"), "draw")
        self.assertIsNone(hub.chess_lm_result("white", rows["c7"][4], "abandoned", "default"))
        self.assertEqual(hub.chess_lm_result("white", {"you": "white"}, "win", "default"), "win")   # no result: the writer's outcome

    def test_streaks_and_longest(self):
        v = hub.chess_view(CHESS_ROWS, {})
        h = next(r for r in v["rivals"] if r["kind"] == "human")
        self.assertEqual((h["games"], h["wins"], h["draws"], h["losses"]), (5, 3, 1, 1))
        self.assertEqual(h["streak"], {"result": "win", "n": 2})
        self.assertEqual(h["longest"], {"wins": 2, "losses": 1})


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


    def test_full_chess_block(self):
        out, _ = run_chess()
        c = out["chess"]
        self.assertEqual((c["games"], c["counted"]), (8, 6))
        self.assertEqual(c["record"], {"wins": 4, "draws": 1, "losses": 1})
        self.assertEqual([r["kind"] for r in c["rivals"]], ["human", "nova"], "humans first, then Nova")
        n = rival(out, "nova")
        self.assertEqual((n["name"], n["wins"], n["streak"]), ("Nova", 1, {"result": "win", "n": 1}))
        self.assertEqual(n["accuracy"], {"you": 77.0, "them": 55.0, "n": 1})
        h = rival(out, "human")
        self.assertEqual(h["accuracy"]["n"], 4)                                 # the draw had no analysis
        self.assertEqual(h["accuracy"]["you"], round((91.0 + 88.4 + 60.0 + 88.4) / 4, 1))
        self.assertEqual(c["review"], {"url": REVIEW[1]["url"], "title": "The Italian Job", "at": "2026-11-11T19:49:00+00:00"})
        json.dumps(c)

    def test_games_view_passes_chess_through(self):
        out, _ = run_chess()
        with hub._EXTRAS_LOCK:
            hub._EXTRAS["games"] = out
        self.assertIn("rivals", hub.games_view(time.time())["chess"])


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


    def test_recent_chess_games_by_role(self):
        out, _ = run_chess()
        r = out["chess"]["recent"]
        self.assertEqual(len(r), hub.CHESS_RECENT_N)
        self.assertEqual((r[0]["white"]["name"], r[0]["black"]["name"], r[0]["lm"], r[0]["lm_result"]),
                         ("Little Mister", "Challenger", "white", "win"))
        self.assertEqual((r[0]["opening"], r[0]["termination"], r[0]["moves"]), ("C50 Italian Game", "checkmate", 29))
        self.assertEqual((r[0]["white"]["accuracy"], r[0]["black"]["accuracy"]), (91.0, 74.0))
        self.assertEqual((r[2]["white"]["name"], r[2]["black"]["name"], r[2]["lm_result"]), ("Challenger", "Little Mister", "loss"))
        amy_vs_nova = r[4]
        self.assertEqual((amy_vs_nova["white"]["name"], amy_vs_nova["black"]["name"], amy_vs_nova["lm"], amy_vs_nova["level"]),
                         ("Challenger", "Nova", None, "Captain"))
        self.assertEqual(r[3]["mode"], "vs Nova")

    def test_chess_wins_count_in_today_and_week_but_not_fame(self):
        self.assertIn("outcome IN ('victory', 'win')", hub.SQL_TODAY)
        self.assertIn("outcome IN ('defeat', 'loss')", hub.SQL_WEEK_WL)
        self.assertIn("game <> 'gtnwchess'", hub.SQL_FAME)

    def test_no_chess_games_is_an_empty_rivalry(self):
        out, _, _ = run_collect(ops=OpsCur(row=None))
        self.assertEqual(out["chess"]["rivals"], [])
        self.assertEqual(out["chess"]["record"], {"wins": 0, "draws": 0, "losses": 0})
        self.assertIsNone(out["chess"]["review"])


class TestFrame(unittest.TestCase):
    def test_selftest_passes(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_minion_hub.py"), "--selftest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_docstring_documents_the_new_payload(self):
        doc = hub.__doc__
        for k in ("timeline", "feed", "today", "week", "fame", "ai_share", "partial", "chess", "rivals", "Challenger",
                  "play.consent"):
            self.assertIn(k, doc)

    def test_chess_payload_keys_are_stable_for_the_app(self):
        c = run_chess()[0]["chess"]
        self.assertEqual(set(c), {"games", "counted", "record", "accuracy", "rivals", "recent", "review"})
        self.assertEqual(set(c["rivals"][0]), {"key", "name", "kind", "games", "wins", "draws", "losses", "streak",
                                               "longest", "accuracy", "last"})
        self.assertTrue(c["rivals"][0]["key"].startswith("h-") and "amy" not in c["rivals"][0]["key"])
        self.assertEqual(set(c["recent"][0]), {"id", "ended", "mode", "white", "black", "lm", "lm_result", "result",
                                               "termination", "opening", "moves", "plies", "level"})

    def test_days_are_burbank_days(self):
        self.assertEqual(hub.GAMES_TZ, "America/Los_Angeles")
        self.assertIn(hub.GAMES_TZ, hub._TODAY)


if __name__ == "__main__":
    unittest.main()
