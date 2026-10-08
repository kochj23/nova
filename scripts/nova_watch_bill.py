#!/usr/bin/env python3
"""nova_watch_bill.py — the Watch Bill: formal turnovers, and the PDB for Little Mister.

A ship never changes watch with "all quiet". The off-going officer turns over, in a fixed order:
what is broken, what is still open, the standing orders in force, what is expected on the next
watch, and — the part that catches the surprise — what would surprise them. Nova turns over
three times a day (06:30, 18:00, 23:00). Each turnover is written to watch_turnover and the
06:30 one feeds the morning PDB. Turnovers are never posted; they are Nova's log.

  degraded   health checks not ok, presence feeds degraded, CARDINAL compromise-suspect sources,
             Nova's own degraded state (nova_escalation.nova_state), new Buick 8 entries
  open loops the Boiler's top components, escalations held/deferred since the last turnover,
             open hotwash items, co-agency proposals waiting on Jordan
  standing   Commander's Intent standing orders (and anything due for reconfirmation)
  expected   open predictions resolving in the next 12h (calibrated, in estimative words), Derry
             Clock cycles in the next day
  surprise   what would surprise me: baselines that, if broken, mean something changed

PDB (compose_pdb, used by nova_night_watch at 07:02): BLUF first; 3-6 items graded A1-F6 by
CARDINAL, ordered by importance — the most important item leads even when it is unpleasant;
estimative language mapped to calibrated probabilities (a detector's own hit-rate, a
prediction's calibrated confidence); "what would change my mind" per item; a gaps section (blind
spots); one red-cell dissent line against the lead item; yesterday's prediction scorecard.
No flattery, no greeting. No address, bearing or home location (journal_safe on every line).

Usage: nova_watch_bill.py --turnover [--watch 0630|1800|2300] [--dry-run] | --show | --pdb-preview
       | --selftest
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

TAG = "watch-bill"
WATCHES = ("0630", "1800", "2300")
SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_turnover (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  watch text NOT NULL,
  body jsonb NOT NULL,
  text text NOT NULL);
CREATE INDEX IF NOT EXISTS watch_turnover_ts ON watch_turnover (ts DESC);
"""
SENSOR_MENU = ("camera", "radio", "traffic", "adsb", "network", "rf_presence", "mmwave")


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a missing table never sinks the turnover
        W.log(TAG, f"query failed: {str(e).splitlines()[0]}")
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


# ── turnover sections ───────────────────────────────────────────────────────

def degraded(cur, ledger: dict, nova: dict) -> list:
    out = []
    for svc, node, st, err in _q(cur,
            "SELECT service_name, node_name, status, left(coalesce(error_message,''), 80) FROM ("
            "SELECT DISTINCT ON (service_name, node_name) * FROM health_checks WHERE checked_at > now() - interval '2 hours' "
            "ORDER BY service_name, node_name, checked_at DESC) x WHERE status NOT IN ('ok','up','healthy')"):
        out.append(f"{svc}@{node} {st}" + (f" ({err})" if err else ""))
    for (feeds,) in _q(cur, "SELECT detail->'degraded_feeds' FROM presence_state WHERE person='jordan'"):
        if feeds:
            out.append("presence feeds degraded: " + ", ".join(feeds if isinstance(feeds, list) else [str(feeds)]))
    for sid, r in sorted(ledger.items()):
        if r.get("compromise_suspect"):
            out.append(f"source suspect: {sid} — {r.get('compromise_reason')}")
    for reason in nova.get("reasons", []):
        out.append(f"Nova degraded: {reason}")
    for kind, desc in _q(cur, "SELECT kind, left(description, 120) FROM unexplained_events "
                              "WHERE first_seen > now() - interval '24 hours' AND kind <> 'source_behaviour_change' "
                              "ORDER BY first_seen DESC LIMIT 5"):
        out.append(f"new unexplained ({kind}, cause unknown): {desc}")
    return out


def open_loops(cur, since: datetime) -> list:
    out = []
    for p, th, top in _q(cur, "SELECT pressure, threshold, top FROM boiler_state ORDER BY ts DESC LIMIT 1"):
        names = []
        for t in (top or [])[:3]:
            names.append(t.get("source") or t.get("name") or str(t)[:40] if isinstance(t, dict) else str(t)[:40])
        out.append(f"Boiler {p:.0f}/{th:.0f}" + (f": {', '.join(names)}" if names else ""))
    try:
        from nova_escalation import deferred_since
        for d in deferred_since(cur, since):
            out.append(f"held escalation #{d['id']} {d['source']}:{d['kind']} — {d['reason']}")
    except Exception:  # noqa: BLE001
        pass
    for (n,) in _q(cur, "SELECT count(*) FROM hotwash WHERE status='open'"):
        if n:
            out.append(f"{n} open hotwash item(s) awaiting the weekly roll-up")
    for (n,) in _q(cur, "SELECT count(*) FROM coagency_proposals WHERE status='pending_human'"):
        if n:
            out.append(f"{n} proposal(s) waiting on Little Mister")
    return out


def standing(cur) -> list:
    try:
        import nova_commanders_intent as CI
        lines = list(CI.standing_orders(cur))[:8]
        due = CI.due_for_review(cur)
        if due:
            lines.append(f"{len(due)} intent(s) due for reconfirmation")
        return [l if isinstance(l, str) else json.dumps(l, default=str) for l in lines]
    except Exception as e:  # noqa: BLE001
        return [f"standing orders unavailable ({str(e).splitlines()[0][:60]})"]


def expected(cur, hours: int = 12) -> list:
    out = []
    import nova_cardinal as C
    for _id, st, dom, conf, rb in _q(cur, "SELECT id, statement, domain, confidence, resolves_by FROM predictions "
                                          "WHERE status='open' AND resolves_by < now() + make_interval(hours => %s) "
                                          "ORDER BY resolves_by LIMIT 6", (hours,)):
        words, _q2 = C.estimative_line(conf, cur, dom)
        out.append(f"#{_id} by {rb.astimezone(W.TZ):%H:%M}: {st[:140]} — {words}")
    try:
        from nova_derry_clock import upcoming_cycles
        for c in upcoming_cycles(days=1, cur=cur):
            out.append(f"{c['kind']}: {c['label']} ({c['strength']})")
    except Exception:  # noqa: BLE001
        pass
    return out


def surprises(cur, ledger: dict) -> list:
    out = []
    for conf, st in _q(cur, "SELECT confidence, statement FROM predictions WHERE status='open' AND confidence >= 0.7 "
                            "ORDER BY confidence DESC LIMIT 3"):
        out.append(f"if this does NOT happen: {st[:120]} (stated {conf:.0%})")
    th = _q(cur, "SELECT value FROM service_config WHERE service='bodach_watch' AND key='threshold'")
    if th:
        out.append(f"a Bodach score above {th[0][0]} (it fires about twice a month)")
    sh = _q(cur, "SELECT value FROM service_config WHERE service='the_shine' AND key='baseline'")
    if sh and isinstance(sh[0][0], dict):
        out.append(f"Little Mister home and silent for more than {max(4.0, 1.5 * float(sh[0][0].get('jordan_p99') or 0)):.1f}h in waking hours")
    trusted = [k for k, v in ledger.items() if v.get("reliability") in ("A", "B") and k.startswith("detector:")]
    if trusted:
        out.append("any A/B-graded detector going silent: " + ", ".join(sorted(trusted)[:4]))
    out.append("a never-seen device joining the network with two independent witnesses")
    return out


def turnover(cur, watch: str) -> dict:
    import nova_cardinal as C
    try:
        from nova_escalation import nova_state
        nova = nova_state(cur)
    except Exception as e:  # noqa: BLE001
        nova = {"degraded": False, "reasons": [], "error": str(e)}
    ledger = C.load_ledger(cur)
    prev = _q(cur, "SELECT ts FROM watch_turnover ORDER BY ts DESC LIMIT 1")
    since = prev[0][0] if prev else W.now_utc() - timedelta(hours=12)
    return {"watch": watch, "at": W.now_utc().isoformat(), "since": since.isoformat(),
            "degraded": degraded(cur, ledger, nova), "open_loops": open_loops(cur, since),
            "standing": standing(cur), "expected": expected(cur), "surprise": surprises(cur, ledger),
            "nova": nova}


def render_turnover(t: dict) -> str:
    try:
        at = datetime.fromisoformat(t["at"]).astimezone(W.TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        at = str(t.get("at"))[:16]
    lines = [f"WATCH TURNOVER {t['watch']} ({at})"]
    for key, title in (("degraded", "Degraded"), ("open_loops", "Open loops"), ("standing", "Standing orders"),
                       ("expected", "Expected"), ("surprise", "Would surprise me")):
        items = t.get(key) or []
        lines.append(f"{title}: " + ("none" if not items else ""))
        lines += [f"  - {i}" for i in items]
    return "\n".join(W.journal_safe(l) for l in lines)


def save_turnover(cur, t: dict, text: str) -> None:
    cur.execute(SCHEMA)
    cur.execute("INSERT INTO watch_turnover (watch, body, text) VALUES (%s, %s::jsonb, %s)",
                (t["watch"], json.dumps(t, default=str), text))


def latest_turnover(cur, watch: str | None = None, max_age_h: int = 6) -> dict | None:
    rows = _q(cur, "SELECT body FROM watch_turnover WHERE (%s::text IS NULL OR watch=%s) "
                   "AND ts > now() - make_interval(hours => %s) ORDER BY ts DESC LIMIT 1", (watch, watch, max_age_h))
    return rows[0][0] if rows else None


# ── PDB ─────────────────────────────────────────────────────────────────────

def _detector_p(ledger: dict, sid: str):
    r = ledger.get(sid) or {}
    return r.get("hit_rate") if (r.get("n") or 0) >= 10 else None


def night_items(g: dict, ledger: dict) -> list:
    """Overnight evidence (nova_night_watch.gather) -> candidate PDB items. Pure."""
    items = []
    if g.get("bodach_fired") or g.get("bodach_types", 0) >= 2:
        items.append({"kind": "bodach", "importance": 0.85 if g.get("bodach_fired") else 0.6,
                      "text": f"Bodach Watch: independent signals clustered near home overnight "
                              f"(peak {g.get('bodach_max', 0):.2f}{', alerted' if g.get('bodach_fired') else ''}).",
                      "item": {"claim": "signals clustered", "sources": [{"id": "detector:nova_bodach_watch",
                                                                           "type": "detector"}]}})
    if g.get("newdev"):
        items.append({"kind": "network", "importance": 0.8,
                      "text": f"{len(g['newdev'])} never-seen device(s) joined the network overnight.",
                      "item": {"claim": "new device", "sources": [{"id": "detector:nova_security_organ",
                                                                    "type": "network", "upstream": ["udm"]}]},
                      "p": _detector_p(ledger, "detector:nova_security_organ")})
    if g.get("scanner"):
        med = sum(1 for r in g["scanner"] if W.is_medical(r[2]))
        chans = sorted({(r[3] if len(r) > 3 else None) or "scanner" for r in g["scanner"]})
        srcs = [{"id": f"scanner:{c}", "type": "radio"} for c in chans]
        if any(l.get("tight") for l in g.get("loiters", [])):
            srcs.append({"id": "adsb:loiter", "type": "adsb"})
        items.append({"kind": "scanner", "importance": 0.55 + (0.2 if med else 0.0),
                      "text": f"{len(g['scanner'])} scanner transmission(s) geocoded near home"
                              + (f", {med} with medical words" if med else "") + ".",
                      "item": {"claim": "police/fire activity near home", "sources": srcs}})
    if g.get("person_n", 0) > max(2, g.get("person_p95", 0)):
        deep = g.get("deep_person", 0)
        items.append({"kind": "cameras", "importance": 0.5 + (0.2 if deep else 0.0),
                      "text": f"Exterior cameras: {g['person_n']} person episodes vs a usual ceiling of "
                              f"{g.get('person_p95', 0):.0f}" + (f", {deep} between 01:00 and 05:00" if deep else "") + ".",
                      "item": {"claim": "more people outside than usual",
                               "sources": [{"id": f"camera:{z}", "type": "camera"} for z in (g.get("person_zones") or {"exterior": 1})]}})
    if any(l.get("tight") and l.get("hits", 0) >= 30 for l in g.get("loiters", [])):
        items.append({"kind": "air", "importance": 0.45, "text": "A helicopter held a sustained tight orbit nearby.",
                      "item": {"claim": "helicopter orbit", "sources": [{"id": "adsb:loiter", "type": "adsb"}]}})
    if g.get("buick"):
        kinds = sorted({k for k, _d in g["buick"]})
        items.append({"kind": "unexplained", "importance": 0.4,
                      "text": f"{len(g['buick'])} new unexplained event(s), cause unknown: {', '.join(kinds)}.",
                      "item": {"claim": "anomalies", "sources": [{"id": "detector:nova_buick8_log", "type": "detector"}]}})
    return items


def turnover_items(t: dict | None, ledger: dict) -> list:
    """Morning turnover -> candidate PDB items. Pure."""
    if not t:
        return []
    items = []
    deg = [d for d in t.get("degraded", []) if not d.startswith(("source suspect", "Nova degraded", "new unexplained"))]
    if deg:
        items.append({"kind": "degraded", "importance": 0.9 if len(deg) >= 3 else 0.7,
                      "text": f"{len(deg)} system(s) degraded: " + "; ".join(deg[:4]) + ".",
                      "item": {"claim": "systems degraded", "sources": [{"id": "detector:health_checks", "type": "detector"}]}})
    sus = [d for d in t.get("degraded", []) if d.startswith("source suspect")]
    if sus:
        items.append({"kind": "sources", "importance": 0.6,
                      "text": f"{len(sus)} of my sources are behaving unlike themselves (graded F until explained): "
                              + "; ".join(_short_suspect(x) for x in sus[:4]) + ".",
                      "item": {"claim": "sources suspect", "sources": [{"id": "detector:nova_cardinal", "type": "detector"}]}})
    nova = [d for d in t.get("degraded", []) if d.startswith("Nova degraded")]
    if nova:
        items.append({"kind": "self", "importance": 0.65, "text": "I am degraded: " + "; ".join(n[15:] for n in nova)
                      + ". My confidence is lowered and I am holding non-life-safety escalations.",
                      "item": {"claim": "Nova degraded", "sources": [{"id": "nova:reasoning"}]}})
    held = [o for o in t.get("open_loops", []) if o.startswith("held escalation")]
    if held:
        items.append({"kind": "held", "importance": 0.75, "text": f"{len(held)} escalation(s) held overnight: "
                      + "; ".join(h.split(' — ')[0][16:] for h in held[:3]) + ".",
                      "item": {"claim": "held escalations", "sources": [{"id": "nova:reasoning"}]}})
    boil = [o for o in t.get("open_loops", []) if o.startswith("Boiler")]
    if boil:
        try:
            p, th = boil[0].split()[1].rstrip(":").split("/")
            imp = min(0.7, 0.6 * float(p) / max(1.0, float(th)))
        except Exception:  # noqa: BLE001
            imp = 0.3
        items.append({"kind": "boiler", "importance": imp, "text": f"Open loops: {boil[0]}.",
                      "item": {"claim": "pressure", "sources": [{"id": "detector:nova_boiler", "type": "detector"}]}})
    exp = t.get("expected") or []
    if exp:
        items.append({"kind": "expected", "importance": 0.2, "text": "Expected today: " + "; ".join(exp[:2]) + ".",
                      "item": {"claim": "forecast", "sources": [{"id": "nova:prediction:all"}]}})
    return items


def _short_suspect(line: str) -> str:
    sid, _, why = line.replace("source suspect: ", "").partition(" — ")
    return f"{sid.split(':', 1)[-1]} ({why.split(':')[0].split(' is below')[0][:48]})"


def change_my_mind(it: dict) -> str:
    a = it.get("grade", {}).get("spinnaker") or {}
    have = set(a.get("independent_types") or [])
    want = [s for s in SENSOR_MENU if s not in have][:2]
    if it["kind"] in ("degraded", "boiler", "held", "self", "sources", "expected"):
        return {"degraded": "a clean recheck of the same services within the hour",
                "boiler": "the top loop closing on its own, or Little Mister closing it",
                "held": "Little Mister saying the held items were noise",
                "self": "two clean health probes in a row",
                "sources": "an explanation in the Buick 8 log (a deploy, a moved sensor)",
                "expected": "the resolution criteria, checked at the deadline"}[it["kind"]]
    if it["kind"] == "network":
        return "a witness outside the UDM (nova-core's ARP table) would raise it; Little Mister naming the device ends it"
    if a.get("verdict") == "CORROBORATED":
        return "an independent contradiction (e.g. presence or camera evidence of a routine cause)"
    return f"corroboration from {' or '.join(want)} would raise it; nothing else seeing it keeps it here"


def red_cell(lead: dict | None, ledger: dict) -> str:
    if not lead:
        return "Red cell: a quiet night can also mean a quiet sensor — check the gaps before trusting it."
    a = (lead.get("grade") or {}).get("spinnaker") or {}
    if lead["kind"] == "degraded":
        weak = [k for k, v in ledger.items() if k.startswith("detector:") and v.get("reliability") in ("D", "E")]
        return (f"Red cell: these may be checker failures, not service failures — {len(weak)} of my detectors "
                "grade D or worse.")
    if lead["kind"] == "expected":
        r = ledger.get("nova:prediction:all") or {}
        return (f"Red cell: my forecasts grade {r.get('reliability', 'F')} — the base rate is as good a guess as mine.")
    if a.get("shared_upstream"):
        return "Red cell: several of these signals come through one upstream — this may be one event counted twice."
    if a.get("verdict") in ("SINGLE_SOURCE", "UNCORROBORATED"):
        return "Red cell: one source, no corroboration — the routine explanation is the likelier one."
    return "Red cell: two sensor types agreeing is also what a busy ordinary night looks like."


def scorecard(cur) -> str:
    rows = _q(cur, "SELECT outcome, confidence, statement FROM predictions WHERE resolved_at > now() - interval '24 hours' "
                   "AND outcome IN ('correct','incorrect','partial')")
    if not rows:
        return "Scorecard: no predictions resolved in the last 24h."
    hv = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}
    n = len(rows)
    c = sum(1 for o, _c, _s in rows if o == "correct")
    i = sum(1 for o, _c, _s in rows if o == "incorrect")
    brier = sum((float(cf) - hv[o]) ** 2 for o, cf, _s in rows) / n
    miss = max(((cf, s) for o, cf, s in rows if o == "incorrect"), default=None)
    line = f"Scorecard (last 24h): {c}/{n} right, {i} wrong, Brier {brier:.2f} (0 is perfect, 0.25 is a coin)."
    if miss:
        line += f" Worst miss: \"{miss[1][:90]}\" at {float(miss[0]):.0%}."
    return line


def gaps(cur, t: dict | None, ledger: dict, g: dict | None) -> list:
    out = []
    no_truth = [k for k, v in ledger.items() if k.startswith("camera:") and v.get("truth_kind") == "none"]
    if no_truth:
        out.append(f"{len(no_truth)} cameras have no independent ground truth (graded F): I can count what they "
                   "see, not vouch for it.")
    for d in (t or {}).get("degraded", []):
        if d.startswith("presence feeds degraded"):
            out.append(d)
        elif d.startswith("source suspect: presence:"):
            out.append("blind: " + _short_suspect(d))
        elif d.startswith("Nova degraded"):
            out.append(d)
    if g is not None:
        if not g.get("scanner") and not _q(cur, "SELECT 1 FROM health_checks WHERE service_name ILIKE %s "
                                                "AND checked_at > now() - interval '12 hours' LIMIT 1", ("%scanner%",)):
            pass
        if not g.get("by_class"):
            out.append("no exterior camera detections at all overnight — quiet, or a silent pipeline.")
    return out[:5]


def compose_pdb(cur, g: dict, start, end, t: dict | None = None, ledger: dict | None = None) -> str:
    import nova_cardinal as C
    ledger = ledger if ledger is not None else C.load_ledger(cur)
    cands = night_items(g, ledger) + turnover_items(t, ledger)
    for it in cands:
        it["grade"] = C.grade(it["item"], ledger=ledger)
    cands.sort(key=lambda x: -x["importance"])
    items = cands[:6]
    day = end.astimezone(W.TZ)
    head = f"*PDB for Little Mister* — {day:%a %-d %b}, covering {start.astimezone(W.TZ):%H:%M}-{day:%H:%M}"
    if not items:
        bluf = "BLUF: nothing above baseline overnight and nothing degraded. Read the gaps before trusting that."
    else:
        lead = items[0]
        more = len(items) - 1
        bluf = f"BLUF: {lead['text']}" + (f" Plus {more} lesser item(s)." if more else "")
    lines = [head, bluf, ""]
    for k, it in enumerate(items, 1):
        gr = it["grade"]
        est = ""
        if it.get("p") is not None:
            words, q = C.estimative_line(it["p"], cur if it["kind"] == "expected" else None)
            est = f" Chance it matters: {words}, from this source's track record."
        lines.append(f"{k}. [{gr['code']}] {it['text']}{est}")
        lines.append(f"   Would change my mind: {change_my_mind(it)}.")
    lines.append("")
    gp = gaps(cur, t, ledger, g)
    lines.append("Gaps: " + (" ".join(f"({i}) {x}" for i, x in enumerate(gp, 1)) if gp else "none I know of."))
    lines.append(red_cell(items[0] if items else None, ledger))
    lines.append(scorecard(cur))
    try:
        import nova_night_watch as NW
        lines.append(f"Sleep evidence: {NW.sleep_line(g)}.")
    except Exception:  # noqa: BLE001
        pass
    lines.append("_Grades: A-F source reliability from track record, 1-6 credibility (1 = two independent sensor types)._")
    return "\n".join(W.journal_safe(l) if l else l for l in lines)


# ── CLI ─────────────────────────────────────────────────────────────────────

def current_watch(now: datetime) -> str:
    h = now.astimezone(W.TZ).hour + now.astimezone(W.TZ).minute / 60
    return "0630" if 4 <= h < 12 else "1800" if 12 <= h < 21 else "2300"


def selftest() -> int:
    led = {"detector:nova_security_organ": {"reliability": "D", "hit_rate": 0.54, "n": 35}}
    g = {"bodach_fired": False, "bodach_types": 0, "newdev": [1], "scanner": [], "person_n": 0, "person_p95": 3,
         "loiters": [], "buick": [], "by_class": {"car": 3}}
    items = night_items(g, led)
    assert items and items[0]["kind"] == "network" and items[0]["p"] == 0.54
    t = {"degraded": ["a@b down", "c@d down", "e@f down", "source suspect: x — y"], "open_loops": ["Boiler 120/100: q"],
         "expected": []}
    ti = turnover_items(t, led)
    assert ti[0]["kind"] == "degraded" and ti[0]["importance"] == 0.9
    assert "checker" in red_cell(dict(ti[0], grade={"spinnaker": {}}), led)
    assert "corroboration" in change_my_mind({"kind": "scanner", "grade": {"spinnaker": {"independent_types": ["radio"]}}})
    assert current_watch(datetime(2026, 1, 1, 6, 30, tzinfo=W.TZ)) == "0630"
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--turnover", action="store_true")
    ap.add_argument("--watch", choices=WATCHES)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--pdb-preview", action="store_true", help="render today's PDB without posting")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    conn = W.connect()
    cur = conn.cursor()
    if a.show:
        for (txt,) in _q(cur, "SELECT text FROM watch_turnover ORDER BY ts DESC LIMIT 1"):
            print(txt)
        return 0
    if a.pdb_preview:
        import nova_night_watch as NW
        start, end = NW.night_bounds(datetime.now(W.TZ).date())
        print(compose_pdb(cur, NW.gather(cur, start, end), start, end, latest_turnover(cur, "0630", 24)))
        return 0
    if a.turnover:
        t = turnover(cur, a.watch or current_watch(W.now_utc()))
        text = render_turnover(t)
        print(text)
        if not a.dry_run:
            save_turnover(cur, t, text)
            try:
                from nova_restraint import record_restraint  # noqa: F401 — turnovers are logged, not posted
            except Exception:  # noqa: BLE001
                pass
            W.log(TAG, f"turnover {t['watch']} saved")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
