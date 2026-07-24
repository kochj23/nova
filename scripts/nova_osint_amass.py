#!/usr/bin/env python3
"""nova_osint_amass.py — subdomain/attack-surface enumeration via OWASP Amass.

Passive-mode enumeration (no active brute-forcing against the target -- Amass
just correlates public data sources: cert transparency, DNS aggregators, etc.)
of the same domains nova_security_surface_monitor.py already watches. Diffs
against the last run's known-subdomain set and flags anything NEW.

Runs weekly via scheduler. Findings -> osint_findings + shared_observations
(so it feeds the same "one voice" context every ops-article script reads) and
Slack via nova_notify.

Written by Jordan Koch (via Claude).
"""
import json
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

DSN = "host=localhost dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/osint_amass.log"
AMASS = str(Path.home() / "go/bin/amass")
DOMAINS = ["digitalnoise.net", "nova.digitalnoise.net"]


def log(msg):
    print(f"[osint-amass] {msg}", flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[osint-amass] {msg}\n")
    except Exception:
        pass


def run_amass(domain: str) -> set:
    try:
        r = subprocess.run(
            [AMASS, "enum", "-passive", "-d", domain, "-timeout", "5"],
            capture_output=True, text=True, timeout=360,
        )
        return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    except Exception as e:
        log(f"amass failed for {domain}: {e}")
        return set()


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    all_new = []
    for domain in DOMAINS:
        found = run_amass(domain)
        log(f"{domain}: {len(found)} subdomains found")
        if not found:
            continue

        cur.execute(
            "SELECT DISTINCT finding FROM osint_findings WHERE tool='amass' AND target=%s",
            (domain,))
        known = {r["finding"] for r in cur.fetchall()}
        new = found - known

        for sub in found:
            cur.execute(
                "INSERT INTO osint_findings (tool, target, finding_type, finding, severity) "
                "VALUES ('amass', %s, 'subdomain', %s, %s)",
                (domain, sub, "warning" if sub in new else "info"))

        if new:
            all_new.extend(new)
            cur.execute("""
                INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                VALUES ('nova_osint_amass', 'security', 'new-subdomain-discovered', %s, 'warning', %s)
            """, (
                f"Amass found {len(new)} new subdomain(s) for {domain}: {', '.join(sorted(new)[:10])}",
                json.dumps({"domain": domain, "new_subdomains": sorted(new)}),
            ))

    cur.close()
    conn.close()

    if all_new:
        try:
            notify("OSINT: New subdomains discovered",
                   body="\n".join(sorted(all_new)[:30]),
                   level="warning", category="security", dedup_key="osint-amass-new")
        except Exception as e:
            log(f"notify failed: {e}")
    else:
        log("no new subdomains")


if __name__ == "__main__":
    main()
