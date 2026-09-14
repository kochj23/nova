#!/usr/bin/env python3
"""7-category tests for nova_cert_watch.classify — pure logic, no DB/Slack.

Categories:
  1. far-off cert (>14d)        -> no alert
  2. within 14d                 -> warning
  3. within 3d                  -> critical
  4. already expired            -> critical + expired flag
  5. NULL days_until_expiry     -> skipped, no crash
  6. boundary values (==14 warn, ==3 crit, ==0 critical+expired)
  7. soonest-first ordering + malformed-row robustness
"""
import datetime
import importlib

m = importlib.import_module("nova_cert_watch")

NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.timezone.utc)


def _row(ep, days):
    na = None if days is None else NOW + datetime.timedelta(days=days)
    return {"endpoint": ep, "host": f"h-{ep}", "port": 443,
            "subject": f"CN={ep}", "not_after": na, "days_until_expiry": days}


# 1 ──────────────────────────────────────────────────────────────────────────
def test_far_cert_no_alert():
    assert m.classify([_row("far", 90)]) == []


# 2 ──────────────────────────────────────────────────────────────────────────
def test_within_14d_warning():
    a = m.classify([_row("warn", 9.4)])
    assert len(a) == 1 and a[0]["level"] == "warning" and a[0]["expired"] is False


# 3 ──────────────────────────────────────────────────────────────────────────
def test_within_3d_critical():
    a = m.classify([_row("soon", 2)])
    assert a[0]["level"] == "critical" and a[0]["expired"] is False


# 4 ──────────────────────────────────────────────────────────────────────────
def test_expired_critical():
    a = m.classify([_row("dead", -5)])
    assert a[0]["level"] == "critical" and a[0]["expired"] is True


# 5 ──────────────────────────────────────────────────────────────────────────
def test_null_days_skipped():
    assert m.classify([_row("unknown", None)]) == []


# 6 ──────────────────────────────────────────────────────────────────────────
def test_boundaries():
    a = {x["endpoint"]: x for x in m.classify([_row("b14", 14), _row("b3", 3), _row("b0", 0)])}
    assert a["b14"]["level"] == "warning"
    assert a["b3"]["level"] == "critical"
    assert a["b0"]["level"] == "critical" and a["b0"]["expired"] is True


# 7 ──────────────────────────────────────────────────────────────────────────
def test_ordering_and_malformed_rows():
    rows = [_row("mid", 10), _row("worst", -3), _row("near", 1),
            {"endpoint": "junk"},          # no days key -> skipped
            {"days_until_expiry": "NaN"}]  # bad type -> skipped, no raise
    a = m.classify(rows)
    assert [x["endpoint"] for x in a] == ["worst", "near", "mid"]
