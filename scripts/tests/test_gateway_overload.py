"""
test_gateway_overload.py — pure unit tests for nova_gateway.overload (deadline math, retry budget, concurrency
limiter) plus the router wiring (shed falls through, retry budget stops the chain, deadline stops the chain).

Offline: no network, no PG. Written by Jordan Koch.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova_gateway import overload as ov  # noqa: E402
from nova_gateway import router as rt     # noqa: E402


class TestDeadline(unittest.TestCase):
    def test_remaining(self):
        self.assertEqual(ov.remaining(110.0, now=100.0), 10.0)
        self.assertEqual(ov.remaining(None), float("inf"))

    def test_attempt_timeout_clips_to_time_left(self):
        self.assertEqual(ov.attempt_timeout(130.0, 45, now=100.0), 30.0)
        self.assertEqual(ov.attempt_timeout(200.0, 45, now=100.0), 45)
        self.assertEqual(ov.attempt_timeout(None, 45), 45)

    def test_attempt_refused_below_floor(self):
        with self.assertRaises(ov.DeadlineExceeded):
            ov.attempt_timeout(101.0, 45, floor=2.0, now=100.0)
        self.assertTrue(issubclass(ov.DeadlineExceeded, TimeoutError))


class TestRetryBudget(unittest.TestCase):
    def test_min_allowance_then_ratio(self):
        b = ov.RetryBudget(ratio=0.1, window_s=60, min_allowance=2)
        for _ in range(10):
            b.record_request(now=0)
        self.assertTrue(b.try_spend(now=1))
        self.assertTrue(b.try_spend(now=1))
        self.assertFalse(b.try_spend(now=1))           # 2 >= max(2, 1.0)
        for _ in range(20):
            b.record_request(now=2)                    # 30 requests -> 3 retries allowed
        self.assertTrue(b.try_spend(now=3))
        self.assertFalse(b.try_spend(now=3))

    def test_window_slides(self):
        b = ov.RetryBudget(ratio=0.1, window_s=60, min_allowance=1)
        self.assertTrue(b.try_spend(now=0))
        self.assertFalse(b.try_spend(now=30))
        self.assertTrue(b.try_spend(now=61))           # old retry aged out

    def test_gate_first_send_free_then_capped(self):
        b = ov.RetryBudget(ratio=0, window_s=60, min_allowance=100)
        g = ov.AttemptGate(budget=b, max_retries=2)
        g(); g(); g()                                  # first + 2 retries
        with self.assertRaises(ov.RetryBudgetExceeded):
            g()

    def test_gate_for_retry_request_spends_on_first_send(self):
        b = ov.RetryBudget(ratio=0, window_s=60, min_allowance=0)
        with self.assertRaises(ov.RetryBudgetExceeded):
            ov.AttemptGate(is_retry=True, budget=b)()
        ov.AttemptGate(is_retry=False, budget=b)()     # fresh request's first send never denied

    def test_backoff_is_jittered_and_capped(self):
        for _ in range(50):
            self.assertTrue(0.5 <= ov.backoff_s(1) <= 1.0)
            self.assertTrue(2.0 <= ov.backoff_s(3) <= 4.0)
            self.assertLessEqual(ov.backoff_s(20, cap=5), 5)


class TestLimiter(unittest.TestCase):
    def test_sheds_at_limit_and_releases(self):
        lim = ov.ConcurrencyLimiter({"mlx": 1})
        before = ov.STATS["shed"]
        with lim.slot("mlx@a"):
            self.assertEqual(lim.inflight["mlx@a"], 1)
            with self.assertRaises(ov.BackendSaturated):
                with lim.slot("mlx@a"):
                    pass
            with lim.slot("mlx@b"):                    # per node
                pass
        self.assertEqual(lim.inflight, {})
        self.assertEqual(ov.STATS["shed"], before + 1)

    def test_released_on_exception_and_gate_denial_holds_nothing(self):
        lim = ov.ConcurrencyLimiter({"ollama": 2})
        with self.assertRaises(ValueError):
            with lim.slot("ollama@x"):
                raise ValueError
        g = ov.AttemptGate(is_retry=True, budget=ov.RetryBudget(ratio=0, min_allowance=0))
        with self.assertRaises(ov.RetryBudgetExceeded):
            with lim.slot("ollama@x", g):
                pass
        self.assertEqual(lim.inflight, {})

    def test_snapshot_keys(self):
        snap = ov.snapshot()
        for k in ("shed", "deadline_exceeded", "retries_budget_denied", "inflight", "requests", "retries"):
            self.assertIn(k, snap)
        self.assertIn("ollama", snap["inflight"])

    def test_configure(self):
        saved = (dict(ov.MAX_INFLIGHT), ov.RETRY_BUDGET.ratio)
        try:
            ch = ov.configure({"retry_ratio": 0.2, "max_inflight": {"mlx": 1}})
            self.assertEqual(ov.RETRY_BUDGET.ratio, 0.2)
            self.assertEqual(ov.MAX_INFLIGHT["mlx"], 1)
            self.assertEqual(len(ch), 2)
        finally:
            ov.MAX_INFLIGHT.clear(); ov.MAX_INFLIGHT.update(saved[0]); ov.RETRY_BUDGET.ratio = saved[1]


# ── router wiring ─────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text = data, code, ""

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    def __init__(self, posts):
        self.posts, self.calls = posts, []

    async def get(self, url, timeout=None):
        return _Resp({"data": []})

    async def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(url)
        for frag, ans in self.posts.items():
            if frag in url:
                if isinstance(ans, Exception):
                    raise ans
                return _Resp(ans)
        raise RuntimeError("no route")


OAI = {"choices": [{"message": {"content": "from mlx"}}]}


def _route(http, **kw):
    r = rt.ModelRouter()
    r.BACKENDS = [b for b in r.BACKENDS if b[0] != "openrouter"]
    with patch.object(rt, "nova_lb", None), patch.object(rt, "_best_url", side_effect=lambda k, d: d), \
            patch.object(rt, "_ollama_candidates", side_effect=lambda d: [d]), \
            patch.object(rt.ModelRouter, "_log_inference", new=AsyncMock()):
        return asyncio.run(r.route([{"role": "user", "content": "hi"}], ctx=SimpleNamespace(http=http), **kw))


class TestRouterWiring(unittest.TestCase):
    def setUp(self):
        self._budget, self._limiter = rt.overload.RETRY_BUDGET, rt.LIMITER
        rt.overload.RETRY_BUDGET = ov.RetryBudget(ratio=0.1, window_s=60, min_allowance=100)

    def tearDown(self):
        rt.overload.RETRY_BUDGET = self._budget

    def test_saturated_backend_is_shed_and_next_answers(self):
        lim = ov.ConcurrencyLimiter({"ollama": 0, "mlx": 2, "llamacpp": 2})
        http = _Http({"/api/chat": RuntimeError("must not be called"), "/v1/chat": OAI})
        with patch.object(rt, "LIMITER", lim):
            self.assertEqual(_route(http), "from mlx")
        self.assertFalse(any("/api/chat" in c for c in http.calls))

    def test_retry_budget_denied_stops_chain(self):
        rt.overload.RETRY_BUDGET = ov.RetryBudget(ratio=0, min_allowance=0)
        http = _Http({"/api/chat": RuntimeError("down"), "/v1/chat": OAI})
        before = ov.STATS["retries_budget_denied"]
        with self.assertRaises(RuntimeError) as cm:
            _route(http)
        self.assertIn("retry budget", str(cm.exception))
        self.assertEqual(ov.STATS["retries_budget_denied"], before + 1)
        self.assertEqual(len(http.calls), 1)          # no amplification

    def test_rescue_route_spends_budget_on_first_send(self):
        rt.overload.RETRY_BUDGET = ov.RetryBudget(ratio=0, min_allowance=0)
        http = _Http({"/api/chat": {"message": {"content": "ok"}}})
        with self.assertRaises(RuntimeError):
            _route(http, is_retry=True)
        self.assertEqual(http.calls, [])

    def test_deadline_counted_and_no_send(self):
        before = ov.STATS["deadline_exceeded"]
        http = _Http({"/api/chat": {"message": {"content": "ok"}}})
        with self.assertRaises(RuntimeError):
            _route(http, budget_s=0.5)
        self.assertEqual(http.calls, [])
        self.assertEqual(ov.STATS["deadline_exceeded"], before + 1)

    def test_failover_within_budget_still_works(self):
        http = _Http({"/api/chat": RuntimeError("down"), "/v1/chat": OAI})
        self.assertEqual(_route(http), "from mlx")


if __name__ == "__main__":
    unittest.main()
