#!/usr/bin/env python3
"""
test_nova_account_and_warm.py — the ACCOUNT organ (nova_account.py), the model warmer (nova_model_warm.py) and the
gateway's warm-sticky backend pick (nova_gateway.router._best_url).

Categories: security, performance, retry, unit, integration, functional, frame.
Run: python3 -m pytest tests/test_nova_account_and_warm.py -v
"""
import json, subprocess, sys, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_account as acct          # noqa: E402
import nova_model_warm as warm       # noqa: E402


# ── unit: brief() keeps every report inside the gateway's 3000-char tool cap ─────────────
def _big_rows(n, **extra):
    return [{"vector": f"v{i}", "n": i, "text": "x" * 400, "description": "d" * 300, "status": "done", "outcome": "o" * 300, **extra} for i in range(n)]


def test_brief_learned_fits_tool_cap():
    out = {"date": "2026-10-05", "total_new_memories": 1, "by_vector": _big_rows(80), "ingest_requests": _big_rows(40),
           "ingests_that_stored_nothing": _big_rows(10, query="q" * 200), "sample_per_vector": _big_rows(80)}
    assert len(json.dumps(acct.brief("learned", out))) < 3000


def test_brief_free_and_pipelines_fit_tool_cap():
    free = {"date": "d", "projects_worked": _big_rows(5, title="t", note="n" * 500, next_step="s" * 300), "pursuit_threads": _big_rows(30, topic="t", wakes=1, note="n" * 400),
            "tinkering": _big_rows(10, topic="t" * 200, wanted_fix=False), "proposals_filed": _big_rows(10), "reaches_to_jordan": _big_rows(10, message="m" * 300),
            "self_directed_memories": _big_rows(60), "growth": _big_rows(10, weakness="w" * 300), "journal_pieces": _big_rows(20)}
    assert len(json.dumps(acct.brief("free", free))) < 3000
    pipes = {"as_of": "x", "nova_speaks": {"in_flight": _big_rows(20), "done_today": [{"rendered": 1}], "failed_today": _big_rows(10, note="n" * 300)},
             "ingests_running": _big_rows(40), "claude_queue_open": [], "scheduler_failures_today": _big_rows(20, task_id="t", error="e" * 300), "open_incidents": _big_rows(20, severity="s", title="t" * 300)}
    assert len(json.dumps(acct.brief("pipelines", pipes))) < 3000


def test_brief_passes_through_errors_unchanged():
    out = {"date": "d", "total_new_memories": None, "by_vector": {"error": "OperationalError: down"}, "ingest_requests": [], "ingests_that_stored_nothing": [], "sample_per_vector": []}
    assert acct.brief("learned", out)["top_vectors"] == {"error": "OperationalError: down"}      # unreachable ledger is reported, never guessed around


# ── unit: article lookup by time / slug / title words, on a fake journal ───────────────────
@pytest.fixture
def journal(tmp_path, monkeypatch):
    (tmp_path / "content" / "local").mkdir(parents=True)
    (tmp_path / "content" / "local" / "2026-10-05-heat-dome.md").write_text('---\ntitle: "Heat Dome Leaves"\ndate: 2026-10-05T10:00:00-07:00\n---\nbody\n')
    (tmp_path / "content" / "local" / "2026-10-04-fall.md").write_text('---\ntitle: "Fall Moved to Oregon"\ndate: 2026-10-04T10:00:00-07:00\n---\nbody\n')
    monkeypatch.setattr(acct, "JOURNAL", tmp_path)
    monkeypatch.setattr(acct, "_git", lambda args, cwd=None, timeout=30: "abc1234 2026-10-05 10:54:04 -0700" if "%ci" in " ".join(args) else ("2026-10-05 10:03:55 -0700" if "%ai" in " ".join(args) else "abc1234"))
    monkeypatch.setattr(acct, "_http", lambda url: 200)
    monkeypatch.setattr(acct.shutil, "which", lambda x: None)
    return tmp_path


def test_article_by_time_explains_late_push(journal):
    r = acct.article("10:00", acct.date(2026, 10, 5))
    assert r["found"] and r["slug"] == "2026-10-05-heat-dome" and r["live_http"] == 200
    assert any("push failed in between" in w for w in r["why_late"])


def test_article_by_words_and_by_slug(journal):
    assert acct.article("fall oregon", acct.date(2026, 10, 5))["slug"] == "2026-10-04-fall"
    assert acct.article("heat-dome", acct.date(2026, 10, 5))["section"] == "local"
    assert acct.article("nothing matches this", acct.date(2026, 10, 5))["found"] is False


# ── security ────────────────────────────────────────────────────────────────
def test_account_is_read_only_and_parameterized():
    src = (SCRIPTS / "nova_account.py").read_text()
    assert not any(k in src.upper() for k in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ")), "account organ must never write"
    assert "%s" in src and ".format(" not in src and "f\"SELECT" not in src   # psycopg2 params, no string-built SQL


def test_warm_never_sends_secrets_and_only_talks_to_placement_hosts():
    src = (SCRIPTS / "nova_model_warm.py").read_text()
    assert "keep_alive" in src and "Authorization" not in src
    assert all(u.startswith("http://192.168.1.") for u in warm.DEFAULT_PLACEMENT)


# ── performance ─────────────────────────────────────────────────────────────
def test_brief_is_fast_on_huge_reports():
    out = {"date": "d", "total_new_memories": 1, "by_vector": _big_rows(5000), "ingest_requests": _big_rows(2000), "ingests_that_stored_nothing": [], "sample_per_vector": _big_rows(5000)}
    t0 = time.time(); acct.brief("learned", out); assert time.time() - t0 < 0.5


# ── retry / resilience ──────────────────────────────────────────────────────
def test_query_helper_returns_error_dict_instead_of_raising():
    r = acct._q("host=127.0.0.1 port=1 dbname=x user=x", "SELECT 1")
    assert isinstance(r, dict) and "error" in r


def test_warm_reports_fail_and_continues(monkeypatch):
    monkeypatch.setattr(warm, "_get", lambda url, timeout=5: (_ for _ in ()).throw(OSError("down")))
    assert warm.warm("http://192.168.1.99:11434", "qwen3:8b").startswith("fail:")


def test_warm_distinguishes_warm_from_cold_load(monkeypatch):
    monkeypatch.setattr(warm, "_get", lambda url, timeout=5: {"models": [{"name": "qwen3:8b"}]})
    monkeypatch.setattr(warm, "_post", lambda url, body, timeout=600: {"done": True})
    assert warm.warm("http://h:11434", "qwen3:8b") == "warm"
    assert warm.warm("http://h:11434", "llama3.2:3b").startswith("loaded in")


# ── integration: the gateway sticks to a node where the chat model is resident ──────────
def test_best_url_prefers_warm_node_and_sticks(monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    from nova_gateway import router as gw
    rows = {"ollama": [{"url": "http://fast-but-cold", "status": "up", "has_chat_model": True, "loaded": []},
                       {"url": "http://warm-a", "status": "up", "has_chat_model": True, "loaded": ["qwen3:8b"]},
                       {"url": "http://warm-b", "status": "up", "has_chat_model": True, "loaded": ["qwen3:8b"]}]}
    monkeypatch.setattr(gw, "_RANK_CACHE", {"ts": time.time() + 10**6, "val": rows})
    assert gw._best_url("ollama", "http://default") == "http://warm-a"
    gw._RANK_CACHE["last_ollama"] = "http://warm-b"
    assert gw._best_url("ollama", "http://default") == "http://warm-b"           # sticky while still warm+up
    rows["ollama"][2]["status"] = "down"
    assert gw._best_url("ollama", "http://default") == "http://warm-a"           # falls back when the sticky node dies
    rows["ollama"] = [{"url": "http://cold-only", "status": "up", "has_chat_model": True, "loaded": []}]
    assert gw._best_url("ollama", "http://default") == "http://cold-only"        # no warm node: old behaviour


def test_tools_registered_and_dispatched():
    src = (SCRIPTS / "nova_gateway/tools.py").read_text()
    for t in ("nova_learned", "nova_free_time", "nova_pipelines", "nova_article_status"):
        assert f'"{t}"' in src
    assert '"nova_account.py", "args": args' in src and '"--brief"' in src


# ── functional ──────────────────────────────────────────────────────────────
def test_cli_pipelines_brief_is_json_under_cap():
    r = subprocess.run([sys.executable, str(SCRIPTS / "nova_account.py"), "pipelines", "--brief"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    d = json.loads(r.stdout); assert "renders_today" in d and len(r.stdout) < 3000


# ── frame ───────────────────────────────────────────────────────────────────
def test_scripts_help_run():
    for s in ("nova_account.py", "nova_model_warm.py"):
        r = subprocess.run([sys.executable, str(SCRIPTS / s), "--help"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, s
