#!/usr/bin/env python3
"""nova_wifi_guard.py — judge the passive Wi-Fi survey against our real access points.

Reads the latest wifi_survey run (nova_wifi_survey.py on the nova-core7 monitor radio) and the
SSID/BSSID list from the UniFi controller (vap_table on every AP), then alerts on:
  * evil twin   — one of OUR SSIDs broadcast from a BSSID that isn't ours
  * deauth flood — DEAUTH_ALERT+ deauth/disassoc frames for one of OUR BSSIDs in one capture
Alerts are deduped per BSSID per day. Known-legit foreign BSSIDs: service_config nova/wifi_guard_allow.

  nova_wifi_guard.py            one evaluation (scheduler, every 10 min)
  nova_wifi_guard.py --selftest
"""
import json, sys

DEAUTH_ALERT = 30          # frames per 60 s capture; a client roaming sends a handful, an attack hundreds
MAX_AGE_MIN = 25           # ignore a survey older than this (sensor down -> say nothing, don't guess)


def evaluate(ours: dict, survey: list, deauth: dict, allow: set) -> list:
    """ours: {bssid: essid}; survey: [{bssid, essid, power, channel}]; deauth: {bssid: frames}.
    Returns [(kind, bssid, detail)]."""
    our_ssids = set(ours.values())
    found = []
    for ap in survey:
        b = ap["bssid"].lower()
        if ap["essid"] in our_ssids and b not in ours and b not in allow:
            found.append(("evil_twin", b, f"broadcasting our SSID '{ap['essid']}' on ch{ap['channel']} at {ap['power']} dBm"))
    for b, n in deauth.items():
        if b.lower() in ours and n >= DEAUTH_ALERT:
            found.append(("deauth_flood", b.lower(), f"{n} deauth/disassoc frames against our '{ours[b.lower()]}' in one capture"))
    return found


def our_bssids() -> dict:
    sys.path.insert(0, str(__import__("pathlib").Path.home() / ".openclaw/scripts"))
    import logging; logging.disable(logging.INFO)
    import nova_unifi_poller as u
    u._unifi_login()
    return {v["bssid"].lower(): v.get("essid", "") for d in (u._fetch_devices() or [])
            for v in d.get("vap_table", []) if v.get("bssid")}


def main():
    import psycopg2
    try:
        import nova_dsn; dsn = nova_dsn.pg_dsn("nova_ops")
    except ImportError:
        dsn = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
    cur = psycopg2.connect(dsn).cursor()
    cur.execute(f"SELECT id, deauth, sensor FROM wifi_survey_runs WHERE ts > now() - interval '{MAX_AGE_MIN} minutes' "
                "ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        print("[wifi-guard] no recent survey (sensor down?) - nothing judged"); return 1
    run, deauth, sensor = row
    cur.execute("SELECT bssid, essid, power, channel FROM wifi_survey WHERE run_id=%s", (run,))
    survey = [dict(zip(("bssid", "essid", "power", "channel"), r)) for r in cur.fetchall()]
    cur.execute("SELECT value FROM service_config WHERE service='nova' AND key='wifi_guard_allow'")
    r = cur.fetchone(); allow = {b.lower() for b in (r[0] if r else [])}
    ours = our_bssids()
    if not ours:
        print("[wifi-guard] UniFi returned no BSSIDs - refusing to judge (everything would look foreign)"); return 1
    found = evaluate(ours, survey, deauth or {}, allow)
    print(f"[wifi-guard] run {run} ({sensor}): {len(survey)} APs heard, {len(ours)} of ours known, {len(found)} finding(s)")
    # stored so the security column (nova_ops_context) and Grafana can show them, not just Slack
    cur.execute("CREATE TABLE IF NOT EXISTS wifi_guard_findings (ts timestamptz NOT NULL DEFAULT now(), run_id bigint, "
                "kind text, bssid text, detail text)")
    for kind, b, detail in found:
        cur.execute("INSERT INTO wifi_guard_findings(run_id,kind,bssid,detail) VALUES (%s,%s,%s,%s)", (run, kind, b, detail))
    cur.connection.commit()
    if found:
        from nova_notify import notify
        for kind, b, detail in found:
            title = "Wi-Fi evil twin" if kind == "evil_twin" else "Wi-Fi deauth flood"
            print(f"  {title}: {b} {detail}")
            notify(title=title, body=f"{b} {detail} (sensor {sensor}, survey run {run}). "
                   f"If legit, add the BSSID to service_config nova/wifi_guard_allow.",
                   level="warning", category="security", source="nova_wifi_guard", dedup_key=f"wifi-{kind}-{b}")
    return 0


def selftest():
    ours = {"ours-5g": "KOCH_5G-2", "ours-24": "KOCH-2.4"}
    survey = [{"bssid": "OURS-5G", "essid": "KOCH_5G-2", "power": -40, "channel": 36},   # ours
              {"bssid": "twin-1", "essid": "KOCH_5G-2", "power": -55, "channel": 36},   # twin
              {"bssid": "neighbor-1", "essid": "Neighbor", "power": -80, "channel": 6}]     # neighbour
    f = evaluate(ours, survey, {"ours-24": 120, "stranger-1": 500, "ours-5g": 4}, set())
    kinds = sorted((k, b) for k, b, _ in f)
    assert kinds == [("deauth_flood", "ours-24"), ("evil_twin", "twin-1")], kinds
    assert evaluate(ours, survey, {}, {"twin-1"}) == []          # allow-listed twin
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else sys.exit(main())
