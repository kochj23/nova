#!/usr/bin/env python3
"""
nova_live_docs.py — live values in identity documents (six-month build #6, 2026-09-28).

Nova's soul said "877,000+ memories", her identity card said "1.3M+ vectors, 177 scripts",
and the live count was 2.24M. The reconciler files the drift but, by rule, never rewrites
her documents. So the documents now carry placeholders for anything measurable, and this
module fills them at load time from the same probes the reconciler trusts:

    {{memory_count}}   memory-server /stats count, e.g. "2,241,191"
    {{script_count}}   number of nova_*.py scripts in the scripts dir
    {{node_count}}     distinct nodes with a service heartbeat in the last 24h
    {{as_of}}          today's date

Values are cached in-process for CACHE_S seconds so a chat turn never blocks on a probe.
If a probe fails, the placeholder renders as "(unmeasured)" — a known unknown beats a
stale number, which is the whole point.

  nova_live_docs.py                  # print the rendered identity + soul
  nova_live_docs.py --write-workspace  # regenerate ~/.openclaw/workspace/{IDENTITY,SOUL,USER}.md
  nova_live_docs.py --selftest
"""
import json
import re
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
MEMSRV = "http://memory-server.digitalnoise.net:18790/stats"
SCRIPTS = Path(__file__).resolve().parent
WORKSPACE = Path.home() / ".openclaw" / "workspace"
CACHE_S = 600
_PH = re.compile(r"\{\{\s*(memory_count|script_count|node_count|as_of)\s*\}\}")
_cache = {"at": 0.0, "vals": {}}


def _probe():
    vals = {"as_of": date.today().isoformat()}
    try:
        with urllib.request.urlopen(MEMSRV, timeout=3) as r:
            vals["memory_count"] = f"{int(json.load(r)['count']):,}"
    except Exception:  # noqa: BLE001
        pass
    try:
        vals["script_count"] = str(len(list(SCRIPTS.glob("nova_*.py"))))
    except Exception:  # noqa: BLE001
        pass
    try:
        import psycopg2
        with psycopg2.connect(OPS_DSN, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("SELECT count(DISTINCT node_name) FROM service_registry WHERE last_heartbeat > now() - interval '24 hours'")
            vals["node_count"] = str(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        pass
    return vals


def values(force=False):
    if force or time.time() - _cache["at"] > CACHE_S:
        _cache["vals"] = _probe(); _cache["at"] = time.time()
    return _cache["vals"]


def render(text, vals=None):
    """Substitute placeholders; unknown/unmeasured -> '(unmeasured)'. Pure given vals."""
    v = vals if vals is not None else values()
    return _PH.sub(lambda m: v.get(m.group(1), "(unmeasured)"), text or "")


def load_docs(doc_types=("identity", "soul", "user")):
    import psycopg2
    with psycopg2.connect(OPS_DSN, connect_timeout=3) as c, c.cursor() as cur:
        cur.execute("SELECT doc_type, content FROM agent_docs WHERE agent_id='all' AND doc_type = ANY(%s)", (list(doc_types),))
        return dict(cur.fetchall())


def write_workspace():
    docs = load_docs()
    v = values(force=True)
    for dt, fname in (("identity", "IDENTITY.md"), ("soul", "SOUL.md"), ("user", "USER.md")):
        if dt in docs:
            (WORKSPACE / fname).write_text(render(docs[dt], v))
            print(f"wrote {fname} ({len(docs[dt])} chars, memory_count={v.get('memory_count', '(unmeasured)')})")


def demo():
    v = {"memory_count": "2,241,191", "script_count": "545", "node_count": "8", "as_of": "2026-09-28"}
    assert render("I have {{memory_count}} memories across {{ node_count }} nodes.", v) == "I have 2,241,191 memories across 8 nodes."
    assert render("{{script_count}} scripts, {{bogus}}", v) == "545 scripts, {{bogus}}"
    assert render("{{memory_count}}", {}) == "(unmeasured)"
    assert render("no placeholders", v) == "no placeholders"
    print("all live-docs assertions passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        demo()
    elif "--write-workspace" in sys.argv:
        write_workspace()
    else:
        for dt, body in load_docs(("identity", "soul")).items():
            print(f"=== {dt} ===\n{render(body)[:1200]}\n")
