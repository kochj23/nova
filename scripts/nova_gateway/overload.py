"""
nova_gateway.overload — overload protection for the LLM route (Google SRE book ch.21 "Handling Overload",
ch.22 "Addressing Cascading Failures").

Three small, stdlib-only pieces used by router.ModelRouter:
  - deadline math: every backend attempt gets min(its own timeout, time left), and no attempt starts with
    less than MIN_ATTEMPT_S left (DeadlineExceeded instead);
  - retry budget: at most MAX_RETRIES_PER_ROUTE extra attempts per route() call AND, process-wide, retries may
    not exceed RETRY_RATIO of requests in a sliding RETRY_WINDOW_S window (with a RETRY_MIN_ALLOWANCE floor so
    a quiet home system can still fail over). Stops retry amplification when every backend is sick;
  - per-backend concurrency limit: a backend/node already at MAX_INFLIGHT[backend] in-flight requests is
    skipped (shed, fall through to the next node/backend) instead of piling more work onto a wedging GPU.

Counters are exposed in /health under "overload" (snapshot()).
Defaults are overridable by env (NOVA_GW_*) and at runtime by service_config gateway/overload via /reload.

Written by Jordan Koch.
"""

import os
import random
import time
from collections import deque
from contextlib import contextmanager


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


MIN_ATTEMPT_S = _env_f("NOVA_GW_MIN_ATTEMPT_S", 2.0)          # never start a backend call with less left
MAX_RETRIES_PER_ROUTE = int(_env_f("NOVA_GW_MAX_RETRIES", 4))  # extra attempts per route() (chain is 5 deep)
RETRY_RATIO = _env_f("NOVA_GW_RETRY_RATIO", 0.10)              # process-wide retries / requests
RETRY_WINDOW_S = _env_f("NOVA_GW_RETRY_WINDOW_S", 60.0)
RETRY_MIN_ALLOWANCE = int(_env_f("NOVA_GW_RETRY_MIN", 20))     # retries always allowed per window (low traffic)
MAX_INFLIGHT = {                                               # per backend NODE (key = backend@url)
    "ollama":     int(_env_f("NOVA_GW_MAXINFLIGHT_OLLAMA", 4)),
    "mlx":        int(_env_f("NOVA_GW_MAXINFLIGHT_MLX", 2)),        # single-threaded server: 1 running + 1 queued
    "llamacpp":   int(_env_f("NOVA_GW_MAXINFLIGHT_LLAMACPP", 2)),
    "openrouter": int(_env_f("NOVA_GW_MAXINFLIGHT_OPENROUTER", 8)),
}


class DeadlineExceeded(TimeoutError):
    """Not enough time left in the request deadline to start another backend attempt."""


class RetryBudgetExceeded(RuntimeError):
    """Per-request retry cap or process-wide retry ratio reached; stop the chain."""


class BackendSaturated(RuntimeError):
    """Backend/node is at its concurrency limit; request shed (try the next one)."""


STATS = {"requests": 0, "retries": 0, "shed": 0, "deadline_exceeded": 0, "retries_budget_denied": 0}


# ── deadline math ─────────────────────────────────────────────────────────────
def remaining(deadline, now: float = None) -> float:
    """Seconds left until deadline (inf when there is none)."""
    if deadline is None:
        return float("inf")
    return deadline - (time.time() if now is None else now)


def attempt_timeout(deadline, default: float, floor: float = None, now: float = None) -> float:
    """Timeout for one backend attempt: min(default, time left). Raises DeadlineExceeded below the floor."""
    left = remaining(deadline, now)
    if left < (MIN_ATTEMPT_S if floor is None else floor):
        raise DeadlineExceeded("route budget exhausted")
    return min(default, left)


# ── retry budget ──────────────────────────────────────────────────────────────
class RetryBudget:
    """Sliding-window retry ratio cap: a retry is allowed while retries < max(min_allowance, ratio * requests)."""

    def __init__(self, ratio: float = None, window_s: float = None, min_allowance: int = None):
        self.ratio = RETRY_RATIO if ratio is None else ratio
        self.window_s = RETRY_WINDOW_S if window_s is None else window_s
        self.min_allowance = RETRY_MIN_ALLOWANCE if min_allowance is None else min_allowance
        self._requests: deque = deque()
        self._retries: deque = deque()

    def _trim(self, now: float) -> None:
        cut = now - self.window_s
        for q in (self._requests, self._retries):
            while q and q[0] < cut:
                q.popleft()

    def record_request(self, now: float = None) -> None:
        now = time.time() if now is None else now
        self._trim(now)
        self._requests.append(now)

    def try_spend(self, now: float = None) -> bool:
        now = time.time() if now is None else now
        self._trim(now)
        if len(self._retries) >= max(self.min_allowance, self.ratio * len(self._requests)):
            return False
        self._retries.append(now)
        return True

    def window(self) -> dict:
        self._trim(time.time())
        return {"requests": len(self._requests), "retries": len(self._retries)}


RETRY_BUDGET = RetryBudget()


class AttemptGate:
    """One per route() call. Call before every real send: the first send of a fresh request is free; each later
    one (or every one, for a request that is itself a retry, e.g. the rescue pass) spends retry budget."""

    def __init__(self, is_retry: bool = False, budget: RetryBudget = None, max_retries: int = None):
        self.is_retry = is_retry
        self.budget = budget or RETRY_BUDGET
        self.max_retries = MAX_RETRIES_PER_ROUTE if max_retries is None else max_retries
        self.sent = 0
        self.retries = 0

    def __call__(self) -> None:
        if self.sent == 0 and not self.is_retry:
            self.sent += 1
            return
        if self.retries >= self.max_retries or not self.budget.try_spend():
            STATS["retries_budget_denied"] += 1
            raise RetryBudgetExceeded("retry budget exhausted")
        self.retries += 1
        self.sent += 1
        STATS["retries"] += 1


def backoff_s(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Jittered exponential backoff ('equal jitter'): uniform in [d/2, d], d = min(cap, base * 2**(attempt-1))."""
    d = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(d / 2, d)


# ── per-backend concurrency limit ─────────────────────────────────────────────
class ConcurrencyLimiter:
    """Non-blocking in-flight counter per key. No queueing: at the limit, the caller is shed immediately."""

    def __init__(self, limits: dict = None):
        self.limits = limits if limits is not None else MAX_INFLIGHT
        self.inflight: dict = {}

    def limit_for(self, key: str) -> int:
        return int(self.limits.get(key.split("@", 1)[0], 4))

    def saturated(self, key: str) -> bool:
        return self.inflight.get(key, 0) >= self.limit_for(key)

    @contextmanager
    def slot(self, key: str, gate: AttemptGate = None):
        """Shed (BackendSaturated) if `key` is full; else spend the gate (may raise RetryBudgetExceeded) and hold
        one in-flight slot for the duration. Released on any exit, including cancellation."""
        if self.saturated(key):
            STATS["shed"] += 1
            raise BackendSaturated(f"{key} at concurrency limit {self.limit_for(key)}")
        if gate is not None:
            gate()
        self.inflight[key] = self.inflight.get(key, 0) + 1
        try:
            yield
        finally:
            self.inflight[key] -= 1
            if self.inflight[key] <= 0:
                self.inflight.pop(key, None)


LIMITER = ConcurrencyLimiter()


def configure(cfg: dict) -> list:
    """Apply service_config gateway/overload overrides; returns a list of change descriptions."""
    global MIN_ATTEMPT_S, MAX_RETRIES_PER_ROUTE
    changes = []
    if "min_attempt_s" in cfg:
        MIN_ATTEMPT_S = float(cfg["min_attempt_s"]); changes.append(f"min_attempt_s={MIN_ATTEMPT_S}")
    if "max_retries_per_route" in cfg:
        MAX_RETRIES_PER_ROUTE = int(cfg["max_retries_per_route"]); changes.append(f"max_retries={MAX_RETRIES_PER_ROUTE}")
    for k, attr, cast in (("retry_ratio", "ratio", float), ("retry_window_s", "window_s", float),
                          ("retry_min", "min_allowance", int)):
        if k in cfg:
            setattr(RETRY_BUDGET, attr, cast(cfg[k])); changes.append(f"{k}={cfg[k]}")
    for name, n in (cfg.get("max_inflight") or {}).items():
        MAX_INFLIGHT[name] = int(n); changes.append(f"max_inflight.{name}={n}")
    return changes


def snapshot() -> dict:
    """/health 'overload' block: cumulative counters since start + current in-flight per backend and per node."""
    by_backend = {name: 0 for name in MAX_INFLIGHT}
    for key, n in LIMITER.inflight.items():
        b = key.split("@", 1)[0]
        by_backend[b] = by_backend.get(b, 0) + n
    return {**STATS, "inflight": by_backend, "inflight_nodes": dict(LIMITER.inflight),
            "max_inflight": dict(MAX_INFLIGHT), "retry_window": RETRY_BUDGET.window()}
