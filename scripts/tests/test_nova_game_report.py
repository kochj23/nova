#!/usr/bin/env python3
"""Tests for nova_game_report.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_game_report.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_game_report_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gr = _load()
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 — computed at call time, never at import
_NUM_RE = re.compile(r"\d[\d,.%]*\d%?|\d%")


def fake_nj(hugo_root: Path, draft: str = "", publish_ok: bool = True):
    """A stand-in for nova_journal: records publishes, writes the file like publish_hugo does."""
    nj = types.SimpleNamespace()
    nj.HUGO_ROOT = hugo_root
    nj.logs, nj.published, nj.pushed = [], [], []
    nj.log = lambda m: nj.logs.append(m)
    nj._NUM_RE = _NUM_RE

    def new_numbers(d, s, e):
        seen = set(_NUM_RE.findall(d)) | set(_NUM_RE.findall(s or ""))
        return sorted({n.rstrip(".,") for n in _NUM_RE.findall(e)} - {n.rstrip(".,") for n in seen})
    nj.new_numbers = new_numbers
    nj.scrub_household = lambda t: t
    nj.call_openrouter = MagicMock(return_value=draft)
    nj.get_image_prompt = MagicMock(return_value="a war room")
    nj.generate_image = MagicMock(return_value=None)
    nj.notify_slack = MagicMock()

    def publish_hugo(title, body, section, tags, desc, **kw):
        nj.published.append({"title": title, "body": body, "section": section, "tags": tags, **kw})
        if not publish_ok:
            return False
        d = hugo_root / "content" / section
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
        (d / f"2026-10-18-{slug}.md").write_text(body)
        return True
    nj.publish_hugo = publish_hugo
    nj.git_push = lambda section, title: nj.pushed.append(title) or "pushed"
    return nj


class FakeOpsCursor:
    """service_config in memory (the only nova_ops table the job touches) + the advisory lock."""

    def __init__(self, cfg=None):
        self.rows = {}
        if cfg is not None:
            self.rows[("nova_game_report", "config")] = cfg
        self._last = None
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self._last = (True,)
        elif sql.startswith("SELECT value FROM service_config"):
            v = self.rows.get(tuple(params))
            self._last = (v,) if v is not None else None
        elif sql.startswith("INSERT INTO service_config"):
            self.rows[(params[0], params[1])] = json.loads(params[2])

    def fetchone(self):
        return self._last


def session(**kw):
    now = NOW()
    s = {"id": "5bceb732-c6d7-47f6-af6b-d2532f7e0ee9", "game": "gtnw", "client": "mac", "player": "default",
         "started_at": now - timedelta(minutes=17), "last_seen_at": now - timedelta(minutes=1),
         "ended_at": now - timedelta(minutes=1), "outcome": "defeat", "score": 0.0, "actions": 36,
         "summary": {"turns": 36, "reason": "Your nation has been destroyed in nuclear fire", "casualties": 25019519,
                     "popularity_start": 58, "popularity_peak": 81.9}}
    s.update(kw)
    return s


class FakeGamesCursor:
    def __init__(self, sessions, events=None, history=None, consent=None):
        self.sessions, self.events, self.history = sessions, events or [], history or []
        self.consent = consent   # play.consent row tuple, None (no row) or "missing" (table not deployed)
        self._rows = []
        self._one = None
        self.sql = []

    def fetchone(self):
        return self._one

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if "FROM play.consent" in sql:
            if self.consent == "missing":
                raise RuntimeError('relation "play.consent" does not exist')
            self._one = self.consent
        elif "FROM play.events" in sql:
            self._rows = [(e["seq"], e["kind"], e["detail"]) for e in self.events]
        elif "id <> %s" in sql:
            cols = ("id", "client", "started_at", "last_seen_at", "ended_at", "outcome", "score", "actions", "summary")
            self._rows = [tuple(h[c] for c in cols) for h in self.history]
        else:
            self._rows = [tuple(s[c] for c in gr.SESSION_COLS) for s in self.sessions]

    def fetchall(self):
        return self._rows


DRAFT = ("TITLE: Little Mister Nuked Uruguay\n\n"
         "Little Mister played the macOS app and lost in 36 turns. Approval went from 58 to 81.9 and then he "
         "pressed the button. " + "He lost the war of attrition with himself. " * 60 +
         "Jordan configured a lamp once. Casualties came to 999,999 in my imagination.")


# ── GTNW Chess fixtures (a real, legal game: the Italian, White hangs a knight on move 9 and is mated on 14) ───
IT = ("e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d3 d6 O-O O-O Re1 a6 Bb3 Ba7 Nxe5 Nxe5 d4 Neg4 Rf1 Nxe4 Qf3 Qh4 Bf4 Bd7 "
      "g3 Qxh2#").split()
IT_MARKS = {17: ("blunder", 0.2, -2.6, "Nbd2"), 21: ("mistake", -3.2, -4.6, "Bg5"), 27: ("blunder", -5.2, -20.0, "Bg3")}
HH = "Amy"   # a household name nova_journal.scrub_household already knows


def chess_events(white="Little Mister", black=HH, sans=IT):
    ev = [{"seq": 0, "kind": "game_start", "detail": {"mode": "lan", "white": white, "black": black,
                                                      "white_kind": "human", "black_kind": "human"}}]
    for i, san in enumerate(sans):
        ply, c = i + 1, IT_MARKS.get(i + 1)
        ev.append({"seq": ply, "kind": "move", "detail": {
            "ply": ply, "move_number": (ply + 1) // 2, "side": "white" if i % 2 == 0 else "black", "san": san,
            "classification": c[0] if c else "good", "eval_before": c[1] if c else 0.1,
            "eval_after": c[2] if c else 0.1, "best_san": c[3] if c else None, "clock_ms": 412345}})
    ev.append({"seq": len(sans) + 1, "kind": "game_over", "detail": {"result": "0-1", "winner": "black",
                                                                     "termination": "checkmate"}})
    return ev


def chess_summary(white=None, black=None, **kw):
    s = {"mode": "lan", "you": "white", "result": "0-1", "winner": "black", "termination": "checkmate",
         "players": {"white": white or {"name": "Little Mister", "kind": "human", "local": True, "account": "default"},
                     "black": black or {"name": HH, "kind": "human", "local": False, "account": "amy"}},
         "opening": {"eco": "C50", "name": "Italian Game"}, "plies": 28, "moves": 14, "duration_seconds": 860,
         "captures": {"white": {"pawn": 1, "knight": 0}, "black": {"pawn": 2, "knight": 1}},
         "checks": {"white": 0, "black": 1}, "material_graph": [0] * 28,
         "pgn": f'[Event "GTNW Chess"]\n[White "Little Mister"]\n[Black "{HH}"]\n\n1. e4 e5 2. Nf3 Nc6 0-1',
         "analysis": {"engine": "Stockfish 19", "depth": 12,
                      "white": {"accuracy": 58.4, "acpl": 61, "blunders": 2, "mistakes": 1, "inaccuracies": 0},
                      "black": {"accuracy": 81.2, "acpl": 38, "blunders": 0, "mistakes": 0, "inaccuracies": 0},
                      "biggest_swing": {"ply": 27, "move_number": 14, "side": "white", "san": "g3", "eval_before": -5.2,
                                        "eval_after": -20.0, "swing": 14.8, "best_san": "Bg3"},
                      "decisive_moment": {"ply": 17, "move_number": 9, "side": "white", "san": "Nxe5",
                                          "eval_before": 0.2, "eval_after": -2.6, "swing": 2.8, "best_san": "Nbd2",
                                          "note": f"{HH}'s knight took the gift."}},
         "notable": ["Castled kingside on move 6 (white)", f"{HH} castled kingside on move 6"]}
    s.update(kw)
    return s


def chess_session(**kw):
    s = session(id="c0ffee00-1111-4222-8333-444455556666", game="gtnwchess", outcome="loss", score=58.4, actions=28,
                summary=chess_summary())
    s.update(kw)
    return s


def chess_view_row(result, i, kind="human"):
    return {"id": str(i), "when": NOW() - timedelta(days=1000 - i), "lm_side": "white", "opp_side": "black",
            "result": result, "opponent_kind": kind, "lm_accuracy": 60.0, "opp_accuracy": 70.0}


CHESS_DRAFT = ("TITLE: Amy Mates Little Mister\n\n"
               "Little Mister lost to Amy on the macOS app, and Amy's Qxh2 finished it. 9. Nxe5 was the blunder, "
               "answered by 9... Nxe5. The engine wanted 9. Nbd2. Then 14. g3 walked into 14... Qxh2#. " +
               "The middlegame was a slow leak of position and patience. " * 60 +
               "Later 15. Qxg7# would have saved him. Amy's 3... Bb4 was a masterpiece. Final word: Amy.")


def consent(on=True, name=HH, account="amy"):
    return {"account": account, "name_in_reviews": on, "display_name": name}


# ── Security ────────────────────────────────────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)password\s*=\s*['\"][^'\"]+['\"]")
        self.assertIn('_keychain(cfg["keychain_service"]', SRC)
        self.assertIn('sslmode="verify-full"', SRC)

    def test_sql_is_parameterized(self):
        for m in re.finditer(r'execute\(\s*f?"([^"]*)"', SRC):
            self.assertNotRegex(m.group(1), r"\{(?!cols\})", "values must be bound parameters")
        self.assertIn("readonly=True", SRC)

    def test_fact_sheet_scrubs_ips_paths_emails_times_and_work(self):
        cfg = dict(gr.DEFAULTS)
        home = str(Path.home())
        s = session(summary={"note": f"from 192.168.1.6 via {home}/x and " + "kochj23" + "@" + "gmail.com",
                             "work": "a Disney project", "started_at": "2026-10-18T21:08:55Z",
                             "ticker": [{"date": "2026-10-18T03:12:00Z", "text": "ok"}]})
        txt = gr.facts_text(gr.build_facts(s, [], [], cfg))
        for bad in ("192.168.1.6", home, "gmail", "Disney", "21:08", "03:12", "started_at"):
            self.assertNotIn(bad, txt)
        self.assertIn("2026-10-18", txt)  # the date stays

    def test_real_name_never_published(self):
        self.assertEqual(gr.guard_names("Jordan's game, Jordan Koch played"),
                         "Little Mister's game, Little Mister Little Mister played")

    # GTNW Chess privacy: household names never reach the sheet or the public column without consent
    def _publish_chess(self, s, consent_row=None, draft=CHESS_DRAFT, real_scrub=False):
        with tempfile.TemporaryDirectory() as d:
            nj = fake_nj(Path(d), draft=draft)
            if real_scrub:
                import nova_journal as real
                nj.scrub_household = real.scrub_household
            g = FakeGamesCursor([s], events=chess_events(), consent=consent_row)
            with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": types.SimpleNamespace(
                    system_prompt=lambda ctx, section="": ctx)}), patch.object(gr, "ollama_chat", return_value=None):
                st = gr.process(s, g, FakeOpsCursor(), {}, dict(gr.DEFAULTS))
            return st, nj

    def test_chess_household_name_never_reaches_sheet_or_published_text(self):
        """PRIVACY: 'Amy' in summary.players, the PGN headers, game_start, an analysis note and notable lines, and in
        the model's draft (title + body): none of it reaches the fact sheet or anything published."""
        s = chess_session()
        f = gr.build_facts(s, chess_events(), [], dict(gr.DEFAULTS))
        sheet = gr.facts_text(f)
        self.assertNotRegex(sheet, r"(?i)\bamy\b")
        self.assertNotIn("pgn", sheet.lower())
        self.assertNotIn("clock_ms", sheet)
        self.assertEqual(f["chess"]["players"], {"white": "Little Mister", "black": "his opponent"})
        st, nj = self._publish_chess(s)
        self.assertEqual(st, "published")
        pub = nj.published[0]
        for text in (pub["title"], pub["body"], pub["sources"], json.dumps(pub["tags"])):
            self.assertNotRegex(text, r"(?i)\bamy\b")
        self.assertIn("his opponent", pub["body"])
        self.assertIsNone(pub.get("extra_front"))

    def test_chess_consent_on_names_only_her_first_name(self):
        s = chess_session(summary=chess_summary(notable=["Dylan watched from the couch", "Amy McCaine castled"]))
        st, nj = self._publish_chess(s, consent_row=("amy", True, "Amy"),
                                     draft=CHESS_DRAFT + " Dylan cheered.", real_scrub=True)
        pub = nj.published[0]
        self.assertIn("Amy", pub["body"])
        self.assertEqual(pub["extra_front"], {"name_consent": "Amy"})
        for text in (pub["body"], pub["sources"]):
            self.assertNotIn("Dylan", text)       # every other household name stays redlined
            self.assertNotIn("McCaine", text)     # her full name too: first name only

    def test_chess_consent_off_or_mismatched_is_never_named(self):
        for row in (("amy", False, "Amy"), ("amy", True, "Amelia"), ("someone", True, "Amy"), None, "missing"):
            st, nj = self._publish_chess(chess_session(), consent_row=row)
            self.assertNotRegex(nj.published[0]["body"] + nj.published[0]["sources"], r"(?i)\bamy\b", row)
        # display name must match the name logged for her in THIS game
        self.assertIsNone(gr.chess_consent_name(chess_session(), "black", consent(name="Amelia")))
        self.assertIsNone(gr.chess_consent_name(chess_session(), "black", consent(account="other")))
        self.assertEqual(gr.chess_consent_name(chess_session(), "black", consent()), "Amy")

    def test_journal_honours_consent_only_from_front_matter(self):
        import nova_journal as real
        self.assertEqual(real.consented_names('---\ntitle: "x"\nname_consent: "Amy"\n---\nAmy won.'), ("Amy",))
        self.assertEqual(real.consented_names('---\ntitle: "x"\n---\nname_consent: "Amy"\n---\n'), ())
        self.assertEqual(real.scrub_household("Amy won. Dylan lost.", ("Amy",)), "Amy won. A household member lost.")
        self.assertEqual(real.scrub_household("Amy McCaine won.", ("Amy",)), "A family member won.")


# ── Performance ─────────────────────────────────────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_sanitize_and_history_on_10k_items_fast(self):
        big = {"ticker": [{"date": "2026-10-18T10:00:00Z", "text": f"event {i} at 10.0.0.{i % 250}"}
                          for i in range(10000)]}
        hist = [session(id=str(i), score=float(i)) for i in range(10000)]
        t = time.perf_counter()
        out = gr.sanitize(big, ["disney"])
        h = gr.history_stats(hist, session())
        self.assertLess(time.perf_counter() - t, 3.0)
        self.assertEqual(len(out["ticker"]), 80)  # lists are capped
        self.assertEqual(h["previous_sessions"], 10000)

    def test_existing_article_scan_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(1000):
                Path(d, f"2026-01-01-{i:04d}.md").write_text("x")
            t = time.perf_counter()
            self.assertIsNone(gr.find_existing_article("deadbeef-0000", Path(d)))
            self.assertLess(time.perf_counter() - t, 2.0)

    def test_chess_block_validator_and_tally_fast(self):
        ev = chess_events(sans=IT * 14)[:401]            # ~400 plies (legality irrelevant here)
        sheet = gr.facts_text(gr.build_facts(chess_session(), ev, [], dict(gr.DEFAULTS)))
        body = "He played 9. Nxe5 and then 99. Qh8 happened. " * 5000
        views = [chess_view_row(("win", "loss", "draw")[i % 3], i) for i in range(10000)]
        t = time.perf_counter()
        out, cut = gr.strip_unknown_moves(body, sheet)
        tally = gr.rivalry_tally(views, "his opponent")
        self.assertLess(time.perf_counter() - t, 3.0)
        self.assertEqual(len(cut), 5000)
        self.assertEqual(tally["games"], 10000)


# ── Retry ───────────────────────────────────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def setUp(self):
        p = patch.object(gr, "ollama_chat", return_value=None)
        self.local = p.start()
        self.addCleanup(p.stop)

    def test_writer_chain_falls_back_and_logs_which_wrote(self):
        with tempfile.TemporaryDirectory() as d:
            nj = fake_nj(Path(d), draft="")
            nj.call_openrouter.side_effect = TimeoutError("cli timed out")
            self.local.return_value = DRAFT
            with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": types.SimpleNamespace(
                    system_prompt=lambda ctx, section="": ctx)}):
                col = gr.write_column(session(), gr.build_facts(session(), [], [], dict(gr.DEFAULTS)),
                                      dict(gr.DEFAULTS))
            self.assertEqual(col[2], "ollama:qwen3:30b-a3b")
            self.assertTrue(any("column written by ollama:qwen3:30b-a3b" in m for m in nj.logs))

    def test_ollama_request_keeps_server_context_and_strips_reasoning(self):
        class R:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        seen = {}

        def urlopen(req, timeout):
            seen.update(json.loads(req.data.decode()))
            return R(json.dumps({"message": {"content": "plan plan</think>\nTITLE: X\n\nbody"}}).encode())
        real = _load().ollama_chat
        with patch("urllib.request.urlopen", side_effect=urlopen):
            out = real("http://127.0.0.1:11434", "qwen3:30b-a3b", "s", "u")
        self.assertEqual(out, "TITLE: X\n\nbody")
        self.assertNotIn("num_ctx", seen.get("options", {}))
        self.assertEqual(seen["keep_alive"], -1)

    def test_games_connect_retries_with_backoff(self):
        import psycopg2
        ok = MagicMock()
        calls = {"n": 0}

        def connect(**kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise psycopg2.OperationalError("down")
            return ok
        sleeps = []
        cfgmod = types.SimpleNamespace(_keychain=lambda *a, **k: "pw")
        with patch.dict(sys.modules, {"nova_config": cfgmod}), patch("psycopg2.connect", side_effect=connect):
            c = gr.games_connect(dict(gr.DEFAULTS), _sleep=sleeps.append)
        self.assertIs(c, ok)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2, 4])

    def test_llm_failure_retries_then_errors_without_publishing(self):
        with tempfile.TemporaryDirectory() as d:
            nj = fake_nj(Path(d), draft="")
            ops = FakeOpsCursor()
            with patch.dict(sys.modules, {"nova_journal": nj}):
                st = gr.process(session(), FakeGamesCursor([session()]), ops, {}, dict(gr.DEFAULTS))
            self.assertEqual(st, "error")
            self.assertEqual(nj.call_openrouter.call_count, 2)
            self.assertEqual(self.local.call_count, 2)  # then the local fallback, twice
            self.assertEqual(nj.published, [])
            rec = ops.rows[("nova_game_report", "reported")][session()["id"]]
            self.assertEqual((rec["status"], rec["attempts"]), ("error", 1))
            # retried on a later run, but only up to max_attempts
            self.assertTrue(gr.needs_work(rec, dict(gr.DEFAULTS)))
            self.assertFalse(gr.needs_work({**rec, "attempts": 3}, dict(gr.DEFAULTS)))

    def test_chess_consent_lookup_fails_open_unnamed(self):
        # RETRY GAP: fetch_consent — one read at writing time, no retry; any failure means "not consented"
        class Boom:
            def execute(self, *a):
                raise RuntimeError('relation "play.consent" does not exist')
        self.assertIsNone(gr.fetch_consent(Boom(), "amy"))
        self.assertIsNone(gr.chess_consent(Boom(), dict(gr.DEFAULTS)))

    def test_chess_draft_citing_invented_moves_is_retried(self):
        bad = "TITLE: X\n\n" + "Then 15. Qxg7# won it. " * 300
        with tempfile.TemporaryDirectory() as d:
            nj = fake_nj(Path(d))
            nj.call_openrouter.side_effect = [bad, CHESS_DRAFT]
            with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": types.SimpleNamespace(
                    system_prompt=lambda ctx, section="": ctx)}):
                col = gr.write_column(None, gr.build_facts(chess_session(), chess_events(), [], dict(gr.DEFAULTS)),
                                      dict(gr.DEFAULTS))
        self.assertEqual(nj.call_openrouter.call_count, 2)
        self.assertNotIn("Qxg7", col[1])
        self.assertTrue(any("moves not in the fact sheet" in m for m in nj.logs))


# ── Unit ────────────────────────────────────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_trivial_session_skip_is_configurable(self):
        cfg = dict(gr.DEFAULTS)
        now = NOW()
        short = session(started_at=now - timedelta(seconds=90), ended_at=now, last_seen_at=now)
        self.assertIn("trivial", gr.trivial_reason(short, cfg))
        self.assertIsNone(gr.trivial_reason(short, {**cfg, "min_minutes": 1}))
        self.assertIn("actions", gr.trivial_reason(session(actions=0), cfg))
        self.assertIsNone(gr.trivial_reason(session(), cfg))

    def test_hidden_time_posts_stamp_end_of_day(self):
        # noon buried evening game columns under the rest of the day; 23:59 hides play time and still sorts first
        src = (SCRIPT.parent / "nova_journal.py").read_text()
        self.assertIn('else "%Y-%m-%dT23:59:00-07:00"', src)

    def test_date_played_is_pacific_not_utc(self):
        # 2026-10-19 21:45 PDT is 2026-10-20 04:45 UTC; the column said "October 20"
        end = datetime(2026, 10, 20, 4, 45, tzinfo=timezone.utc)
        s = session(started_at=end - timedelta(minutes=10), ended_at=end, last_seen_at=end)
        self.assertEqual(gr.build_facts(s, [], [], dict(gr.DEFAULTS))["date_played"], "2026-10-19")

    def test_duration_uses_last_seen_for_abandoned(self):
        now = NOW()
        s = session(ended_at=None, started_at=now - timedelta(minutes=50), last_seen_at=now - timedelta(minutes=40))
        self.assertAlmostEqual(gr.duration_minutes(s), 10.0, places=1)

    def test_exactly_once_states(self):
        cfg = dict(gr.DEFAULTS)
        for final in ("published", "live", "skipped", "failed", "not_live"):
            self.assertFalse(gr.needs_work({"status": final, "at": gr._iso(NOW())}, cfg))
        self.assertTrue(gr.needs_work(None, cfg))
        self.assertFalse(gr.needs_work({"status": "claimed", "at": gr._iso(NOW()), "attempts": 1}, cfg))
        old = gr._iso(NOW() - timedelta(hours=2))
        self.assertTrue(gr.needs_work({"status": "claimed", "at": old, "attempts": 1}, cfg))
        self.assertTrue(gr.needs_work({"status": "live"}, cfg, force=True))

    def test_parse_draft_and_platform(self):
        self.assertEqual(gr.parse_draft("TITLE: **Big Loss**\n\nbody"), ("Big Loss", "body"))
        self.assertEqual(gr.parse_draft(""), (None, ""))
        self.assertIn("macOS", gr.platform_name("mac"))
        self.assertIn("web", gr.platform_name("web"))
        self.assertEqual(gr.game_name("gtnw", "web"), "GTNW Lite")
        self.assertEqual(gr.game_name("blompie-mac", "mac"), "Blompie for Mac")

    def test_stats_table_is_deterministic(self):
        f = gr.build_facts(session(), [], [], dict(gr.DEFAULTS))
        t = gr.stats_table(f, "gtnw")
        self.assertIn("| Platform | the macOS app |", t)
        self.assertIn("| Casualties | 25,019,519 |", t)
        self.assertIn("| Score | 0 |", t)
        self.assertIn("| Earlier sessions | none on record |", t)

    def test_number_check_normalises_commas_and_percent(self):
        with patch.dict(sys.modules, {"nova_journal": fake_nj(Path(tempfile.gettempdir()))}):
            body, inv = gr.strip_invented("He killed 25,019,519 at 81.9% approval. He scored 4242 too.",
                                          '{"casualties": 25019519, "popularity_peak": 81.9}')
        self.assertEqual(inv, ["4242"])
        self.assertIn("25,019,519", body)
        self.assertNotIn("4242", body)

    def test_selftest(self):
        self.assertEqual(gr.selftest(), 0)

    def test_chess_san_tokens_and_normalisation(self):
        found = [m.group(0) for m in gr.SAN_RE.finditer(
            "1. e4 then 14... Qxh2# and O-O-O, 0-0, e8=Q+, exd5, Nbd2, R1e2 but not C50, a4-paper or Stockfish 19")]
        self.assertEqual(found, ["1. e4", "14... Qxh2#", "O-O-O", "0-0", "e8=Q+", "exd5", "Nbd2", "R1e2"])
        self.assertEqual(gr._norm_san("0-0+"), "O-O")
        self.assertEqual(gr._norm_san("e8Q"), "e8=Q")
        self.assertEqual(gr.move_label(14, "black", "Qxh2#"), "14... Qxh2#")

    def test_chess_validator_cuts_only_sentences_with_unknown_moves(self):
        sheet = '["9. Nxe5", "9... Nxe5", "14... Qxh2#", "engine_preferred": "9. Nbd2"]'
        body = ("## Move 9\nHe played 9. Nxe5. It lost a piece. Better was 9. Nbd2. Then 15. Qxg7# came.\n"
                "## The 10. Qh5 attack\nHe also tried 12. Nxe5 there. Black's Qxh2# ended it.")
        out, cut = gr.strip_unknown_moves(body, sheet)
        self.assertEqual(cut, ["15. Qxg7#", "10. Qh5", "12. Nxe5"])   # wrong move number counts as unknown
        self.assertIn("He played 9. Nxe5.", out)
        self.assertIn("Better was 9. Nbd2.", out)
        self.assertIn("Black's Qxh2# ended it.", out)
        self.assertNotIn("Qh5", out)

    def test_chess_little_mister_side(self):
        S = gr.chess_lm_side
        self.assertEqual(S(chess_session()), "white")
        amy_host = chess_session(player="amy", summary=chess_summary(
            white={"name": "Amy", "kind": "human", "local": True},
            black={"name": "Little Mister", "kind": "human", "local": False}))
        self.assertEqual(S(amy_host), "black")
        hot = chess_session(summary=chess_summary(white={"name": "Amy", "kind": "human", "local": True},
                                                  black={"name": "Jordan", "kind": "human", "local": True}, you="both"))
        self.assertEqual(S(hot), "black")
        amy_vs_nova = chess_session(player="amy", summary=chess_summary(
            white={"name": "Amy", "kind": "human", "local": True},
            black={"name": "Nova", "kind": "nova", "level": 3, "local": True}))
        self.assertIsNone(S(amy_vs_nova))
        self.assertEqual(gr.chess_result(amy_host, "black"), "win")   # winner black = him, whatever her row says

    def test_chess_rivalry_tally_streaks(self):
        rows = [chess_view_row(r, i) for i, r in enumerate(["win", "win", "loss", "draw", "loss", "loss", "loss"])]
        rows.append(chess_view_row("win", 99, kind="nova"))       # vs Nova: not part of the rivalry
        t = gr.rivalry_tally(rows, "his opponent")
        self.assertEqual((t["little_mister_wins"], t["draws"], t["opponent_wins"]), (2, 1, 4))
        self.assertEqual(t["record_from_little_mister_side"], "2-1-4 (W-D-L)")
        self.assertEqual(t["current_streak"], "his opponent has won 3 in a row")
        self.assertEqual((t["longest_little_mister_win_streak"], t["longest_opponent_win_streak"]), (2, 3))
        self.assertIsNone(gr.rivalry_tally([chess_view_row("win", 1, kind="nova")], "x"))

    def test_chess_week_bounds_are_pacific(self):
        wk = gr.week_bounds("2026-W45")
        self.assertEqual((wk["from"], wk["to"]), ("2026-11-02", "2026-11-08"))
        self.assertEqual(wk["start"].utcoffset(), timedelta(hours=-8))
        # Monday 2026-11-09 00:30 PST is still "last week = W45"; UTC already says Monday 08:30
        self.assertEqual(gr.week_bounds(now=datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc))["week"], "2026-W45")

    def test_chess_pgn_fallback_and_live_events_ignored(self):
        s = chess_session(summary=chess_summary(pgn='[White "Amy"]\n[Black "X"]\n\n1. e4 e5 2. Nf3 {ok} Nc6 1-0'))
        live = [{"seq": 1, "kind": "ply", "detail": {"ply": 1, "san": "d4"}},
                {"seq": 2, "kind": "takeback", "detail": {"from_ply": 2, "to_ply": 1}}]
        f = gr.build_facts(s, live, [], dict(gr.DEFAULTS))
        self.assertEqual(f["chess"]["move_list"], ["1. e4", "1... e5", "2. Nf3", "2... Nc6"])
        self.assertEqual(f["chess"]["takebacks"], 1)
        self.assertFalse(any(e["kind"] == "ply" for e in f["events"]))


# ── Integration ─────────────────────────────────────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_uses_shared_helpers_and_config_keys(self):
        self.assertEqual(gr.DEFAULTS["writer_chain"][0]["kind"], "cloud")
        self.assertEqual(SRC.count('"qwen3:30b-a3b"'), 1)  # the local model is named in exactly one place
        for s in ("nova_voice", "system_prompt(ctx", "nj.publish_hugo(", "nj.git_push(", "nj.new_numbers(",
                  "nova_dsn.pg_dsn(\"nova_ops\")", "profile=PROFILE", "sources=facts_text(facts)"):
            self.assertIn(s, SRC)
        self.assertEqual((gr.SERVICE, gr.SECTION), ("nova_game_report", "nova"))
        nj_src = (SCRIPTS / "nova_journal.py").read_text()
        self.assertRegex(nj_src, r'"game-report":\s+\(\d+, \d+, EXPAND_GROUNDED\)')

    def test_config_overrides_from_service_config(self):
        ops = FakeOpsCursor(cfg={"min_minutes": 5, "unknown_key": 1})
        cfg = gr.load_config(ops)
        self.assertEqual(cfg["min_minutes"], 5)
        self.assertNotIn("unknown_key", cfg)

    def test_history_comparison_flows_into_facts(self):
        prev = [session(id="a", score=10.0, outcome="victory"), session(id="b", score=4.0, outcome="defeat")]
        f = gr.build_facts(session(score=12.0), [], prev, dict(gr.DEFAULTS))
        h = f["his_record_in_this_game_before_this_session"]
        self.assertEqual((h["previous_sessions"], h["best_score"], h["this_score_beats_previous_best"]), (2, 10, True))
        self.assertEqual(h["previous_outcomes"], {"victory": 1, "defeat": 1})

    def test_candidate_query_finishes_by_end_or_idle_and_filters_players(self):
        g = FakeGamesCursor([session()])
        gr.fetch_candidates(g, dict(gr.DEFAULTS))
        sql, params = g.sql[-1]
        self.assertIn("ended_at IS NOT NULL OR last_seen_at < now() - make_interval(mins => %s)", sql)
        self.assertEqual(params[0], 30)
        self.assertEqual(params[2], ["default", "admin", "One"])

    def test_chess_queries_include_her_mac_only_for_chess(self):
        g = FakeGamesCursor([chess_session()])
        gr.fetch_candidates(g, dict(gr.DEFAULTS))
        sql, params = g.sql[-1]
        self.assertIn("game || ':' || player = ANY(%s)", sql)
        self.assertEqual(params[3], ["gtnwchess:amy"])
        gr.fetch_history(g, chess_session(), cfg=dict(gr.DEFAULTS))
        self.assertEqual(g.sql[-1][1][1], ["default", "admin", "One", "amy"])
        gr.fetch_history(g, session(), cfg=dict(gr.DEFAULTS))
        self.assertEqual(g.sql[-1][1][1], ["default"])
        gr.fetch_events(g, "x", skip_kinds=gr.CHESS_LIVE_KINDS)
        self.assertEqual(g.sql[-1][1][1], ["ply"])

    def test_chess_facts_from_his_side_with_head_to_head_first(self):
        earlier = chess_session(id="e1", player="amy", outcome="win", ended_at=NOW() - timedelta(days=2),
                                summary=chess_summary(winner="white", result="1-0", white={
                                    "name": "Amy", "kind": "human", "local": True},
                                    black={"name": "Little Mister", "kind": "human", "local": False}))
        f = gr.build_facts(chess_session(), chess_events(), [earlier], dict(gr.DEFAULTS))
        keys = list(f)
        self.assertLess(keys.index("chess"), keys.index("summary"))
        self.assertLess(keys.index("rivalry_head_to_head"), keys.index("events"))
        self.assertEqual(f["his_record_in_this_game_before_this_session"]["previous_outcomes"], {"loss": 1})
        self.assertEqual(f["rivalry_head_to_head"]["record_from_little_mister_side"], "0-0-2 (W-D-L)")
        ch = f["chess"]
        self.assertEqual(ch["decisive_moment"]["move"], "9. Nxe5")
        self.assertEqual(ch["decisive_moment"]["engine_preferred"], "9. Nbd2")
        self.assertEqual(ch["key_moments"][-1]["answered_with"], "14... Qxh2# by his opponent (black)")
        self.assertEqual(ch["accuracy_and_errors"]["black"]["captured"], {"pawn": 2, "knight": 1})
        self.assertEqual((ch["duration_minutes"], ch["opening"]), (14, "C50 Italian Game"))
        self.assertIn("| Head-to-head (Little Mister W-D-L) | 0-0-2 (W-D-L) |", gr.stats_table(f, "gtnwchess"))

    def test_chess_prompt_rules(self):
        with patch.dict(sys.modules, {"nova_voice": types.SimpleNamespace(system_prompt=lambda ctx, section="": ctx)}):
            system, _ = gr.build_prompt({"chess": {}})
            rsys, _ = gr.build_rivalry_prompt({"rivals": ["Little Mister", "his opponent"]})
        self.assertIn("CHESS (GTNW Chess)", system)
        self.assertIn("never the people", system)
        self.assertIn("lean all the way into Little Mister getting beaten", system)
        self.assertIn("roast the MOVES, never the people", rsys)


# ── Functional ──────────────────────────────────────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def setUp(self):
        p = patch.object(gr, "ollama_chat", side_effect=AssertionError("no local calls in these tests"))
        p.start()
        self.addCleanup(p.stop)

    def _run(self, nj, ops, sessions, state=None):
        with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": types.SimpleNamespace(
                system_prompt=lambda ctx, section="": "VOICE\n" + ctx)}):
            st = state if state is not None else {}
            return [gr.process(s, FakeGamesCursor(sessions, events=[{"seq": 0, "kind": "game_over",
                                                                       "detail": {"reason": "fire"}}]),
                               ops, st, dict(gr.DEFAULTS)) for s in sessions], st

    def test_golden_path_publishes_once_grounded(self):
        with tempfile.TemporaryDirectory() as d:
            nj, ops = fake_nj(Path(d), draft=DRAFT), FakeOpsCursor()
            (res,), state = self._run(nj, ops, [session()])
            self.assertEqual(res, "published")
            pub = nj.published[0]
            self.assertEqual((pub["section"], pub["tags"], pub["profile"]), ("nova", ["games", "gtnw", "opinion"],
                                                                            "game-report"))
            self.assertIn("25019519", pub["sources"])           # facts went in as sources
            self.assertNotIn("999,999", pub["body"])            # invented number cut
            self.assertNotIn("Jordan", pub["body"])             # real name guarded
            self.assertIn("Session ref: 5bceb732", pub["body"])
            self.assertIn("## The numbers", pub["body"])
            self.assertIs(pub["show_time"], False)  # no time of day on the public post
            rec = ops.rows[("nova_game_report", "reported")][session()["id"]]
            self.assertEqual(rec["status"], "published")
            self.assertTrue(rec["url"].startswith("https://nova.digitalnoise.net/nova/2026-10-18-"))
            self.assertEqual(nj.pushed, [pub["title"]])
            # exactly once: the next run doesn't touch it
            self.assertFalse(gr.needs_work(rec, dict(gr.DEFAULTS)))

    def test_crash_after_publish_is_recovered_not_duplicated(self):
        with tempfile.TemporaryDirectory() as d:
            nj, ops = fake_nj(Path(d), draft=DRAFT), FakeOpsCursor()
            art = Path(d) / "content" / "nova"
            art.mkdir(parents=True)
            (art / "2026-10-18-old.md").write_text("...\n*Session ref: 5bceb732.*\n")
            (res,), _ = self._run(nj, ops, [session()], state={session()["id"]: {"status": "claimed",
                                                                                  "attempts": 1}})
            self.assertEqual(res, "published")
            self.assertEqual(nj.published, [])
            nj.call_openrouter.assert_not_called()

    def test_trivial_session_is_skipped_and_marked(self):
        with tempfile.TemporaryDirectory() as d:
            nj, ops = fake_nj(Path(d), draft=DRAFT), FakeOpsCursor()
            now = NOW()
            (res,), _ = self._run(nj, ops, [session(started_at=now - timedelta(seconds=40), ended_at=now,
                                                    last_seen_at=now)])
            self.assertEqual(res, "skipped")
            self.assertEqual(nj.published, [])
            self.assertEqual(ops.rows[("nova_game_report", "reported")][session()["id"]]["status"], "skipped")

    def test_journal_refusal_is_an_error_not_a_publish(self):
        with tempfile.TemporaryDirectory() as d:
            nj, ops = fake_nj(Path(d), draft=DRAFT, publish_ok=False), FakeOpsCursor()
            (res,), _ = self._run(nj, ops, [session()])
            self.assertEqual(res, "error")
            self.assertEqual(ops.rows[("nova_game_report", "reported")][session()["id"]]["status"], "error")

    def test_chess_golden_path_and_not_his_game_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            nj, ops = fake_nj(Path(d), draft=CHESS_DRAFT), FakeOpsCursor()
            with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": types.SimpleNamespace(
                    system_prompt=lambda ctx, section="": ctx)}):
                st = gr.process(chess_session(), FakeGamesCursor([chess_session()], events=chess_events()), ops, {},
                                dict(gr.DEFAULTS))
                amy_vs_nova = chess_session(id="dd", player="amy", summary=chess_summary(
                    white={"name": "Amy", "kind": "human", "local": True},
                    black={"name": "Nova", "kind": "nova", "level": 2, "local": True}))
                st2 = gr.process(amy_vs_nova, FakeGamesCursor([amy_vs_nova]), ops, {}, dict(gr.DEFAULTS))
            self.assertEqual((st, st2), ("published", "skipped"))
            pub = nj.published[0]
            self.assertEqual(pub["tags"], ["games", "gtnwchess", "chess", "opinion"])
            self.assertNotIn("Qxg7", pub["body"])     # invented move cut
            self.assertNotIn("Bb4", pub["body"])
            self.assertIn("9. Nxe5", pub["body"])
            self.assertIn("| Black | his opponent |", pub["body"])
            self.assertEqual(len(nj.published), 1)

    def test_rivalry_report_once_per_week_and_skips_empty_weeks(self):
        wk = gr.week_bounds("2026-W45")
        game = chess_session(ended_at=wk["start"] + timedelta(days=2), last_seen_at=wk["start"] + timedelta(days=2))
        with tempfile.TemporaryDirectory() as d:
            nj, ops = fake_nj(Path(d), draft=CHESS_DRAFT), FakeOpsCursor()
            with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": types.SimpleNamespace(
                    system_prompt=lambda ctx, section="": ctx)}):
                g = FakeGamesCursor([game], events=chess_events())
                self.assertEqual(gr.process_rivalry(g, ops, dict(gr.DEFAULTS), wk), "published")
                self.assertEqual(gr.process_rivalry(g, ops, dict(gr.DEFAULTS), wk), "done")
                empty = gr.week_bounds("2026-W40")
                self.assertEqual(gr.process_rivalry(FakeGamesCursor([]), ops, dict(gr.DEFAULTS), empty), "skipped")
            rec = ops.rows[("nova_game_report", "rivalry_reported")]
            self.assertEqual(rec["2026-W45"]["status"], "published")
            self.assertEqual(rec["2026-W40"]["status"], "skipped")
            self.assertNotIn(("nova_game_report", "reported"), ops.rows)   # per-session state untouched
            pub = nj.published[0]
            self.assertIn("Rivalry ref: 2026-W45", pub["body"])
            self.assertEqual(pub["tags"], ["games", "gtnwchess", "chess", "rivalry", "opinion"])
            self.assertNotRegex(pub["body"] + pub["sources"], r"(?i)\bamy\b")

    def test_fixture_dry_run_touches_no_database_and_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "fx.json")
            s = chess_session()
            p.write_text(json.dumps({"kind": "session", "session": s, "events": chess_events(), "history": []},
                                    default=str))
            nj = fake_nj(Path(d), draft=CHESS_DRAFT)
            out = []
            with patch.dict(sys.modules, {"nova_journal": nj, "nova_voice": types.SimpleNamespace(
                    system_prompt=lambda ctx, section="": ctx)}), \
                    patch("psycopg2.connect", side_effect=AssertionError("no DB in a fixture run")), \
                    patch.object(gr, "games_connect", side_effect=AssertionError("no Games DB")), \
                    patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                self.assertEqual(gr.main(["--dry-run", "--fixture", str(p)]), 0)
            self.assertEqual((nj.published, nj.pushed), ([], []))
            self.assertTrue(any("no database, no state, no publish" in o for o in out))
            self.assertNotRegex("\n".join(out), r"(?i)\bamy\b")


# ── Frame ───────────────────────────────────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_help_and_selftest_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        for flag in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True, timeout=30,
                               env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        with patch.object(gr, "run") as run:
            _load()
            run.assert_not_called()

    def test_fixture_flag_requires_dry_run_and_help_lists_new_flags(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--fixture", "/nonexistent.json"], capture_output=True,
                           text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--fixture needs --dry-run", r.stderr)
        h = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertIn("--rivalry", h.stdout)
        self.assertIn("--fixture", h.stdout)


class TestCaseFile(unittest.TestCase):
    """GTNW 3.22 roast log: the case file (Unit / Functional / Security / Performance)."""

    def _ev(self):
        order = {"action": "Air Strike", "target_name": "Cuba", "overruled_advisor": True, "advisor": "Secretary of State Jane Hawk",
                 "advisor_said": "This will start a war we cannot win.", "advisor_odds": 0.22, "worked": False,
                 "result": ["AIR STRIKE FAILED"], "approval_before": 48, "approval_after": 41, "game_date": "2025-03-01"}
        return [{"seq": 0, "kind": "game_start", "detail": {"country": "USA"}},
                {"seq": 1, "kind": "president_order", "detail": order},
                {"seq": 2, "kind": "president_order", "detail": {"action": "Sanctions", "danger_warnings_ignored": 1,
                                                                 "cabinet_warnings": [{"who": "Joint Chiefs", "level": 2, "said": "No."}]}},
                {"seq": 3, "kind": "crisis_decision", "detail": {"title": "The October Surprise", "chose": "Change the subject",
                                                                 "worked": False, "other_options": ["Come clean"]}},
                {"seq": 4, "kind": "approval_swing", "detail": {"from": 48, "to": 36, "change": -12}},
                {"seq": 5, "kind": "approval_swing", "detail": {"from": 36, "to": 42, "change": 6}},
                {"seq": 6, "kind": "election_result", "detail": {"year": 2028, "outcome": "lost", "electoral_votes": 112}},
                {"seq": 7, "kind": "game_over", "detail": {"reason": "Impeached"}}]

    def test_case_file_is_built_and_sits_ahead_of_the_summary(self):
        s = session(game="gtnw", summary={"session_stats": {"advisors_overruled": 1},
                                          "polls": [{"date": "2025-01-01", "popularity": i} for i in range(3000)]})
        f = gr.build_facts(s, self._ev(), [], dict(gr.DEFAULTS))
        cf = f["case_file"]
        self.assertEqual(cf["orders_over_advisor_objections"][0]["advisor_said"], "This will start a war we cannot win.")
        self.assertEqual(cf["danger_warnings_clicked_past"][0]["action"], "Sanctions")
        self.assertEqual(cf["orders_that_failed"][0]["action"], "Air Strike")
        self.assertEqual(cf["crisis_calls"][0]["other_options"], ["Come clean"])
        self.assertEqual(cf["worst_approval_drops"][0]["change"], -12)
        self.assertEqual(cf["best_approval_gains"][0]["change"], 6)
        self.assertEqual(cf["elections"][0]["outcome"], "lost")
        self.assertEqual(cf["tallies"], {"advisors_overruled": 1})
        keys = list(f)
        self.assertLess(keys.index("case_file"), keys.index("summary"))
        self.assertIn("This will start a war", gr.facts_text(f), "a huge summary can't push the case file past the cap")
        self.assertFalse(any(e["kind"] == "president_order" for e in f["events"]), "orders live in the case file only")

    def test_other_games_and_old_builds_get_no_case_file(self):
        self.assertNotIn("case_file", gr.build_facts(session(game="cartest"), self._ev(), [], dict(gr.DEFAULTS)))
        old = [{"seq": 0, "kind": "ticker", "detail": {"text": "FLASH"}}]
        self.assertNotIn("case_file", gr.build_facts(session(game="gtnw"), old, [], dict(gr.DEFAULTS)))

    def test_long_games_keep_their_ending(self):
        evs = [{"seq": i, "kind": "ticker", "detail": {"text": f"item {i}"}} for i in range(1000)]
        evs.append({"seq": 1000, "kind": "game_over", "detail": {"reason": "Impeached"}})
        f = gr.build_facts(session(game="gtnw"), evs, [], dict(gr.DEFAULTS))
        self.assertEqual(len(f["events"]), 120)
        self.assertEqual(f["events"][-1]["kind"], "game_over")
        self.assertEqual(f["events"][0]["text"], "item 0")

    def test_case_file_is_sanitised(self):
        evs = [{"seq": 0, "kind": "president_order",
                "detail": {"action": "Air Strike", "overruled_advisor": True, "advisor_said": "call 192.168.1.5 at /home/someone/x"}}]
        f = gr.build_facts(session(game="gtnw"), evs, [], dict(gr.DEFAULTS))
        said = f["case_file"]["orders_over_advisor_objections"][0]["advisor_said"]
        self.assertNotIn("192.168.1.5", said)
        self.assertNotIn("/home/someone", said)

    def test_prompt_makes_the_case_file_the_spine(self):
        system, _ = gr.build_prompt({"case_file": {}})
        self.assertIn("CASE FILE (GTNW)", system)
        self.assertIn("never about him as a person", system)

    def test_case_file_fast_on_5k_events(self):
        evs = [{"seq": i, "kind": "president_order", "detail": {"action": "A", "overruled_advisor": i % 2 == 0, "worked": False}}
               for i in range(5000)]
        t = time.time()
        f = gr.build_facts(session(game="gtnw"), evs, [], dict(gr.DEFAULTS))
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(len(f["case_file"]["orders_over_advisor_objections"]), 8)


if __name__ == "__main__":
    unittest.main()
