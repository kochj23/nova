#!/opt/homebrew/bin/python3
"""
nova_maintenance.py — maintenance-window gate (PG-backed).

When a window is ACTIVE, nova_notifier mutes security-category Slack routing so an
authorized pentest (e.g. Strix) doesn't flood the channels. Crucially, the event
rows are STILL written to telemetry.events (nova_notify never stops logging) — only
the outbound Slack post is suppressed — so "did Wazuh detect it?" purple-team scoring
stays fully intact. Blanket window: all sources, for the window's duration.

  nova_maintenance.py start --minutes 120 --reason "strix run 1"
  nova_maintenance.py status
  nova_maintenance.py stop
  nova_maintenance.py check     # exit 0 if active, 1 if not (for shell gating)

State lives in service_config(service='nova', key='maintenance_mode') per the
all-state-in-PG rule. Every read fails OPEN (errors => "not in maintenance") so a
gate problem can never mute a real alert or crash the notifier.
"""
import sys, json, argparse
from datetime import datetime, timedelta, timezone

_DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
SERVICE, KEY = "nova", "maintenance_mode"

# Categories whose Slack routing is muted while a window is active (event still logged).
# These are DETECTION-side categories — the raw Wazuh/IDS flood we want silenced.
# Deliberately NOT muted: 'probe' (Nova's own health checks) and 'strix' (the curated
# run-status stream), so you still see the pentest's own progress + the purple-team result.
SECURITY_CATEGORIES = frozenset({
    "security", "wazuh", "intrusion", "threat", "attack",
    "ids", "vuln", "exploit",
})


def _conn():
    import psycopg2
    return psycopg2.connect(_DSN, connect_timeout=5)


def get_state() -> dict:
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (SERVICE, KEY))
            r = cur.fetchone()
        if not r:
            return {"active": False}
        return r[0] if isinstance(r[0], dict) else json.loads(r[0])
    except Exception:
        return {"active": False}   # fail open


def _window_active(s: dict, now: datetime) -> bool:
    """Pure predicate: is this state a live window at `now`? (unit-tested below)"""
    if not s.get("active"):
        return False
    until = s.get("until")
    if until:
        try:
            if now > datetime.fromisoformat(until):
                return False   # auto-expired
        except Exception:
            pass               # unparseable end => treat as open-ended, stay active
    return True


def is_active() -> bool:
    return _window_active(get_state(), datetime.now(timezone.utc))


def _set(v: dict):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO service_config(service, key, value, updated_by) "
            "VALUES(%s, %s, %s::jsonb, 'nova_maintenance') "
            "ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, "
            "updated_at=now(), updated_by='nova_maintenance'",
            (SERVICE, KEY, json.dumps(v)))
        c.commit()


def start(minutes: int, reason: str):
    now = datetime.now(timezone.utc)
    until = (now + timedelta(minutes=minutes)).isoformat()
    _set({"active": True, "until": until, "reason": reason, "started": now.isoformat()})
    print(f"MAINTENANCE ON until {until} ({minutes}m) — {reason}")


def stop():
    _set({"active": False, "stopped": datetime.now(timezone.utc).isoformat()})
    print("MAINTENANCE OFF")


def _selftest():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert _window_active({"active": True, "until": "2026-01-01T13:00:00+00:00"}, now) is True   # future end
    assert _window_active({"active": True, "until": "2026-01-01T11:00:00+00:00"}, now) is False  # expired
    assert _window_active({"active": False}, now) is False                                       # off
    assert _window_active({"active": True}, now) is True                                         # open-ended
    assert _window_active({"active": True, "until": "garbage"}, now) is True                     # unparseable => stay on
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("start"); p.add_argument("--minutes", type=int, default=120); p.add_argument("--reason", default="maintenance")
    sub.add_parser("stop"); sub.add_parser("status"); sub.add_parser("check"); sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "start": start(a.minutes, a.reason)
    elif a.cmd == "stop": stop()
    elif a.cmd == "check": sys.exit(0 if is_active() else 1)
    elif a.cmd == "selftest": _selftest()
    else: print(json.dumps(get_state(), indent=2), "\nis_active:", is_active())
