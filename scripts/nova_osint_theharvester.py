#!/usr/bin/env python3
"""nova_osint_theharvester.py — email/subdomain/name harvest via theHarvester.

Passive OSINT harvest (search engines, cert transparency, etc.) against the
same domains nova_security_surface_monitor.py and nova_osint_amass.py watch.
Diffs against known findings, flags anything NEW.

Runs weekly via scheduler. Findings -> osint_findings + shared_observations
and Slack via nova_notify.

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

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/osint_theharvester.log"
THEHARVESTER = str(Path.home() / "osint-venv/bin/theHarvester")
DOMAINS = ["digitalnoise.net", "nova.digitalnoise.net"]
SOURCES = "certspotter,crtsh,hackertarget,otx,rapiddns,urlscan"


def log(msg):
    print(f"[osint-theharvester] {msg}", flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[osint-theharvester] {msg}\n")
    except Exception:
        pass


def run_harvester(domain: str) -> dict:
    """Returns {"emails": set, "hosts": set}."""
    out_json = Path(f"/tmp/theharvester_{domain.replace('.', '_')}.json")
    try:
        subprocess.run(
            [THEHARVESTER, "-d", domain, "-b", SOURCES, "-f", str(out_json.with_suffix(""))],
            capture_output=True, text=True, timeout=300,
        )
        data = json.loads(out_json.read_text()) if out_json.exists() else {}
        return {
            "emails": set(data.get("emails", []) or []),
            "hosts": set(data.get("hosts", []) or []),
        }
    except Exception as e:
        log(f"theHarvester failed for {domain}: {e}")
        return {"emails": set(), "hosts": set()}
    finally:
        out_json.unlink(missing_ok=True)


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    all_new = []
    for domain in DOMAINS:
        results = run_harvester(domain)
        for finding_type, items in (("email", results["emails"]), ("host", results["hosts"])):
            if not items:
                continue
            log(f"{domain}: {len(items)} {finding_type}(s) found")
            cur.execute(
                "SELECT DISTINCT finding FROM osint_findings WHERE tool='theharvester' "
                "AND target=%s AND finding_type=%s",
                (domain, finding_type))
            known = {r["finding"] for r in cur.fetchall()}
            new = items - known

            for item in items:
                cur.execute(
                    "INSERT INTO osint_findings (tool, target, finding_type, finding, severity) "
                    "VALUES ('theharvester', %s, %s, %s, %s)",
                    (domain, finding_type, item, "warning" if item in new else "info"))

            if new:
                all_new.extend(new)
                cur.execute("""
                    INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                    VALUES ('nova_osint_theharvester', 'security', %s, %s, 'warning', %s)
                """, (
                    f"new-{finding_type}-discovered",
                    f"theHarvester found {len(new)} new {finding_type}(s) for {domain}: "
                    f"{', '.join(sorted(new)[:10])}",
                    json.dumps({"domain": domain, "type": finding_type, "new": sorted(new)}),
                ))

    cur.close()
    conn.close()

    if all_new:
        try:
            notify("OSINT: New emails/hosts discovered",
                   body="\n".join(sorted(all_new)[:30]),
                   level="warning", category="security", dedup_key="osint-theharvester-new")
        except Exception as e:
            log(f"notify failed: {e}")
    else:
        log("nothing new")


if __name__ == "__main__":
    main()
