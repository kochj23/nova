#!/usr/bin/env python3
"""nova_osint_lookup.py — on-demand OSINT lookups (not scheduled; these tools need
a specific target each time, unlike the scheduled domain-watch scripts).

Usage:
  nova_osint_lookup.py username <handle>       # Sherlock: where does this handle exist?
  nova_osint_lookup.py gmail <email>            # GHunt: public info tied to a Gmail address
  nova_osint_lookup.py exif <file_or_dir>       # ExifTool: metadata dump (GPS/device/timestamps)
  nova_osint_lookup.py reconng                  # Drop into an interactive recon-ng shell
  nova_osint_lookup.py spiderfoot <domain>      # SpiderFoot: broad passive footprint scan
  nova_osint_lookup.py phone <number>           # PhoneInfoga: carrier/line-type/social OSINT (E.164, e.g. +14155552671)
  nova_osint_lookup.py nuclei <url_or_host>     # Nuclei: safe non-intrusive vulnerability/misconfig templates

Every result also gets written to osint_findings (tool='lookup:<subtool>', target=<query>)
so ad-hoc investigations still show up in the same place as the scheduled scans.

Written by Jordan Koch (via Claude).
"""
import json
import subprocess
import sys
from pathlib import Path

import psycopg2

DSN = "host=localhost dbname=nova_ops user=kochj"
VENV = Path.home() / "osint-venv/bin"


def record(tool, target, finding_type, finding, metadata=None, severity=None):
    try:
        conn = psycopg2.connect(DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO osint_findings (tool, target, finding_type, finding, metadata, severity) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (tool, target, finding_type, finding, json.dumps(metadata or {}), severity))
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[warn] failed to record finding: {e}", file=sys.stderr)


def cmd_username(handle):
    print(f"Checking '{handle}' across platforms via Sherlock...\n")
    r = subprocess.run([str(VENV / "sherlock"), handle, "--print-found", "--timeout", "10"],
                       capture_output=True, text=True, timeout=180)
    print(r.stdout)
    found = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("[+]")]
    for ln in found:
        record("lookup:sherlock", handle, "profile_found", ln.strip())
    print(f"\n{len(found)} profile(s) found, logged to osint_findings.")


def cmd_gmail(email):
    print(f"GHunt lookup for {email} (first run requires `ghunt login` once, interactively)\n")
    r = subprocess.run([str(VENV / "ghunt"), "email", email],
                       capture_output=True, text=True, timeout=120)
    print(r.stdout or r.stderr)
    record("lookup:ghunt", email, "profile_dump", (r.stdout or r.stderr)[:2000])


def cmd_exif(path):
    r = subprocess.run(["exiftool", "-j", "-a", "-G1", path],
                       capture_output=True, text=True, timeout=60)
    print(r.stdout)
    try:
        data = json.loads(r.stdout)
        for entry in data:
            record("lookup:exiftool", entry.get("SourceFile", path), "metadata",
                   json.dumps(entry)[:2000], metadata=entry)
    except Exception:
        pass


def cmd_reconng():
    subprocess.run([str(VENV / "python3"), str(Path.home() / "recon-ng/recon-ng")])


SPIDERFOOT_MODULES = "sfp_dnsresolve,sfp_crt"
    # ponytail: deliberately narrow. The wider key-free set (hackertarget,
    # dnsdumpster, whois, sslcert, webserver, ...) legitimately fans out once
    # per discovered subdomain, and against digitalnoise.net that stalled past
    # 15 minutes at 0% CPU -- serial network waits on unresponsive subdomains,
    # not a tuning problem worth chasing for a secondary on-demand tool. This
    # pair (DNS resolution + crt.sh cert history) is proven fast (~3s) and
    # covers the two highest-value passive signals. Widen if a real need shows up.


def cmd_spiderfoot(domain):
    print(f"Running SpiderFoot passive scan (DNS + cert transparency) against {domain}...\n")
    sf_py = Path.home() / "spiderfoot/sf.py"
    r = subprocess.run(
        [str(VENV / "python3"), str(sf_py), "-s", domain, "-m", SPIDERFOOT_MODULES, "-o", "json", "-q"],
        capture_output=True, text=True, timeout=60, cwd=str(sf_py.parent))
    try:
        events = json.loads(r.stdout)
    except Exception:
        print(r.stdout or r.stderr)
        return
    for e in events:
        record("lookup:spiderfoot", domain, e.get("type", "unknown"), e.get("data", ""),
               metadata={"module": e.get("module")})
    print(f"{len(events)} finding(s) logged to osint_findings.")


GO_BIN = Path.home() / "go/bin"


def cmd_phone(number):
    print(f"Running PhoneInfoga scan against {number}...\n")
    r = subprocess.run([str(GO_BIN / "phoneinfoga"), "scan", "-n", number],
                       capture_output=True, text=True, timeout=90)
    out = r.stdout or r.stderr
    print(out)
    record("lookup:phoneinfoga", number, "phone_osint", out[:2000])


# ponytail: curated allow-list, not "everything except a few excluded tags" --
# the full non-dos/fuzz/intrusive template set is still ~14k templates and blew
# past a 300s budget on a single live URL. This set covers the genuinely
# high-value, fast, non-destructive categories.
NUCLEI_SAFE_TAGS = "cves,exposures,misconfiguration,default-login,takeover,tech"


def cmd_nuclei(target):
    print(f"Running Nuclei ({NUCLEI_SAFE_TAGS}) against {target}...\n")
    r = subprocess.run(
        [str(GO_BIN / "nuclei"), "-u", target, "-tags", NUCLEI_SAFE_TAGS,
         "-jsonl", "-silent", "-rate-limit", "50"],
        capture_output=True, text=True, timeout=240)
    findings = []
    for line in (r.stdout or "").splitlines():
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # Nuclei's 5-tier severity (info/low/medium/high/critical) -> the 3-tier
    # vocabulary the rest of osint_findings/the digest article uses.
    SEVERITY_MAP = {"info": "info", "low": "warning", "medium": "warning",
                    "high": "critical", "critical": "critical"}
    for f in findings:
        info = f.get("info", {})
        record("lookup:nuclei", target, info.get("name", "finding"),
               f.get("matched-at", target),
               severity=SEVERITY_MAP.get(info.get("severity"), "info"),
               metadata={"template-id": f.get("template-id"), "nuclei_severity": info.get("severity")})
    print(f"{len(findings)} finding(s) logged to osint_findings.")
    if not findings:
        print("(clean -- or the target simply didn't match any safe-tagged template)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    mode = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    if mode == "username" and arg:
        cmd_username(arg)
    elif mode == "gmail" and arg:
        cmd_gmail(arg)
    elif mode == "exif" and arg:
        cmd_exif(arg)
    elif mode == "reconng":
        cmd_reconng()
    elif mode == "spiderfoot" and arg:
        cmd_spiderfoot(arg)
    elif mode == "phone" and arg:
        cmd_phone(arg)
    elif mode == "nuclei" and arg:
        cmd_nuclei(arg)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
