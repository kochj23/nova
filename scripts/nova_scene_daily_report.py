#!/usr/bin/env python3
"""
nova_scene_daily_report.py — Daily digest of HomeKit scene activations.

Sourced from home_scene_activations, populated by per-scene Shortcuts
automations posting to nova_homekit_receiver.py's /homekit/scene endpoint
(HomeKit itself doesn't expose scene-activation as an observable event via
its public API, so this is the only reliable capture path).

Runs daily via scheduler. Writes to shared_observations (feeds the shared
"one voice" context every ops-article script already reads) and posts to
Slack via nova_notify. Grafana: dashboard uid home-scenes.

Written by Jordan Koch (via Claude).
"""

import sys
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/scene_daily_report.log"


def log(msg):
    line = f"[scene_report {date.today().isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT scene_name, count(*) AS n
        FROM home_scene_activations
        WHERE ts > now() - interval '1 day'
        GROUP BY scene_name
        ORDER BY n DESC
    """)
    today = cur.fetchall()

    if not today:
        log("No scene activations in the last 24h")
        # Still worth a quiet-day note -- silence could mean "nothing happened"
        # or "the Shortcuts wiring broke," and those look identical otherwise.
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova_scene_daily_report', 'home', 'scene-activity',
                    'No HomeKit scene activations logged in the last 24h.', 'info', '{}')
        """)
        conn.commit()
        try:
            notify("Home Scenes Report", body="No scene activations logged in the last 24h.",
                   level="info", category="home", dedup_key="scene-daily-report")
        except Exception as e:
            log(f"Notify failed: {e}")
        cur.close(); conn.close()
        return

    total = sum(r["n"] for r in today)
    summary = ", ".join(f"{r['scene_name']} x{r['n']}" for r in today)

    cur.execute("""
        INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
        VALUES ('nova_scene_daily_report', 'home', 'scene-activity', %s, 'info', %s)
    """, (
        f"{total} scene activation(s) in the last 24h: {summary}",
        psycopg2.extras.Json({"scenes": {r["scene_name"]: r["n"] for r in today}}),
    ))
    conn.commit()

    log(f"total={total} scenes={len(today)}")

    lines = [f"{total} activation(s) across {len(today)} scene(s):"]
    lines += [f"  {r['scene_name']}: {r['n']}" for r in today]

    try:
        notify(
            f"Home Scenes Report ({date.today().isoformat()})",
            body="\n".join(lines),
            level="info",
            category="home",
            dedup_key="scene-daily-report",
        )
    except Exception as e:
        log(f"Notify failed: {e}")

    cur.close()
    conn.close()
    log("Report complete")


if __name__ == "__main__":
    run()
