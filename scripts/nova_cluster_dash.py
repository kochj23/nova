#!/usr/bin/env python3
"""Generate the Grafana 13 "Nova Cluster" dashboard (uid nova-cluster), designed for a 1920x1080 kiosk.
Usage: nova_cluster_dash.py sql   -> prints every rawSql (for psql testing)
       nova_cluster_dash.py json  -> prints the POST /api/dashboards/db body
Grid: 24 cols x 27 rows (title 2 + stat tiles 5 + fleet 9 + detail 11) = 1018 px at 38 px/row, no scroll at 1080p.
"""
import json, sys

OPS = {"type": "grafana-postgresql-datasource", "uid": "nova-ops-pg"}
VIOLET, CYAN, AMBER, RED = "#8b5cf6", "#22d3ee", "#f59e0b", "#ed8796"
GOOD, WARN, BAD, NEUTRAL = CYAN, AMBER, RED, VIOLET
# fixed categorical order for per-node series (identity never cycles)
NODE_ORDER = ["nova-core8/.6", "nova-core2/.86", "nova-core3/.5", "nova-core6/.252", "nova-core7/.125", "nova-core9/.7", "nova-core10/.77"]
NODE_COLORS = [VIOLET, CYAN, "#c6a0f6", "#8aadf4", "#f5a97f", "#eed49f", "#b7bdf8"]

_pid = [0]
def pid():
    _pid[0] += 1
    return _pid[0]

def target(sql, fmt="table", ref="A"):
    return {"datasource": OPS, "refId": ref, "rawQuery": True, "editorMode": "code", "format": fmt, "rawSql": sql}

def thr(*steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}

def vmap(d):
    """d: value -> (text, color)"""
    return [{"type": "value", "options": {str(k): {"text": t, "color": c, "index": i} for i, (k, (t, c)) in enumerate(d.items())}}]

def base(ptype, title, x, y, w, h, targets, defaults, overrides, options, desc=""):
    return {"id": pid(), "type": ptype, "title": title, "description": desc, "datasource": OPS, "transparent": True,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides}, "options": options}

def tile(title, sql, x, y, w, h, t, unit="short", decimals=0, spark=True, desc="", mappings=None, fmt="time_series"):
    """big stat tile: value + sparkline, gradient background coloured by thresholds"""
    d = {"color": {"mode": "thresholds"}, "thresholds": t, "unit": unit, "decimals": decimals, "mappings": mappings or [], "min": 0}
    p = base("stat", title, x, y, w, h, [target(sql, fmt)], d, [],
                {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                 "colorMode": "background", "graphMode": "area" if spark else "none", "textMode": "value",
                 "justifyMode": "center", "orientation": "auto", "wideLayout": True, "showPercentChange": False}, desc)
    p["transparent"] = False
    return p

def wall(title, sql, x, y, w, h, t, unit=None, mappings=None, text_mode="value_and_name", color_mode="background_gradient", desc=""):
    """status wall: one tile per row of the result (name = text column, value = numeric column)"""
    d = {"color": {"mode": "thresholds"}, "thresholds": t, "mappings": mappings or [], "decimals": 0}
    if unit: d["unit"] = unit
    return base("stat", title, x, y, w, h, [target(sql, "table")], d, [],
                {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
                 "colorMode": color_mode, "graphMode": "none", "textMode": text_mode, "justifyMode": "center",
                 "orientation": "auto", "wideLayout": True, "text": {"titleSize": 11, "valueSize": 14}}, desc)

def ov(field, *props):
    return {"matcher": {"id": "byName", "options": field}, "properties": [{"id": k, "value": v} for k, v in props]}

def cell_bg(field, t, unit=None, mappings=None, width=None):
    p = [("custom.cellOptions", {"type": "color-background", "mode": "gradient"}), ("thresholds", t), ("color", {"mode": "thresholds"})]
    if unit: p.append(("unit", unit))
    if mappings: p.append(("mappings", mappings))
    if width: p.append(("custom.width", width))
    return ov(field, *p)

def cell_text(field, t, unit=None, mappings=None, width=None):
    p = [("custom.cellOptions", {"type": "color-text"}), ("thresholds", t), ("color", {"mode": "thresholds"})]
    if unit: p.append(("unit", unit))
    if mappings: p.append(("mappings", mappings))
    if width: p.append(("custom.width", width))
    return ov(field, *p)

def cell_gauge(field, t, maxv=100, unit="percent", width=None):
    p = [("custom.cellOptions", {"type": "gauge", "mode": "gradient", "valueDisplayMode": "text"}), ("min", 0), ("max", maxv),
         ("unit", unit), ("thresholds", t), ("color", {"mode": "thresholds"}), ("decimals", 0)]
    if width: p.append(("custom.width", width))
    return ov(field, *p)

def table(title, sql, x, y, w, h, overrides, desc="", sort=None):
    d = {"color": {"mode": "fixed", "fixedColor": "text"}, "thresholds": thr((None, "text")), "decimals": 0,
         "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": False, "inspect": False}}
    return base("table", title, x, y, w, h, [target(sql, "table")], d, overrides,
                {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["count"], "countRows": False},
                 "sortBy": sort or []}, desc)

def tseries(title, sql, x, y, w, h, unit, overrides, legend=True, bars=False, stacked=False, time_from=None, desc=""):
    custom = {"lineWidth": 2, "fillOpacity": 25, "gradientMode": "scheme", "lineInterpolation": "smooth", "showPoints": "never",
              "spanNulls": True, "axisBorderShow": False, "axisGridShow": True, "pointSize": 4}
    if bars:
        custom.update({"drawStyle": "bars", "fillOpacity": 70, "lineWidth": 1, "barAlignment": 0})
    if stacked:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    d = {"unit": unit, "decimals": 0, "min": 0, "color": {"mode": "fixed", "fixedColor": VIOLET}, "custom": custom}
    p = base("timeseries", title, x, y, w, h, [target(sql, "time_series")], d, overrides,
             {"legend": {"displayMode": "list" if legend else "hidden", "placement": "bottom", "showLegend": legend, "calcs": []},
              "tooltip": {"mode": "multi", "sort": "desc"}}, desc)
    if time_from: p["timeFrom"] = time_from
    return p

def fixed_color(name, color):
    return ov(name, ("color", {"mode": "fixed", "fixedColor": color}))

AGE_T = thr((None, GOOD), (120, WARN), (600, BAD))          # heartbeat age seconds
PCT_T = thr((None, GOOD), (70, WARN), (90, BAD))
STATUS_MAP = vmap({"up": ("UP", GOOD), "down": ("DOWN", BAD), "slow": ("SLOW", WARN), "streaming": ("streaming", GOOD)})

# ------------------------------------------------------------------ SQL -----
SQL = {}
SQL["status_line"] = """WITH n AS (SELECT count(*) alive FROM node_status WHERE last_heartbeat > now()-interval '3 minutes'),
s AS (SELECT count(*) FILTER (WHERE status='up' AND last_heartbeat > now()-interval '3 minutes') up, count(*) total FROM service_registry),
l AS (SELECT count(*) FILTER (WHERE status='up') up, count(*) total FROM (SELECT DISTINCT ON (node_name, service_name) status FROM health_checks
      WHERE checked_by='nova_llm_ping' AND checked_at > now()-interval '30 minutes' ORDER BY node_name, service_name, checked_at DESC) t),
i AS (SELECT count(*) open, count(*) FILTER (WHERE severity='critical') crit FROM telemetry.incidents WHERE status='open'),
r AS (SELECT count(*) FILTER (WHERE state='streaming') ok, round(coalesce(max(extract(epoch FROM replay_lag)),0)*1000) lag FROM pg_stat_replication)
SELECT CASE WHEN i.crit > 0 OR r.ok < 3 OR n.alive < 8 THEN 'CRITICAL' WHEN i.open > 0 OR s.up < s.total OR l.up < l.total OR n.alive < 10 THEN 'DEGRADED' ELSE 'NOMINAL' END
       || '  ·  ' || n.alive || '/10 nodes  ·  ' || s.up || '/' || s.total || ' services  ·  ' || l.up || '/' || l.total || ' LLM  ·  '
       || i.open || ' open incidents, ' || i.crit || ' crit  ·  ' || r.ok || '/3 standbys, lag ' || CASE WHEN r.lag >= 1000 THEN round(r.lag/1000.0,1) || ' s' ELSE r.lag || ' ms' END AS status
FROM n, s, l, i, r"""
SQL["clock"] = """SELECT to_char(now() AT TIME ZONE 'America/Los_Angeles', 'Dy DD Mon  HH24:MI') AS clock"""

SQL["nodes_alive"] = """SELECT count(*) AS "nodes alive" FROM node_status WHERE last_heartbeat > now()-interval '3 minutes'"""
SQL["services_up_ts"] = """SELECT g AS time,
  (SELECT count(*) FILTER (WHERE status='up') FROM (SELECT DISTINCT ON (service_name) status FROM health_checks h
     WHERE h.checked_by='mac-studio' AND h.checked_at > g - interval '10 minutes' AND h.checked_at <= g ORDER BY service_name, checked_at DESC) x) AS "services up"
FROM (SELECT generate_series(date_trunc('minute', $__timeFrom()::timestamptz), $__timeTo()::timestamptz, interval '5 minutes') UNION SELECT $__timeTo()::timestamptz) s(g) ORDER BY 1"""
SQL["llm_healthy_ts"] = """SELECT $__timeGroupAlias(checked_at,'15m'), count(DISTINCT node_name||service_name) FILTER (WHERE status='up') AS "LLM healthy"
FROM health_checks WHERE $__timeFilter(checked_at) AND checked_by='nova_llm_ping' GROUP BY 1 ORDER BY 1"""
SQL["incidents_ts"] = """SELECT g AS time, (SELECT count(*) FROM telemetry.incidents i WHERE i.opened_at <= g AND (i.resolved_at IS NULL OR i.resolved_at > g)) AS "open incidents"
FROM (SELECT generate_series(date_trunc('minute', $__timeFrom()::timestamptz), $__timeTo()::timestamptz, interval '10 minutes') UNION SELECT $__timeTo()::timestamptz) s(g) ORDER BY 1"""
SQL["lag_ts"] = """SELECT $__timeGroupAlias(ts,'2m'), max(replay_lag_ms) AS "replay lag" FROM telemetry.replication_health WHERE $__timeFilter(ts) GROUP BY 1 ORDER BY 1"""
SQL["memories_ts"] = """SELECT $__timeGroupAlias(ts,'5m'), max(value) AS memories FROM telemetry.nova_meta WHERE $__timeFilter(ts) AND metric='memories_total' GROUP BY 1 ORDER BY 1"""

SQL["fleet"] = """SELECT node_name AS "node", host(node_ip) AS "ip", os_family AS "os", cpu_cores AS "cores", ram_gb AS "ram",
       status, round(extract(epoch FROM now()-last_heartbeat))::int AS "heartbeat",
       round(load_avg_1m::numeric,1) AS "load1", round((load_avg_1m / greatest(cpu_cores,1) * 100)::numeric)::int AS "load",
       round(memory_percent::numeric)::int AS "memory", round(disk_percent::numeric)::int AS "disk"
FROM node_status ORDER BY node_name"""
SQL["sr_nodes"] = """SELECT node_name AS "node", count(*) AS "svcs", round(extract(epoch FROM now()-max(last_heartbeat)))::int AS "heartbeat"
FROM service_registry GROUP BY node_name ORDER BY max(last_heartbeat) ASC, node_name"""
SQL["services_wall"] = """SELECT service_name || ' @' || replace(replace(replace(node_name,'nova-',''),'mac-',''),'tv-movies-mini','tvmini') AS name,
       CASE WHEN status <> 'up' THEN -1 ELSE round(extract(epoch FROM now()-last_heartbeat))::int END AS age
FROM service_registry ORDER BY status='up', last_heartbeat ASC, service_name"""

SQL["llm_ts"] = """SELECT checked_at AS time, node_name AS metric, latency_ms AS value
FROM health_checks WHERE checked_by='nova_llm_ping' AND $__timeFilter(checked_at) AND latency_ms IS NOT NULL ORDER BY 1"""
SQL["llm_wall"] = """SELECT DISTINCT ON (node_name, service_name) node_name || '  ·  ' || replace(service_name,'llm:','') AS name,
       CASE WHEN status='up' THEN latency_ms ELSE -1 END AS latency
FROM health_checks WHERE checked_by='nova_llm_ping' AND checked_at > now()-interval '6 hours'
ORDER BY node_name, service_name, checked_at DESC"""

SQL["events_ts"] = """SELECT $__timeGroupAlias(ts,'10m'), level AS metric, count(*) AS value
FROM telemetry.events WHERE $__timeFilter(ts) AND level IN ('warning','critical') GROUP BY 1,2 ORDER BY 1"""
SQL["incidents"] = """SELECT severity, host, left(title,64) AS title, round(extract(epoch FROM now()-opened_at)/60)::int AS "open"
FROM telemetry.incidents WHERE status='open' ORDER BY severity='critical' DESC, opened_at DESC LIMIT 6"""

SQL["replication"] = """SELECT host(client_addr) AS standby, round(extract(epoch FROM coalesce(replay_lag, interval '0'))::numeric*1000,1) AS "replay lag"
FROM pg_stat_replication WHERE state='streaming' ORDER BY client_addr"""
SQL["sched_fail"] = """SELECT task_id AS task, count(*) FILTER (WHERE status IN ('failure','timeout')) AS "fails 24h"
FROM scheduler_runs WHERE started_at > (extract(epoch FROM now())-86400)*1000
GROUP BY task_id HAVING count(*) FILTER (WHERE status IN ('failure','timeout')) > 0 ORDER BY 2 DESC, 1 LIMIT 6"""
SQL["health_wall"] = """WITH latest AS (SELECT DISTINCT ON (service_name, node_name) service_name, node_name, status
  FROM health_checks WHERE checked_at > now()-interval '1 hour' AND checked_by <> 'nova_llm_ping' ORDER BY service_name, node_name, checked_at DESC)
SELECT (SELECT count(*) FILTER (WHERE status='up') FROM latest) || ' / ' || (SELECT count(*) FROM latest) || ' checks up' AS name,
       CASE WHEN (SELECT count(*) FROM latest WHERE status='down') > 0 THEN 0 WHEN (SELECT count(*) FROM latest WHERE status<>'up') > 0 THEN 2 ELSE 1 END AS v
UNION ALL (SELECT service_name, CASE status WHEN 'slow' THEN 2 ELSE 0 END FROM latest WHERE status <> 'up' ORDER BY status, service_name)"""

# --------------------------------------------------------------- panels -----
TITLE_HTML = """<div style="display:flex;align-items:baseline;gap:18px;padding:2px 6px 0 6px;font-family:Inter,system-ui,sans-serif">
<span style="font-size:34px;font-weight:800;letter-spacing:.06em;color:#cad3f5">NOVA</span>
<span style="font-size:34px;font-weight:300;color:#8b5cf6">&mdash;</span>
<span style="font-size:26px;font-weight:500;letter-spacing:.14em;color:#22d3ee">CLUSTER</span>
<span style="font-size:12px;color:#8087a2;margin-left:auto">pg-primary &middot; nova_ops &middot; refresh 30 s</span>
</div>"""

def build():
    P = []
    # ---- title row (y 0-2)
    P.append({"id": pid(), "type": "text", "title": "", "transparent": True, "gridPos": {"x": 0, "y": 0, "w": 7, "h": 2},
              "options": {"mode": "html", "content": TITLE_HTML}})
    P.append(base("stat", "", 7, 0, 13, 2, [target(SQL["status_line"])],
                  {"color": {"mode": "thresholds"}, "thresholds": thr((None, NEUTRAL)),
                   "mappings": [{"type": "regex", "options": {"pattern": "^NOMINAL.*", "result": {"color": GOOD, "index": 0}}},
                                {"type": "regex", "options": {"pattern": "^DEGRADED.*", "result": {"color": WARN, "index": 1}}},
                                {"type": "regex", "options": {"pattern": "^CRITICAL.*", "result": {"color": BAD, "index": 2}}}]}, [],
                  {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/", "values": False}, "colorMode": "value", "graphMode": "none",
                   "textMode": "value", "justifyMode": "center", "orientation": "horizontal", "wideLayout": True, "text": {"valueSize": 20}}))
    P.append(base("stat", "", 20, 0, 4, 2, [target(SQL["clock"])],
                  {"color": {"mode": "fixed", "fixedColor": VIOLET}, "thresholds": thr((None, VIOLET)), "mappings": []}, [],
                  {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/", "values": False}, "colorMode": "value", "graphMode": "none",
                   "textMode": "value", "justifyMode": "center", "orientation": "horizontal", "wideLayout": True, "text": {"valueSize": 22}}))

    # ---- headline tiles (y 2-7), 6 x w4
    P.append(tile("🛰  nodes alive", SQL["nodes_alive"], 0, 2, 4, 5, thr((None, BAD), (8, WARN), (10, GOOD)), spark=False, fmt="table",
                  desc="node_status rows whose own mesh-agent heartbeat is < 3 min (target 10; no history table, so no sparkline). Was service_registry until 2026-10-06, which only counts nodes with registered services."))
    P.append(tile("🧩  services up", SQL["services_up_ts"], 4, 2, 4, 5, thr((None, BAD), (30, WARN), (32, GOOD)),
                  desc="services whose latest health_check (mac-studio checker, 10-min window) is up; 32 known"))
    P.append(tile("🧠  LLM backends healthy", SQL["llm_healthy_ts"], 8, 2, 4, 5, thr((None, BAD), (7, WARN), (10, GOOD)),
                  desc="nova_llm_ping: node/backend pairs up per 15-min round (10 known)"))
    P.append(tile("🔥  open incidents", SQL["incidents_ts"], 12, 2, 4, 5, thr((None, GOOD), (1, WARN), (3, BAD)),
                  desc="telemetry.incidents open at each 10-min point"))
    P.append(tile("🐘  worst replica lag", SQL["lag_ts"], 16, 2, 4, 5, thr((None, GOOD), (100, WARN), (1000, BAD)), unit="ms", decimals=1,
                  desc="max replay_lag_ms across standbys (telemetry.replication_health)"))
    P.append(tile("💾  memories", SQL["memories_ts"], 20, 2, 4, 5, thr((None, NEUTRAL)), unit="short", decimals=2,
                  desc="nova_memories.memories total (telemetry.nova_meta memories_total)"))

    # ---- fleet row (y 7-16)
    fleet_ov = [ov("node", ("custom.width", 104), ("color", {"mode": "fixed", "fixedColor": "#cad3f5"})),
                ov("ip", ("custom.width", 100)), ov("os", ("custom.width", 52)), ov("cores", ("custom.width", 48)),
                ov("ram", ("custom.width", 58), ("unit", "decgbytes")), ov("load1", ("custom.width", 52), ("decimals", 1)),
                cell_bg("status", thr((None, GOOD)), mappings=STATUS_MAP, width=54),
                cell_text("heartbeat", AGE_T, unit="s", width=78),
                cell_gauge("load", thr((None, GOOD), (70, WARN), (100, BAD)), maxv=150, width=100),
                cell_gauge("memory", PCT_T, width=100), cell_gauge("disk", PCT_T, width=100)]
    P.append(table("🖥  fleet", SQL["fleet"], 0, 7, 11, 9, fleet_ov, desc="node_status (heartbeat age coloured: <2 min good, <10 min warn)"))
    P.append(table("📡  nodes in service registry", SQL["sr_nodes"], 11, 7, 4, 9,
                   [cell_text("heartbeat", AGE_T, unit="s", width=80), ov("svcs", ("custom.width", 48))],
                   desc="every node that registers services (covers nodes missing from node_status)"))
    P.append(wall("🧩  services (heartbeat age)", SQL["services_wall"], 15, 7, 9, 9, AGE_T, unit="s",
                  mappings=vmap({"-1": ("DOWN", BAD)}), desc="service_registry; DOWN or stale first"))

    # ---- detail row (y 16-28)
    P.append(tseries("📶  LLM ping latency", SQL["llm_ts"], 0, 16, 8, 4, "ms",
                     [fixed_color(n, c) for n, c in zip(NODE_ORDER, NODE_COLORS)], legend=False))
    P.append(wall("🧠  LLM fabric (latest ping)", SQL["llm_wall"], 0, 20, 8, 7, thr((None, GOOD), (400, WARN), (800, BAD)), unit="ms",
                  mappings=vmap({"-1": ("DOWN", BAD)})))
    P.append(tseries("🚨  events per 10 min, 24 h  —  amber warning · red critical", SQL["events_ts"], 8, 16, 8, 4, "short",
                     [fixed_color("warning", AMBER), fixed_color("critical", RED)], legend=False, bars=True, stacked=True, time_from="24h"))
    P.append(table("🔥  open incidents", SQL["incidents"], 8, 20, 8, 7,
                   [cell_bg("severity", thr((None, WARN)), mappings=vmap({"critical": ("CRIT", BAD), "warning": ("WARN", WARN)}), width=64),
                    ov("host", ("custom.width", 104)), cell_text("open", thr((None, "#cad3f5")), unit="m", width=66)]))
    P.append(base("gauge", "🐘  standby replay lag", 16, 16, 8, 4, [target(SQL["replication"], "table")],
                  {"color": {"mode": "thresholds"}, "thresholds": thr((None, GOOD), (100, WARN), (1000, BAD)), "unit": "ms", "decimals": 1,
                   "min": 0, "max": 1000, "mappings": []}, [],
                  {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}, "orientation": "auto",
                   "showThresholdLabels": False, "showThresholdMarkers": True, "sizing": "auto", "minVizWidth": 75, "minVizHeight": 75},
                  desc="pg_stat_replication on the primary (3 standbys expected: .7 .10 .125)"))
    P.append(base("bargauge", "⏱  scheduler tasks failing (24 h)", 16, 20, 8, 4, [target(SQL["sched_fail"], "table")],
                  {"color": {"mode": "thresholds"}, "thresholds": thr((None, WARN), (5, BAD)), "unit": "short", "decimals": 0, "min": 0,
                   "mappings": []}, [],
                  {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True}, "orientation": "horizontal",
                   "displayMode": "gradient", "valueMode": "color", "namePlacement": "left", "showUnfilled": True, "sizing": "auto",
                   "minVizHeight": 12, "maxVizHeight": 24, "minVizWidth": 8, "text": {"titleSize": 12, "valueSize": 12}},
                  desc="scheduler_runs, both schedulers; failure+timeout count per task, top 6"))
    P.append(wall("💓  health checks (last hour)", SQL["health_wall"], 16, 24, 8, 3, thr((None, BAD), (1, GOOD), (2, WARN)),
                  mappings=vmap({"0": ("DOWN", BAD), "1": ("OK", GOOD), "2": ("SLOW", WARN)}), text_mode="name", color_mode="background_solid",
                  desc="latest health_checks row per service; one tile per service that is not up"))
    return P

def dashboard():
    return {"dashboard": {
        "uid": "nova-cluster", "title": "Nova Cluster", "tags": ["nova", "cluster"], "style": "dark", "timezone": "browser",
        "editable": True, "graphTooltip": 1, "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
        "timepicker": {"refresh_intervals": ["30s", "1m", "5m"]}, "schemaVersion": 41, "version": 0,
        "panels": build(), "templating": {"list": []}, "annotations": {"list": []}, "links": [],
        "description": "Live Nova fleet dashboard (nova_ops on pg-primary), laid out for the 1920x1080 kiosk on nova-core7 HEADLESS-1 (VNC)."},
        "folderUid": "", "overwrite": True, "message": "nova-cluster kiosk dashboard"}

if __name__ == "__main__":
    if sys.argv[1:] == ["sql"]:
        for k, v in SQL.items():
            print(f"--@@ {k}\n{v}\n")
    else:
        print(json.dumps(dashboard()))
