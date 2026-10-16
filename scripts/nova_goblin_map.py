#!/usr/bin/env python3
"""nova_goblin_map.py — THE GREEN GOBLIN MAP: which controller, if subverted, moves the most.

From Maximum Overdrive (1986): the trucks that besiege the Dixie Boy are led by one rig with a
giant Green Goblin mask on its grille. The others circle when it circles and hold back when it
holds; the survivors only get away once Bill blows the Goblin truck apart with a rocket
launcher. One machine directing the rest is the whole problem, so you map which machine that
is before the comet comes. Nova's version: for every CONTROLLER in the house, count what it
could move or reach if it were subverted, and watch the counts week over week.

Controllers (each from real inventories, each fail-open — an unreadable source is 'unknown'):
  home_assistant    entity/device/config-entry registries (.storage, key-filtered: no credentials)
  scheduler_studio  config/scheduler.yaml tasks -> their scripts and everything they import
  scheduler_core    config/scheduler-core.yaml likewise
  nova_gateway      gateway TOOL_REGISTRY (AST), channels, secrets its package names, hosts running it
  homekit_bridge    telemetry.homekit_outlets
  unifi_controller  unifi_client_state, telemetry.net_inventory (infra, cameras)
  lutron            HA entity registry, platform lutron_caseta
  hue_bridge        hue_light_state
  frigate           camera tier of telemetry.net_inventory (the streams it consumes)
  pg_primary        nova.secrets names (count only), node_status, outbound_ledger channels, tasks
  nas               nodes whose scripts dir is a network mount (fleet probe), the tasks they run
  claude_ssh        nodes this host can ssh to (BatchMode), NOPASSWD-ALL among them, secrets, MCP
Reach metrics: actuators, cameras, devices, hosts, root_hosts (reachable AND passwordless sudo),
secrets, outbound (channel kinds), tasks, tools, acting_tools. score = weighted sum of the known
metrics (WEIGHTS). Weekly snapshot rows go to goblin_map; a controller whose score grew by more
than growth_pct (and at least MIN_DELTA points) since the last snapshot is flagged, and only
growth notifies. The top-3 concentration points each get one concrete reduction suggestion.
READ-ONLY: it never changes, revokes or restarts anything — it proposes.

CLI:   --scheduled   --dry-run   --report   --selftest
Table: goblin_map (ts, controller, reach jsonb, score)
service_config: goblin_map/latest (top score, above threshold, week-over-week change),
                goblin_map/score_threshold (default 300), goblin_map/growth_pct (default 10),
                goblin_map/schedulers ({controller: [yaml, node]} override)
Schedule: weekly, Monday 07:30.

MERGED 2026-10-15 (consolidation #11) into nova_capability_ledger.py as `--blast`: logic and table
stay here; main() is a thin wrapper. TMA-1 decoys never counted (via C.fleet_secret_names).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402
import nova_hendershot_cellar as C  # noqa: E402

SERVICE = "goblin_map"
CONFIG_DIR = C.REPO_DIR / "config"
SCHEDULERS = {"scheduler_studio": ("scheduler.yaml", "mac-studio"),
              "scheduler_core": ("scheduler-core.yaml", "nova-core")}
WEIGHTS = {"actuators": 3, "cameras": 3, "devices": 0.1, "hosts": 10, "root_hosts": 15, "secrets": 2,
           "outbound": 8, "tasks": 0.25, "tools": 2, "acting_tools": 4}
SCORE_THRESHOLD = 300
GROWTH_PCT = 10
MIN_DELTA = 5
ACT_DOMAINS = {"light", "switch", "lock", "cover", "climate", "fan", "valve", "siren", "vacuum", "scene",
               "button", "media_player", "remote", "number", "select", "alarm_control_panel",
               "water_heater", "humidifier", "script", "automation"}
NOTIFY_DOMAINS = {"slack", "discord", "smtp", "mobile_app", "telegram_bot", "signal_messenger", "twilio",
                  "pushover", "ntfy", "notify", "matrix"}
CHANNELS = {
    "slack": re.compile(r"chat\.postMessage|hooks\.slack\.com|\bpost_slack\b|\bpost_both\b|slack_sdk"),
    "discord": re.compile(r"discord(?:app)?\.com/api|discord_webhook|\bpost_discord\b"),
    "imessage": re.compile(r"\bsend_imessage\b|\bimsg\b|tell application \"Messages\""),
    "email": re.compile(r"\bsmtplib\b|nova_herd_mail|\bsendmail\b"),
    "signal": re.compile(r"signal-cli|\bsignal_send\b"),
    "notify_bus": re.compile(r"\bnova_notify\b|telemetry\.events"),
    "voice": re.compile(r"nova_voice_room|\bpyatv\b"),
}
ACT_RE = re.compile(r"/api/services/|\bhue_control\b|\blutron\b|\bhomekit\b|nova_home_control|turn_on|turn_off")
SUGGEST = {
    "claude_ssh": "replace '(ALL) NOPASSWD: ALL' on the {root_hosts} reachable hosts with command-scoped sudoers "
                  "entries for the helpers actually used, and drop authorized keys this host does not need",
    "nas": "run the {tasks} scheduled tasks from a per-node read-only checkout (pull of a signed tag) instead of "
           "the live NAS share mounted on {hosts} nodes, so one write to the share is not code on every node",
    "pg_primary": "move the fleet secret store ({secrets} names) to its own database and role, and scope each "
                  "node's ~/.pgpass to the roles it needs, so nova_ops access is not a path to every credential",
    "home_assistant": "give Nova's HA token a non-admin user limited to the entities it drives "
                      "({actuators} actuators today), and remove unused integrations ({secrets} config entries)",
    "scheduler_studio": "split the tasks that read secrets ({secrets} names) onto a separate runner user with "
                        "vault access; leave the other {tasks} tasks without it",
    "scheduler_core": "split the tasks that read secrets ({secrets} names) onto a separate runner user with "
                      "vault access; leave the other {tasks} tasks without it",
    "nova_gateway": "put the {acting_tools} state-changing tools behind 'approve' autonomy rules or a separate "
                    "process that does not hold the chat path's {secrets} secrets",
    "unifi_controller": "give the pollers a read-only UniFi local account; keep the admin credential out of the "
                        "fleet store ({actuators} infra devices can be reconfigured by it)",
    "homekit_bridge": "split the {actuators} outlets across two bridges by room, or pin the bridge's admin "
                      "UI to the rack VLAN only",
    "lutron": "keep the SmartBridge integration in one controller (HA) and remove any second client "
              "holding its certificate",
    "hue_bridge": "replace the shared Hue app key with one key per consumer so one can be revoked alone "
                  "({actuators} lights)",
    "frigate": "give Frigate a view-only Protect account for its {cameras} streams",
}


def log(m: str) -> None:
    print(f"[goblin-map {datetime.now():%H:%M:%S}] {m}", flush=True)


_q = C._q


def _one(cur, sql, args=()):
    r = _q(cur, sql, args)
    return int(r[0][0]) if r and r[0][0] is not None else None


# ── shared evidence ─────────────────────────────────────────────────────────

def scheduler_tasks(path: Path) -> list | None:
    """Enabled task script basenames from a scheduler YAML (read only), or None if unreadable."""
    import yaml
    try:
        tasks = (yaml.safe_load(path.read_text()) or {}).get("tasks") or {}
    except (OSError, yaml.YAMLError) as e:
        log(f"{path.name} unreadable: {type(e).__name__}")
        return None
    return [str(v["script"]).split()[0] for v in tasks.values()
            if isinstance(v, dict) and v.get("script") and v.get("enabled", True) is not False]


def reach_of_modules(mods: set, index: dict, graph: dict, refs: dict, nodes: list) -> dict:
    """Secrets named, channel kinds and fleet nodes named across mods and everything they import."""
    allm = set()
    for m in mods:
        allm |= C.closure(m, graph)
    src = "\n".join(index.get(m, "") for m in allm)
    return {"secrets": len({n for m in allm for n in refs.get(m, ())}),
            "outbound": sum(1 for rx in CHANNELS.values() if rx.search(src)),
            "named_nodes": sorted(n for n in nodes if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", src)),
            "acting": sum(1 for m in mods if ACT_RE.search("\n".join(index.get(x, "") for x in C.closure(m, graph))))}


def module_of(script: str, index: dict) -> str | None:
    stem = script[:-3] if script.endswith(".py") else script
    return stem if stem in index else (script if script in index else None)


def root_ok(p: dict | None) -> bool | None:
    if p is None:
        return None
    return any(r.endswith("NOPASSWD: ALL") for r in p["sudo_rules"])


def hosts_running(probes: dict, names: tuple) -> list:
    return sorted(n for n, p in probes.items() if p and any(x in names for x in p["procs"]))


def gather(cur, _sleep=None) -> dict:
    probes = C.probe_fleet(cur, _sleep=_sleep)
    index = C.script_index()
    import nova_capability_ledger as CL   # TMA-1 decoys are never counted
    fleet = CL.drop_decoys(C.fleet_secret_names(cur))
    kc = CL.drop_decoys(C.keychain_names() if sys.platform == "darwin" else None)
    names = set(fleet or {}) | set(kc or {})
    cfg = C.ha_config_dir(cur)
    try:
        scheds = W.get_config(cur, SERVICE, "schedulers") or SCHEDULERS
    except Exception:  # noqa: BLE001
        scheds = SCHEDULERS
    return {"probes": probes, "index": index, "graph": C.import_graph(index), "refs": C.name_refs(names, index),
            "fleet": fleet, "keychain": kc, "nodes": [r[0] for r in _q(cur, "SELECT node_name FROM node_status")],
            "ha": {k: C.ha_storage(cfg, f"core.{k}") for k in ("entity_registry", "device_registry", "config_entries")},
            "schedulers": {c: (scheduler_tasks(CONFIG_DIR / y), node) for c, (y, node) in scheds.items()},
            "gateway": C.gateway_tools(), "claude": C.claude_config(), "cur": cur}


# ── controllers ─────────────────────────────────────────────────────────────

def r_home_assistant(ctx):
    ha, cur = ctx["ha"], ctx["cur"]
    ents = ((ha.get("entity_registry") or {}).get("data") or {}).get("entities")
    entries = ((ha.get("config_entries") or {}).get("data") or {}).get("entries")
    devs = ((ha.get("device_registry") or {}).get("data") or {}).get("devices")
    if ents is None:   # fall back to what the poller saw in the last day
        doms = dict(_q(cur, "SELECT domain, count(DISTINCT entity_id) FROM telemetry.ha_sensors "
                            "WHERE ts > now() - interval '1 day' GROUP BY 1"))
        return {"actuators": sum(v for k, v in doms.items() if k in ACT_DOMAINS) if doms else None,
                "cameras": None, "devices": None, "secrets": None, "outbound": None,
                "source": "telemetry.ha_sensors (registry unreadable)"}
    live = [e for e in ents if not e.get("disabled_by")]
    dom = Counter(str(e.get("entity_id", "")).split(".")[0] for e in live)
    return {"actuators": sum(v for k, v in dom.items() if k in ACT_DOMAINS), "cameras": dom.get("camera", 0),
            "devices": len(devs) if devs is not None else None,
            "secrets": len(entries) if entries is not None else None,
            "outbound": sum(1 for e in entries or [] if e.get("domain") in NOTIFY_DOMAINS) if entries is not None else None,
            "source": "HA registries"}


def r_scheduler(ctx, name):
    tasks, node = ctx["schedulers"].get(name, (None, None))
    if tasks is None:
        return {"tasks": None, "secrets": None, "outbound": None, "hosts": None, "root_hosts": None,
                "acting_tools": None, "source": "scheduler yaml unreadable"}
    mods = {m for m in (module_of(t, ctx["index"]) for t in tasks) if m}
    r = reach_of_modules(mods, ctx["index"], ctx["graph"], ctx["refs"], ctx["nodes"])
    root = root_ok(ctx["probes"].get(node))
    return {"tasks": len(tasks), "secrets": r["secrets"], "outbound": r["outbound"],
            "hosts": len(set(r["named_nodes"]) | {node}), "root_hosts": None if root is None else int(root),
            "acting_tools": r["acting"], "source": f"{len(mods)} scripts on {node}"}


def r_gateway(ctx):
    reg, ro = ctx["gateway"]
    mods = {m for m in ctx["index"] if m == "nova_gateway" or m.startswith("nova_gateway.")
            or m == "nova_gateway_v2"}
    r = reach_of_modules(mods, ctx["index"], ctx["graph"], ctx["refs"], ctx["nodes"])
    chans = [m for m in mods if m.startswith("nova_gateway.channels.") and not m.endswith("__init__")]
    run_on = hosts_running(ctx["probes"], ("nova_gateway_v2.py", "nova_gateway"))
    return {"tools": len(reg) if reg is not None else None,
            "acting_tools": len([t for t in reg if t not in ro]) if reg is not None else None,
            "secrets": r["secrets"] if mods else None, "outbound": max(r["outbound"], len(chans)) if mods else None,
            "hosts": len(run_on), "root_hosts": sum(1 for n in run_on if root_ok(ctx["probes"][n])),
            "source": f"registry + {len(mods)} modules; running on {len(run_on)} node(s)"}


def r_homekit(ctx):
    n = _one(ctx["cur"], "SELECT count(DISTINCT uid) FROM telemetry.homekit_outlets WHERE ts > now() - interval '7 days'")
    return {"actuators": n, "source": "telemetry.homekit_outlets"}


def r_unifi(ctx):
    cur = ctx["cur"]
    tiers = dict(_q(cur, "SELECT tier, count(*) FROM telemetry.net_inventory GROUP BY 1"))
    return {"devices": _one(cur, "SELECT count(*) FROM unifi_client_state WHERE online"),
            "actuators": tiers.get("infra") if tiers else None, "cameras": tiers.get("camera") if tiers else None,
            "source": "unifi_client_state + net_inventory"}


def r_platform(ctx, platform):
    ents = (((ctx["ha"].get("entity_registry") or {}).get("data") or {}).get("entities"))
    if ents is None:
        return {"actuators": None, "source": "HA entity registry unreadable"}
    mine = [e for e in ents if e.get("platform") == platform and not e.get("disabled_by")]
    return {"actuators": sum(1 for e in mine if str(e.get("entity_id")).split(".")[0] in ACT_DOMAINS),
            "devices": len(mine), "source": f"HA registry platform {platform}"}


def r_hue(ctx):
    return {"actuators": _one(ctx["cur"], "SELECT count(*) FROM hue_light_state"), "source": "hue_light_state"}


def r_frigate(ctx):
    return {"cameras": _one(ctx["cur"], "SELECT count(*) FROM telemetry.net_inventory WHERE tier='camera'"),
            "source": "net_inventory camera tier (streams Frigate consumes)"}


def r_pg(ctx):
    cur = ctx["cur"]
    tasks = [t for t, _ in ctx["schedulers"].values() if t is not None]
    return {"secrets": len(ctx["fleet"]) if ctx["fleet"] is not None else None,
            "hosts": _one(cur, "SELECT count(*) FROM node_status"),
            "outbound": _one(cur, "SELECT count(DISTINCT channel_kind) FROM outbound_ledger "
                                  "WHERE ts > now() - interval '30 days'"),
            "tasks": sum(len(t) for t in tasks) if tasks else None,
            "source": "nova.secrets (names) + node_status + outbound_ledger"}


def r_nas(ctx):
    probes = ctx["probes"]
    if all(p is None for p in probes.values()):
        return {"hosts": None, "root_hosts": None, "tasks": None, "secrets": None, "source": "no node probed"}
    mounted = sorted(n for n, p in probes.items() if p and p["scripts_fs"] == "network")
    tasks = sum(len(t or []) for t, node in ctx["schedulers"].values() if node in mounted)
    names = {n for ns in ctx["refs"].values() for n in ns}
    return {"hosts": len(mounted), "root_hosts": sum(1 for n in mounted if root_ok(probes[n])),
            "tasks": tasks, "secrets": len(names),
            "source": f"scripts dir network-mounted on {len(mounted)} node(s); "
                      f"{sum(p is None for p in probes.values())} node(s) unprobed"}


def r_claude(ctx):
    probes = ctx["probes"]
    ok = [n for n, p in probes.items() if p]
    names = set(ctx["fleet"] or {}) | set(ctx["keychain"] or {})
    cc = ctx["claude"]
    allsrc = "\n".join(ctx["index"].values())
    return {"hosts": len(ok), "root_hosts": sum(1 for n in ok if root_ok(probes[n])),
            "secrets": len(names) if (ctx["fleet"] is not None or ctx["keychain"] is not None) else None,
            "tools": len(cc.get("mcp", [])) + len(cc.get("allow", [])),
            "outbound": sum(1 for rx in CHANNELS.values() if rx.search(allsrc)),
            "source": f"BatchMode ssh to {len(ok)}/{len(probes)} nodes"}


CONTROLLERS = {
    "home_assistant": r_home_assistant,
    "scheduler_studio": lambda c: r_scheduler(c, "scheduler_studio"),
    "scheduler_core": lambda c: r_scheduler(c, "scheduler_core"),
    "nova_gateway": r_gateway,
    "homekit_bridge": r_homekit,
    "unifi_controller": r_unifi,
    "lutron": lambda c: r_platform(c, "lutron_caseta"),
    "hue_bridge": r_hue,
    "frigate": r_frigate,
    "pg_primary": r_pg,
    "nas": r_nas,
    "claude_ssh": r_claude,
}


# ── scoring (pure) ──────────────────────────────────────────────────────────

def score(reach: dict) -> float:
    return round(sum(WEIGHTS[k] * v for k, v in reach.items() if k in WEIGHTS and isinstance(v, (int, float))), 1)


def unknowns(reach: dict) -> list:
    return sorted(k for k, v in reach.items() if k in WEIGHTS and v is None)


def build(ctx: dict, controllers: dict = CONTROLLERS) -> list:
    out = []
    for name, fn in controllers.items():
        try:
            reach = fn(ctx)
        except Exception as e:  # noqa: BLE001 — one controller never sinks the map
            log(f"{name}: {type(e).__name__}: {str(e)[:120]}")
            reach = {"source": f"error: {type(e).__name__}"}
        reach["unknown"] = unknowns(reach) if any(k in WEIGHTS for k in reach) else ["all"]
        out.append({"controller": name, "reach": reach, "score": score(reach)})
    return sorted(out, key=lambda r: -r["score"])


def compare(rows: list, prev: dict, growth_pct: float = GROWTH_PCT, min_delta: float = MIN_DELTA) -> list:
    """Annotate rows with prev score/delta; mark grew=True past both thresholds. prev={ctl: (score, reach)}."""
    for r in rows:
        p = prev.get(r["controller"])
        r["prev"] = p[0] if p else None
        r["delta"] = round(r["score"] - p[0], 1) if p else None
        r["grew_metrics"] = sorted(k for k, v in r["reach"].items() if k in WEIGHTS and isinstance(v, (int, float))
                                   and isinstance((p[1] or {}).get(k), (int, float)) and v > p[1][k]) if p else []
        r["grew"] = bool(p and p[0] > 0 and r["delta"] >= min_delta and r["delta"] / p[0] * 100 > growth_pct)
    return rows


def suggestion(r: dict) -> str:
    tmpl = SUGGEST.get(r["controller"], "split this controller's reach across two narrower identities")
    vals = {k: ("?" if v is None else v) for k, v in r["reach"].items() if k in WEIGHTS}
    return tmpl.format_map({**{k: "?" for k in WEIGHTS}, **vals})


def measures(rows: list, threshold: float) -> dict:
    known_prev = [r for r in rows if r.get("prev") is not None]
    return {"top_score": rows[0]["score"] if rows else 0, "top": rows[0]["controller"] if rows else None,
            "above_threshold": sum(r["score"] >= threshold for r in rows),
            "wow_change": round(sum(r["delta"] for r in known_prev), 1) if known_prev else None,
            "grew": [r["controller"] for r in rows if r.get("grew")]}


def render(rows: list, m: dict, threshold: float) -> str:
    out = [f"top score {m['top_score']} ({m['top']}), {m['above_threshold']} controller(s) >= {threshold}, "
           f"week-over-week {m['wow_change'] if m['wow_change'] is not None else 'n/a (first snapshot)'}"]
    for r in rows:
        rc = {k: v for k, v in r["reach"].items() if k in WEIGHTS}
        d = "" if r.get("delta") is None else f" ({'+' if r['delta'] >= 0 else ''}{r['delta']})"
        out.append(f"  {r['controller']:<17} {r['score']:>7}{d:<10} {'GREW ' if r.get('grew') else ''}"
                   f"{json.dumps(rc, separators=(',', ':'))}"
                   + (f"  unknown={','.join(r['reach']['unknown'])}" if r["reach"].get("unknown") else ""))
    out.append("Top-3 concentration points:")
    for i, r in enumerate(rows[:3], 1):
        out.append(f"  {i}. {r['controller']} ({r['score']}): {suggestion(r)}")
    return "\n".join(out)


# ── PG ──────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS goblin_map (
  ts timestamptz NOT NULL DEFAULT now(), controller text NOT NULL, reach jsonb NOT NULL,
  score double precision NOT NULL, PRIMARY KEY (ts, controller));
CREATE INDEX IF NOT EXISTS goblin_map_controller_ts ON goblin_map (controller, ts);
"""


def previous(cur) -> dict:
    if (_q(cur, "SELECT to_regclass('goblin_map')") or [(None,)])[0][0] is None:
        return {}
    rows = _q(cur, "SELECT controller, score, reach FROM goblin_map WHERE ts = (SELECT max(ts) FROM goblin_map)")
    return {c: (s, r if isinstance(r, dict) else json.loads(r or "{}")) for c, s, r in rows}


def cfg(cur, key, default):
    try:
        return float(W.get_config(cur, SERVICE, key, default))
    except Exception:  # noqa: BLE001
        return default


def run(dry: bool = False, _sleep=None) -> list:
    t0 = datetime.now(timezone.utc)
    conn = W.connect()
    try:
        cur = conn.cursor()
        thr, gp = cfg(cur, "score_threshold", SCORE_THRESHOLD), cfg(cur, "growth_pct", GROWTH_PCT)
        ctx = gather(cur, _sleep=_sleep)
        rows = compare(build(ctx), previous(cur), gp)
        m = measures(rows, thr)
        log(f"{'DRY RUN ' if dry else ''}{len(rows)} controllers ({(datetime.now(timezone.utc) - t0).total_seconds():.1f}s)")
        print(render(rows, m, thr))
        if dry:
            return rows
        from nova_pg_schema import safe_ddl
        safe_ddl(cur, SCHEMA)
        for r in rows:
            cur.execute("INSERT INTO goblin_map (ts, controller, reach, score) VALUES (%s,%s,%s::jsonb,%s)",
                        (t0, r["controller"], json.dumps(r["reach"], default=str), r["score"]))
        W.set_config(cur, SERVICE, "latest", {"ts": t0.isoformat(), **m, "threshold": thr},
                     by="nova_goblin_map")
        grew = [r for r in rows if r["grew"]]
        if grew:
            from nova_notify import notify
            body = "\n".join(f"• {r['controller']}: {r['prev']} → {r['score']} (+{r['delta']}; grew: "
                             f"{', '.join(r['grew_metrics']) or 'mix'}). Suggest: {suggestion(r)}" for r in grew)
            notify(f"Green Goblin Map: {len(grew)} controller(s) gained reach this week", body=body,
                   level="warning", category="security", source="nova_goblin_map.py",
                   dedup_key=f"goblin-map-growth-{t0:%G-W%V}")
        log(f"snapshot stored; {len(grew)} grew")
        return rows
    finally:
        conn.close()


def report() -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        prev = previous(cur)
        if not prev:
            print("no snapshot yet")
            return 0
        thr = cfg(cur, "score_threshold", SCORE_THRESHOLD)
        older = _q(cur, "SELECT controller, score FROM goblin_map WHERE ts = (SELECT max(ts) FROM goblin_map "
                        "WHERE ts < (SELECT max(ts) FROM goblin_map))")
        rows = [{"controller": c, "score": s, "reach": r} for c, (s, r) in prev.items()]
        rows = compare(sorted(rows, key=lambda r: -r["score"]), {c: (s, {}) for c, s in older})
        print(render(rows, measures(rows, thr), thr))
        return 0
    finally:
        conn.close()


def selftest() -> int:
    assert score({"hosts": 2, "root_hosts": 1, "secrets": None, "source": "x"}) == 35
    assert unknowns({"hosts": 1, "secrets": None, "source": "x"}) == ["secrets"]
    rows = [{"controller": "a", "reach": {"hosts": 12}, "score": 120.0},
            {"controller": "b", "reach": {"hosts": 2}, "score": 20.0}]
    compare(rows, {"a": (100.0, {"hosts": 10}), "b": (19.0, {"hosts": 2})})
    assert rows[0]["grew"] and rows[0]["grew_metrics"] == ["hosts"] and not rows[1]["grew"], rows
    m = measures(rows, 100)
    assert m == {"top_score": 120.0, "top": "a", "above_threshold": 1, "wow_change": 21.0, "grew": ["a"]}, m
    assert "?" in suggestion({"controller": "nas", "reach": {"hosts": 3}})
    out = build({}, {"ok": lambda c: {"hosts": 1}, "bad": lambda c: 1 / 0})
    assert [r["controller"] for r in out] == ["ok", "bad"] and out[1]["reach"]["unknown"] == ["all"]
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="map, store the weekly snapshot, notify on growth")
    ap.add_argument("--dry-run", action="store_true", help="map and compare with the last snapshot; write nothing")
    ap.add_argument("--report", action="store_true", help="latest stored snapshot vs the one before")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.dry_run or a.scheduled or a.report:   # merged 2026-10-15 (#11): nova_capability_ledger.py --blast
        import nova_capability_ledger as CL
        log(f"merged into nova_capability_ledger.py --blast on {CL.MERGED_ON}; delegating")
        if a.dry_run or a.scheduled:
            return CL.main(["--blast"] + (["--dry-run"] if a.dry_run else []))
        return CL.main(["--blast", "--show"])
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
