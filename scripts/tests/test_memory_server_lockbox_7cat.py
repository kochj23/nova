#!/usr/bin/env python3
"""7-category tests for the lockbox filter in memory_server.py ("Dan's Lockboxes"): boxed memories
(metadata.boxed=true) never surface in casual recall (/recall, /recall_batch, /recall/deep incl.
its linked hops, /random) and come back only through /recall?include_boxed=true.

Offline: the PG pool, Redis, Ollama embed and the anchor lookup are replaced with in-memory
fakes; nothing touches a real database or network. Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("NOVA_TEST_QUIET", "1")

pytest.importorskip("fastapi")
pytest.importorskip("asyncpg")


def _load():
    spec = importlib.util.spec_from_file_location("memory_server_lockbox_7cat", ROOT / "memory_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = _load()
SRC = (ROOT / "memory_server.py").read_text()
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
BOX_MARK = "IS DISTINCT FROM 'true'"


def _row(mid, boxed=False, score=0.9, text=None):
    meta = {"boxed": True} if boxed else {}
    return {"id": mid, "text": text or f"memory {mid}", "metadata": meta, "source": "conversation",
            "created_at": NOW, "access_count": 0, "score": score, "rank": 0.5, "tier": "long_term",
            "accessed_at": None, "link_type": "related", "strength": 0.8}


class FakeConn:
    """Filters boxed rows exactly when the SQL carries the boxed clause — like PG would."""

    def __init__(self, rows, links=None):
        self.rows, self.links, self.sql = rows, links or [], []

    def _filter(self, sql, rows):
        return [r for r in rows if not (BOX_MARK in sql and r["metadata"].get("boxed"))]

    async def fetch(self, sql, *args):
        self.sql.append((sql, args))
        if "memory_links" in sql:
            return self._filter(sql, self.links)
        if "ORDER BY RANDOM()" in sql:
            return self._filter(sql, self.rows)[: args[-1]]
        return self._filter(sql, self.rows)

    async def fetchval(self, sql, *args):
        self.sql.append((sql, args))
        return 10

    async def execute(self, sql, *args):
        self.sql.append((sql, args))

    def transaction(self):
        conn = self

        class T:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return T()


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class A:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return A()


class FakeRedis:
    def __init__(self, fail=False):
        self.store, self.fail, self.keys = {}, fail, []

    async def get(self, k):
        self.keys.append(k)
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(k)

    async def setex(self, k, ttl, v):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[k] = v


@pytest.fixture
def env(monkeypatch):
    rows = [_row("open1"), _row("boxed1", boxed=True)]
    links = [_row("link_open"), _row("link_boxed", boxed=True)]
    conn = FakeConn(rows, links)
    redis = FakeRedis()
    monkeypatch.setattr(ms, "_pg_pool", FakePool(conn))
    monkeypatch.setattr(ms, "_redis", redis)
    monkeypatch.setattr(ms, "embed", AsyncMock(return_value=[0.1] * 4))
    monkeypatch.setattr(ms, "_anchored_subjects", AsyncMock(return_value=[]))
    monkeypatch.setattr(ms, "_update_access", AsyncMock(return_value=None))
    monkeypatch.setattr(ms, "_TOKEN", "")
    return {"conn": conn, "redis": redis}


def run(coro):
    return asyncio.run(coro)


def ids(result):
    return [m["id"] for m in result["memories"]]


# ── Security ───────────────────────────────────────────────────────────────────────

class TestSecurity:
    def test_clauses_are_constant_sql(self):
        for c in (ms.BOXED_CLAUSE, ms.BOXED_CLAUSE_M):
            assert "$" not in c and "{" not in c and "%" not in c and BOX_MARK in c

    def test_recall_excludes_boxed_by_default(self, env):
        assert ids(run(ms._do_recall("q"))) == ["open1"]

    def test_recall_batch_cannot_opt_into_boxed(self, env):
        req = MagicMock()
        req.json = AsyncMock(return_value={"queries": [{"q": "a", "include_boxed": True}]})
        resp = run(ms.recall_batch(req))
        body = json.loads(resp.body)
        assert [m["id"] for m in body["results"][0]["memories"]] == ["open1"]

    def test_random_excludes_boxed_both_branches(self, env):
        for src in (None, "conversation"):
            out = run(ms.random_memory(source=src, n=10))
            assert [m["id"] for m in out["memories"]] == ["open1"]
        rnd = [s for s, _ in env["conn"].sql if "RANDOM()" in s]
        assert len(rnd) == 2 and all(ms.BOXED_CLAUSE in s for s in rnd)

    def test_search_excludes_boxed_in_every_branch(self, env):
        # Federal Hill Lights (2026-10-08) found /search had no lockbox filter.
        for mode in ("fts", "ilike"):
            for src in (None, "conversation"):
                out = run(ms.text_search(q="memory", n=10, source=src, mode=mode, include_boxed=False))
                assert all(not m["metadata"].get("boxed") for m in out["memories"])
        srch = [s for s, _ in env["conn"].sql if "FROM memories WHERE t" in s]
        assert len(srch) >= 4 and all(ms.BOXED_CLAUSE in s for s in srch)

    def test_search_include_boxed_is_explicit(self, env):
        out = run(ms.text_search(q="memory", n=10, source=None, mode="ilike", include_boxed=True))
        assert any(m["metadata"].get("boxed") for m in out["memories"])

    def test_deep_recall_excludes_boxed_hits_and_linked_hops(self, env):
        out = run(ms.deep_recall(q="q", n=5, source=None, min_score=0.0))
        assert ids(out) == ["open1"]
        assert "link_boxed" not in [l["id"] for l in out["linked"]]     # was a leak before the fix
        hops = [s for s, _ in env["conn"].sql if "memory_links" in s]
        assert hops and all(ms.BOXED_CLAUSE_M in s for s in hops)

    def test_boxed_cache_key_is_separate(self, env):
        run(ms._do_recall("same q"))
        run(ms._do_recall("same q", include_boxed=True))
        assert len(set(env["redis"].keys)) == 2
        cached_default = [json.loads(v) for v in env["redis"].store.values()]
        assert any(c["memories"] and all(m["id"] != "boxed1" for m in c["memories"]) for c in cached_default)

    def test_http_include_boxed_must_be_bool(self, env):
        from fastapi.testclient import TestClient
        c = TestClient(ms.app)
        assert c.get("/recall", params={"q": "x", "include_boxed": "1; DROP TABLE memories"}).status_code == 422
        assert c.get("/recall", params={"q": "  "}).status_code == 400


# ── Performance ────────────────────────────────────────────────────────────────────

class TestPerformance:
    def test_clause_added_once_per_recall(self, env):
        run(ms._do_recall("q"))
        for s, _ in env["conn"].sql:
            if "FROM memories" in s and "tsv" not in s or "tsv @@" in s:
                assert s.count(BOX_MARK) <= 1

    def test_recall_two_queries_regardless_of_box_state(self, env):
        run(ms._do_recall("q"))
        n_default = len([s for s, _ in env["conn"].sql if "FROM memories" in s])
        env["conn"].sql.clear()
        run(ms._do_recall("q2", include_boxed=True))
        assert len([s for s, _ in env["conn"].sql if "FROM memories" in s]) == n_default == 2

    def test_batch_capped_at_five(self, env):
        req = MagicMock()
        req.json = AsyncMock(return_value={"queries": [{"q": f"q{i}"} for i in range(50)]})
        assert json.loads(run(ms.recall_batch(req)).body)["count"] == 5

    def test_many_rows_filtered_fast(self, env):
        env["conn"].rows = [_row(f"m{i}", boxed=i % 2 == 0) for i in range(5000)]
        t = time.monotonic()
        out = run(ms._do_recall("q", n=50))
        assert all(not m["metadata"].get("boxed") for m in out["memories"]) and time.monotonic() - t < 3


# ── Retry ──────────────────────────────────────────────────────────────────────────

class TestRetry:
    def _http(self, failures):
        calls = {"n": 0}

        class Resp:
            def raise_for_status(self): pass
            def json(self): return {"embeddings": [[0.5, 0.5]]}

        async def post(*a, **k):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise ConnectionError("ollama blip")
            return Resp()
        return MagicMock(post=post), calls

    def test_embed_retries_then_succeeds(self, monkeypatch):
        http, calls = self._http(2)
        sleeps = []

        async def fake_sleep(s): sleeps.append(s)
        monkeypatch.setattr(ms, "_http", http)
        monkeypatch.setattr(ms.asyncio, "sleep", fake_sleep)
        assert run(ms.embed("x")) == [0.5, 0.5]
        assert calls["n"] == 3 and sleeps == [0.5, 1.0]

    def test_embed_raises_after_three(self, monkeypatch):
        http, calls = self._http(99)

        async def fake_sleep(s): pass
        monkeypatch.setattr(ms, "_http", http)
        monkeypatch.setattr(ms.asyncio, "sleep", fake_sleep)
        with pytest.raises(ConnectionError):
            run(ms.embed("x"))
        assert calls["n"] == 3                     # never silent on the first attempt, never endless

    def test_redis_down_still_filters_boxed(self, env, monkeypatch):
        monkeypatch.setattr(ms, "_redis", FakeRedis(fail=True))
        assert ids(run(ms._do_recall("q"))) == ["open1"]

    def test_fts_leg_failure_degrades_but_stays_filtered(self, env, monkeypatch):
        async def boom(*a, **k): raise RuntimeError("fts down")
        conn = env["conn"]
        orig = conn.fetch

        async def fetch(sql, *args):
            if "tsv @@" in sql:
                await boom()
            return await orig(sql, *args)
        monkeypatch.setattr(conn, "fetch", fetch)
        assert ids(run(ms._do_recall("q"))) == ["open1"]


# ── Unit ───────────────────────────────────────────────────────────────────────────

class TestUnit:
    def test_empty_query_short_circuits(self, env):
        assert run(ms._do_recall("   ")) == {"memories": [], "query": "   ", "count": 0}
        ms.embed.assert_not_called()

    def test_sup_includes_clause_only_when_not_boxed(self, env):
        run(ms._do_recall("q"))
        legs = [s for s, _ in env["conn"].sql if "FROM memories" in s]
        assert all(ms.BOXED_CLAUSE in s for s in legs)
        env["conn"].sql.clear()
        run(ms._do_recall("q", include_boxed=True))
        legs = [s for s, _ in env["conn"].sql if "FROM memories" in s]
        assert legs and not any(ms.BOXED_CLAUSE in s for s in legs)

    def test_cache_key_suffix(self):
        assert ":b1" in SRC and "include_boxed: bool = Query(False)" in SRC


# ── Integration ────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_relationship_client_and_server_agree(self):
        rel_src = (SCRIPTS / "nova_relationship.py").read_text()
        assert '"include_boxed": "true"' in rel_src and "/recall?" in rel_src
        assert '"boxed": True' in rel_src and "metadata->>'boxed'" in ms.BOXED_CLAUSE
        assert "'boxed'" in rel_src.split("def unbox")[1].split("def ")[0]

    def test_recall_endpoint_passes_flag_through(self, env):
        captured = {}

        async def fake(*a):
            captured["a"] = a
            return {"memories": [], "query": a[0], "count": 0}
        with patch.object(ms, "_do_recall", fake):
            run(ms.recall(q="x", n=5, source=None, min_score=0.0, tier="standard",
                          include_private=True, include_boxed=True))
        assert captured["a"][-1] is True


# ── Functional ─────────────────────────────────────────────────────────────────────

class TestFunctional:
    def test_http_golden_casual_vs_explicit_door(self, env):
        from fastapi.testclient import TestClient
        c = TestClient(ms.app)
        casual = c.get("/recall", params={"q": "the threat"}).json()
        door = c.get("/recall", params={"q": "the threat", "include_boxed": "true"}).json()
        assert ids(casual) == ["open1"]
        assert set(ids(door)) == {"open1", "boxed1"}
        assert c.get("/random", params={"n": 5}).json()["count"] == 1

    def test_embed_down_is_an_error_not_a_silent_leak(self, env, monkeypatch):
        monkeypatch.setattr(ms, "embed", AsyncMock(side_effect=ConnectionError("down")))
        with pytest.raises(ConnectionError):
            run(ms._do_recall("q"))
        req = MagicMock()
        req.json = AsyncMock(return_value={"queries": [{"q": "a"}]})
        body = json.loads(run(ms.recall_batch(req)).body)
        assert body["results"][0]["memories"] == [] and "error" in body["results"][0]


# ── Frame ──────────────────────────────────────────────────────────────────────────

class TestFrame:
    def test_routes_present(self):
        paths = {getattr(r, "path", "") for r in ms.app.routes}
        assert {"/recall", "/recall_batch", "/recall/deep", "/random"} <= paths

    def test_selftest_subprocess(self):
        r = subprocess.run([sys.executable, str(ROOT / "memory_server.py"), "--selftest"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "selftest passed" in r.stdout
