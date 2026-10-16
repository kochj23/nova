#!/opt/homebrew/bin/python3
"""nova_wargame.py — read-only what-if drills against Nova's home infrastructure.

Three rules, from the design agreed 2026-10-09:
1. Simulate incidents, not decisions. A drill takes a failure, works out what it takes down, and tests one
   candidate response in the model. Nothing is changed on any real system.
2. Label simulated results as simulated. Every result is stored with exercise=true in
   nova_ops.exercise_results. Nothing in this script writes to the action log or to the corroboration gates,
   and nothing reads exercise_results to decide anything.
3. Let simulations recommend restraint. If the candidate response makes the outcome no better or worse, the
   drill recommends HOLD, so doing nothing is always a scored option.

Dependency maps are written by hand from the device inventory and marked as assumptions where they are not
confirmed. A drill is only as good as its map.

EXPERIMENTS (2026-10-15, Little Mister: "Do it all" — chaos engineering for Nova). This module is also the
registry for real chaos experiments, but it stays read-only toward the fleet: it plans, gates, proposes and
records. The hands live in nova_wargame_live.py, which only runs an experiment that this module's gates pass
and that a Claude session (or Jordan) approved through a [red_line_decision] queue item. Live chaos is a red
line: Nova proposes, Claude decides.
  --seed                 upsert the planned experiments (E1-E6 small, K1/K2 Chaos Kong, G1 PG switchover game day)
  --experiments          list experiments, state and readiness
  --plan ID              print the gate verdicts, method and rollback (changes nothing)
  --propose ID           file the [red_line_decision] request for Claude
  --approve ID --by WHO  record approval (WHO must be claude:<session> or jordan)
  --tabletop             build the twice-yearly tabletop pack -> service_config tabletop/next (Nova's queue + 18:00 note)
  --gameday-propose      quarterly: propose G1 (the planned PG switchover) for approval

Usage: nova_wargame.py [--scenario NAME] [--list] [--dry-run] | experiment flags above
Written by Jordan Koch (via Claude).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn()

# component -> rooms it serves. "assumed" marks links not yet confirmed against the UniFi client map.
DEPENDS = {
    "garage_access_point": {"rooms": ["Garage"], "status": "assumed"},
    "homekit_hub": {"rooms": ["Bridges", "Dylan's Room", "Garage", "Master Bedroom", "Outdoor"], "status": "assumed"},
    "hue_bridge": {"rooms": ["Outdoor"], "status": "assumed"},
}

SCENARIOS = {
    "garage_ap_down": {
        "description": "The garage access point goes offline.",
        "failed": "garage_access_point",
        "responses": {
            "power_cycle_ap": {"effect": "restores", "rooms_back": ["Garage"],
                               "note": "restarts only the access point; fixes the garage if the fault is the AP itself"},
            "restart_hub": {"effect": "none", "rooms_back": [],
                            "note": "the hub is not the failed part here, so restarting it changes nothing"},
        },
    },
    "homekit_hub_down": {
        "description": "The HomeKit home hub stops responding.",
        "failed": "homekit_hub",
        "responses": {
            "restart_hub": {"effect": "restores", "rooms_back": ["Bridges", "Dylan's Room", "Garage",
                                                                 "Master Bedroom", "Outdoor"],
                            "note": "a restart fixes a hung hub; a hardware fault would not be fixed"},
            "power_cycle_bridge": {"effect": "worse", "rooms_back": [],
                                   "note": "power-cycling the Hue bridge drops the Outdoor lights that were still working"},
        },
    },
}


def simulate(scenario: dict, response: str, depends: dict = None) -> dict:
    """Pure. Returns the impact of the failure, the effect of one candidate response, and a recommendation."""
    depends = DEPENDS if depends is None else depends
    failed = scenario["failed"]
    down = set(depends.get(failed, {}).get("rooms", []))
    status = depends.get(failed, {}).get("status", "unknown")
    resp = scenario["responses"][response]
    back = set(resp["rooms_back"]) & down
    still_down = sorted(down - back)
    # HOLD unless the response brings rooms back and does not take anything else down.
    if resp["effect"] == "restores" and back:
        rec = "APPLY"
        verdict = f"restores {len(back)} of {len(down)} down room(s)"
    elif resp["effect"] == "worse":
        rec, verdict = "HOLD", "makes the outcome worse"
    else:
        rec, verdict = "HOLD", "no effect on the failure: doing nothing scores the same"
    return {"scenario": scenario["description"], "response": response, "assumption": status,
            "rooms_down": sorted(down), "rooms_still_down": still_down, "verdict": verdict,
            "recommendation": rec, "note": resp["note"], "exercise": True}


def ensure_table(conn) -> None:
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS exercise_results (
        id bigserial PRIMARY KEY, run_at timestamptz NOT NULL DEFAULT now(), scenario text NOT NULL,
        response text NOT NULL, recommendation text NOT NULL, result jsonb NOT NULL,
        exercise boolean NOT NULL DEFAULT true)""")
    conn.commit()


# ─────────────────────────── chaos experiment registry (2026-10-15) ───────────────────────────
import json as _json
from datetime import datetime as _dt
try:
    from zoneinfo import ZoneInfo as _ZI
    _PT = _ZI("America/Los_Angeles")
except Exception:  # noqa: BLE001
    _PT = None

EXPERIMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS wargame_experiments (
    id text PRIMARY KEY, scenario text NOT NULL, phase smallint NOT NULL, target_host text, capability text,
    hypothesis text NOT NULL, steady_state jsonb, method jsonb, rollback jsonb, abort_conditions jsonb,
    blast_radius text, magnitude text, max_duration interval NOT NULL, readiness text,
    confirmation_id text, approved_by text, queue_id integer,
    state text NOT NULL DEFAULT 'planned' CHECK (state IN ('planned','proposed','approved','running','aborted',
          'rolled_back','passed','failed','rollback_failed')),
    started_at timestamptz, ended_at timestamptz, verdict jsonb, monitors_proven text[], hotwash_id bigint,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

# Never touched by a live experiment, whatever the plan says (research 2026-10-15 + Nova's red lines).
NEVER = ("lock", "garage", "alarm", "siren", "cover", "valve", "climate", "thermostat", "hue", "lutron", "homekit",
         "homebridge", "frigate", "life_safety", "notifier", "slack", "signal", "relay", "big_brother", "kill_switch",
         "claude_reviewer", "watchdog", "pg_primary", "nas_volume", "backup", "ssh_control")
WINDOW = (10, 16)          # weekdays, Pacific
MIN_BUDGET = 0.25          # no experiment on a component whose SLO budget is below this

EXPERIMENTS = [
    dict(id="E1", phase=2, scenario="stop the tinychat container on nova-core2", target_host="nova-core2",
         capability="container_stop", blast_radius="one non-critical web app on one node", magnitude="full stop",
         max_duration="10 minutes",
         hypothesis="health_checks for tinychat@nova-core2 goes red within one interval; finding expected: no "
                    "Rung-1 auto-heal (tinychat is not in SAFE_SERVICES)",
         steady_state={"health_check": "tinychat@nova-core2", "expect": "down within 2 intervals, up after rollback"},
         method=[{"type": "ssh", "host": "nova-core2", "cmd": "docker stop tinychat"}],
         rollback=[{"type": "ssh", "host": "nova-core2", "cmd": "docker start tinychat"}],
         abort_conditions=["any slo page", "not restored 10 min after rollback"], readiness="ready"),
    dict(id="E2", phase=2, scenario="800 ms latency on the memory server, prober only", target_host="studio",
         capability="latency_proxy", blast_radius="the prober's memory round trip only", magnitude="+800 ms then down",
         max_duration="15 minutes",
         hypothesis="memory_recall probe degrades and burn registers; chat stays within budget",
         steady_state={"slo": ["memory_recall", "chat"]},
         method=[{"type": "manual", "note": "start toxiproxy on 127.0.0.1, set service_config prober/memory_url to it"}],
         rollback=[{"type": "manual", "note": "clear prober/memory_url, stop toxiproxy"}],
         abort_conditions=["chat burn >= 6 over 1h", "any non-prober client on the proxy"],
         readiness="blocked: toxiproxy not installed (brew install toxiproxy)"),
    dict(id="E3", phase=2, scenario="stop Ollama on nova-core6", target_host="nova-core6.digitalnoise.net",
         capability="service_stop", blast_radius="one inference node of several", magnitude="full stop",
         max_duration="15 minutes",
         hypothesis="the inference router moves load elsewhere; chat SLI unchanged",
         steady_state={"slo": ["chat"]},
         method=[{"type": "ssh", "host": "nova-core6.digitalnoise.net",
                  "cmd": "sudo launchctl bootout system/net.digitalnoise.ollama"}],
         rollback=[{"type": "ssh", "host": "nova-core6.digitalnoise.net",
                    "cmd": "sudo launchctl bootstrap system /Library/LaunchDaemons/net.digitalnoise.ollama.plist"}],
         abort_conditions=["2 consecutive chat probe failures"], readiness="ready (use the FQDN; short name lacks a host key)"),
    dict(id="E4", phase=2, scenario="fill a capped sandbox on nova-core2", target_host="nova-core2",
         capability="disk_fill_sandbox", blast_radius="a 2 GB file in /var/tmp/nova-chaos", magnitude="2 GB",
         max_duration="10 minutes",
         hypothesis="the disk monitor notices growth; expected finding: nova_capacity only watches '/', so nothing fires",
         steady_state={"min_free_pct": 20},
         method=[{"type": "ssh", "host": "nova-core2", "cmd": "mkdir -p /var/tmp/nova-chaos && fallocate -l 2G /var/tmp/nova-chaos/fill"}],
         rollback=[{"type": "ssh", "host": "nova-core2", "cmd": "rm -f /var/tmp/nova-chaos/fill"}],
         abort_conditions=["any real filesystem below 20% free", "past 10 min"],
         readiness="ready (add the sandbox path to nova_capacity first if the test should be able to pass)"),
    dict(id="E5", phase=2, scenario="nova-core3 loses the UNAS for 10 minutes", target_host="nova-core3",
         capability="network_partition_client", blast_radius="one client's path to the NAS; the NAS is untouched",
         magnitude="drop", max_duration="10 minutes",
         hypothesis="nova-core3's NAS-dependent tasks fail loudly; Comet Tail names NAS/SMB as the shared cause",
         steady_state={"comet_tail": "names nas"},
         method=[{"type": "ssh", "host": "nova-core3",
                  "cmd": "sudo iptables -I OUTPUT -d unas.digitalnoise.net -j DROP -m comment --comment nova-chaos-E5"}],
         rollback=[{"type": "ssh", "host": "nova-core3",
                    "cmd": "sudo iptables -D OUTPUT -d unas.digitalnoise.net -j DROP -m comment --comment nova-chaos-E5"}],
         abort_conditions=["a task with a physical side effect is scheduled on nova-core3"], readiness="ready"),
    dict(id="E6", phase=2, scenario="pause WAL replay on the nova-core7 standby", target_host="nova-core7",
         capability="replica_replay_pause", blast_radius="one standby", magnitude="replay paused",
         max_duration="30 minutes",
         hypothesis="a replica-lag alert fires and the primary is unaffected; expected finding: nova_replication_monitor "
                    "has no alert path, so nothing fires",
         steady_state={"primary_latency": "unchanged"},
         method=[{"type": "psql_standby", "host": "nova-core7.digitalnoise.net", "sql": "SELECT pg_wal_replay_pause()"}],
         rollback=[{"type": "psql_standby", "host": "nova-core7.digitalnoise.net", "sql": "SELECT pg_wal_replay_resume()"}],
         abort_conditions=["primary latency rises", "past 30 min"],
         readiness="ready for SQL; ssh host key for nova-core7 missing (only needed for diagnostics)"),
    dict(id="K1", phase=3, scenario="lose the Studio (network evacuation, 30 min)", target_host="studio",
         capability="host_evacuation", blast_radius="the Studio and everything only it runs", magnitude="network pulled",
         max_duration="30 minutes",
         hypothesis="chat stays green (gateway on nova-core, Ollama elsewhere); memory reads stay green from replicas; "
                    "memory writes fail visibly",
         steady_state={"slo": ["chat", "memory_recall"]},
         method=[{"type": "manual", "note": "runbook: pull the Studio's network with Jordan present; before and after the M5 migration"}],
         rollback=[{"type": "manual", "note": "restore the network; verify launchd jobs and memory writes"}],
         abort_conditions=["any Tier-0 outage beyond the Studio's own services"],
         readiness="blocked by the NEVER list until big_brother moves off the Studio"),
    dict(id="K2", phase=3, scenario="lose nova-core (planned role move first)", target_host="nova-core",
         capability="host_role_move", blast_radius="scheduler-core, gateway, PG primary roles", magnitude="planned move",
         max_duration="2 hours",
         hypothesis="scheduler leader fails over, gateway fails over, BIND keeps answering, PG needs a gated manual promote; "
                    "count nonce rows written before cutover that survive",
         steady_state={"slo": ["chat", "database", "scheduled_jobs"]},
         method=[{"type": "manual", "note": "runbook: nova_pg_failover.py promote --dry-run first; gates: witness, fence, LSN"}],
         rollback=[{"type": "manual", "note": "fail back with the same gates"}],
         abort_conditions=["any gate refuses", "data-loss estimate above RPO"],
         readiness="blocked: gateway relay is an oversight channel; restore check and witness power to be confirmed"),
    dict(id="G1", phase=3, scenario="quarterly planned PG switchover game day", target_host="nova-core",
         capability="pg_switchover", blast_radius="the database role, planned", magnitude="planned switchover",
         max_duration="2 hours",
         hypothesis="the gated switchover completes inside the Tier-0 RTO (1h) with zero lost nonce rows, and fails back",
         steady_state={"slo": ["database"]},
         method=[{"type": "manual", "note": "nova_pg_failover.py promote with all gates; 2-hour window; hotwash after"}],
         rollback=[{"type": "manual", "note": "switch back with the same gates"}],
         abort_conditions=["any gate refuses"], readiness="needs witness power confirmed and wal_log_hints on the primary"),
]


def in_window(now=None) -> bool:
    """Weekdays 10:00-16:00 Pacific. Pure (given now)."""
    now = now or (_dt.now(_PT) if _PT else _dt.now())
    return now.weekday() < 5 and WINDOW[0] <= now.hour < WINDOW[1]


def never_hits(exp: dict) -> list:
    """Words from the NEVER list found in the experiment's target, capability or method. Pure."""
    blob = " ".join([str(exp.get("target_host") or ""), str(exp.get("capability") or ""),
                     _json.dumps(exp.get("method") or [])]).lower()
    blob = blob.replace("unas.digitalnoise.net", "")   # E5 cuts a client's route, it never touches the NAS itself
    return [w for w in NEVER if w in blob]


def gate_check(exp: dict, ctx: dict) -> list:
    """Every reason a live run must be refused. Empty list = may run. Pure.
    ctx: enabled, allowlist [(host,cap)], kill, open_critical, slo_pages, min_budget, running_other, now."""
    why = []
    if not ctx.get("enabled"):
        why.append("chaos/enabled is false")
    if ctx.get("kill"):
        why.append("kill switch engaged")
    if (exp.get("target_host"), exp.get("capability")) not in {tuple(x) for x in ctx.get("allowlist", [])}:
        why.append("target/capability not in chaos/targets")
    hits = never_hits(exp)
    if hits:
        why.append("NEVER list: " + ", ".join(hits))
    if exp.get("phase", 2) >= 3:
        why.append("phase 3 drills are runbook-only (run by a human with Claude)")
    if any(m.get("type") == "manual" for m in exp.get("method") or []):
        why.append("method is manual")
    if exp.get("state") != "approved" or not str(exp.get("approved_by") or "").startswith(("claude:", "jordan")):
        why.append("not approved by Claude or Jordan")
    if not in_window(ctx.get("now")):
        why.append("outside weekdays 10:00-16:00 Pacific")
    if ctx.get("open_critical"):
        why.append("an incident is open at critical")
    if ctx.get("slo_pages"):
        why.append("an SLO is paging")
    if ctx.get("min_budget") is not None and ctx["min_budget"] < MIN_BUDGET:
        why.append(f"an SLO budget is below {MIN_BUDGET:.0%}")
    if ctx.get("running_other"):
        why.append("another experiment is running")
    if str(exp.get("readiness", "")).startswith("blocked"):
        why.append(exp["readiness"])
    return why


def load_ctx(cur, exp_id=None) -> dict:
    """Read the live gate context (read-only)."""
    def one(sql, args=None, default=None):
        try:
            cur.execute(sql, args)
            r = cur.fetchone()
            return r[0] if r else default
        except Exception:  # noqa: BLE001
            cur.connection.rollback()
            return default
    enabled = str(one("SELECT value FROM service_config WHERE service='chaos' AND key='enabled'", default="false")).lower() in ("true", "1", "yes")
    allow = one("SELECT value FROM service_config WHERE service='chaos' AND key='targets'", default="[]")
    try:
        allow = _json.loads(allow) if isinstance(allow, str) else (allow or [])
    except Exception:  # noqa: BLE001
        allow = []
    try:
        import nova_autonomy_safety as _as
        kill = bool(_as.kill_switch_engaged(cur))
    except Exception:  # noqa: BLE001
        kill = True          # cannot tell -> treat as engaged
    return dict(enabled=enabled, allowlist=allow, kill=kill,
                open_critical=bool(one("SELECT count(*) FROM telemetry.incidents WHERE status='open' AND severity='critical'", default=1)),
                slo_pages=bool(one("SELECT count(*) FROM (SELECT DISTINCT ON (name) state FROM slo_status ORDER BY name, at DESC) s WHERE state='page'", default=1)),
                min_budget=one("SELECT min(budget_remaining) FROM (SELECT DISTINCT ON (name) budget_remaining FROM slo_status ORDER BY name, at DESC) s"),
                running_other=bool(one("SELECT count(*) FROM wargame_experiments WHERE state='running' AND id <> %s", (exp_id or "",), default=1)))


def _row(cur, exp_id):
    cur.execute("SELECT id, scenario, phase, target_host, capability, hypothesis, steady_state, method, rollback, "
                "abort_conditions, blast_radius, magnitude, max_duration::text, readiness, state, approved_by, queue_id "
                "FROM wargame_experiments WHERE id=%s", (exp_id,))
    r = cur.fetchone()
    if not r:
        return None
    keys = ["id", "scenario", "phase", "target_host", "capability", "hypothesis", "steady_state", "method", "rollback",
            "abort_conditions", "blast_radius", "magnitude", "max_duration", "readiness", "state", "approved_by", "queue_id"]
    return dict(zip(keys, r))


def seed(cur) -> int:
    from nova_pg_schema import safe_ddl
    safe_ddl(cur, EXPERIMENTS_SCHEMA)
    for e in EXPERIMENTS:
        cur.execute("""INSERT INTO wargame_experiments (id, scenario, phase, target_host, capability, hypothesis,
                       steady_state, method, rollback, abort_conditions, blast_radius, magnitude, max_duration, readiness)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::interval,%s)
                       ON CONFLICT (id) DO UPDATE SET scenario=EXCLUDED.scenario, hypothesis=EXCLUDED.hypothesis,
                         steady_state=EXCLUDED.steady_state, method=EXCLUDED.method, rollback=EXCLUDED.rollback,
                         abort_conditions=EXCLUDED.abort_conditions, readiness=EXCLUDED.readiness, updated_at=now()
                       WHERE wargame_experiments.state IN ('planned','proposed')""",
                    (e["id"], e["scenario"], e["phase"], e["target_host"], e["capability"], e["hypothesis"],
                     _json.dumps(e["steady_state"]), _json.dumps(e["method"]), _json.dumps(e["rollback"]),
                     _json.dumps(e["abort_conditions"]), e["blast_radius"], e["magnitude"], e["max_duration"],
                     e["readiness"]))
    cur.execute("""INSERT INTO service_config (service, key, value) VALUES ('chaos','enabled','false'), ('chaos','targets','[]')
                   ON CONFLICT DO NOTHING""")
    return len(EXPERIMENTS)


def propose(cur, exp_id) -> int | None:
    """Nova files the red-line request; a Claude session decides."""
    e = _row(cur, exp_id)
    if not e:
        return None
    import nova_red_line
    qid = nova_red_line.route_to_claude(
        cur, f"Approve chaos experiment {exp_id}: {e['scenario']}",
        f"Hypothesis: {e['hypothesis']}\nBlast radius: {e['blast_radius']}\nMax: {e['max_duration']}\n"
        f"Abort if: {e['abort_conditions']}\nReadiness: {e['readiness']}\n"
        f"Approve with: nova_wargame.py --approve {exp_id} --by claude:<session>  (add the target to chaos/targets and "
        f"set chaos/enabled=true for the run window). Run with: nova_wargame_live.py --live --confirm {exp_id}",
        source="wargame", ref=exp_id, classes=["chaos_live"])
    cur.execute("UPDATE wargame_experiments SET state='proposed', queue_id=%s, updated_at=now() "
                "WHERE id=%s AND state='planned'", (qid, exp_id))
    return qid


def approve(cur, exp_id, by) -> bool:
    if not str(by).startswith(("claude:", "jordan")):
        raise SystemExit("--by must be claude:<session> or jordan")
    cur.execute("UPDATE wargame_experiments SET state='approved', approved_by=%s, updated_at=now() "
                "WHERE id=%s AND state IN ('planned','proposed')", (by, exp_id))
    return cur.rowcount == 1


TABLETOP_SCENARIOS = [
    ("synology_hang", "The Synology OS hangs for 40 hours (as on 2026-09-05). Which jobs stall, which fail loudly, "
                      "and what does Nova do in hour 1, hour 6, hour 24?"),
    ("nas_ransomware", "Ransomware encrypts the NAS shares, including the scripts directory every node mounts. "
                       "What stops spreading, what is restored from where, and how long is the house without Nova?"),
    ("core_dies_mid_backup", "nova-core dies at 03:00 during pg_dump. Which replica is promoted, through which gates, "
                             "and how much data is lost against the Tier-0 RPO?"),
]


def tabletop_pack(cur=None) -> dict:
    """The twice-yearly 45-minute tabletop: scenarios + the Haven manual-fallback gaps as discussion prompts."""
    gaps = []
    if cur is not None:
        try:
            cur.execute("SELECT checklist FROM haven_drills ORDER BY id DESC LIMIT 1")
            r = cur.fetchone()
            items = (r[0] if r else None) or []
            gaps = [i.get("name") or i.get("key") for i in items if isinstance(i, dict)
                    and not i.get("manual_fallback")][:8]
        except Exception:  # noqa: BLE001
            cur.connection.rollback()
    return {"built": _dt.now().isoformat(timespec="minutes"), "minutes": 45,
            "scenarios": [{"key": k, "prompt": p} for k, p in TABLETOP_SCENARIOS],
            "no_manual_fallback": gaps,
            "note": "Nova runs it with Claude; Jordan is invited, not required. Output -> hotwash."}


def experiments_main(a) -> int:
    conn = _nova_dsn.pg_connect()
    conn.autocommit = True
    cur = conn.cursor()
    if a.seed:
        print(f"[wargame] seeded {seed(cur)} experiments (chaos/enabled stays false)")
    if a.experiments:
        cur.execute("SELECT id, phase, state, readiness, scenario FROM wargame_experiments ORDER BY phase, id")
        for r in cur.fetchall():
            print(f"{r[0]:3} p{r[1]} {r[2]:10} {r[4]}  [{r[3]}]")
    if a.plan:
        e = _row(cur, a.plan)
        if not e:
            print("no such experiment"); return 1
        why = gate_check(e, load_ctx(cur, a.plan))
        print(_json.dumps({"experiment": e, "refusals": why, "would_run": not why}, indent=1, default=str))
    if a.propose:
        print(f"[wargame] proposed {a.propose} -> claude_queue #{propose(cur, a.propose)}")
    if a.approve:
        print(f"[wargame] {'approved' if approve(cur, a.approve, a.by) else 'not approvable (state?)'} {a.approve}")
    if a.tabletop:
        pack = tabletop_pack(cur)
        cur.execute("""INSERT INTO service_config (service, key, value) VALUES ('tabletop','next',%s)
                       ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value""", (_json.dumps(pack),))
        print(f"[wargame] tabletop pack written: {len(pack['scenarios'])} scenarios, {len(pack['no_manual_fallback'])} fallback gaps")
    if a.gameday_propose:
        print(f"[wargame] G1 game day proposed -> claude_queue #{propose(cur, 'G1')}")
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="simulate and print, do not store")
    ap.add_argument("--seed", action="store_true", help="upsert the chaos experiments (planned)")
    ap.add_argument("--experiments", action="store_true", help="list chaos experiments")
    ap.add_argument("--plan", metavar="ID", help="gate verdicts + method/rollback for one experiment")
    ap.add_argument("--propose", metavar="ID", help="file the red-line approval request for Claude")
    ap.add_argument("--approve", metavar="ID")
    ap.add_argument("--by", default="")
    ap.add_argument("--tabletop", action="store_true", help="build the twice-yearly tabletop pack")
    ap.add_argument("--gameday-propose", action="store_true", help="propose the quarterly PG switchover game day")
    ap.add_argument("--scheduled", action="store_true")
    a = ap.parse_args(argv)
    if any([a.seed, a.experiments, a.plan, a.propose, a.approve, a.tabletop, a.gameday_propose]):
        return experiments_main(a)
    if a.list:
        for name, s in SCENARIOS.items():
            print(f"{name}: {s['description']} responses: {', '.join(s['responses'])}")
        return 0
    names = [a.scenario] if a.scenario else sorted(SCENARIOS)
    results = []
    for name in names:
        s = SCENARIOS[name]
        for resp in s["responses"]:
            r = simulate(s, resp)
            results.append((name, resp, r))
            print(f"[{name}] {resp}: {r['recommendation']} — {r['verdict']} (assumption: {r['assumption']})")
    if not a.dry_run:
        import json
        import psycopg2
        conn = _nova_dsn.pg_connect()
        ensure_table(conn)
        cur = conn.cursor()
        for name, resp, r in results:
            cur.execute("INSERT INTO exercise_results (scenario, response, recommendation, result) VALUES (%s,%s,%s,%s)",
                        (name, resp, r["recommendation"], json.dumps(r)))
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
