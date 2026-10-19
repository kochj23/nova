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
    def __init__(self, sessions, events=None, history=None):
        self.sessions, self.events, self.history = sessions, events or [], history or []
        self._rows = []
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if "FROM play.events" in sql:
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


if __name__ == "__main__":
    unittest.main()
