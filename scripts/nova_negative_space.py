#!/usr/bin/env python3
"""nova_negative_space.py — alert on correlations that SHOULD have happened and didn't.

Every alerting system here has the same disease: it fires on the presence of something
unknown. That is useless at this scale — 6,100 distinct BLE devices a week walk past the
house and every one of them is a neighbour. Presence of an unknown is the base rate.

The signal is ABSENCE. Specifically, the absence of a correlation that normally holds:

  ARRIVAL WITH NO DEVICE   a vehicle or a person detected at the property with no known
                           phone appearing alongside it -> somebody who isn't us is here.
  FACE WITH NO CARRIER     a camera sees a person while no known device is present at all.
  DEVICE WITH NO HUMAN     a person's devices are home but no camera, mmWave or face has
                           seen anybody for days -> a phone left behind, or a welfare issue.
  SENSOR WENT QUIET        a signal that reports continuously stopped reporting. A sensor
                           that has produced nothing for hours is far more likely broken
                           than the world having gone still — the same 'zero work is not
                           success' rule the ingest and health checks now use.

Deliberately conservative. Each rule needs a sustained window, not a single sample, because
the cost of crying wolf is that the one real alert gets ignored.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def log(m):
    print(f"[negative-space] {m}", flush=True)


def q(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


def main(alert):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    findings = []

    # ── 1. Arrival with no known device ──────────────────────────────────────
    # A vehicle or camera-detected person in the last 30 min, while no owned device has been
    # seen for 30 min. Someone arrived and nothing we recognise arrived with them.
    arrivals = q(cur, """
        SELECT count(*) FROM telemetry.presence
        WHERE ts > now() - interval '30 minutes'
          AND method IN ('vehicle_vision','camera_vision')
          AND room IN ('driveway','carport','front_yard','entry','garage')""")[0][0]
    owned_seen = q(cur, """
        SELECT count(*) FROM telemetry.bluetooth b
        JOIN telemetry.device_owner o ON lower(b.device_mac) = o.mac
        WHERE b.ts > now() - interval '30 minutes'""")[0][0]
    wifi_seen = q(cur, """
        SELECT count(*) FROM telemetry.presence
        WHERE ts > now() - interval '30 minutes' AND method='wifi_rssi'""")[0][0]
    if arrivals >= 3 and owned_seen == 0 and wifi_seen == 0:
        findings.append(("arrival_no_device",
                         f"{arrivals} arrival detections at the property in 30 min with NO known "
                         f"device present (no owned BLE, no WiFi presence). Someone is here who "
                         f"isn't carrying anything we recognise."))

    # ── 2. Devices home, nobody seen ─────────────────────────────────────────
    # Someone's phone has been present for a day but no camera/mmWave/face has seen a human.
    stale = q(cur, """
        SELECT o.person, max(b.ts)
        FROM telemetry.bluetooth b
        JOIN telemetry.device_owner o ON lower(b.device_mac) = o.mac
        WHERE b.ts > now() - interval '36 hours'
        GROUP BY 1 HAVING max(b.ts) > now() - interval '2 hours'""")
    humans = q(cur, """
        SELECT count(*) FROM telemetry.presence
        WHERE ts > now() - interval '36 hours'
          AND method IN ('camera_vision','mmwave')""")[0][0]
    if stale and humans == 0:
        who = ", ".join(p for p, _ in stale)
        findings.append(("device_no_human",
                         f"Devices belonging to {who} are present, but no camera or mmWave has "
                         f"detected a person in 36 hours. Either a phone was left behind, or the "
                         f"person sensors have failed."))

    # ── 3. A sensor went quiet ───────────────────────────────────────────────
    # Zero work is not success — the rule that caught the dead search-ingest today.
    quiet = q(cur, """
        SELECT method, max(ts), now() - max(ts) AS gap
        FROM telemetry.presence
        WHERE ts > now() - interval '7 days'
        GROUP BY 1
        HAVING now() - max(ts) > interval '6 hours'
        ORDER BY 3 DESC""")
    for method, last, gap in quiet:
        findings.append(("sensor_quiet",
                         f"Presence method '{method}' has reported nothing for {str(gap).split('.')[0]} "
                         f"(last: {last:%Y-%m-%d %H:%M}). A sensor that goes silent is usually broken, "
                         f"not observing stillness."))

    # ── 4. BLE observers disagree about the world ────────────────────────────
    # Two radios in the same house should both see traffic. One reporting nothing while the
    # other is busy is a broken observer, not a quiet room.
    obs = q(cur, """
        SELECT observer, count(*) FROM telemetry.bluetooth
        WHERE ts > now() - interval '1 hour' GROUP BY 1""")
    if len(obs) >= 2:
        busiest = max(o[1] for o in obs)
        for name, n in obs:
            if busiest > 100 and n < busiest * 0.02:
                findings.append(("observer_quiet",
                                 f"BLE observer '{name}' logged {n} sightings in the last hour "
                                 f"while another logged {busiest}. That is a broken radio, not a "
                                 f"quiet room."))

    # ── report ───────────────────────────────────────────────────────────────
    if not findings:
        log("no negative-space findings — expected correlations all held")
    for kind, msg in findings:
        log(f"[{kind}] {msg}")
    if findings and alert:
        try:
            import re as _re
            from nova_notify import notify
            for kind, msg in findings:
                # STABLE dedup key: a silent sensor stays silent, and the message's ticking
                # duration ("nothing for 5 days, 13:34:11") changed every fire, so with a NULL
                # key this producer fired 196x/night — a quarter of the whole storm. Strip the
                # volatile numbers/timestamps so every re-fire of the SAME condition collapses,
                # and widen the re-notify window to 8h (a broken sensor doesn't need 24 reminders).
                stable = _re.sub(r"\d[\d:.,\s-]*", "#", msg)[:80]
                notify(f"Negative-space: {msg}", level="warning", category="security",
                       source="nova_negative_space.py",
                       dedup_key=f"negspace:{kind}:{stable}",
                       meta={"dedup_window_s": 28800})
            log(f"alerted on {len(findings)} finding(s)")
        except Exception as e:
            log(f"notify failed: {e}")
    conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--alert", action="store_true")
    sys.exit(main(ap.parse_args().alert))
