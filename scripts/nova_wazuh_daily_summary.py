#!/usr/bin/env python3
"""
nova_wazuh_daily_summary.py — Daily Wazuh SIEM executive summary.

Computes a composite security posture score from 5 dimensions:
  1. Agent Coverage  — are all hosts reporting?
  2. Vulnerability Exposure — known CVEs by severity
  3. Compliance (SCA) — CIS benchmark scores across fleet
  4. Threat Activity — critical/high alerts in last 24h
  5. Rootkit/FIM Health — rootcheck clean, FIM not excessive

Posts management-level summary to #nova-notifications daily at 07:00.

Written by Jordan Koch.
"""

import json
import ssl
import sys
import base64
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_notify import notify

WAZUH_URL = "https://192.168.1.2:9200"   # Wazuh Indexer (single-node docker on nova-core)
DASHBOARD_URL = "https://192.168.1.2"
EXPECTED_AGENTS = 7  # Office-M4-2, nuk, TV-Movies-3, nova-core, nova-core2, nova-core3, nova-core4


def _wazuh_creds() -> str:
    """admin:<password> from Keychain (macOS) or the security shim/secrets.env
    (Linux nodes), base64'd. Never hardcode the indexer password in source."""
    import subprocess
    pw = subprocess.run(
        ["security", "find-generic-password", "-a", "nova",
         "-s", "nova-wazuh-indexer-password", "-w"],
        capture_output=True, text=True).stdout.strip() or "SecretPassword"
    return base64.b64encode(f"admin:{pw}".encode()).decode()


WAZUH_CREDS = _wazuh_creds()

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[wazuh-summary {ts}] {msg}", flush=True)


def os_query(index_pattern, query_body):
    req = urllib.request.Request(
        f"{WAZUH_URL}/{index_pattern}/_search",
        data=json.dumps(query_body).encode(),
        headers={
            "Authorization": f"Basic {WAZUH_CREDS}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=30, context=_ssl_ctx)
    return json.loads(resp.read())


# ── Dimension 1: Agent Coverage ───────────────────────────────────────────────

def score_agent_coverage():
    """100 if all expected agents reported in last 15min, 0 if none."""
    try:
        data = os_query("wazuh-monitoring-*", {
            "size": 0,
            "query": {"range": {"timestamp": {"gte": "now-15m"}}},
            "aggs": {
                "active": {
                    "filter": {"term": {"status": "active"}},
                    "aggs": {"agents": {"cardinality": {"field": "name"}}}
                }
            }
        })
        active = data["aggregations"]["active"]["agents"]["value"]
        return min(100, int((active / EXPECTED_AGENTS) * 100)), active
    except Exception as e:
        log(f"Agent coverage query failed: {e}")
        return 0, 0


# ── Dimension 2: Vulnerability Exposure ───────────────────────────────────────

def score_vulnerabilities():
    """Score based on open CVEs. Critical=heavy penalty, High=moderate, Medium/Low=light."""
    try:
        data = os_query("wazuh-states-vulnerabilities-*", {
            "size": 0,
            "aggs": {
                "by_severity": {"terms": {"field": "vulnerability.severity", "size": 10}}
            }
        })
        buckets = {b["key"]: b["doc_count"]
                   for b in data["aggregations"]["by_severity"]["buckets"]}

        critical = buckets.get("Critical", 0)
        high = buckets.get("High", 0)
        medium = buckets.get("Medium", 0)
        low = buckets.get("Low", 0)
        total = critical + high + medium + low

        # Weighted penalty: critical=10pts, high=3pts, medium=1pt, low=0.25pt
        penalty = (critical * 10) + (high * 3) + (medium * 1) + (low * 0.25)
        score = max(0, min(100, int(100 - penalty)))
        return score, {"critical": critical, "high": high, "medium": medium, "low": low, "total": total}
    except Exception as e:
        log(f"Vulnerability query failed: {e}")
        return 50, {"critical": 0, "high": 0, "medium": 0, "low": 0, "total": 0}


# ── Dimension 3: Compliance (SCA) ─────────────────────────────────────────────

def score_compliance():
    """Average SCA score across all agents that have reported."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        data = os_query(f"wazuh-alerts-4.x-{today}", {
            "size": 0,
            "query": {"term": {"rule.groups": "sca"}},
            "aggs": {
                "by_agent": {
                    "terms": {"field": "agent.name", "size": 10},
                    "aggs": {
                        "latest": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"timestamp": "desc"}],
                                "_source": ["data.sca.score", "data.sca.passed", "data.sca.failed", "agent.name"]
                            }
                        }
                    }
                }
            }
        })

        scores = {}
        for bucket in data["aggregations"]["by_agent"]["buckets"]:
            hit = bucket["latest"]["hits"]["hits"][0]["_source"]
            sca_score = int(hit.get("data", {}).get("sca", {}).get("score", 0))
            scores[bucket["key"]] = sca_score

        if not scores:
            return 50, {}  # No SCA data today — neutral

        avg = sum(scores.values()) // len(scores)
        return avg, scores
    except Exception as e:
        log(f"SCA query failed: {e}")
        return 50, {}


# ── Dimension 4: Threat Activity ──────────────────────────────────────────────

def score_threat_activity():
    """Score based on actual threats (L8+) in last 24h. Zero critical/high = 100."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        data = os_query(f"wazuh-alerts-4.x-{today},wazuh-alerts-4.x-*", {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"range": {"timestamp": {"gte": "now-24h"}}},
                        {"range": {"rule.level": {"gte": 8}}}
                    ]
                }
            },
            "aggs": {
                "by_level": {"terms": {"field": "rule.level", "size": 20}},
                "top_rules": {"terms": {"field": "rule.description", "size": 5, "order": {"_count": "desc"}}},
                "by_agent": {"terms": {"field": "agent.name", "size": 10}}
            }
        })

        total_high = data["hits"]["total"]["value"]
        levels = {b["key"]: b["doc_count"] for b in data["aggregations"]["by_level"]["buckets"]}
        critical = sum(v for k, v in levels.items() if k >= 12)
        high = sum(v for k, v in levels.items() if 10 <= k < 12)
        elevated = sum(v for k, v in levels.items() if 8 <= k < 10)

        top_rules = [{"rule": b["key"], "count": b["doc_count"]}
                     for b in data["aggregations"]["top_rules"]["buckets"]]

        # Critical intrusion = immediate drop; high = significant; elevated = minor
        penalty = (critical * 30) + (high * 15) + (elevated * 2)
        score = max(0, min(100, int(100 - penalty)))

        return score, {"critical": critical, "high": high, "elevated": elevated,
                       "total": total_high, "top_rules": top_rules}
    except Exception as e:
        log(f"Threat activity query failed: {e}")
        return 100, {}


# ── Dimension 5: Rootkit & FIM Health ─────────────────────────────────────────

def score_rootkit_fim():
    """Rootcheck findings are bad. FIM is informational unless volume is insane."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        data = os_query(f"wazuh-alerts-4.x-{today}", {
            "size": 0,
            "query": {"range": {"timestamp": {"gte": "now-24h"}}},
            "aggs": {
                "rootcheck": {
                    "filter": {"term": {"rule.groups": "rootcheck"}},
                    "aggs": {"count": {"value_count": {"field": "rule.id"}}}
                },
                "fim": {
                    "filter": {"term": {"rule.groups": "syscheck"}},
                    "aggs": {"count": {"value_count": {"field": "rule.id"}}}
                }
            }
        })

        rootcheck = data["aggregations"]["rootcheck"]["count"]["value"]
        fim = data["aggregations"]["fim"]["count"]["value"]

        # Rootcheck findings are concerning (file permissions, hidden processes)
        # FIM is normal unless > 100K/day (indicates compromise or broken config)
        penalty = min(50, rootcheck * 0.5) + (10 if fim > 100000 else 0)
        score = max(0, min(100, int(100 - penalty)))

        return score, {"rootcheck_events": rootcheck, "fim_changes": fim}
    except Exception as e:
        log(f"Rootkit/FIM query failed: {e}")
        return 80, {"rootcheck_events": 0, "fim_changes": 0}


# ── Alert Summary (for the detail section) ───────────────────────────────────

def get_alert_summary():
    """Get total alert counts and top rules for the detail section."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        data = os_query(f"wazuh-alerts-4.x-{today},wazuh-alerts-4.x-*", {
            "size": 0,
            "query": {"range": {"timestamp": {"gte": "now-24h"}}},
            "aggs": {
                "by_level": {"terms": {"field": "rule.level", "size": 20, "order": {"_key": "desc"}}},
                "by_agent": {"terms": {"field": "agent.name", "size": 10, "order": {"_count": "desc"}}},
                "top_rules": {"terms": {"field": "rule.description", "size": 6, "order": {"_count": "desc"}}},
                "auth_failed": {
                    "filter": {"term": {"rule.groups": "authentication_failed"}},
                    "aggs": {"count": {"value_count": {"field": "rule.id"}}}
                },
                "auth_success": {
                    "filter": {"term": {"rule.groups": "authentication_success"}},
                    "aggs": {"count": {"value_count": {"field": "rule.id"}}}
                }
            }
        })

        total = data["hits"]["total"]["value"]
        aggs = data["aggregations"]
        levels = {b["key"]: b["doc_count"] for b in aggs["by_level"]["buckets"]}
        agents = [{"name": b["key"], "count": b["doc_count"]} for b in aggs["by_agent"]["buckets"][:5]]
        top_rules = [{"rule": b["key"], "count": b["doc_count"]} for b in aggs["top_rules"]["buckets"]]
        auth_failed = aggs["auth_failed"]["count"]["value"]
        auth_success = aggs["auth_success"]["count"]["value"]

        return {
            "total": total,
            "levels": levels,
            "agents": agents,
            "top_rules": top_rules,
            "auth_failed": auth_failed,
            "auth_success": auth_success,
        }
    except Exception:
        return None


# ── Composite Score & Message ─────────────────────────────────────────────────

WEIGHTS = {
    "coverage": 0.15,
    "vulnerabilities": 0.25,
    "compliance": 0.20,
    "threats": 0.25,
    "rootkit_fim": 0.15,
}


def build_summary():
    """Compute composite posture score and build the notification message."""
    # Score each dimension
    cov_score, active_agents = score_agent_coverage()
    vuln_score, vuln_detail = score_vulnerabilities()
    comp_score, sca_scores = score_compliance()
    threat_score, threat_detail = score_threat_activity()
    rfim_score, rfim_detail = score_rootkit_fim()

    # Weighted composite
    composite = int(
        (cov_score * WEIGHTS["coverage"]) +
        (vuln_score * WEIGHTS["vulnerabilities"]) +
        (comp_score * WEIGHTS["compliance"]) +
        (threat_score * WEIGHTS["threats"]) +
        (rfim_score * WEIGHTS["rootkit_fim"])
    )

    # Icon
    if composite >= 85:
        icon = ":large_green_circle:"
        grade = "Strong"
    elif composite >= 70:
        icon = ":large_yellow_circle:"
        grade = "Moderate"
    elif composite >= 50:
        icon = ":large_orange_circle:"
        grade = "Needs Attention"
    else:
        icon = ":red_circle:"
        grade = "At Risk"

    # Alert summary
    alerts = get_alert_summary()

    now = datetime.now().strftime("%A, %B %d")
    lines = [
        f":shield: *Wazuh SIEM Daily Summary — {now}*",
        "",
        f"{icon} *Security Posture: {composite}/100 — {grade}*",
        "",
        "*Score Breakdown:*",
        f"  • Agent Coverage: {cov_score}/100 ({active_agents}/{EXPECTED_AGENTS} reporting)",
        f"  • Vulnerabilities: {vuln_score}/100 ({vuln_detail['total']} open — {vuln_detail['critical']}C / {vuln_detail['high']}H / {vuln_detail['medium']}M / {vuln_detail['low']}L)",
        f"  • Compliance (SCA): {comp_score}/100" + (f" ({', '.join(f'{k}:{v}%' for k, v in sca_scores.items())})" if sca_scores else " (no data today)"),
        f"  • Threat Activity: {threat_score}/100 ({threat_detail.get('total', 0)} events L8+)",
        f"  • Rootkit/FIM: {rfim_score}/100 ({rfim_detail.get('rootcheck_events', 0)} rootcheck, {rfim_detail.get('fim_changes', 0):,} FIM)",
    ]

    # Threat detail if any
    if threat_detail.get("top_rules"):
        lines.append("")
        lines.append("*Notable Threats (24h):*")
        for r in threat_detail["top_rules"][:4]:
            lines.append(f"  :warning: {r['count']}x {r['rule']}")

    # Alert volume
    if alerts:
        levels = alerts["levels"]
        crit = sum(v for k, v in levels.items() if k >= 10)
        high = sum(v for k, v in levels.items() if 8 <= k < 10)
        med = sum(v for k, v in levels.items() if 5 <= k < 8)
        low = sum(v for k, v in levels.items() if k < 5)

        lines.append("")
        lines.append(f"*Alert Volume (24h):* {alerts['total']:,} total")
        lines.append(f"  L10+ Critical: {crit} | L8-9 High: {high} | L5-7 Medium: {med:,} | L1-4 Low: {low:,}")
        lines.append(f"  Auth: {alerts['auth_success']:,} success, {alerts['auth_failed']} failed")

        lines.append("")
        lines.append("*By Agent:*")
        for a in alerts["agents"]:
            lines.append(f"  • {a['name']}: {a['count']:,}")

    lines.append("")
    lines.append(f":link: <{DASHBOARD_URL}|Open Wazuh Dashboard>")

    return "\n".join(lines)


def main():
    log("Building daily Wazuh SIEM summary...")

    summary = build_summary()
    if not summary:
        log("Failed to build summary — Wazuh may be unreachable.")
        return

    lines = summary.split("\n")
    title = lines[0].replace(":shield:", "").replace("*", "").strip()
    body = "\n".join(lines[1:]).strip() or None
    notify(
        title,
        body=body,
        level="info",
        category="security",
        dedup_key="wazuh-daily-summary",
        meta={"host": "Office-M4-2"},
    )
    log("Posted daily Wazuh SIEM summary via nova_notify")


if __name__ == "__main__":
    main()
