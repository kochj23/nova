#!/usr/bin/env python3
"""nova_osint_nuclei_sweep.py — feeds hosts discovered by Amass/theHarvester into
Nuclei's safe/non-intrusive template set. The one genuinely valuable interaction
between the OSINT recon tools and the vuln-check tool: recon finds it, nuclei
checks it for known misconfigs/exposures.

Runs weekly (Sun 08:55, after amass/theharvester finish, before the digest
article at 09:00) so any real findings show up in that week's article.

Written by Jordan Koch (via Claude).
"""
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

DSN = "host=localhost dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/osint_nuclei_sweep.log"
NUCLEI = str(Path.home() / "go/bin/nuclei")
NUCLEI_SAFE_TAGS = "cves,exposures,misconfiguration,default-login,takeover,tech"
FQDN_RE = re.compile(r"([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9-]{1,63})+)\s*\(FQDN\)")
SEVERITY_MAP = {"info": "info", "low": "warning", "medium": "warning",
                "high": "critical", "critical": "critical"}


def log(msg):
    line = f"[nuclei-sweep {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def discovered_hosts() -> set:
    """Pull real FQDNs out of amass's relationship-graph finding strings and
    theharvester's host findings from the last 30 days."""
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    hosts = set()

    cur.execute("SELECT finding FROM osint_findings WHERE tool='amass' "
               "AND ts > now() - interval '30 days'")
    for row in cur.fetchall():
        hosts.update(m.group(1) for m in FQDN_RE.finditer(row["finding"]))

    cur.execute("SELECT finding FROM osint_findings WHERE tool='theharvester' "
               "AND finding_type='host' AND ts > now() - interval '30 days'")
    for row in cur.fetchall():
        hosts.add(row["finding"].strip())

    cur.close()
    conn.close()
    return {h for h in hosts if h and "." in h}


def record(target, finding_type, finding, severity, metadata):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM osint_findings WHERE tool='nuclei-sweep' AND target=%s "
        "AND finding=%s LIMIT 1", (target, finding))
    is_new = cur.fetchone() is None
    cur.execute(
        "INSERT INTO osint_findings (tool, target, finding_type, finding, severity, metadata) "
        "VALUES ('nuclei-sweep', %s, %s, %s, %s, %s)",
        (target, finding_type, finding, severity if is_new else "info", json.dumps(metadata)))
    cur.close()
    conn.close()
    return is_new


def main():
    hosts = discovered_hosts()
    if not hosts:
        log("No discovered hosts to scan -- skipping")
        return
    log(f"Scanning {len(hosts)} discovered host(s): {', '.join(sorted(hosts))}")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("\n".join(sorted(hosts)))
        target_file = f.name

    r = subprocess.run(
        [NUCLEI, "-l", target_file, "-tags", NUCLEI_SAFE_TAGS,
         "-jsonl", "-silent", "-rate-limit", "50"],
        capture_output=True, text=True, timeout=600)
    Path(target_file).unlink(missing_ok=True)

    findings = []
    for line in (r.stdout or "").splitlines():
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    new_notable = []
    for f in findings:
        info = f.get("info", {})
        target = f.get("host") or f.get("matched-at", "unknown")
        sev = SEVERITY_MAP.get(info.get("severity"), "info")
        is_new = record(target, info.get("name", "finding"), f.get("matched-at", target),
                        sev, {"template-id": f.get("template-id"), "nuclei_severity": info.get("severity")})
        if is_new and sev != "info":
            new_notable.append(f"[{sev.upper()}] {target}: {info.get('name')}")

    log(f"{len(findings)} total finding(s), {len(new_notable)} new notable")
    if new_notable:
        notify("OSINT: Nuclei sweep found new issues", body="\n".join(new_notable[:20]),
              level="warning", category="security", dedup_key=None)
    log("Done")


if __name__ == "__main__":
    main()
