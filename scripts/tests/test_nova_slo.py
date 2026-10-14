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
