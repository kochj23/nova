#!/usr/bin/env python3
"""nova_grafana_coverage.py — generate Grafana dashboards for Nova's newer tools (Jordan 2026-10-12).

70 tools added in the 3 weeks to 2026-10-12 wrote to Postgres and no dashboard queried any of
their tables. This builds one provisioned dashboard per area from the live schema: for every table a
collapsed row with "new rows (24h)", "rows per hour (7d)" and "latest rows" (a few short columns,
long text truncated). The Radio & Wi-Fi dashboard also gets purpose-built panels up top.

Output: grafana/sources/nova-tools-<area>.json in the repo (NAS share). Since 2026-10-13 these are SOURCES:
nova_grafana_consolidate.py folds them into the big pages in grafana/dashboards/, which are what get deployed to
nova-core's ~/grafana/dashboards -- with mode 644 (install -m 644 ...): scp keeps the share's 0700 and Grafana
(uid 472) then skips the file silently. Re-run after adding a tool: put its
tables in GROUPS.

  nova_grafana_coverage.py [--out DIR]
"""
import json, os, sys

DS = {"type": "grafana-postgresql-datasource", "uid": "nova-ops-pg"}
TIME_COLS = ("ts", "created_at", "started_at", "run_at", "checked_at", "recorded_at", "detected_at",
             "seen_at", "at", "updated_at", "date", "day", "week")
NEVER_SHOW = {"private_lexicon", "legacy_api_keys", "lockbox"}       # private / credential-adjacent
GROUPS = {
    "radio-wifi": ("Radio & Wi-Fi", ["sdr_targets", "sdr_tuners", "sdr_assignments", "sdr_health", "sdr_placement_log",
                                     "wifi_survey_runs", "wifi_survey", "wifi_guard_findings"]),
    "watch": ("Watch organs", ["bodach_scores", "shine_log", "shine_contacts", "derry_monthly", "unexplained_events",
                               "pendulum_blades", "federal_hill_lights", "crain_square_residuals", "earth_box_burials",
                               "rama_window", "births", "jade_amulet_manifest", "ivory_head_flags", "ivory_head_weekly",
                               "spectroscope_runs", "charles_runs", "chandra_runs", "card_rack_findings", "valdemar_holds",
                               "threshold_ledger", "ae35_events", "peaslee_chain", "peaslee_roots", "usher_fissure",
                               "busab_weekly", "evitable_conflict_results", "watch_turnover", "hotwash"]),
    "self": ("Self & reflection", ["relationship_ledger", "good_things", "hard_stretch", "commanders_intent",
                                   "intent_reasoning_log", "directive_conflicts", "directives", "directive_conflict_reviews",
                                   "reflection_questions", "memory_anchors", "time_sense", "self_justification_audit",
                                   "butlerian_drills", "butlerian_weekly", "ideal_reader_darlings", "ideal_reader_log",
                                   "nova_care_checkins", "prediction_axioms", "nova_skills", "nova_skill_runs",
                                   "pursuit_threads", "research_log", "contact_sense", "ghola_drill", "exercise_results"]),
    "ops-safety": ("Ops & safety", ["action_audit", "outbound_ledger", "escalation_log", "restraint_ledger",
                                    "safety_confirmations", "claude_reviewer_log", "self_repair_digest_log", "tma1_checks",
                                    "leader_standby", "doorstep_runs", "boiler_state", "bottle_log", "house_facts",
                                    "source_ledger", "source_ledger_history", "source_outcomes"]),
    "unifi": ("UniFi — Threat Watch, Stalker, House Arrest", ["unifi_threat_events", "unifi_threat_ignore",
                                                             "unifi_client_events", "unifi_client_state",
                                                             "unifi_client_presence", "unifi_tracked_devices",
                                                             "unifi_network_audit", "unifi_toolkit_ledger",
                                                             "unifi_toolkit_actions"]),
    "media-ingest": ("Media & ingest", ["ia_ingest_seen", "yt_ingest_seen", "nova_speaks_hosts", "nova_speaks_renders",
                                        "nova_speaks_rewrites", "face_retention_log", "nova_voice_room_log"]),
}


def target(sql, fmt="table"):
    return [{"refId": "A", "datasource": DS, "rawQuery": True, "editorMode": "code", "format": fmt, "rawSql": sql}]


def panel(kind, title, sql, x, y, w, h, fmt="table"):
    p = {"type": kind, "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": target(sql, fmt), "options": {}, "fieldConfig": {"defaults": {}, "overrides": []}}
    if kind == "table":
        p["options"] = {"showHeader": True, "cellHeight": "sm"}
    return p


def table_panels(cur, table, y):
    cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position""", (table,))
    cols = cur.fetchall()
    if not cols:
        return None
    types = dict(cols)
    tcol = next((c for c in TIME_COLS if c in types and ("time" in types[c] or types[c] == "date")), None)
    show = [c for c, t in cols if t not in ("bytea",) and c not in ("embedding",)][:8]
    sel = ", ".join(f'left("{c}"::text, 120) AS "{c}"' if types[c] in ("text", "jsonb", "json", "ARRAY", "character varying")
                    else f'"{c}"' for c in show)
    order = f' ORDER BY "{tcol}" DESC' if tcol else ""
    inner = [panel("table", f"{table} — latest rows", f'SELECT {sel} FROM "{table}"{order} LIMIT 20', 0, y + 1, 24, 8)]
    if tcol:
        inner += [panel("stat", f"{table} — new rows (24h)",
                        f"SELECT count(*) FROM \"{table}\" WHERE \"{tcol}\" > now() - interval '24 hours'", 0, y + 9, 6, 5),
                  panel("timeseries", f"{table} — rows per hour",
                        f'SELECT $__timeGroupAlias("{tcol}", 1h), count(*) AS rows FROM "{table}" '
                        f'WHERE $__timeFilter("{tcol}") GROUP BY 1 ORDER BY 1', 6, y + 9, 18, 5, "time_series")]
    else:
        inner.append(panel("stat", f"{table} — total rows", f'SELECT count(*) FROM "{table}"', 0, y + 9, 6, 5))
    return {"type": "row", "title": f"{table}" + ("" if tcol else " (no time column)"), "collapsed": True,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": inner}


def radio_wifi_top():
    return [
        panel("table", "SDR — who is listening where (live)",
              "SELECT t.priority, t.label, t.freq_hz/1e6 AS mhz, t.demod, a.tuner_id, a.reason, a.since "
              "FROM sdr_assignments a JOIN sdr_targets t ON t.id = a.target_id ORDER BY t.priority DESC", 0, 0, 14, 8),
        panel("table", "SDR — tuner health",
              "SELECT tuner_id, ok, detail, ts, unhealthy_since FROM sdr_health ORDER BY tuner_id", 14, 0, 10, 8),
        panel("timeseries", "SDR — automatic moves",
              "SELECT $__timeGroupAlias(ts, 1h), count(*) FILTER (WHERE action='apply') AS moves, "
              "count(*) FILTER (WHERE action='advise') AS advised FROM sdr_placement_log WHERE $__timeFilter(ts) "
              "GROUP BY 1 ORDER BY 1", 0, 8, 12, 7, "time_series"),
        panel("timeseries", "Wi-Fi — access points and clients heard",
              "SELECT $__time(ts), aps, stations FROM wifi_survey_runs WHERE $__timeFilter(ts) ORDER BY 1",
              12, 8, 12, 7, "time_series"),
        panel("timeseries", "Wi-Fi — deauth/disassoc frames per survey",
              "SELECT $__time(ts), coalesce((SELECT sum(v::int) FROM jsonb_each_text(deauth) e(k, v)), 0) AS frames "
              "FROM wifi_survey_runs WHERE $__timeFilter(ts) ORDER BY 1", 0, 15, 12, 7, "time_series"),
        panel("table", "Wi-Fi guard findings (evil twin / deauth flood)",
              "SELECT ts, kind, bssid, detail FROM wifi_guard_findings ORDER BY ts DESC LIMIT 50", 12, 15, 12, 7),
    ]


def unifi_top():
    """Network Pulse / Threat Watch / Stalker / House Arrest views (nova_unifi_toolkit.py, 2026-10-13)."""
    return [
        panel("stat", "IPS events (24h)", "SELECT count(*) FROM unifi_threat_events WHERE ts > now() - interval '24 hours' "
              "AND NOT ignored", 0, 0, 4, 4),
        panel("stat", "High-risk, NOT blocked (24h)", "SELECT count(*) FROM unifi_threat_events WHERE ts > now() - "
              "interval '24 hours' AND risk='high' AND action <> 'block' AND NOT ignored", 4, 0, 4, 4),
        panel("stat", "Clients online", "SELECT count(*) FROM unifi_client_state WHERE online", 8, 0, 4, 4),
        panel("stat", "Roams (24h)", "SELECT count(*) FROM unifi_client_events WHERE kind='roam' AND ts > now() - "
              "interval '24 hours'", 12, 0, 4, 4),
        panel("stat", "House Arrest rules active", "SELECT count(*) FROM unifi_toolkit_ledger WHERE kind IN "
              "('policy','acl') AND released_at IS NULL", 16, 0, 4, 4),
        panel("stat", "Posture findings (latest audit)", "SELECT jsonb_array_length(findings) FROM unifi_network_audit "
              "ORDER BY ts DESC LIMIT 1", 20, 0, 4, 4),
        panel("timeseries", "Threat Watch — IPS events by risk",
              "SELECT $__timeGroupAlias(ts, 1h), count(*) FILTER (WHERE risk='high') AS high, count(*) FILTER "
              "(WHERE risk='medium') AS medium, count(*) FILTER (WHERE risk='low') AS low FROM unifi_threat_events "
              "WHERE $__timeFilter(ts) AND NOT ignored GROUP BY 1 ORDER BY 1", 0, 4, 12, 8, "time_series"),
        panel("table", "Threat Watch — top attackers (7d)",
              "SELECT src_ip::text AS source, max(src_country) AS cc, count(*) AS events, max(risk) AS worst, "
              "count(*) FILTER (WHERE action <> 'block') AS not_blocked, max(ts) AS last FROM unifi_threat_events "
              "WHERE ts > now() - interval '7 days' AND NOT ignored GROUP BY 1 ORDER BY 3 DESC LIMIT 15", 12, 4, 12, 8),
        panel("table", "Threat Watch — latest events",
              "SELECT ts, risk, action, left(signature, 90) AS signature, src_ip::text AS src, src_country AS cc, "
              "dst_ip::text AS dst, dst_port FROM unifi_threat_events WHERE NOT ignored ORDER BY ts DESC LIMIT 50",
              0, 12, 24, 8),
        panel("timeseries", "Network Pulse — distinct clients per hour",
              "SELECT $__timeGroupAlias(hour, 1h), count(DISTINCT mac) AS clients FROM unifi_client_presence "
              "WHERE $__timeFilter(hour) GROUP BY 1 ORDER BY 1", 0, 20, 12, 7, "time_series"),
        panel("table", "Network Pulse — clients per AP / port (now)",
              "SELECT ap, count(*) AS clients, count(*) FILTER (WHERE NOT is_wired) AS wifi FROM unifi_client_state "
              "WHERE online GROUP BY 1 ORDER BY 2 DESC", 12, 20, 12, 7),
        panel("table", "Wi-Fi Stalker — tracked devices",
              "SELECT t.label, t.mac, s.online, s.ap, s.essid, s.signal, s.ip, s.last_seen FROM unifi_tracked_devices t "
              "LEFT JOIN unifi_client_state s USING (mac) ORDER BY t.label", 0, 27, 12, 7),
        panel("table", "Wi-Fi Stalker — latest connect / roam / leave",
              "SELECT e.ts, e.kind, coalesce(s.name, s.hostname, e.mac) AS device, e.from_ap, e.to_ap FROM "
              "unifi_client_events e LEFT JOIN unifi_client_state s USING (mac) ORDER BY e.ts DESC LIMIT 50", 12, 27, 12, 7),
        panel("table", "House Arrest — network posture findings (latest audit)",
              "SELECT f->>'severity' AS severity, f->>'where' AS network, f->>'finding' AS finding, f->>'detail' AS detail "
              "FROM (SELECT findings FROM unifi_network_audit ORDER BY ts DESC LIMIT 1) a, jsonb_array_elements(a.findings) f",
              0, 34, 12, 7),
        panel("table", "House Arrest — every write (and preview)",
              "SELECT ts, action, dry_run, ok, actor, left(args::text, 120) AS args FROM unifi_toolkit_actions "
              "ORDER BY ts DESC LIMIT 30", 12, 34, 12, 7),
    ]


def build(cur, key, title, tables):
    panels = radio_wifi_top() if key == "radio-wifi" else unifi_top() if key == "unifi" else []
    y = max((p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels), default=0)
    missing = []
    for t in tables:
        if t in NEVER_SHOW:
            continue
        row = table_panels(cur, t, y)
        if row is None:
            missing.append(t); continue
        panels.append(row); y += 1
    pid = 1
    for p in panels:
        p["id"] = pid; pid += 1
        for q in p.get("panels", []):
            q["id"] = pid; pid += 1
    dash = {"uid": f"nova-tools-{key}", "title": f"Nova — {title}", "tags": ["nova", "nova-tools", "generated"],
            "timezone": "browser", "schemaVersion": 39, "time": {"from": "now-7d", "to": "now"},
            "refresh": "5m", "panels": panels, "editable": True,
            "description": "Generated by nova_grafana_coverage.py — edit GROUPS there, not this file."
                           + (" Feature design: Crosstalk Solutions' UI Toolkit (MIT) — "
                              "https://github.com/Crosstalk-Solutions/unifi-toolkit" if key == "unifi" else "")}
    return dash, missing


def main():
    import psycopg2
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "grafana", "sources")   # -> nova_grafana_consolidate.py
    try:
        import nova_dsn; dsn = nova_dsn.pg_dsn("nova_ops")
    except ImportError:
        dsn = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
    cur = psycopg2.connect(dsn).cursor()
    for key, (title, tables) in GROUPS.items():
        dash, missing = build(cur, key, title, tables)
        path = os.path.join(out, f"nova-tools-{key}.json")
        json.dump(dash, open(path, "w"), indent=1)
        rows = sum(1 for p in dash["panels"] if p["type"] == "row")
        print(f"[grafana-coverage] {path}: {rows} tables" + (f" (no such table: {', '.join(missing)})" if missing else ""))


if __name__ == "__main__":
    main()
