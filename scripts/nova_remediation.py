#!/usr/bin/env python3
"""
nova_remediation.py — the runbook / auto-remediation engine for Nova's incident stack.

Detection (telemetry.events -> nova_correlator -> telemetry.incidents) is solved.
NOTHING acts on it. This module is the action layer: given an OPEN incident, it
matches a RUNBOOK (an ordered list of named, allowlisted actions) and either
PROPOSES the fix (default) or — once a human flips the master switch — EXECUTES
only the low-blast-radius ('safe') steps. Anything 'impactful' (reboot/delete/
failover) is NEVER auto-run; it is proposed and requires explicit approve().

SAFETY DESIGN (non-negotiable):
  * REMEDIATION_ENABLED = False by default. While False the engine ONLY PROPOSES
    (emits a notification describing the fix) and EXECUTES NOTHING.
  * Allowlist ONLY. Actions map a NAME -> an exact argv list. There is never any
    shell, never any string interpolation of incident data into a command. If a
    name isn't in ACTIONS, it cannot run. shell=False, always.
  * Two tiers. 'safe' = idempotent, low blast radius (kickstart a launchd service,
    clear a cache, kill+respawn one stuck pid). 'impactful' = reboot/delete/
    failover — proposed + approval-gated, never auto-executed.
  * Cooldown. The same action for the same incident is not proposed/executed twice
    inside COOLDOWN_S (30 min).
  * dry_run is honored everywhere; the impactful reboot argv is deliberately a
    no-op echo so it CANNOT fire even if mis-approved.

All alerts go through nova_notify.notify (never hardcoded Slack). Nothing here
raises into a caller that doesn't want it to — the public API swallows and logs.

  API:
    propose_for_incident(conn, incident_id, dry_run=False) -> dict
    approve(conn, remediation_id, dry_run=False)           -> dict
    execute_action(name, dry_run=False)                    -> dict
    ensure_schema(conn)
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# ── MASTER SAFETY SWITCH ──────────────────────────────────────────────────────
# False -> engine PROPOSES only and executes NOTHING (not even 'safe' steps).
# The lead flips this to True after reviewing the runbooks. Even when True, only
# 'safe' steps auto-execute; 'impactful' steps always require approve().
REMEDIATION_ENABLED = False

# Don't re-propose / re-run the same action for the same incident within this.
COOLDOWN_S = 1800  # 30 minutes

SAFE = "safe"
IMPACTFUL = "impactful"

# ── ALLOWLIST ─────────────────────────────────────────────────────────────────
# name -> {tier, argv, desc}. argv is the EXACT, fully-formed command. No shell,
# no interpolation. Anything not in here cannot be executed, full stop.
#
# The impactful reboot is intentionally a HARMLESS echo, not a real `shutdown`/
# `reboot`, so that even an erroneous approval cannot take a host down. Swapping
# in a real reboot argv is a deliberate, reviewed change.
ACTIONS = {
    "restart_ollama": {
        "tier": SAFE,
        "argv": ["launchctl", "kickstart", "-k", "gui/501/net.digitalnoise.llama-server"],
        "desc": "Kickstart (restart) the local llama/ollama launchd service to clear a wedged GPU inference server.",
    },
    "restart_notifier": {
        "tier": SAFE,
        "argv": ["launchctl", "kickstart", "-k", "gui/501/net.digitalnoise.nova-notifier"],
        "desc": "Restart the nova-notifier daemon if the event drain loop has stalled.",
    },
    "clear_ollama_cache": {
        "tier": SAFE,
        # Idempotent: removing a runner-temp dir; -f so absence isn't an error.
        "argv": ["/bin/rm", "-rf", "/tmp/ollama-runners"],
        "desc": "Clear stale Ollama runner temp files (idempotent cache clear).",
    },
    "restart_cloudflared": {
        "tier": SAFE,
        # Tunnel now runs HA on .2 + .10 (systemd, Restart=always). Restart the
        # primary connector (.2); .10 keeps serving during the bounce.
        "argv": ["ssh", "-o", "BatchMode=yes", "kochj@192.168.1.2",
                 "sudo", "systemctl", "restart", "cloudflared"],
        "desc": "restart the .2 Cloudflare connector (.10 stays up — HA)",
    },
    "reboot_host": {
        "tier": IMPACTFUL,
        # DELIBERATELY a no-op. A real reboot would be ["/sbin/shutdown","-r","now"].
        # Gated behind approve() AND this harmless argv so it can never take a host
        # down by accident. Replacing this is a reviewed, intentional change.
        "argv": ["/bin/echo", "[gated] reboot_host requested — no-op placeholder; replace with real argv only after review"],
        "desc": "REBOOT the host (impactful, approval-gated). Placeholder no-op until reviewed.",
    },
}

# ── RUNBOOK REGISTRY ──────────────────────────────────────────────────────────
# Match an OPEN incident to an ordered list of action names. Keyed by
# (host, root_category). Steps run in order; safe first, impactful last (gated).
RUNBOOKS = {
    ("Office-M4-2", "gpu"): ["restart_ollama", "reboot_host"],
    ("Office-M4-2", "tunnel"): ["restart_cloudflared"],  # Cloudflare tunnel down -> reconnect
}


# ── schema ────────────────────────────────────────────────────────────────────
def ensure_schema(conn):
    """Idempotent DDL for telemetry.remediations."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telemetry.remediations (
                id            bigserial PRIMARY KEY,
                incident_id   bigint NOT NULL,
                action        text   NOT NULL,
                tier          text   NOT NULL,
                status        text   NOT NULL DEFAULT 'proposed',
                argv          text   NOT NULL,
                requested_at  timestamptz NOT NULL DEFAULT now(),
                executed_at   timestamptz,
                result        text,
                dry_run       boolean NOT NULL DEFAULT false
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS remediations_incident_action
            ON telemetry.remediations (incident_id, action, requested_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS remediations_status
            ON telemetry.remediations (status) WHERE status IN ('proposed','approved')
        """)
    try:
        conn.commit()
    except Exception:
        pass


# ── helpers ───────────────────────────────────────────────────────────────────
def _tuple_cur(conn):
    """Plain-tuple cursor regardless of the connection's default factory
    (the notifier daemon passes a RealDictCursor connection)."""
    import psycopg2.extensions
    return conn.cursor(cursor_factory=psycopg2.extensions.cursor)


def _recently_acted(conn, incident_id, action):
    """True if this action was proposed/executed for this incident within COOLDOWN_S."""
    cur = _tuple_cur(conn)
    cur.execute(
        "SELECT 1 FROM telemetry.remediations "
        "WHERE incident_id=%s AND action=%s "
        "AND requested_at > now() - make_interval(secs => %s) "
        "AND status IN ('proposed','approved','executed') LIMIT 1",
        (incident_id, action, COOLDOWN_S))
    return cur.fetchone() is not None


def _incident_context(conn, incident_id):
    """(host, root_category, title) for an OPEN incident, or None."""
    cur = _tuple_cur(conn)
    cur.execute(
        "SELECT i.host, i.title, "
        "(SELECT category FROM telemetry.events WHERE id=i.root_event) AS root_cat "
        "FROM telemetry.incidents i WHERE id=%s AND status='open'",
        (incident_id,))
    row = cur.fetchone()
    if not row:
        return None
    host, title, root_cat = row
    return {"host": host, "title": title, "root_cat": root_cat}


def _lookup_runbook(host, root_cat):
    """Return the ordered list of action names for (host, root_cat), or []."""
    return RUNBOOKS.get((host, root_cat), [])


def _record(conn, incident_id, action, tier, argv, status, dry_run, result=None):
    """Insert a remediation row; returns its id. Best-effort."""
    cur = _tuple_cur(conn)
    cur.execute(
        "INSERT INTO telemetry.remediations "
        "(incident_id, action, tier, status, argv, dry_run, result, "
        " executed_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s IN ('executed','failed') THEN now() ELSE NULL END) "
        "RETURNING id",
        (incident_id, action, tier, status, " ".join(argv), dry_run, result, status))
    rid = cur.fetchone()[0]
    try:
        conn.commit()
    except Exception:
        pass
    return rid


def _update(conn, remediation_id, status, result=None):
    cur = _tuple_cur(conn)
    cur.execute(
        "UPDATE telemetry.remediations SET status=%s, result=%s, "
        "executed_at = CASE WHEN %s IN ('executed','failed') THEN now() ELSE executed_at END "
        "WHERE id=%s",
        (status, result, status, remediation_id))
    try:
        conn.commit()
    except Exception:
        pass


# ── execution (allowlist only) ────────────────────────────────────────────────
def execute_action(name, dry_run=False):
    """Run an ALLOWLISTED action's exact argv. Never shell, never interpolation.

    Returns {ok, name, argv, dry_run, returncode, stdout, stderr, error}.
    Defensive: any failure is captured into the dict, never raised.
    """
    spec = ACTIONS.get(name)
    if not spec:
        return {"ok": False, "name": name, "argv": None, "dry_run": dry_run,
                "error": f"action '{name}' not in allowlist"}
    argv = spec["argv"]
    if dry_run:
        return {"ok": True, "name": name, "argv": argv, "dry_run": True,
                "returncode": None, "stdout": "", "stderr": "",
                "error": None, "note": "dry_run: not executed"}
    try:
        # shell=False (list argv), bounded time, no inherited stdin.
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=60, check=False, shell=False)
        return {"ok": p.returncode == 0, "name": name, "argv": argv, "dry_run": False,
                "returncode": p.returncode,
                "stdout": (p.stdout or "")[:2000], "stderr": (p.stderr or "")[:2000],
                "error": None}
    except Exception as e:
        return {"ok": False, "name": name, "argv": argv, "dry_run": False,
                "returncode": None, "stdout": "", "stderr": "", "error": str(e)}


def _fmt_result(res):
    if res.get("error"):
        return f"error: {res['error']}"
    if res.get("note"):
        return res["note"]
    rc = res.get("returncode")
    out = (res.get("stdout") or "").strip()
    err = (res.get("stderr") or "").strip()
    parts = [f"rc={rc}"]
    if out:
        parts.append(f"out={out[:300]}")
    if err:
        parts.append(f"err={err[:300]}")
    return " ".join(parts)


# ── public API ────────────────────────────────────────────────────────────────
def propose_for_incident(conn, incident_id, dry_run=False):
    """Look up the runbook for an OPEN incident and record/propose its steps.

    Behavior per step:
      * SAFE     : if REMEDIATION_ENABLED -> execute (honoring dry_run) and record
                   'executed'/'failed'; else record 'proposed' and notify.
      * IMPACTFUL: NEVER auto-executed. Record 'proposed' and emit a CRITICAL
                   notification asking for approval.
    Cooldown: an action acted on for this incident within COOLDOWN_S is skipped.

    Returns {incident_id, runbook, steps:[{action,tier,status,...}]}. Never raises.
    """
    out = {"incident_id": incident_id, "runbook": None, "steps": []}
    try:
        ensure_schema(conn)
        ctx = _incident_context(conn, incident_id)
        if not ctx:
            out["error"] = "no open incident / not found"
            return out
        runbook = _lookup_runbook(ctx["host"], ctx["root_cat"])
        out["runbook"] = runbook
        if not runbook:
            out["error"] = f"no runbook for host={ctx['host']} root_cat={ctx['root_cat']}"
            return out

        for action in runbook:
            spec = ACTIONS.get(action)
            if not spec:
                out["steps"].append({"action": action, "status": "skipped",
                                     "reason": "not in allowlist"})
                continue
            tier, argv, desc = spec["tier"], spec["argv"], spec["desc"]

            if _recently_acted(conn, incident_id, action):
                out["steps"].append({"action": action, "tier": tier,
                                     "status": "skipped", "reason": "cooldown"})
                continue

            # IMPACTFUL — always propose + approval-gate. Never auto-execute.
            if tier == IMPACTFUL:
                rid = _record(conn, incident_id, action, tier, argv,
                              status="proposed", dry_run=dry_run)
                nova_notify.notify(
                    f"Proposed fix for incident #{incident_id}: {desc}",
                    body=(f"IMPACTFUL action `{action}` is approval-gated and will "
                          f"NOT auto-run. Approve remediation #{rid} to execute.\n"
                          f"argv: {' '.join(argv)}"),
                    level="critical", category="remediation",
                    source="nova_remediation.py",
                    dedup_key=f"remediation-{incident_id}-{action}",
                    meta={"incident_id": incident_id, "remediation_id": rid,
                          "action": action, "tier": tier})
                out["steps"].append({"action": action, "tier": tier,
                                     "status": "proposed", "remediation_id": rid})
                continue

            # SAFE — execute only if the master switch is on; else propose.
            if REMEDIATION_ENABLED:
                res = execute_action(action, dry_run=dry_run)
                status = "executed" if res.get("ok") else "failed"
                rid = _record(conn, incident_id, action, tier, argv,
                              status=status, dry_run=dry_run,
                              result=_fmt_result(res))
                nova_notify.notify(
                    f"Auto-remediation {status} for incident #{incident_id}: {desc}",
                    body=(f"SAFE action `{action}` {status}"
                          f"{' (dry_run)' if dry_run else ''}.\n"
                          f"argv: {' '.join(argv)}\nresult: {_fmt_result(res)}"),
                    level="warning" if status == "executed" else "critical",
                    category="remediation", source="nova_remediation.py",
                    dedup_key=f"remediation-{incident_id}-{action}",
                    meta={"incident_id": incident_id, "remediation_id": rid,
                          "action": action, "tier": tier, "status": status})
                out["steps"].append({"action": action, "tier": tier,
                                     "status": status, "remediation_id": rid,
                                     "result": _fmt_result(res)})
            else:
                rid = _record(conn, incident_id, action, tier, argv,
                              status="proposed", dry_run=dry_run)
                nova_notify.notify(
                    f"Proposed fix for incident #{incident_id}: {desc}",
                    body=(f"SAFE action `{action}` would run, but REMEDIATION_ENABLED "
                          f"is False — proposing only, executed nothing.\n"
                          f"argv: {' '.join(argv)}"),
                    level="warning", category="remediation",
                    source="nova_remediation.py",
                    dedup_key=f"remediation-{incident_id}-{action}",
                    meta={"incident_id": incident_id, "remediation_id": rid,
                          "action": action, "tier": tier})
                out["steps"].append({"action": action, "tier": tier,
                                     "status": "proposed", "remediation_id": rid})
        return out
    except Exception as e:
        out["error"] = f"propose_for_incident failed: {e}"
        return out


def approve(conn, remediation_id, dry_run=False):
    """Execute a previously-proposed remediation (the only path for IMPACTFUL).

    Re-validates against the allowlist and only acts on a row still 'proposed'/
    'approved'. Records the outcome and notifies. Never raises.
    """
    out = {"remediation_id": remediation_id}
    try:
        ensure_schema(conn)
        cur = _tuple_cur(conn)
        cur.execute(
            "SELECT incident_id, action, tier, status FROM telemetry.remediations WHERE id=%s",
            (remediation_id,))
        row = cur.fetchone()
        if not row:
            out["error"] = "remediation not found"
            return out
        incident_id, action, tier, status = row
        out.update({"incident_id": incident_id, "action": action, "tier": tier})

        if status not in ("proposed", "approved"):
            out["error"] = f"remediation is '{status}', not approvable"
            return out
        if action not in ACTIONS:
            _update(conn, remediation_id, "failed", "action no longer in allowlist")
            out["error"] = "action not in allowlist"
            return out

        _update(conn, remediation_id, "approved")
        res = execute_action(action, dry_run=dry_run)
        final = "executed" if res.get("ok") else "failed"
        _update(conn, remediation_id, final, _fmt_result(res))
        out.update({"status": final, "result": _fmt_result(res)})

        nova_notify.notify(
            f"Approved remediation #{remediation_id} {final} for incident #{incident_id}",
            body=(f"{tier.upper()} action `{action}` {final}"
                  f"{' (dry_run)' if dry_run else ''}.\nresult: {_fmt_result(res)}"),
            level="warning" if final == "executed" else "critical",
            category="remediation", source="nova_remediation.py",
            dedup_key=f"remediation-approve-{remediation_id}",
            meta={"incident_id": incident_id, "remediation_id": remediation_id,
                  "action": action, "tier": tier, "status": final})
        return out
    except Exception as e:
        out["error"] = f"approve failed: {e}"
        return out


# ── smoke / dry run ───────────────────────────────────────────────────────────
def _smoke():
    """Self-contained dry run against a SYNTHETIC GPU incident.

    Confirms propose-only mode: SAFE steps are PROPOSED (not executed) while
    REMEDIATION_ENABLED is False, IMPACTFUL steps are proposed + approval-gated,
    and NOTHING is executed. Cleans up all test rows afterward.
    """
    import psycopg2
    assert REMEDIATION_ENABLED is False, "smoke must run in propose-only mode"
    conn = psycopg2.connect(DSN, connect_timeout=5)
    ensure_schema(conn)
    cur = _tuple_cur(conn)

    # Synthetic root event (gpu) + open incident on Office-M4-2.
    cur.execute(
        "INSERT INTO telemetry.events (source, level, category, title, body, meta) "
        "VALUES ('nova_remediation.py','critical','gpu',"
        "'[SMOKE] GPU wedged on Office-M4-2','synthetic test event',"
        "'{\"host\":\"Office-M4-2\",\"smoke\":true}'::jsonb) RETURNING id")
    ev_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO telemetry.incidents (status, severity, host, title, root_event, member_count) "
        "VALUES ('open','critical','Office-M4-2','[SMOKE] GPU wedge',%s,1) RETURNING id",
        (ev_id,))
    inc_id = cur.fetchone()[0]
    conn.commit()
    print(f"smoke: synthetic incident #{inc_id} (root event #{ev_id})")

    result = propose_for_incident(conn, inc_id, dry_run=True)
    print("smoke: propose_for_incident ->")
    print(json.dumps(result, indent=2, default=str))

    # Assertions: propose-only, nothing executed.
    statuses = {s["action"]: s["status"] for s in result["steps"]}
    assert result["runbook"] == ["restart_ollama", "reboot_host"], result.get("runbook")
    assert statuses.get("restart_ollama") == "proposed", statuses
    assert statuses.get("reboot_host") == "proposed", statuses
    cur.execute("SELECT action, tier, status, dry_run FROM telemetry.remediations "
                "WHERE incident_id=%s ORDER BY id", (inc_id,))
    rows = cur.fetchall()
    print("smoke: remediation rows ->", rows)
    assert all(r[2] == "proposed" for r in rows), "nothing should be executed"
    assert not any(r[2] == "executed" for r in rows), "NOTHING may have executed"

    # Cooldown check: re-proposing skips both.
    again = propose_for_incident(conn, inc_id, dry_run=True)
    again_statuses = {s["action"]: s["status"] for s in again["steps"]}
    print("smoke: re-propose (cooldown) ->", again_statuses)
    assert all(v == "skipped" for v in again_statuses.values()), again_statuses

    # execute_action allowlist guard.
    bad = execute_action("rm_rf_root", dry_run=True)
    print("smoke: non-allowlisted action ->", bad)
    assert bad["ok"] is False and "allowlist" in bad["error"]

    # Clean up ALL test data.
    cur.execute("DELETE FROM telemetry.remediations WHERE incident_id=%s", (inc_id,))
    cur.execute("DELETE FROM telemetry.incidents WHERE id=%s", (inc_id,))
    cur.execute("DELETE FROM telemetry.events WHERE id=%s", (ev_id,))
    conn.commit()
    conn.close()
    print("smoke: cleaned up test data. PASS — proposed only, executed nothing.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        _smoke()
    else:
        print("nova_remediation.py — runbook engine. "
              "REMEDIATION_ENABLED =", REMEDIATION_ENABLED)
        print("Run with --smoke for a synthetic propose-only dry run.")
