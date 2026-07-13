#!/usr/bin/env python3
"""nova_bb_escalator.py — shared escalation-tier engine for Big Brother.

Extracted from nova_big_brother.py (#511) as the first, behavior-preserving step of
splitting the monolith. This is the SHARED escalator: the tier state machine
(info -> warning -> critical), per-issue cooldowns, auto-bump, suppression counting,
and resolution. Both the future service_monitor and system_monitor route their alerts
through here so escalation state is unified and neither owns it.

Pure logic — depends only on nova_logger + stdlib. Its escalation state is private and
guarded by its own lock (previously it shared big_brother's module lock; the state is
only ever touched through should_notify()/_resolve_escalation(), so a dedicated lock is
equivalent and cleaner).
"""
import threading
import time

from nova_logger import log, LOG_WARN

# Per-severity escalation policy. bump_after=None means "no further auto-escalation".
ESCALATION_RULES = {
    "info":     {"initial_cooldown": 300,  "max_notifications": 3,  "bump_after": 3600},   # 5min cooldown, bump to warning after 1h
    "warning":  {"initial_cooldown": 600,  "max_notifications": 5,  "bump_after": 7200},   # 10min cooldown, bump to critical after 2h
    "critical": {"initial_cooldown": 300,  "max_notifications": 10, "bump_after": None},   # 5min cooldown, no further bump
}

_SEVERITY_ORDER = ["info", "warning", "critical"]

_lock = threading.Lock()
_escalations: dict = {}  # key: issue_id -> {severity, first_seen, last_notified, notify_count, suppressed_count}


def _next_severity(current: str) -> str:
    """Bump severity one level up. Returns same if already at max."""
    idx = _SEVERITY_ORDER.index(current) if current in _SEVERITY_ORDER else 0
    if idx < len(_SEVERITY_ORDER) - 1:
        return _SEVERITY_ORDER[idx + 1]
    return current


def should_notify(issue_id: str, severity: str) -> tuple:
    """Escalation-aware notification gate.

    Returns (should_send: bool, modified_message_suffix: str).
    Tracks state per issue_id. Handles cooldowns, auto-bumps, and resolution.
    """
    now = time.time()

    with _lock:
        if issue_id not in _escalations:
            # First detection — notify immediately
            _escalations[issue_id] = {
                "severity": severity,
                "first_seen": now,
                "last_notified": now,
                "notify_count": 1,
                "suppressed_count": 0,
            }
            return (True, "")

        state = _escalations[issue_id]
        rules = ESCALATION_RULES.get(state["severity"], ESCALATION_RULES["warning"])

        # Check if severity should auto-bump
        if rules["bump_after"] is not None:
            if now - state["first_seen"] > rules["bump_after"]:
                old_sev = state["severity"]
                state["severity"] = _next_severity(old_sev)
                log(f"[escalation] {issue_id} bumped {old_sev} -> {state['severity']} "
                    f"(ongoing {int((now - state['first_seen']) / 60)}m)",
                    level=LOG_WARN, source="big-brother")
                # Bump triggers immediate notification
                state["last_notified"] = now
                state["notify_count"] += 1
                duration_m = int((now - state["first_seen"]) / 60)
                suffix = f" [ESCALATED to {state['severity']} after {duration_m}m]"
                return (True, suffix)

        # Check cooldown
        cooldown = rules["initial_cooldown"]
        if now - state["last_notified"] < cooldown:
            state["suppressed_count"] += 1
            return (False, "")

        # Check max notifications
        if state["notify_count"] >= rules["max_notifications"]:
            state["suppressed_count"] += 1
            return (False, "")

        # Cooldown expired and under max — notify with context
        state["last_notified"] = now
        state["notify_count"] += 1
        duration_m = int((now - state["first_seen"]) / 60)
        suppressed = state["suppressed_count"]
        suffix = f" (ongoing {duration_m}m"
        if suppressed > 0:
            suffix += f", suppressed {suppressed} alerts"
        suffix += ")"
        return (True, suffix)


def _resolve_escalation(issue_id: str) -> tuple:
    """Mark an issue as resolved. Returns (was_tracked: bool, message_suffix: str).

    Call this when a previously-detected issue clears.
    """
    with _lock:
        if issue_id not in _escalations:
            return (False, "")
        state = _escalations.pop(issue_id)
        duration_m = int((time.time() - state["first_seen"]) / 60)
        suffix = f" RESOLVED after {duration_m}m"
        return (True, suffix)


def active_count() -> int:
    """Number of currently-tracked (unresolved) escalations — for dashboard/stats."""
    with _lock:
        return len(_escalations)


def active_keys() -> list:
    """Snapshot of currently-tracked escalation issue_ids — for reconciliation/cleanup."""
    with _lock:
        return list(_escalations.keys())


if __name__ == "__main__":
    # Self-check: first-seen notifies; immediate repeat is cooled down; resolve clears.
    ok, _ = should_notify("t:demo", "info")
    assert ok, "first detection must notify"
    ok2, _ = should_notify("t:demo", "info")
    assert not ok2, "immediate repeat must be suppressed by cooldown"
    assert active_count() == 1, "one active escalation expected"
    tracked, sfx = _resolve_escalation("t:demo")
    assert tracked and "RESOLVED" in sfx, "resolve must report tracked+RESOLVED"
    assert active_count() == 0, "resolved escalation must clear"
    print("nova_bb_escalator self-check: PASS")
