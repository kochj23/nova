#!/usr/bin/env python3
"""
nova_prober.py — synthetic end-to-end probes for Nova's stack.

WHY THIS EXISTS: a previous monitor watched *pageviews* as a proxy for health and
declared a site "down for 9 days" while it was actually serving 200s the whole
time. Proxies lie. This prober does the REAL action and asserts REAL success:
it fetches the actual page and checks the actual bytes, writes a memory and reads
it back out of the database, asks Ollama for an actual 768-dim vector, and runs a
SELECT against each Postgres it depends on.

Each probe is a small function returning (ok: bool, detail: str). The runner:
  - times every probe and records a row in telemetry.probe_results (uptime/SLO history),
  - emits via nova_notify.notify ONLY on state changes:
        steady success      -> silent
        success -> failure  -> critical/warning alert (per probe policy)
        failure -> recovery -> info "recovered" alert
  - dedups alerts per probe via dedup_key=f"probe-{name}" so a flapping probe
    doesn't storm the bus (the notifier/correlator handle the rest).

"Was it failing before?" is answered from telemetry.probe_results itself (the last
row for that probe), so recovery detection survives restarts — no in-process state.

Run:
    python3 nova_prober.py            # one full sweep
    python3 nova_prober.py --once     # same (explicit); exits non-zero if any probe failed
    python3 nova_prober.py --list     # list probes and exit
    python3 nova_prober.py --quiet    # suppress per-probe stdout (still records + alerts)

Never raises out of a probe — a probe failure is a result, not a crash. A probe that
throws is recorded as ok=False with the exception text as detail.
"""
import argparse
import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.error

import psycopg2
import psycopg2.extensions

# nova_notify lives alongside this file; make the import robust to cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nova_notify import notify  # noqa: E402

# ---------------------------------------------------------------------------
# Config — all the real endpoints this stack must actually be able to do.
# ---------------------------------------------------------------------------
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"

OLLAMA = "http://127.0.0.1:11434"
EMBED_MODEL = "nomic-embed-text"

MEMORY_REMEMBER_URL = "http://memory-server.digitalnoise.net:18790/remember"

HTTP_TIMEOUT = 12       # seconds per HTTP probe (short — a hang IS a failure)
PG_CONNECT_TIMEOUT = 8

# HTTP checks: (url, expected content substring). Asserting bytes — not status
# alone — is the whole point: a 200 serving the wrong/blank page must FAIL.
HTTP_CHECKS = [
    ("https://nova.digitalnoise.net/", "<html"),
    ("https://digitalnoise.net/",      "digitalnoise.net"),
    ("http://192.168.1.2:3000/api/health", '"database": "ok"'),
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _http_get(url, timeout=HTTP_TIMEOUT):
    """GET following redirects; return (status, body_text). Raises on transport error."""
    req = urllib.request.Request(url, headers={"User-Agent": "nova-prober/1.0"})
    # urllib follows redirects by default for GET via HTTPRedirectHandler.
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status = r.getcode()
        raw = r.read()
    try:
        body = raw.decode("utf-8", "replace")
    except Exception:
        body = repr(raw[:500])
    return status, body


def _ops_conn():
    return psycopg2.connect(OPS_DSN, connect_timeout=PG_CONNECT_TIMEOUT)


def _mem_conn():
    return psycopg2.connect(MEM_DSN, connect_timeout=PG_CONNECT_TIMEOUT)


# ---------------------------------------------------------------------------
# Probes  — each returns (ok: bool, detail: str). They must not raise; the
# runner wraps them, but keeping them clean makes detail messages precise.
# ---------------------------------------------------------------------------
def probe_http():
    """GET every public/internal endpoint and assert status 200 AND the expected
    content substring is present. This is the anti-'9-days-down' probe."""
    failures = []
    oks = []
    for url, needle in HTTP_CHECKS:
        try:
            status, body = _http_get(url)
            if status != 200:
                failures.append(f"{url} -> HTTP {status}")
            elif needle not in body:
                failures.append(f"{url} -> 200 but missing {needle!r} (got {len(body)}B)")
            else:
                oks.append(url)
        except urllib.error.HTTPError as e:
            failures.append(f"{url} -> HTTP {e.code}")
        except Exception as e:
            failures.append(f"{url} -> {type(e).__name__}: {e}")
    if failures:
        return False, "; ".join(failures)
    return True, f"{len(oks)} endpoints 200 + content OK"


def probe_memory_roundtrip():
    """Write a unique memory through the real /remember API, then read it back out
    of nova_memories.public.memories by id, then delete it. Asserts the write
    pipeline (embed -> store) actually landed a row — not just that the API said OK."""
    token = uuid.uuid4().hex
    # /remember rejects very short text ("too_short"); make it comfortably long.
    text = (f"nova_prober synthetic memory roundtrip probe — do not keep — "
            f"token {token} ts {int(time.time())}")

    # 1) write via the real API
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        MEMORY_REMEMBER_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "nova-prober/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            resp = json.loads(r.read())
    except Exception as e:
        return False, f"remember POST failed: {type(e).__name__}: {e}"

    mem_id = resp.get("id")
    if not mem_id or resp.get("status") not in (None, "stored"):
        return False, f"remember rejected/odd response: {resp}"

    # 2) read it back from the DB by id (the actual landed row)
    found = False
    try:
        with _mem_conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, text FROM public.memories WHERE id = %s", (mem_id,))
            row = cur.fetchone()
            found = row is not None and token in (row[1] or "")
    except Exception as e:
        return False, f"readback query failed: {type(e).__name__}: {e}"
    finally:
        # 3) clean up regardless — never leave probe litter in memory
        try:
            with _mem_conn() as c, c.cursor() as cur:
                cur.execute("DELETE FROM public.memories WHERE id = %s", (mem_id,))
                c.commit()
        except Exception:
            pass

    if not found:
        return False, f"wrote id={mem_id} but could not read it back from DB"
    return True, f"roundtrip ok (id={mem_id})"


def probe_embedding():
    """Ask Ollama nomic-embed-text for a real embedding and assert a 768-float vector."""
    payload = json.dumps({"model": EMBED_MODEL,
                          "prompt": "nova prober embedding health check"}).encode()
    req = urllib.request.Request(
        OLLAMA + "/api/embeddings", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            data = json.loads(r.read())
    except Exception as e:
        return False, f"embeddings call failed: {type(e).__name__}: {e}"
    vec = data.get("embedding")
    if not isinstance(vec, list):
        return False, f"no embedding in response: {str(data)[:200]}"
    if len(vec) != 768:
        return False, f"expected 768 dims, got {len(vec)}"
    if not all(isinstance(x, (int, float)) for x in vec[:8]):
        return False, "embedding contained non-numeric values"
    return True, f"768-dim vector from {EMBED_MODEL}"


def probe_postgres():
    """SELECT 1 against both nova_ops and nova_memories — assert each is reachable."""
    fails = []
    for label, dsn in (("nova_ops", OPS_DSN), ("nova_memories", MEM_DSN)):
        try:
            with psycopg2.connect(dsn, connect_timeout=PG_CONNECT_TIMEOUT) as c, c.cursor() as cur:
                cur.execute("SELECT 1")
                if cur.fetchone()[0] != 1:
                    fails.append(f"{label}: unexpected SELECT 1 result")
        except Exception as e:
            fails.append(f"{label}: {type(e).__name__}: {e}")
    if fails:
        return False, "; ".join(fails)
    return True, "nova_ops + nova_memories reachable"


# ---------------------------------------------------------------------------
# Probe registry
# ---------------------------------------------------------------------------
_TUNNEL_ID = "a20ae87c-c869-4cb0-83de-cc6c663df763"  # nova-chatroom Cloudflare tunnel


def probe_cloudflared():
    """The Cloudflare tunnel is part of Nova — it fronts digitalnoise.net, chat,
    gauges, analytics. It now runs HA on .2 + .10 (off the GPU box). This checks the
    tunnel has at least one live connector via the CF API (run from .6, which holds
    the management cert), so a total ingress outage is caught."""
    import subprocess
    try:
        info = subprocess.run(["/opt/homebrew/bin/cloudflared", "tunnel", "info", _TUNNEL_ID],
                              capture_output=True, text=True, timeout=20)
        out = info.stdout or ""
        n = out.count("linux_amd64") + out.count("darwin")
        if "CONNECTOR ID" not in out or n == 0:
            return False, "no active tunnel connectors (.2 AND .10 down?)"
        return True, f"tunnel up, {n} connector(s) active"
    except Exception as e:
        return False, f"tunnel check failed: {type(e).__name__}: {e}"


def probe_inference_vantage():
    """CONSUMER-SIDE check: don't ask a node if it's healthy — ask the thing
    that depends on it whether it's getting answers.

    core3 (2026-07-26) passed every health check for weeks while the inference
    router could not reach it at all: its own ufw allowed only .6, so the check
    ran from the one vantage point that worked. A node saying "I'm fine" is not
    evidence; the consumer proving it gets served is.

    Fails on a VANTAGE GAP: a backend this prober can reach directly but the
    router — the actual consumer — cannot. A node that's down from BOTH
    vantage points is just down, and belongs to the ordinary liveness check.
    """
    router = "http://192.168.1.2:37475/pool/status"
    try:
        with urllib.request.urlopen(router, timeout=8) as r:
            pool = json.loads(r.read()).get("backends", {})
    except Exception as e:
        return False, f"cannot reach inference router (the consumer): {type(e).__name__}: {e}"
    if not pool:
        return False, "router returned an empty backend list — nothing to witness"

    gaps, agreed_down = [], []
    for addr, info in pool.items():
        if info.get("healthy"):
            continue
        host, _, port = addr.partition(":")
        try:  # second opinion from THIS vantage point
            url = f"http://{host}:{port}/" + ("v1/models" if port == "5050" else "api/version")
            with urllib.request.urlopen(url, timeout=4) as r:
                reachable_here = r.status == 200
        except Exception:
            reachable_here = False
        (gaps if reachable_here else agreed_down).append(addr)

    healthy = sum(1 for i in pool.values() if i.get("healthy"))
    if gaps:
        return False, (f"VANTAGE GAP: {', '.join(gaps)} reachable from the prober but NOT "
                       f"from the inference router that depends on them — check firewall/routing "
                       f"(this is the core3 failure mode). {healthy}/{len(pool)} backends healthy.")
    note = f"; {len(agreed_down)} down from both vantages ({', '.join(agreed_down)})" if agreed_down else ""
    return True, f"consumer and prober agree on all {len(pool)} backends; {healthy} healthy{note}"


PROBES = [
    {"name": "inference_vantage", "fn": probe_inference_vantage,
     "level_on_fail": "warning", "category": "probe",
     "host": "192.168.1.2"},
    {"name": "cloudflared_tunnel", "fn": probe_cloudflared,
     "level_on_fail": "critical", "category": "tunnel",
     "host": "Office-M4-2"},
    {"name": "http_endpoints",   "fn": probe_http,
     "level_on_fail": "critical", "category": "probe",
     "host": "digitalnoise.net"},
    {"name": "memory_roundtrip", "fn": probe_memory_roundtrip,
     "level_on_fail": "critical", "category": "probe",
     "host": "192.168.1.6"},
    {"name": "embedding",        "fn": probe_embedding,
     "level_on_fail": "warning",  "category": "probe",
     "host": "127.0.0.1"},
    {"name": "postgres",         "fn": probe_postgres,
     "level_on_fail": "critical", "category": "probe",
     "host": "127.0.0.1"},
]


# ---------------------------------------------------------------------------
# Result persistence + state-change alerting
# ---------------------------------------------------------------------------
def _last_ok(conn, probe):
    """Return the previous probe's ok value (True/False) or None if no history."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute(
            "SELECT ok FROM telemetry.probe_results WHERE probe = %s "
            "ORDER BY ts DESC LIMIT 1", (probe,))
        row = cur.fetchone()
    return None if row is None else bool(row[0])


def _record(conn, probe, ok, latency_ms, detail):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry.probe_results (probe, ok, latency_ms, detail) "
            "VALUES (%s, %s, %s, %s)",
            (probe, ok, latency_ms, (detail or "")[:2000]))
    conn.commit()


def run_probe(conn, spec, quiet=False):
    """Run one probe: execute, time, persist, and alert on state change.
    Returns the bool ok."""
    name = spec["name"]
    prev = _last_ok(conn, name)            # None on first-ever run

    t0 = time.time()
    try:
        ok, detail = spec["fn"]()
    except Exception as e:                 # a probe must never crash the sweep
        ok, detail = False, f"probe raised {type(e).__name__}: {e}"
    latency_ms = int((time.time() - t0) * 1000)

    # Minimum grain: a pass with no evidence body, or one returned faster than
    # physics allows, did not actually check anything — it produced an absence
    # wearing a green hat. Downgrade it to a failure rather than banking a
    # counterfeit green. (herd thread 2026-07-26; see nova_witness.)
    try:
        from nova_witness import check_grain
        ok, detail = check_grain(ok, detail, latency_ms)
    except ImportError:
        pass

    try:
        _record(conn, name, ok, latency_ms, detail)
    except Exception:
        # never let history-writing failure mask the probe result
        pass

    meta = {"probe": name, "host": spec.get("host"),
            "latency_ms": latency_ms, "detail": detail}

    # Host/service prominent in the title so the alert is identifiable at a
    # glance, e.g. "PROBE FAIL: embedding @ 127.0.0.1".
    host = spec.get("host")
    where = f" @ {host}" if host else ""

    # State-change alerting: quiet on steady success.
    # FAIL and RECOVERED use DISTINCT dedup keys: with a shared key, a flapping
    # probe's RECOVERED post consumed the dedup slot and the next real FAIL was
    # silently suppressed (observed on inference_vantage, 2026-07-29). FAILs get
    # a 6h window so a flapper pages at most 4x/day; recoveries keep the default.
    # A notification-bus outage must never abort the rest of the sweep: an
    # unguarded notify() here meant one bad emit hid the whole health picture,
    # because every probe after it never ran or recorded.
    try:
        if not ok and prev is not False:
            # newly failing (or first run already broken)
            notify(f"PROBE FAIL: {name}{where}", body=detail,
                   level=spec["level_on_fail"], category=spec["category"],
                   source="nova_prober.py", dedup_key=f"probe-{name}-fail",
                   meta={**meta, "dedup_window_s": 21600})
        elif ok and prev is False:
            # recovered
            notify(f"PROBE RECOVERED: {name}{where}", body=detail,
                   level="info", category=spec["category"],
                   source="nova_prober.py", dedup_key=f"probe-{name}-recovered", meta=meta)
    except Exception as e:
        print(f"  [WARN] notify failed for {name} (probe result still recorded): "
              f"{type(e).__name__}: {e}", file=sys.stderr)

    if not quiet:
        flag = "OK " if ok else "FAIL"
        trans = ""
        if prev is None:
            trans = " (first run)"
        elif prev and not ok:
            trans = " (-> FAILING)"
        elif (not prev) and ok:
            trans = " (-> RECOVERED)"
        print(f"  [{flag}] {name:18s} {latency_ms:5d}ms{trans}  {detail}")
    return ok


def sweep(quiet=False):
    """Run all probes once. Returns True if every probe passed."""
    conn = _ops_conn()
    all_ok = True
    try:
        if not quiet:
            print(f"nova_prober sweep @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
        for spec in PROBES:
            ok = run_probe(conn, spec, quiet=quiet)
            all_ok = all_ok and ok
    finally:
        conn.close()
    return all_ok


def main():
    ap = argparse.ArgumentParser(description="Nova synthetic end-to-end probes.")
    ap.add_argument("--once", action="store_true",
                    help="run a single sweep (default behavior) and exit")
    ap.add_argument("--list", action="store_true", help="list probes and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="no per-probe stdout (still records + alerts)")
    args = ap.parse_args()

    if args.list:
        for s in PROBES:
            print(f"{s['name']:18s} fail_level={s['level_on_fail']:8s} host={s.get('host')}")
        return 0

    all_ok = sweep(quiet=args.quiet)
    if not args.quiet:
        print("RESULT:", "all probes passed" if all_ok else "ONE OR MORE PROBES FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
