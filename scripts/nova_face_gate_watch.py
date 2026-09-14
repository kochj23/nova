#!/usr/bin/env python3
"""nova_face_gate_watch.py — catch false 'unknown person' alerts that slipped the gate.

WHY (2026-09-09): the face-rec vision gate (looks_like_person) fails OPEN when the
local vision backend hiccups, so cars/glare/hubcaps occasionally leak through as
"unknown person" alerts (the 9/3 Alley North car-wheel). The gate now retries, but a
full vision-backend outage can still leak. This watcher closes the loop: it re-checks
recent UNRESOLVED unknown candidates with the (now-reliable) gate, auto-resolves the
ones that are clearly NOT people, and pings if leaks cluster — the tell that vision is
down. It NEVER auto-clears a candidate the gate calls a real person; those stay for the
human. Alert-only + janitorial; it does not touch the camera pipeline.

Runs hourly via launchd (net.digitalnoise.nova-face-gate-watch). Stamps
telemetry.face_gate_runs so nova_freshness_monitor notices if THIS watcher stops.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOOKBACK_HOURS = 24
LEAK_ALERT_THRESHOLD = 1   # notify if >=1 leak auto-cleared this run


def _camera_from_crop(path):
    """Crop files are named unknown_<camera>_latest_<top>_<left>.jpg — pull <camera>."""
    import re
    if not path:
        return "?"
    m = re.match(r'(?:unknown|known)_(.+?)_latest', os.path.basename(path))
    return m.group(1) if m else "?"


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("NOVA_OPS_DSN", DSN), connect_timeout=8)


def _notify(msg, level="info", dedup="face-gate-watch"):
    try:
        import nova_notify
        nova_notify.notify(msg, level=level, category="face-gate",
                           source="nova_face_gate_watch.py", dedup_key=dedup)
    except Exception as e:  # noqa: BLE001 — notify must never crash the watcher
        print(f"[face-gate-watch] notify failed: {e}", flush=True)


def _stamp(conn, checked, leaked, unverifiable):
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS telemetry.face_gate_runs (
                id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
                checked int, leaked int, unverifiable int)""")
            cur.execute("INSERT INTO telemetry.face_gate_runs (checked,leaked,unverifiable) "
                        "VALUES (%s,%s,%s)", (checked, leaked, unverifiable))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[face-gate-watch] stamp failed: {e}", flush=True)


def check_candidates(gate=None, dry_run=False):
    """Re-verify recent unresolved unknown candidates. Returns (checked, leaked, unverifiable, leaks)."""
    if gate is None:
        import nova_face_recognition as fr
        gate = fr.looks_like_person
    conn = _conn()
    with conn.cursor() as cur:
        # No 'camera' column exists — the camera name lives in the crop filename
        # (e.g. unknown_alley_north_latest_470_1224.jpg). Derive it below.
        cur.execute(f"""SELECT id, face_crop_path, detected_at
                        FROM face_unknown_candidates
                        WHERE (resolved IS NULL OR resolved = 0)
                          AND detected_at >= (now() - interval '{LOOKBACK_HOURS} hours')::text
                          AND face_crop_path IS NOT NULL""")
        rows = cur.fetchall()
    checked = leaked = unverifiable = 0
    leaks = []
    for cid, crop, detected_at in rows:
        camera = _camera_from_crop(crop)
        if not crop or not os.path.exists(crop):
            unverifiable += 1
            continue
        try:
            is_person = gate(crop)
        except Exception as e:  # noqa: BLE001
            print(f"[face-gate-watch] gate error on {cid}: {e}", flush=True)
            unverifiable += 1
            continue
        checked += 1  # count only successful evaluations
        if not is_person:
            # A candidate the gate now calls NOT a person = a leak (gate was down when it fired).
            leaked += 1
            leaks.append((cid, camera, detected_at))
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute("UPDATE face_unknown_candidates SET resolved=1, "
                                "resolved_as=%s WHERE id=%s",
                                ("auto: non-person (face-gate-watch re-check — vehicle/glare/object)", cid))
                conn.commit()
    if not dry_run:
        _stamp(conn, checked, leaked, unverifiable)
    conn.close()
    return checked, leaked, unverifiable, leaks


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    checked, leaked, unverifiable, leaks = check_candidates(dry_run=dry)
    print(f"[face-gate-watch] checked={checked} leaked={leaked} unverifiable={unverifiable} dry_run={dry}", flush=True)
    if leaked >= LEAK_ALERT_THRESHOLD and not dry:
        cams = ", ".join(sorted({c for _, c, _ in leaks}))
        _notify(f"Auto-cleared {leaked} false 'unknown person' alert(s) that slipped the vision "
                f"gate (vehicles/glare) at: {cams}. If this recurs, the local vision backend is "
                f"down and real gating is degraded.", level="warning",
                dedup=f"face-gate-leak-{time.strftime('%Y%m%d%H')}")
    # log to claude_actions (best-effort)
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO claude_actions (session_id,action_type,target,description,outcome,rationale) "
                        "VALUES ('nova-face-gate-watch','monitor','face_unknown_candidates',"
                        "%s,%s,'catch car/glare false-positives that slip the fail-open vision gate')",
                        (f"re-verified {checked} recent unknown candidates",
                         f"auto-cleared {leaked} non-person leaks, {unverifiable} unverifiable"))
        conn.commit(); conn.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
