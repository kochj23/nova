#!/usr/bin/env python3
"""nova_spinnaker.py — the SPINNAKER test: is this conclusion corroborated, or one voice heard twice?

In Clancy's world an analyst never escalates on a single source with a motive. Before any
conclusion Nova reaches is allowed to trigger an escalation, a recommendation or an outbound
message, it goes through this test:

  1. COUNT INDEPENDENT SOURCES. Sources are grouped by shared upstream (union-find): two
     cameras on the same NVR / Frigate box are ONE source; two news articles that are both the
     same AP wire story are ONE source; UniFi's client table and the UDM's DHCP log are both the
     UDM. Only groups count.
  2. SHARED UPSTREAM. Every merge is reported (which sources, which upstream) so the reader sees
     why "three witnesses" became one.
  3. MOTIVE. Nova's own reasoning, her predictions, and LLM-generated text carry a motive (she
     wants to be right; a generator is rewarded for answering). They never count as a sensor.
  4. CONFIRMATION OF EXPECTATION. When the conclusion is what Nova already expected (an open
     prediction, a standing worry), it needs ONE MORE independent group than usual.

Verdicts:
  CORROBORATED     >= 2 independent sensor groups (3 when it confirms an expectation).
  SINGLE_SOURCE    one independent group, no motive: may ask, never mention or above.
  UNCORROBORATED   single source WITH motive, or nothing but reasoning: journal only.
  CONTESTED        an independent source contradicts it: may ask, nothing more.
Only CORROBORATED may trigger anything above "ask".

may_trigger(item, rung) is the gate other organs call. Rungs, least to most invasive:
journal < ask < mention < recommend < escalate < act.

Library only; no DB, no network. `--selftest` runs the worked examples.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import json
import re
import sys

# Non-sensor source types: they carry motive and never count toward corroboration.
MOTIVE_TYPES = {
    "reasoning": "Nova's own inference — she wants her reading to be right",
    "prediction": "Nova's forecast — she wants it to come true",
    "llm": "generated text — a model is rewarded for producing an answer",
}
SENSOR_TYPES = {"camera", "face", "radio", "news", "network", "adsb", "traffic", "rf_presence",
                "gps", "mmwave", "power", "ha", "detector", "absence", "human_report", "weather"}

# Source-id prefix -> upstream(s). Anything sharing an upstream element is ONE source.
DEFAULT_UPSTREAMS = {
    "camera:": ["unifi-protect", "frigate"],
    "face:": ["unifi-protect"],                 # face recognition reads the same camera frames
    "chp:": ["chp-cad"],
    "adsb:": ["adsb.lol"],
    "network:unifi": ["udm"],
    "network:dhcp": ["udm"],                    # the UDM's own DHCP server log — same box as stat/sta
    "network:arp": ["nova-core-arp"],
    "presence:wifi_rssi": ["udm"],
    "presence:wifi_home": ["udm"],
    "presence:ble_rssi": ["ble-office-scanner"],
    "presence:gps_tracker": ["ha", "phone-gps"],
    "presence:mmwave": ["novahomekit"],
    "presence:camera_vision": ["unifi-protect", "frigate"],
    "presence:vehicle_vision": ["unifi-protect", "frigate"],
    "ha:": ["home-assistant"],
    "nova:": ["nova"],
    "llm:": ["llm"],
}
RUNGS = ("journal", "ask", "mention", "recommend", "escalate", "act")

_WIRE = [
    (re.compile(r"\(AP\)|\bAssociated Press\b|\bAP News\b|apnews\.com", re.I), "wire:ap"),
    (re.compile(r"\(Reuters\)|\bReuters\b|reuters\.com", re.I), "wire:reuters"),
    (re.compile(r"\(AFP\)|\bAgence France-Presse\b|\bAFP\b"), "wire:afp"),
    (re.compile(r"\bCity News Service\b|\(CNS\)", re.I), "wire:cns"),
    (re.compile(r"\bBloomberg News\b", re.I), "wire:bloomberg"),
]


def wire_of(text: str | None) -> str | None:
    """The wire service a news text is carrying, if it says so."""
    for rx, tag in _WIRE:
        if rx.search(text or ""):
            return tag
    return None


def upstreams(src: dict, overrides: dict | None = None) -> set:
    """Upstream elements for one source dict {id, type, upstream?, text?}."""
    sid = str(src.get("id") or "")
    out = set(src.get("upstream") or [])
    table = dict(DEFAULT_UPSTREAMS)
    table.update(overrides or {})
    for prefix, ups in table.items():
        if sid.startswith(prefix):
            out.update(ups)
    if sid.startswith("scanner:"):
        out.add("radio:" + sid.split(":", 1)[1].strip().lower())     # one talkgroup = one dispatcher
    if sid.startswith("news:"):
        out.add("outlet:" + sid.split(":", 1)[1].strip().lower())
    w = wire_of(src.get("text"))
    if w:
        out.add(w)
    if not out:
        out.add("self:" + sid)            # unknown provenance: independent only of itself
    return out


def stype(src: dict) -> str:
    t = (src.get("type") or "").strip().lower()
    if t:
        return t
    sid = str(src.get("id") or "")
    for pfx, ty in (("camera:", "camera"), ("face:", "face"), ("scanner:", "radio"), ("news:", "news"),
                    ("network:", "network"), ("adsb:", "adsb"), ("chp:", "traffic"), ("nova:pred", "prediction"),
                    ("nova:", "reasoning"), ("llm:", "llm"), ("presence:mmwave", "mmwave"),
                    ("presence:gps", "gps"), ("presence:", "rf_presence"), ("ha:", "ha"),
                    ("detector:", "detector")):
        if sid.startswith(pfx):
            return ty
    return "unknown"


def group(sources: list, overrides: dict | None = None) -> tuple[list, list]:
    """Union-find over shared upstream. -> (groups [[src,...]], merges [(id_a, id_b, shared)])."""
    n = len(sources)
    parent = list(range(n))
    ups = [upstreams(s, overrides) for s in sources]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    merges = []
    for i in range(n):
        for j in range(i + 1, n):
            shared = ups[i] & ups[j]
            if shared:
                merges.append((sources[i].get("id"), sources[j].get("id"), sorted(shared)))
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(sources[i])
    return list(groups.values()), merges


def _motive(src: dict) -> str | None:
    if src.get("motive"):
        return str(src["motive"]) if src["motive"] is not True else "stated motive"
    return MOTIVE_TYPES.get(stype(src))


def assess(item: dict, overrides: dict | None = None) -> dict:
    """item = {"claim": str, "sources": [{id, type?, upstream?, motive?, text?}],
               "contradicted_by": [sources], "expected": bool}
    -> {"verdict", "independent", "independent_types", "groups", "shared_upstream", "motive",
        "expectation", "needed", "reasons", "max_rung"}"""
    sources = [s for s in (item.get("sources") or []) if s and s.get("id")]
    sensors = [s for s in sources if stype(s) not in MOTIVE_TYPES]
    motive = {s["id"]: _motive(s) for s in sources if _motive(s)}
    groups, merges = group(sensors, overrides)
    indep = len(groups)
    types = sorted({stype(s) for g in groups for s in g})
    # independent TYPES: a type counts once per group that carries it (a group of one type each)
    expected = bool(item.get("expected"))
    needed = 3 if expected else 2
    reasons = []
    for a, b, shared in merges:
        reasons.append(f"{a} and {b} share upstream {', '.join(shared)} — counted once")
    contra = [s for s in (item.get("contradicted_by") or []) if s and s.get("id")]
    contra_indep = False
    if contra:
        sup_ups = set().union(*(upstreams(s, overrides) for s in sensors)) if sensors else set()
        contra_indep = any(not (upstreams(c, overrides) & sup_ups) and stype(c) not in MOTIVE_TYPES for c in contra)
    motivated = any(motive.get(s["id"]) for s in sensors)
    if contra_indep:
        reasons.append("an independent source contradicts it: " + ", ".join(c["id"] for c in contra))
    if not sensors:
        verdict = "UNCORROBORATED"
        reasons.append("no sensor evidence — only reasoning/prediction/generated text")
    elif contra_indep:
        verdict = "CONTESTED"
    elif indep >= needed:
        verdict = "CORROBORATED"
    elif indep == 1 and motivated:
        verdict = "UNCORROBORATED"
        reasons.append("single source with a motive")
    else:
        verdict = "SINGLE_SOURCE"
        if expected and indep >= 2:
            reasons.append(f"it confirms what Nova already expected — needs {needed} independent sources, has {indep}")
    if expected:
        reasons.append("confirmation of expectation: the bar is one independent source higher")
    # only CORROBORATED may go above "ask"; SINGLE_SOURCE and CONTESTED may ask, never mention or act
    max_rung = {"CORROBORATED": "act", "SINGLE_SOURCE": "ask", "CONTESTED": "ask",
                "UNCORROBORATED": "journal"}[verdict]
    return {"verdict": verdict, "independent": indep, "independent_types": types,
            "groups": [[s["id"] for s in g] for g in groups],
            "shared_upstream": [{"a": a, "b": b, "shared": sh} for a, b, sh in merges],
            "motive": motive, "expectation": expected, "needed": needed,
            "reasons": reasons, "max_rung": max_rung}


def independent_types(item: dict, overrides: dict | None = None) -> int:
    """How many DIFFERENT sensor types survive independence grouping (credibility 1 needs 2)."""
    a = assess(item, overrides)
    return len(a["independent_types"]) if a["independent"] >= 2 else (1 if a["independent"] else 0)


def may_trigger(item: dict, rung: str, overrides: dict | None = None) -> tuple[bool, dict]:
    """Gate: (allowed, assessment). rung in RUNGS. Journal is always allowed."""
    a = assess(item, overrides)
    rung = rung if rung in RUNGS else "act"
    return RUNGS.index(rung) <= RUNGS.index(a["max_rung"]), a


def line(a: dict) -> str:
    """One human line for a message footer."""
    s = f"SPINNAKER: {a['verdict']} — {a['independent']} independent source(s)"
    if a["independent_types"]:
        s += f" ({', '.join(a['independent_types'])})"
    if a["shared_upstream"]:
        s += f"; {len(a['shared_upstream'])} shared-upstream merge(s)"
    if a["expectation"]:
        s += "; confirms an expectation"
    return s


def selftest() -> int:
    two_cams = {"claim": "person in yard", "sources": [{"id": "camera:front_yard"}, {"id": "camera:front_door"}]}
    a = assess(two_cams)
    assert a["independent"] == 1 and a["verdict"] == "SINGLE_SOURCE", a
    wire = {"claim": "x", "sources": [{"id": "news:LA Times", "text": "LOS ANGELES (AP) — ..."},
                                      {"id": "news:KTLA", "text": "The Associated Press reported"}]}
    assert assess(wire)["independent"] == 1
    cam_radio = {"claim": "y", "sources": [{"id": "camera:alley_north"}, {"id": "scanner:Burbank PD"}]}
    assert assess(cam_radio)["verdict"] == "CORROBORATED"
    motive = {"claim": "z", "sources": [{"id": "nova:reasoning"}]}
    assert assess(motive)["verdict"] == "UNCORROBORATED" and not may_trigger(motive, "mention")[0]
    lone_motive = {"claim": "z", "sources": [{"id": "news:Some Blog", "motive": "sells the product"}]}
    assert assess(lone_motive)["verdict"] == "UNCORROBORATED"
    udm = {"claim": "new device", "sources": [{"id": "network:unifi"}, {"id": "network:dhcp"}]}
    assert assess(udm)["independent"] == 1
    exp = dict(cam_radio, expected=True)
    assert assess(exp)["verdict"] == "SINGLE_SOURCE"
    con = dict(cam_radio, contradicted_by=[{"id": "presence:mmwave"}])
    assert assess(con)["verdict"] == "CONTESTED"
    print("selftest ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if len(sys.argv) > 1 and sys.argv[1] not in ("-h", "--help"):
        print(json.dumps(assess(json.loads(sys.argv[1])), indent=1))
        sys.exit(0)
    print(__doc__)
