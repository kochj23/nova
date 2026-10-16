"""Pure checks for nova_slo burn-rate math and the Workbook multiwindow rules (no DB)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_slo as s  # noqa: E402


def test_burn_rate():
    assert abs(s.burn_rate(990, 1000, 0.99) - 1.0) < 1e-9
    assert abs(s.burn_rate(856, 1000, 0.99) - 14.4) < 1e-9
    assert s.burn_rate(0, 0, 0.99) == 0.0          # no events, no burn


def test_budget_remaining():
    assert s.budget_remaining(1000, 1000, 0.99) == 1.0
    assert abs(s.budget_remaining(995, 1000, 0.99) - 0.5) < 1e-9
    assert s.budget_remaining(980, 1000, 0.99) < 0     # overspent
    assert s.budget_remaining(0, 0, 0.99) == 1.0


def test_evaluate_needs_both_windows():
    assert s.evaluate({"1h": 20, "5m": 20}) == "page"
    assert s.evaluate({"1h": 20, "5m": 0}) == "ok"          # already recovered: no page
    assert s.evaluate({"6h": 7, "30m": 7}) == "page"
    assert s.evaluate({"3d": 1.5, "6h": 1.2}) == "ticket"
    assert s.evaluate({"3d": 1.5, "6h": 0.5}) == "ok"
    assert s.evaluate({}) == "ok"


def test_seed_targets_valid():
    names = [n for n, *_ in s.SEED]
    assert len(names) == len(set(names))
    for n, desc, sql, target, comps in s.SEED:
        assert 0 < target < 1 and "%(s)s" in sql and "%(e)s" in sql and comps


def test_is_stale():
    assert s.is_stale(13000, 0, 28) is True          # probe-fed SLI went quiet
    assert s.is_stale(13000, 30, 28) is False
    assert s.is_stale(28, 0, 28) is False            # daily SLI (morning BLUF): an empty hour is normal


# ── Little Mister, 2026-10-15: #42 state bug, #41 budget policy, #53 chat_jordan, #37 incident stats ──
import json  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def test_exhausted_budget_is_at_least_ticket():
    # #42: memory_embedding read 'ok' at budget -0.91 with calm recent windows
    burns = {"5m": 0.0, "30m": 0.0, "1h": 0.0, "6h": 0.0, "3d": 3.265}
    assert s.evaluate(burns) == "ok"                               # the old evaluation
    assert s.state_for(burns, -0.91, 13611) == "ticket"            # the fix
    assert s.state_for(burns, 0.5, 13611) == "ok"
    assert s.state_for({"1h": 20, "5m": 20}, -0.5, 13611) == "page"   # never downgrades a page
    assert s.state_for({}, 1.0, 0) == "no_data"


def test_min_events_suppresses_low_traffic_burn():
    burns = {"1h": 20, "5m": 20, "6h": 20, "30m": 20, "3d": 20}
    assert s.evaluate(burns, {"1h": 1, "6h": 2, "3d": 4}, 10) == "ok"     # one bad message must not page
    assert s.evaluate(burns, {"1h": 12, "6h": 12, "3d": 12}, 10) == "page"
    assert s.evaluate(burns, {"1h": 1, "6h": 3, "3d": 15}, 10) == "ticket"
    assert s.state_for(burns, -2.0, 4, {"1h": 1, "6h": 2, "3d": 4}, 10) == "ok"   # too few to freeze either
    assert s.budget_exhausted(-2.0, 4, 10) is False
    assert s.budget_exhausted(-0.1, 52, 10) is True


def test_chat_jordan_seed():
    row = next(r for r in s.SEED if r[0] == "chat_jordan")
    _, desc, sql, target, comps = row
    assert "person = 'jordan'" in sql and "gateway_traces" in sql
    assert f"total_ms <= {s.CHAT_JORDAN_LATENCY_MS}" in sql
    assert "something went wrong on my end" in sql                  # fallback replies are failures
    assert "%" not in sql.replace("%(s)s", "").replace("%(e)s", "")   # safe with psycopg2 params
    assert s.MIN_EVENTS["chat_jordan"] >= 10 and 0 < target < 1
    assert "ADD COLUMN IF NOT EXISTS min_events" in s.SCHEMA


class _Cur:
    """In-memory service_config + claude_queue stand-in for apply_budget_policy."""
    def __init__(self, freeze=None, open_items=0):
        self.freeze, self.open_items, self.sql, self._r = freeze, open_items, [], None
        self.queued = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if sql.startswith("SELECT value FROM service_config"):
            self._r = (self.freeze,) if self.freeze is not None else None
        elif sql.startswith("DELETE FROM service_config"):
            self.freeze = None
        elif sql.startswith("INSERT INTO service_config"):
            self.freeze = json.loads(params[2])
        elif sql.startswith("INSERT INTO claude_queue"):
            if not self.open_items:
                self.queued.append(params[1]); self.open_items += 1

    def fetchone(self):
        return self._r


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def _row(budget, exhausted):
    return dict(name="memory_embedding", budget=budget, sli=0.98, target=0.99, total=13611, exhausted=exhausted)


def test_budget_policy_sets_freeze_and_files_one_item():
    c = _Cur()
    assert s.apply_budget_policy(c, _row(-0.9, True), NOW) == "set"
    assert c.freeze["since"] == NOW.isoformat() and c.freeze["budget_remaining"] == -0.9
    assert len(c.queued) == 1 and c.queued[0].startswith("[slo-freeze:memory_embedding]")
    assert s.apply_budget_policy(c, _row(-0.95, True), NOW) == "held"   # 5 min later: same episode
    assert len(c.queued) == 1                                           # deduped
    assert c.freeze["since"] == NOW.isoformat()                         # episode start kept
    sc = [p for q, p in c.sql if q.startswith("INSERT INTO service_config")][0]
    assert sc[0] == "slo_freeze" and sc[1] == "memory_embedding"


def test_budget_policy_lifts_freeze_on_recovery():
    c = _Cur(freeze={"since": "x"})
    assert s.apply_budget_policy(c, _row(0.2, False), NOW) == "lifted"
    assert c.freeze is None
    assert s.apply_budget_policy(_Cur(), _row(0.2, False), NOW) is None


def test_queue_sql_is_parameterized_and_deduped():
    c = _Cur()
    s.apply_budget_policy(c, _row(-0.9, True), NOW)
    q, p = next((q, p) for q, p in c.sql if q.startswith("INSERT INTO claude_queue"))
    assert "NOT EXISTS" in q and "LIKE %s" in q
    assert p[3] == "[slo-freeze:memory_embedding]%"
    assert "memory_embedding" not in q                                   # never spliced into SQL


def test_is_frozen():
    class C:
        def execute(self, sql, params=None):
            self.sql, self.params = sql, params

        def fetchone(self):
            return (1,)

        def fetchall(self):
            return [("chat_jordan",), ("memory_embedding",)]
    assert s.is_frozen(C(), "memory_embedding") is True
    assert s.is_frozen(C()) == ["chat_jordan", "memory_embedding"]


def test_percentile_matches_percentile_cont():
    assert s.percentile([], 0.5) is None
    assert s.percentile([None, 5], 0.5) == 5
    assert s.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert abs(s.percentile(range(1, 11), 0.9) - 9.1) < 1e-9


def test_incident_stats_are_percentiles_not_means():
    rows = [("telemetry", "2026-10", "critical", 60, 30, False),
            ("telemetry", "2026-10", "critical", 120, None, True),
            ("telemetry", "2026-10", "critical", 100000, None, True),   # long tail: a mean would say 9h
            ("telemetry", "2026-10", "warning", None, None, False),     # still open
            ("sre", "2026-09", "critical", 3600, None, False)]
    st = s.incident_stats(rows)
    crit = next(r for r in st if r["src"] == "telemetry" and r["severity"] == "critical")
    assert crit["n"] == 3 and crit["resolved"] == 3 and crit["ttr_p50"] == 120
    assert crit["acked"] == 1 and crit["tta_p50"] == 30
    assert abs(crit["repeat_share"] - 2 / 3) < 1e-9
    warn = next(r for r in st if r["severity"] == "warning")
    assert warn["resolved"] == 0 and warn["ttr_p50"] is None
    text = "\n".join(s.incident_report_lines(st))
    assert "p50" in text and "mean" not in text.split("\n", 1)[1].lower()


def test_fmt_dur():
    assert s.fmt_dur(None) == "—" and s.fmt_dur(42) == "42s" and s.fmt_dur(1020) == "17m"
    assert s.fmt_dur(3.2 * 3600) == "3.2h" and s.fmt_dur(3 * 86400) == "3.0d"


def test_incident_sql_never_averages():
    assert "avg(" not in s.INCIDENT_SQL.lower()
    assert "%(since)s" in s.INCIDENT_SQL
