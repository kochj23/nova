#!/usr/bin/env python3
"""Fleet-vendor security-advisory watcher.

Watches for vulnerabilities affecting the gear Nova's fleet ACTUALLY runs (Ubiquiti/UniFi,
Synology, Ubuntu, Home Assistant, Plex, Postgres, Grafana, Wazuh, Docker, Ollama...). This is
the gap that let max-severity UniFi CVE-2026-50746 slip past on 2026-07 — Nova had security
feeds, but none scoped to her own hardware vendors.

Sources (stdlib only, no API key):
  - CISA KEV  — Known Exploited Vulnerabilities (actively exploited = top priority)
  - Security news RSS (BleepingComputer, The Hacker News) filtered to fleet keywords

New hits -> #nova-critical. Dedups via a seen-state file; first run baselines (no alert flood).
Run every ~4h via the scheduler.
"""
import html
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

import nova_config

STATE = os.path.expanduser("~/.openclaw/state/vendor_advisories.json")
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
RSS = [
    "https://www.bleepingcomputer.com/feed/",
    "https://feeds.feedburner.com/TheHackersNews",
]
# gear the fleet actually runs — extend as the fleet changes
FLEET = ["ubiquiti", "unifi", "udm", "unvr", "synology", "\\bdsm\\b", "ubuntu", "home assistant",
         "homeassistant", "frigate", "plex", "ollama", "postgresql", "pgvector", "grafana",
         "wazuh", "docker", "cinc", "\\bchef\\b", "redis", "beelink", "mac studio", "macos"]
FLEET_RE = re.compile("(" + "|".join(FLEET) + ")", re.I)


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "nova-advisory-watch/1.0 (+fleet security)"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def check_kev():
    hits = []
    try:
        kev = json.loads(_get(KEV_URL))
        for v in kev.get("vulnerabilities", []):
            blob = f"{v.get('vendorProject','')} {v.get('product','')} {v.get('vulnerabilityName','')}"
            if FLEET_RE.search(blob):
                cve = v.get("cveID", "")
                hits.append(("CISA KEV ⚠️ actively-exploited", cve,
                             f"{v.get('vendorProject')} {v.get('product')} — {v.get('vulnerabilityName')}",
                             f"https://nvd.nist.gov/vuln/detail/{cve}", cve))
    except Exception as e:
        print("KEV fetch failed:", e)
    return hits


def check_rss():
    hits = []
    for url in RSS:
        try:
            root = ET.fromstring(_get(url))
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                blob = html.unescape(f"{title} {item.findtext('description') or ''}")
                if FLEET_RE.search(blob):
                    hits.append(("news", FLEET_RE.search(blob).group(1), title, link, link or title))
        except Exception as e:
            print(f"RSS {url} failed:", e)
    return hits


def main():
    try:
        st = json.load(open(STATE))
    except Exception:
        st = None
    seen = set((st or {}).get("seen", []))

    all_hits = check_kev() + check_rss()
    new = [h for h in all_hits if h[4] not in seen]

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    new_seen = list(seen | {h[4] for h in all_hits})[-3000:]

    if st is None:  # first run: baseline, don't flood
        json.dump({"seen": new_seen}, open(STATE, "w"))
        print(f"baselined {len(all_hits)} current fleet-relevant advisories (would have flagged: "
              + "; ".join(f"{h[1]} {h[2][:50]}" for h in all_hits[:8]) + ")")
        return

    if new:
        lines = "\n".join(f"• *[{s}]* {title}\n  {link}" for s, tag, title, link, key in new[:12])
        nova_config.post_both(
            f":shield: *Fleet security advisory* — {len(new)} new item(s) affecting gear you run:\n{lines}",
            slack_channel=nova_config.SLACK_BB)
    json.dump({"seen": new_seen}, open(STATE, "w"))
    print(f"{len(new)} new advisories alerted ({len(all_hits)} matched total)")


if __name__ == "__main__":
    main()
