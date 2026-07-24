#!/usr/bin/env python3
"""nova_osint_hibp.py — breach/credential exposure monitoring via Have I Been Pwned.

Checks a fixed list of known emails/domains against HIBP's breach API. Flags any
breach not seen on a prior run. Same shape as the existing crt.sh cert-transparency
watch in nova_security_surface_monitor.py -- "did something about me just become
newly exposed."

Requires an HIBP API key (paid, ~$3.50/mo as of 2026 -- HIBP retired the free tier
years ago). Store it via:
    security add-generic-password -a nova -s nova-hibp-api-key -w '<key>'
(or the fleet pgcrypto store on Linux hosts, same as every other Nova secret.)

Runs daily via scheduler. Findings -> osint_findings + shared_observations and
Slack via nova_notify.

Written by Jordan Koch (via Claude).
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_notify import notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/osint_hibp.log"
API = "https://haveibeenpwned.com/api/v3"

# Monitored accounts live in the fleet secret store (not hardcoded here -- an
# email address doesn't belong in committed source any more than an API key
# does). Configure via: echo '<email1>,<email2>' | python3 nova_secrets.py set
# nova-hibp-monitored-accounts
_accounts_raw = nova_config._keychain("nova-hibp-monitored-accounts", required=False)
MONITORED_ACCOUNTS = [a.strip() for a in _accounts_raw.split(",") if a.strip()] if _accounts_raw else []


def log(msg):
    print(f"[osint-hibp] {msg}", flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[osint-hibp] {msg}\n")
    except Exception:
        pass


def check_account(email: str, api_key: str) -> list:
    req = urllib.request.Request(
        f"{API}/breachedaccount/{urllib.parse.quote(email)}?truncateResponse=false",
        headers={"hibp-api-key": api_key, "User-Agent": "nova-osint-hibp"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []  # no breaches -- the good outcome
        log(f"HIBP HTTP {e.code} for {email}: {e.read()[:200]}")
        return []
    except Exception as e:
        log(f"HIBP request failed for {email}: {e}")
        return []


def main():
    api_key = nova_config._keychain("nova-hibp-api-key", required=False)
    if not api_key:
        log("No HIBP API key configured -- skipping. Add one with: "
            "security add-generic-password -a nova -s nova-hibp-api-key -w '<key>'")
        return 1
    if not MONITORED_ACCOUNTS:
        log("No monitored accounts configured -- skipping. Add via: "
            "echo '<email1>,<email2>' | python3 nova_secrets.py set nova-hibp-monitored-accounts")
        return 1

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    all_new = []
    for email in MONITORED_ACCOUNTS:
        breaches = check_account(email, api_key)
        names = {b["Name"] for b in breaches}
        log(f"{email}: {len(names)} breach(es) on record")

        cur.execute(
            "SELECT DISTINCT finding FROM osint_findings WHERE tool='hibp' AND target=%s",
            (email,))
        known = {r["finding"] for r in cur.fetchall()}
        new = names - known

        for b in breaches:
            cur.execute(
                "INSERT INTO osint_findings (tool, target, finding_type, finding, severity, metadata) "
                "VALUES ('hibp', %s, 'breach', %s, %s, %s)",
                (email, b["Name"], "critical" if b["Name"] in new else "info",
                 json.dumps({"breach_date": b.get("BreachDate"), "data_classes": b.get("DataClasses")})))

        if new:
            all_new.extend(f"{email}: {n}" for n in new)
            cur.execute("""
                INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                VALUES ('nova_osint_hibp', 'security', 'new-breach-exposure', %s, 'critical', %s)
            """, (
                f"{email} appeared in {len(new)} newly-recorded breach(es): {', '.join(sorted(new))}",
                json.dumps({"email": email, "new_breaches": sorted(new)}),
            ))

        time.sleep(1.6)  # HIBP rate limit is ~1 req/1.5s on the base tier

    cur.close()
    conn.close()

    if all_new:
        try:
            notify("OSINT: New breach exposure", body="\n".join(all_new),
                   level="critical", category="security", dedup_key="osint-hibp-new")
        except Exception as e:
            log(f"notify failed: {e}")
    else:
        log("no new breaches")


if __name__ == "__main__":
    sys.exit(main() or 0)
