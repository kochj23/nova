#!/usr/bin/env python3
"""7-category gap-fill tests for nova_relationship.py (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Complements test_nova_relationship.py.

Rules: offline only. PG, Ollama, Slack and the memory server are mocked; the private seed file is
never read (load_seed / SEED_FILE are guarded by an autouse fixture). Employer strings used to
probe the content guard are derived from the module's own regex, never written here.
Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("NOVA_TEST_QUIET", "1")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rel = _load("rel_7cat", SCRIPTS / "nova_relationship.py")
SRC = (SCRIPTS / "nova_relationship.py").read_text()
NOW = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)


class FakeCur:
    """Scripted cursor: responses = [(sql-substring, rows)], each consumed once on first match."""

    def __init__(self, responses=None, rowcount=1):
        self.responses = list(responses or [])
        self.sql = []
        self.rowcount = rowcount
        self._rows = []

    def execute(self, sql, args=()):
        self.sql.append((sql, args))
        self._rows = []
        for i, (frag, rows) in enumerate(self.responses):
            if frag in sql:
                self._rows = rows
                self.responses.pop(i)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def executed(self, frag):
        return [(s, a) for s, a in self.sql if frag in s]

    def writes(self):
        return [s for s, _ in self.sql if re.match(r"\s*(INSERT|UPDATE|DELETE)", s)]


@pytest.fixture(autouse=True)
def _guard_seed_and_sleep(monkeypatch):
    """The private relationship seed must never be read by these tests; sleeps are instant."""
    def no_seed(*a, **k):
        raise AssertionError("private seed file read during tests")
    monkeypatch.setattr(rel, "load_seed", no_seed)
    monkeypatch.setattr(rel, "SEED_FILE", Path("/nonexistent/relationship_seed.json"))
    monkeypatch.setattr(rel.time, "sleep", lambda s: None)
    monkeypatch.setattr(rel, "CLAUDE_HISTORY", Path("/nonexistent/history.jsonl"))


def _employer_samples():
    """Words that the module's own employer guard matches — derived, never hard-coded."""
    body = rel._EMPLOYER.pattern.split("(", 1)[1].rsplit(")", 1)[0]
    return [alt.replace("\\w*", "") for alt in body.split("|")]


# ── Security ───────────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_brief_public_returns_empty_without_touching_pg(self):
        with patch.object(rel, "_connect", side_effect=AssertionError("must not connect")):
            assert rel.brief(public=True) == ""
            assert rel.brief(max_chars=5000, public=True, cur=FakeCur()) == ""

    def test_is_clean_blocks_every_employer_alternative(self):
        samples = _employer_samples()
        assert len(samples) >= 3
        for w in samples:
            assert not rel.is_clean(f"we talked about {w} today"), w
            assert not rel.is_clean(f"we talked about {w.upper()} today"), w

    @pytest.mark.parametrize("txt", ["that was nsfw", "Porn site", "NAKED truth", "an erotic novel",
                                     "fetishes", "sexting"])
    def test_is_clean_blocks_sexual(self, txt):
        assert not rel.is_clean(txt)

    @pytest.mark.parametrize("txt", ["", None, "the toaster fleet", "Essex county", "a nudge"])
    def test_is_clean_allows_benign_and_word_bounded(self, txt):
        assert rel.is_clean(txt)

    def test_good_thing_candidates_drop_unclean_text(self):
        bad = _employer_samples()[0]
        cur = FakeCur([("FROM feature_wishes", [(1, f"{bad} badge")]),
                       ("FROM claude_queue", [(2, "nsfw cleanup task")])])
        with patch.dict(sys.modules, {"nova_affect": MagicMock(tone_score=lambda m: (0, [], []))}):
            assert rel.good_thing_candidates(cur) == []

    def test_lockbox_recall_query_cannot_override_include_boxed(self):
        seen = {}

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"memories": []}'

        def urlopen(url, timeout=0):
            seen["url"] = url
            return R()
        with patch.object(rel.urllib.request, "urlopen", urlopen):
            rel.lockbox_recall("x&include_boxed=false&min_score=0")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
        assert qs["include_boxed"] == ["true"] and qs["min_score"] == ["0.75"]
        assert qs["q"] == ["x&include_boxed=false&min_score=0"]

    def test_lockbox_recall_only_returns_boxed(self):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"memories": [{"id": "a", "metadata": None}, {"id": "b"},
                                                {"id": "c", "metadata": {"boxed": True}}]}).encode()
        with patch.object(rel.urllib.request, "urlopen", lambda u, timeout=0: R()):
            assert [m["id"] for m in rel.lockbox_recall("q")] == ["c"]

    def test_llm_only_talks_to_lan_nodes(self):
        for node in rel.OLLAMA_NODES:
            host = urllib.parse.urlparse(node).hostname
            assert re.match(r"^(192\.168\.|10\.|127\.)", host), node   # PII never leaves the LAN

    def test_proposals_with_injection_trace_ids_rejected(self):
        props = [{"kind": "taught", "text": "He likes BSD a great deal.", "trace_id": "x'; DROP TABLE t;--"},
                 {"kind": "drop table", "text": "He likes BSD a great deal.", "trace_id": "x"}]
        assert rel.validate_proposals(props, {"x": "I like BSD a great deal"}, []) == []

    def test_propose_boxes_never_writes_memories(self):
        mem = FakeCur([("FROM memories", [("m1", "imessage", "he threatened to kill me")])])
        ops = FakeCur()
        assert rel.propose_boxes(ops, mem) == 1
        assert mem.writes() == []                              # memories untouched: proposal only
        ins = ops.executed("INSERT INTO lockbox")
        assert ins and "'letting_go'" in ins[0][0] and "status" not in ins[0][0].split("VALUES")[0]

    def test_box_instead_of_delete_only_proposes(self):
        cur = FakeCur([("INSERT INTO lockbox", [(7,)])])
        conn = MagicMock(cursor=lambda: cur)
        with patch.object(rel, "_connect", return_value=conn):
            assert rel.box_instead_of_delete("m9", "x" * 1000) == 7
        sql, args = cur.executed("INSERT INTO lockbox")[0]
        assert "boxed" not in sql and len(args[1]) == 300           # reason capped
        assert not cur.executed("UPDATE memories") and not cur.executed("DELETE")

    def test_cli_approve_requires_proposed_status(self):
        cur = FakeCur([("FROM lockbox WHERE id", [])])            # not 'proposed' -> nothing boxed
        conn = MagicMock(cursor=lambda: cur)
        with patch.object(rel, "_connect", return_value=conn), patch.object(rel, "box") as bx:
            assert rel.main(["lockbox", "approve", "5"]) == 0
        bx.assert_not_called()
        assert "status='proposed'" in cur.executed("FROM lockbox WHERE id")[0][0]

    def test_no_delete_from_memories_and_no_hardcoded_paths(self):
        assert not re.search(r"DELETE\s+FROM\s+memories", SRC, re.I)
        assert "/Users/" not in SRC
        assert not re.search(r"(password|api_key|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I)


# ── Performance ────────────────────────────────────────────────────────────────────

class TestPerformance:
    def test_propose_boxes_capped_per_run(self):
        rows = [(f"m{i}", "email", "harassment again") for i in range(1000)]
        mem, ops = FakeCur([("FROM memories", rows)]), FakeCur()
        t = time.monotonic()
        assert rel.propose_boxes(ops, mem) == rel.MAX_PROPOSALS
        assert len(ops.executed("INSERT INTO lockbox")) == rel.MAX_PROPOSALS
        assert "LIMIT 20" in mem.sql[0][0] and time.monotonic() - t < 1

    def test_interlude_prompt_is_bounded(self):
        rows = [(f"t{i}", NOW - timedelta(minutes=i), "word " * 500, "r" * 240) for i in range(500)]  # SQL left(...,240)
        cur = FakeCur([("FROM gateway_traces", rows),
                       ("FROM relationship_ledger WHERE active", [(f"entry {i}",) for i in range(500)])])
        seen = {}
        with patch.object(rel, "llm", lambda p, s: seen.setdefault("p", p) and "[]"):
            rel.interlude(cur, dry_run=True)
        assert len(seen["p"]) < 60 * 720 + 40 * 120 + 500   # 60 exchanges x (400+240), 40 entries

    def test_render_brief_10k_rows_fast_and_bounded(self):
        ledger = [(rel.LEDGER_KINDS[i % 8], f"entry number {i} " * 5, i / 10000, "open") for i in range(10000)]
        lex = [(f"p{i}", "m") for i in range(1000)]
        t = time.monotonic()
        for mc in (300, 900, 5000):
            assert len(rel.render_brief(ledger, lex, mc, "2026-10-08")) <= mc
        assert time.monotonic() - t < 1

    def test_history_scan_50k_lines_fast(self, tmp_path, monkeypatch):
        h = tmp_path / "history.jsonl"
        ts = int(NOW.timestamp() * 1000)
        with h.open("w") as f:
            for i in range(50000):
                f.write(json.dumps({"timestamp": ts - i * 1000, "display": f"msg {i}"}) + "\n")
            f.write("not json\n")
        monkeypatch.setattr(rel, "CLAUDE_HISTORY", h)
        t = time.monotonic()
        out = rel.his_message_times(FakeCur())
        assert len(out) == 50000 and time.monotonic() - t < 5

    def test_queries_are_limited(self):
        for frag in ("FROM feature_wishes", "FROM claude_queue", "telemetry.overhead_flights",
                     "FROM hard_stretch WHERE ended_at IS NULL"):
            stmt = SRC[SRC.index(frag):SRC.index(frag) + 400]
            assert "LIMIT" in stmt, frag


# ── Retry ──────────────────────────────────────────────────────────────────────────

class TestRetry:
    def test_connect_retries_three_times_with_backoff_then_raises(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(rel.time, "sleep", sleeps.append)
        pg = MagicMock()
        pg.connect.side_effect = OSError("pg down")
        with patch.dict(sys.modules, {"psycopg2": pg}):
            with pytest.raises(OSError):
                rel._connect("dsn")
        assert pg.connect.call_count == 3 and sleeps == sorted(sleeps) and sleeps[0] > 0

    def test_connect_recovers_on_second_attempt(self):
        pg = MagicMock()
        good = MagicMock()
        pg.connect.side_effect = [OSError("blip"), good]
        with patch.dict(sys.modules, {"psycopg2": pg}):
            assert rel._connect("dsn") is good
        assert good.autocommit is True

    def test_llm_fails_over_with_increasing_backoff(self, monkeypatch):
        sleeps, calls = [], []
        monkeypatch.setattr(rel.time, "sleep", sleeps.append)

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"message": {"content": " ok "}}).encode()

        def urlopen(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return R()
        with patch.object(rel.urllib.request, "urlopen", urlopen):
            assert rel.llm("p", "s") == "ok"
        assert len(calls) == 3 and len(set(calls)) == 3
        assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0

    def test_llm_empty_reply_tries_next_node(self):
        replies = iter([{"message": {"content": ""}}, {"message": {"content": "[]"}}])

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(next(replies)).encode()
        with patch.object(rel.urllib.request, "urlopen", lambda r, timeout=0: R()):
            assert rel.llm("p", "s") == "[]"

    def test_post_gives_up_after_three_and_reports(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(rel.time, "sleep", sleeps.append)
        import nova_config
        with patch.object(nova_config, "post_both", side_effect=OSError("slack down")) as pb:
            assert rel._post("hi") is False
        assert pb.call_count == 3 and sleeps == [2, 4, 6]

    def test_lockbox_recall_all_fail_returns_empty_after_three(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(rel.time, "sleep", sleeps.append)
        with patch.object(rel.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            assert rel.lockbox_recall("q") == []
        assert uo.call_count == 3 and sleeps == [1.5, 3.0, 4.5]

    def test_stretch_line_not_marked_sent_when_post_fails(self):
        open_row = (4, 0.6, None, NOW - timedelta(hours=2))
        sig = [{"signal": s, "v": 1.0, "note": "n"} for s in rel.W_STRETCH]
        cur = FakeCur([("WHERE ended_at IS NULL", [open_row]), ("max(line_sent_at)", [(None,)])])
        with patch.object(rel, "his_message_times", return_value=[]), \
                patch.object(rel, "sig_late_nights", return_value=sig[0]), \
                patch.object(rel, "sig_terse", return_value=sig[1]), \
                patch.object(rel, "sig_silence", return_value=sig[2]), \
                patch.object(rel, "sig_quiet_house", return_value=sig[3]), \
                patch.object(rel, "in_line_window", return_value=True), \
                patch.object(rel, "_post", return_value=False):
            r = rel.run_stretch(cur, now=NOW)
        assert r["line"] is None and not cur.executed("SET line_text")

    def test_brief_and_box_proposal_fail_open_when_pg_down(self):
        with patch.object(rel, "_connect", side_effect=OSError("down")):
            assert rel.brief() == ""
            assert rel.box_instead_of_delete("m1", "r") is None


# ── Unit ───────────────────────────────────────────────────────────────────────────

class TestUnit:
    def test_extract_json_list(self):
        assert rel._extract_json_list('<think>[1,2]</think> here: [{"a":1}]') == [{"a": 1}]
        assert rel._extract_json_list("no json") == []
        assert rel._extract_json_list("[broken") == []
        assert rel._extract_json_list('{"a": [1]}') == [1]
        assert rel._extract_json_list(None) == []

    def test_text_hash_normalises(self):
        assert rel.text_hash("Hello,   World!") == rel.text_hash("hello world")
        assert len(rel.text_hash("x")) == 32 and rel.text_hash("a") != rel.text_hash("b")

    def test_similar_and_words(self):
        assert not rel.similar("", "anything")
        assert rel.similar("tea and water daily", "water and tea daily")
        assert "the" not in rel._words("the tea")

    def test_short_and_clamp(self):
        assert rel._short("  short ") == "short"
        s = rel._short("word " * 50, 20)
        assert s.endswith("…") and len(s) <= 21
        assert rel.clamp(5, 0, 1) == 1 and rel.clamp(-1, 0, 1) == 0

    def test_line_window_boundaries_and_pick_line(self):
        d = datetime(2026, 1, 1)
        assert not rel.in_line_window(d.replace(hour=8)) and rel.in_line_window(d.replace(hour=9))
        assert rel.in_line_window(d.replace(hour=20)) and not rel.in_line_window(d.replace(hour=21))
        assert rel.pick_line(0) == rel.pick_line(len(rel.WARM_LINES))
        assert all(rel.is_clean(l) for l in rel.WARM_LINES)

    def test_validate_proposals_weight_and_cap(self):
        valid = {f"t{i}": f"we built a printer bracket number {i} together" for i in range(6)}
        props = [{"kind": "milestone", "text": f"We built printer bracket number {i} together.",
                  "trace_id": f"t{i}", "weight": w} for i, w in enumerate([5, "bad", -1, 0.5, 0.5, 0.5])]
        out = rel.validate_proposals(props, valid, [])
        assert len(out) <= rel.INTERLUDE_MAX
        assert out[0]["weight"] == 0.9

    def test_validate_proposals_rejects_ungrounded_and_bad_length(self):
        valid = {"x": "completely unrelated chatter about weather"}
        assert rel.validate_proposals([{"kind": "taught", "text": "He loves BSD systems.", "trace_id": "x"}],
                                      valid, []) == []
        assert rel.validate_proposals([{"kind": "taught", "text": "short", "trace_id": "x"}], valid, []) == []
        assert rel.validate_proposals(["not a dict", None], valid, []) == []

    def test_sig_terse_neutral_and_shortened(self):
        assert rel.sig_terse([], NOW)["v"] == 0.0
        before = [(NOW - timedelta(days=5, hours=i), "x" * 200) for i in range(12)]
        now_m = [(NOW - timedelta(hours=i), "ok") for i in range(5)]
        with patch.dict(sys.modules, {"nova_affect": MagicMock(tone_score=lambda m: (0, [], []))}):
            assert rel.sig_terse(before + now_m, NOW)["v"] == 1.0

    def test_sig_quiet_house(self):
        assert rel.sig_quiet_house(FakeCur(), NOW)["v"] == 0.0
        local_noon = NOW.astimezone().replace(hour=12)
        rows = [(local_noon - timedelta(minutes=i), -1.5, {"residents_home": ["jordan"]}) for i in range(8)]
        assert rel.sig_quiet_house(FakeCur([("embodiment_state", rows)]), NOW)["v"] == 1.0

    def test_stretch_score_ignores_unknown_signal(self):
        assert rel.stretch_score([{"signal": "bogus", "v": 1.0}]) == (0, 1)


# ── Integration ────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_refresh_user_doc_writes_between_markers(self):
        cur = FakeCur([
            ("FROM private_lexicon WHERE active\"", []),
            ("doc_type IN ('identity','soul')", [("identity", "i" * 3000), ("soul", "s" * 2000)]),
            ("doc_type='user'", [("# USER\n\n## Context\n\n_(Build this over time.)_\n",)]),
            ("FROM relationship_ledger", [("never_do", "No crude content, ever.", 1.0, None)]),
            ("phrase, meaning", []),
        ])
        out = rel.refresh_user_doc(cur)
        assert "No crude content" in out
        upd = cur.executed("UPDATE agent_docs")
        assert upd and rel.DOC_BEGIN in upd[0][1][0] and "Build this over time" not in upd[0][1][0]

    def test_refresh_user_doc_dry_run_no_write(self):
        cur = FakeCur([("doc_type IN ('identity','soul')", []),
                       ("doc_type='user'", [("# USER\n",)]),
                       ("FROM relationship_ledger", [("taught", "He prefers tea.", 0.8, None)])])
        assert rel.refresh_user_doc(cur, dry_run=True)
        assert not cur.executed("UPDATE agent_docs")

    def test_check_responses_answered_and_no_reply(self):
        sent_old = NOW - timedelta(hours=30)
        cur = FakeCur([("response_state IS NULL", [(1, NOW - timedelta(hours=2)), (2, sent_old)]),
                       ("FROM gateway_traces", [("tr1", NOW - timedelta(hours=1))]),
                       ("FROM gateway_traces", [])])
        assert rel.check_responses(cur, NOW) == 2
        assert cur.executed("response_state='answered'")[0][1][1] == "trace:tr1"
        assert cur.executed("response_state='no_reply'")

    def test_quiet_mode_stale_reads_inactive(self):
        old = datetime.now(timezone.utc) - timedelta(hours=7)
        cur = FakeCur([("nova_quiet_mode", [({"active": True, "score": 0.7}, old)])])
        q = rel.quiet_mode(cur)
        assert q["active"] is False and q["stale"] is True
        fresh = FakeCur([("nova_quiet_mode", [(json.dumps({"active": True}), datetime.now(timezone.utc))])])
        assert rel.quiet_mode(fresh)["active"] is True

    def test_box_patch_key_matches_memory_server_filter(self):
        mem = FakeCur([("SELECT metadata", [({"k": 1},)])])
        ops = FakeCur()
        assert rel.box(mem, ops, "m1", "r")
        patch_json = json.loads(mem.executed("UPDATE memories")[0][1][0])
        msrv = (SCRIPTS.parent / "memory_server.py").read_text()
        assert patch_json["boxed"] is True and "metadata->>'boxed'" in msrv
        assert "include_boxed" in msrv

    def test_cli_approve_boxes_as_jordan(self):
        cur = FakeCur([("FROM lockbox WHERE id", [("m5", "painful")])])
        conn = MagicMock(cursor=lambda: cur)
        with patch.object(rel, "_connect", return_value=conn), patch.object(rel, "box") as bx:
            rel.main(["lockbox", "approve", "5"])
        assert bx.call_args.kwargs == {"by": "jordan"} and bx.call_args.args[2:] == ("m5", "painful")


# ── Functional ─────────────────────────────────────────────────────────────────────

class TestFunctional:
    def _rows(self):
        return [("tr1", NOW - timedelta(days=1), "you taught me printers need dry filament", "noted")]

    def test_interlude_golden_inserts_with_evidence(self):
        cur = FakeCur([("FROM gateway_traces", self._rows()), ("FROM relationship_ledger WHERE active", [])])
        reply = json.dumps([{"kind": "taught", "text": "He taught me printers need dry filament.",
                             "trace_id": "tr1", "weight": 0.6}])
        with patch.object(rel, "llm", return_value=reply):
            out = rel.interlude(cur)
        assert len(out) == 1
        ins = cur.executed("INSERT INTO relationship_ledger")
        assert ins and ins[0][1][3] == "trace:tr1"

    def test_interlude_garbage_llm_inserts_nothing(self):
        cur = FakeCur([("FROM gateway_traces", self._rows()), ("FROM relationship_ledger WHERE active", [])])
        with patch.object(rel, "llm", return_value="sorry, I can't"):
            assert rel.interlude(cur) == []
        assert not cur.executed("INSERT INTO relationship_ledger")

    def test_interlude_no_conversations_noop(self):
        with patch.object(rel, "llm", side_effect=AssertionError("no LLM without conversations")):
            assert rel.interlude(FakeCur([("FROM gateway_traces", [])])) == []

    def test_stretch_dry_run_writes_nothing(self):
        with patch.object(rel, "his_message_times", return_value=[]), \
                patch.object(rel, "_post", side_effect=AssertionError("no post")):
            cur = FakeCur()
            r = rel.run_stretch(cur, dry_run=True, now=NOW)
        assert r["active"] is False
        assert [s for s in cur.writes() if "hard_stretch" in s or "service_config" in s] == []

    def test_good_thing_already_logged_today(self):
        cur = FakeCur([("FROM good_things WHERE day", [(1,)])])
        with patch.object(rel, "good_thing_candidates", side_effect=AssertionError("no scan")):
            assert rel.run_good_thing(cur) is None

    def test_good_thing_weather_golden(self):
        cur = FakeCur([("telemetry.weather", [(75.0, 60.0, 0, 8.0)])])
        with patch.dict(sys.modules, {"nova_affect": MagicMock(tone_score=lambda m: (0, [], []))}):
            c = rel.good_thing_candidates(cur)
        assert c and c[0][0] == "weather" and c[0][2] == "telemetry.weather:24h"

    def test_cli_decline_and_unbox(self):
        cur = FakeCur()
        conn = MagicMock(cursor=lambda: cur)
        with patch.object(rel, "_connect", return_value=conn):
            assert rel.main(["lockbox", "decline", "3"]) == 0
        assert cur.executed("status='declined'")[0][1] == (3,)
        mcur = FakeCur()
        mconn = MagicMock(cursor=lambda: mcur)
        with patch.object(rel, "_connect", side_effect=[conn, mconn]):
            rel.main(["lockbox", "unbox", "m7"])
        assert mcur.executed("metadata - 'boxed'")[0][1] == ("m7",)

    def test_cli_bad_lockbox_action_errors(self):
        with pytest.raises(SystemExit):
            rel.main(["lockbox", "explode"])


# ── Frame ──────────────────────────────────────────────────────────────────────────

class TestFrame:
    def test_import_exposes_api(self):
        for name in ("brief", "quiet_mode", "lockbox_recall", "box", "unbox", "box_instead_of_delete",
                     "propose_boxes", "run_stretch", "run_good_thing", "interlude", "main"):
            assert callable(getattr(rel, name))

    def test_main_no_cmd_prints_help(self, capsys):
        with patch.object(rel, "_connect", side_effect=AssertionError("no PG")):
            assert rel.main([]) == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_main_selftest_returns_zero(self):
        assert rel.main(["--selftest"]) == 0

    def test_cli_help_subprocess(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_relationship.py"), "--help"],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        assert r.returncode == 0 and "lockbox" in r.stdout
