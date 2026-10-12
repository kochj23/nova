#!/usr/bin/env python3
"""nova_grafana_coverage.py — generate Grafana dashboards for Nova's newer tools (Jordan 2026-10-12).

70 tools added in the 3 weeks to 2026-10-12 wrote to Postgres and no dashboard queried any of
their tables. This builds one provisioned dashboard per area from the live schema: for every table a
collapsed row with "new rows (24h)", "rows per hour (7d)" and "latest rows" (a few short columns,
long text truncated). The Radio & Wi-Fi dashboard also gets purpose-built panels up top.

Output: grafana/dashboards/nova-tools-<area>.json in the repo (NAS share); copy to nova-core's
~/grafana/dashboards for Grafana to pick up -- with mode 644 (install -m 644 ...): scp keeps the
share's 0700 and Grafana (uid 472) then skips the file silently. Re-run after adding a tool: put its
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


def build(cur, key, title, tables):
    panels = radio_wifi_top() if key == "radio-wifi" else []
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
            "description": "Generated by nova_grafana_coverage.py — edit GROUPS there, not this file."}
    return dash, missing


def main():
    import psycopg2
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "grafana", "dashboards")
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
