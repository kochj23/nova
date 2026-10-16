#!/usr/bin/env python3
"""nova_service_tiers.py — service tiers with recovery objectives (RTO / RPO) for every Nova component.

Little Mister, 2026-10-15 (#35): "Service tiers with RTO/RPO."  Google SRE book ch.26 (Data Integrity)
and BSRS ch.9 (Design for Recovery): a recovery objective nobody wrote down is not an objective.

  Tier 0  RTO 1h,  RPO 15m   — Nova's own continuity: Postgres nova_ops, the memory server,
                               gateway/chat (and the inference path chat needs)
  Tier 1  RTO 4h,  RPO 24h   — the platform around her: both schedulers, alert delivery,
                               HomeKit/Home Assistant/house control, backups, monitoring, security
  Tier 2  RTO 72h, RPO 7d    — media, journal, analytics, ingest, personality organs

Storage: nova_ops.service_tiers (component -> tier + rto_s/rpo_s + note). A component is an exact
name (task id, service name, script stem) or a glob pattern ("journal_*"). Lookup: exact match
first, then the LONGEST matching pattern (a tie goes to the stricter tier). A dedicated table rather than columns on
service_registry because tiers apply to scheduled tasks and scripts too, and service_registry is a
per-node heartbeat table rewritten by the health probes.

Consumers:
  nova_prr.py           check "tier": a component with no tier FAILS (Nova assigns one with --set)
  nova_restore_drill.py records measured RTA/RPA against its Tier-0 targets in restore_drills

CLI:
  nova_service_tiers.py --list                 every row
  nova_service_tiers.py --lookup NAME          which tier NAME resolves to (exit 1 if none)
  nova_service_tiers.py --set NAME TIER [--note TEXT]   upsert (Nova's own call — no question to Jordan)

Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TIERS = {  # tier -> (rto_s, rpo_s)
    0: (3600, 900),
    1: (4 * 3600, 24 * 3600),
    2: (72 * 3600, 7 * 86400),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_tiers (
    component  text PRIMARY KEY,
    tier       smallint NOT NULL CHECK (tier BETWEEN 0 AND 2),
    rto_s      integer NOT NULL,
    rpo_s      integer NOT NULL,
    note       text,
    updated_by text NOT NULL DEFAULT 'system',
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

_T0 = ["postgresql", "postgresql_replica", "pg-primary", "postgres", "nova_ops", "memory_server",
       "nova_memory_server", "memory-server", "gateway", "nova_gateway", "chat", "chat_jordan",
       "inference_router", "nova_inference_router", "ollama", "llm:ollama", "haproxy_lb", "redis",
       "pg_backup", "restore_drill", "wal_archive*", "replication_monitor", "core_liveness",
       "dead_mans_switch"]
_T1 = ["scheduler", "scheduler-core", "nova_scheduler", "task_sentinel", "notify_jordan",
       "nova_notifier", "nova_notify", "alert_*", "slo", "prober", "canary", "endpoint_monitor",
       "service_down_watch", "session_watchdog", "app_watchdog", "health_check", "cert_monitor",
       "incident_*", "auto_postmortem", "hotwash_*", "morning_brief", "novahomekit", "homebridge",
       "hue", "hue_bridge", "presence_engine", "home_*", "homekit_*", "ha_metrics", "house_facts",
       "boiler", "big_brother", "security_*", "wazuh_*", "syslog", "syslog_*", "snmp_poller",
       "network_health", "wan_monitor", "unifi_*", "wifi_guard", "rogue_ap_sentinel",
       "iot_egress_watch", "watchtower", "cve_autopatch", "pkg_audit", "backup_*", "nas_*",
       "synology_monitor", "unas_*", "pg_maintain", "pg_stat_poller", "partition_ensure",
       "retention", "log_rotate", "disk_forecast", "storage_metrics", "memory_health",
       "memory_verify", "memory_anchor", "index_integrity", "vector_audit", "claude_*",
       "config_drift", "chronic_failures", "dead_letter_replay", "queue_aging", "crontab_audit",
       "llm:mlx", "llm:llamacpp", "mlx_server", "llama_server", "model_warm", "ollama_preload",
       "llm_ping", "llm_budget_guard", "openrouter_credit_watch", "op_sync", "dns_sync",
       "component_metrics", "watcher_engine", "ae35_audit", "self_justification_audit",
       "output_drift", "sre_postmortem", "toil_*", "prr_review"]
_T2 = ["plex", "plex_*", "frigate", "tinychat", "searxng", "grafana", "novacontrol_web", "browser",
       "sds200", "journal_*", "weekly_journal", "livetv_*", "yt_*", "tv_ingest", "*_ingest",
       "analytics_*", "weekly_*", "nightly_media", "daily_news_*", "osint_*", "rando_*",
       "imagination_*", "predictions_*", "nova_growth_*", "mesh_*", "meshtastic_watch", "local_*",
       "scanner_digest", "face_*", "protect_monitor", "nightly_protect", "weight_of_*",
       "learning_*", "turing_*", "mail_deliver_*", "context_bridge_*", "watch_turnover_*"]
SEED = ([(c, 0) for c in _T0] + [(c, 1) for c in _T1] + [(c, 2) for c in _T2])


def targets(tier: int) -> tuple[int, int]:
    """tier -> (rto_s, rpo_s). Pure."""
    return TIERS[int(tier)]


def _variants(names) -> list[str]:
    out = []
    for n in names:
        if not n:
            continue
        n = str(n)
        stem = n.rsplit("/", 1)[-1]
        stem = stem[:-3] if stem.endswith((".py", ".sh")) else stem
        for v in (n, stem, stem[5:] if stem.startswith("nova_") else stem):
            if v not in out:
                out.append(v)
    return out


def tier_for(names, rows) -> dict | None:
    """names: candidate names for one component (task id, service, script). rows: dicts with
    component/tier/rto_s/rpo_s. Exact match wins, else the longest matching glob. Pure."""
    cands = _variants(names if isinstance(names, (list, tuple)) else [names])
    by = {r["component"]: r for r in rows}
    for c in cands:
        if c in by and not any(ch in c for ch in "*?["):
            return by[c]
    globs = sorted((r for r in rows if any(ch in r["component"] for ch in "*?[")),
                   key=lambda r: (-len(r["component"]), r["tier"], r["component"]))   # tie -> stricter tier
    for r in globs:
        if any(fnmatch.fnmatchcase(c, r["component"]) for c in cands):
            return r
    return None


def ensure(cur) -> None:
    """Create the table (lock-safe) and seed defaults once; later edits live in the table."""
    from nova_pg_schema import safe_ddl
    safe_ddl(cur, SCHEMA)
    for comp, tier in SEED:
        rto, rpo = targets(tier)
        cur.execute("INSERT INTO service_tiers (component, tier, rto_s, rpo_s, note) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (component) DO NOTHING", (comp, tier, rto, rpo, "seed 2026-10-15 (#35)"))


def load(cur) -> list[dict] | None:
    """All rows, or None when the table does not exist yet."""
    cur.execute("SELECT to_regclass('public.service_tiers') IS NOT NULL")
    if not cur.fetchone()[0]:
        return None
    cur.execute("SELECT component, tier, rto_s, rpo_s, note FROM service_tiers")
    return [dict(component=c, tier=t, rto_s=rto, rpo_s=rpo, note=n) for c, t, rto, rpo, n in cur.fetchall()]


def set_tier(cur, component: str, tier: int, note: str | None = None, by: str = "nova") -> None:
    rto, rpo = targets(tier)
    cur.execute("INSERT INTO service_tiers (component, tier, rto_s, rpo_s, note, updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (component) DO UPDATE SET tier=EXCLUDED.tier, "
                "rto_s=EXCLUDED.rto_s, rpo_s=EXCLUDED.rpo_s, note=EXCLUDED.note, "
                "updated_by=EXCLUDED.updated_by, updated_at=now()", (component, tier, rto, rpo, note, by))


def _fmt_s(s: int) -> str:
    return f"{s // 86400}d" if s >= 86400 and s % 86400 == 0 else (f"{s // 3600}h" if s >= 3600 else f"{s // 60}m")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova service tiers (RTO/RPO)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--lookup")
    ap.add_argument("--set", nargs=2, metavar=("COMPONENT", "TIER"))
    ap.add_argument("--note")
    a = ap.parse_args(argv)
    import nova_dsn
    conn = nova_dsn.pg_connect("nova_ops")
    conn.autocommit = True
    cur = conn.cursor()
    ensure(cur)
    if a.set:
        comp, tier = a.set[0], int(a.set[1])
        if tier not in TIERS:
            print("tier must be 0, 1 or 2", file=sys.stderr)
            return 2
        set_tier(cur, comp, tier, a.note)
        print(f"{comp} -> Tier{tier}")
        return 0
    rows = load(cur) or []
    if a.lookup:
        r = tier_for([a.lookup], rows)
        if not r:
            print(f"{a.lookup}: no tier")
            return 1
        print(f"{a.lookup}: Tier{r['tier']} via {r['component']} (RTO {_fmt_s(r['rto_s'])}, RPO {_fmt_s(r['rpo_s'])})")
        return 0
    for r in sorted(rows, key=lambda r: (r["tier"], r["component"])):
        print(f"Tier{r['tier']}  RTO {_fmt_s(r['rto_s']):>4}  RPO {_fmt_s(r['rpo_s']):>4}  {r['component']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
