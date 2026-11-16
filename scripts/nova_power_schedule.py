#!/usr/bin/env python3
"""nova_power_schedule.py — which nodes are OFF ON PURPOSE right now (nightly power schedule).

2026-11-16, Jordan: cut power, heat and noise in the master bedroom. nova-core6 and nova-core7
power off at 22:00 and back on at 10:00 (core7: systemd timer + rtcwake RTC alarm; core6: pmset
repeat). While a node is inside its window, nova_notify downgrades warning/critical alerts that
name it to info, so a scheduled outage never pages. GRACE minutes after "on" cover the boot; a
node that fails to come back pages normally after that.

Config: service_config(service='node_power_schedule', key='nodes') =
  {"nova-core7": {"aliases": ["192.168.1.197", ...], "off": "22:00", "on": "10:00"}, ...}
Fail-open: any error -> no node is considered scheduled-off (alerts flow as usual).
"""
import os
import re
import time
from datetime import datetime, time as dtime

DSN = os.environ.get("NOVA_OPS_DSN", "host=localhost dbname=nova_ops user=kochj")
GRACE_MIN = 20
_CACHE = {"t": 0.0, "nodes": {}}


def _load():
    if time.time() - _CACHE["t"] < 60:
        return _CACHE["nodes"]
    nodes = {}
    try:
        import psycopg2
        with psycopg2.connect(DSN, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE service='node_power_schedule' AND key='nodes'")
            row = cur.fetchone()
            nodes = row[0] if row and isinstance(row[0], dict) else {}
    except Exception:
        nodes = {}
    _CACHE.update(t=time.time(), nodes=nodes)
    return nodes


def _hm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_window(off, on, now=None, grace_min=GRACE_MIN):
    """True if `now` (local) is between off and on+grace; handles windows crossing midnight."""
    now = (now or datetime.now()).time()
    o = _hm(off)
    e_total = _hm(on).hour * 60 + _hm(on).minute + grace_min
    e = dtime((e_total // 60) % 24, e_total % 60)
    if o <= e:
        return o <= now < e
    return now >= o or now < e


def off_nodes(now=None, nodes=None):
    """Names of nodes currently inside their scheduled off-window."""
    nodes = _load() if nodes is None else nodes
    out = []
    for name, spec in nodes.items():
        try:
            if in_window(spec["off"], spec["on"], now):
                out.append(name)
        except Exception:
            continue
    return out


def mentioned_off_node(text, now=None, nodes=None):
    """The first scheduled-off node named (by name or alias) in `text`, else None."""
    if not text:
        return None
    nodes = _load() if nodes is None else nodes
    for name in off_nodes(now, nodes):
        for token in [name, *nodes[name].get("aliases", [])]:
            if re.search(rf"(?<![\w.]){re.escape(token)}(?![\w])", text):
                return name
    return None
