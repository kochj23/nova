#!/usr/bin/env python3
"""7-category tests for nova_incident_escalation.classify — pure logic, no DB/Slack.

Categories:
  1. below threshold          -> not escalated
  2. chronic + recent         -> escalated
  3. single-day burst         -> not escalated (span < MIN_SPAN_DAYS)
  4. stale (last_open old)     -> not escalated
  5. meta-key exclusion        -> :incident_recurring / :incident_escalation ignored
  6. NULL recurrence_key        -> ignored, and reason string is well-formed
  7. ordering + malformed-row robustness
"""
import datetime
import importlib

m = importlib.import_module("nova_incident_escalation")

NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.timezone.utc)


def _row(key, pages, days_span, last_age_days, resolved=0, acked=0, open_now=0):
    return {
        "recurrence_key": key,
        "pages": pages,
        "resolved": resolved,
        "acked": acked,
        "open_now": open_now,
        "distinct_days": days_span,
        "first_open": NOW - datetime.timedelta(days=6),
        "last_open": NOW - datetime.timedelta(days=last_age_days),
    }


# 1 ──────────────────────────────────────────────────────────────────────────
def test_below_threshold_not_escalated():
    rows = [_row("host:cat", pages=m.ESCALATE_THRESHOLD - 1, days_span=4, last_age_days=0)]
    assert m.classify(rows, now=NOW) == []


# 2 ──────────────────────────────────────────────────────────────────────────
def test_chronic_recent_escalated():
    rows = [_row("host:cat", pages=17, days_span=5, last_age_days=0,
                 resolved=17, acked=17)]
    hits = m.classify(rows, now=NOW)
    assert len(hits) == 1 and hits[0]["recurrence_key"] == "host:cat"
    assert int(hits[0]["pages"]) == 17


# 3 ──────────────────────────────────────────────────────────────────────────
def test_single_day_burst_not_escalated():
    # lots of pages but all on one calendar day -> a burst, not chronic
    rows = [_row("host:cat", pages=50, days_span=1, last_age_days=0)]
    assert m.classify(rows, now=NOW) == []


# 4 ──────────────────────────────────────────────────────────────────────────
def test_stale_not_escalated():
    # crossed count + span, but nothing recent -> problem went away
    rows = [_row("host:cat", pages=20, days_span=4,
                 last_age_days=m.RECENT_DAYS + 2)]
    assert m.classify(rows, now=NOW) == []


# 5 ──────────────────────────────────────────────────────────────────────────
def test_meta_keys_excluded():
    rows = [
        _row("Mac-mini:incident_recurring", pages=30, days_span=5, last_age_days=0),
        _row("Mac-mini:incident_escalation", pages=30, days_span=5, last_age_days=0),
    ]
    assert m.classify(rows, now=NOW) == []


# 6 ──────────────────────────────────────────────────────────────────────────
def test_null_key_ignored_and_reason_wellformed():
    rows = [
        {"recurrence_key": None, "pages": 99, "distinct_days": 9,
         "last_open": NOW, "resolved": 0, "acked": 0, "open_now": 0},
        _row("host:cat", pages=9, days_span=3, last_age_days=0,
             resolved=8, acked=8, open_now=1),
    ]
    hits = m.classify(rows, now=NOW)
    assert [h["recurrence_key"] for h in hits] == ["host:cat"]
    reason = m._reason(hits[0])
    assert "paged 9x" in reason and "ack'd 8x" in reason and "still open" in reason


# 7 ──────────────────────────────────────────────────────────────────────────
def test_ordering_and_malformed_rows():
    rows = [
        _row("low:cat", pages=8, days_span=2, last_age_days=0),
        _row("high:cat", pages=45, days_span=6, last_age_days=0),
        {"garbage": True},                       # malformed -> skipped, no raise
        {"recurrence_key": "bad", "pages": "NaN",  # bad types -> skipped
         "distinct_days": None, "last_open": None},
    ]
    hits = m.classify(rows, now=NOW)
    assert [h["recurrence_key"] for h in hits] == ["high:cat", "low:cat"]
