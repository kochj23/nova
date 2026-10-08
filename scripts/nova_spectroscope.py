#!/usr/bin/env python3
"""nova_spectroscope.py — THE SPECTROSCOPE: the corruption no alarm names.

Lovecraft, "The Colour Out of Space". A fragment of the meteorite, heated before the
spectroscope, "displayed shining bands unlike any known colours of the normal spectrum".
The coloured globule inside it could not be described at all; when struck with a hammer it
burst and was gone. The instrument could not name the colour; it could only register that it
did not know. Afterwards the farm went grey over one season: bad milk by late May, verdure
"going grey" and brittle through the summer, the vegetation crumbling to grey powder by
September, and the blasted heath still spreading "little by little, perhaps an inch a year".
No single day looked like an emergency.

Nova's version is a defence against silent data corruption: things that go wrong while every
component reports healthy. Nightly she re-measures a sample of what she stores, and runs
computations whose answers she already knows, on more than one machine. Anything that no
longer matches its own spectrum is logged to the Buick 8 Logbook as `substrate_mismatch`,
cause unknown. She never guesses a cause.

Minimal first version, two checks:
  1. memory_integrity — ~200 random memories (memory server /random). The memory server has
     no endpoint that returns a stored vector, so this is a SELF-RETRIEVAL check through the
     memory server: each memory's own text is the query (/recall_batch). The server re-embeds it
     with its pinned embedder and reports the true cosine (1 - embedding <=> query) of every
     vector hit. Flag: own id missing from the top 5 (miss) or own cosine below 0.999.
     nova_memories is never touched directly; nothing is written to the memory server
     (recall's own access_count bump is the server's side effect, not ours).
  2. known_answer — a fixed integer/matrix/sha256 suite with hardcoded expected values, run
     locally and on nova-core over SSH; each host is compared to the expected values and to
     its peer. An unreachable remote is recorded as unreachable (fail open), never as a mismatch.

CLI:     --run [--dry-run] [--sample N]   --show   --selftest
Table:   spectroscope_runs (ts, check, host, n, mismatches, detail jsonb)
Buick 8: substrate_mismatch, signature '<check>:<host>', one occurrence per day.
Cadence: nightly 02:50.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402

# ponytail: the "pinned embedder" is whatever model the memory server is configured with; no
# digest check until the Jade Amulet inventories model blobs.
MEMORY_URL = os.environ.get("NOVA_MEMORY_URL", "http://memory-server.digitalnoise.net:18790")
REMOTE = os.environ.get("NOVA_SPECTROSCOPE_REMOTE", "nova-core")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
COS_MIN = 0.999
TOP_N = 5
BATCH = 5            # /recall_batch caps at 5 queries per call
FTS_ONLY = 0.5       # memory server's documented score marker for a text-only (non-vector) hit

SCHEMA = """
CREATE TABLE IF NOT EXISTS spectroscope_runs (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  "check" text NOT NULL,
  host text NOT NULL,
  n int NOT NULL DEFAULT 0,
  mismatches int NOT NULL DEFAULT 0,
  detail jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS spectroscope_runs_ts ON spectroscope_runs (ts DESC);
"""

# The suite is plain source so the exact same bytes run locally (exec) and remotely (python3 -c).
SUITE = r'''
import hashlib, json
r = {}
r["int_sum"] = sum(i * i % 1000003 for i in range(3000000))
r["int_pow"] = pow(3, 10**6, 2**127 - 1)
n = 80
a = [[(i * 31 + j * 17) % 101 - 50 for j in range(n)] for i in range(n)]
b = [[(i * 13 + j * 7) % 97 - 48 for j in range(n)] for i in range(n)]
c = [[sum(a[i][k] * b[k][j] for k in range(n)) for j in range(n)] for i in range(n)]
r["matmul"] = hashlib.sha256(json.dumps(c).encode()).hexdigest()
h = bytes(range(256)) * 4096
for _ in range(1000):
    h = hashlib.sha256(h).digest() + h[32:]
r["sha256"] = hashlib.sha256(h).hexdigest()
print(json.dumps(r))
'''
EXPECTED = {
    "int_sum": 1499692498779,
    "int_pow": 76680424781939633926089563193284323913,
    "matmul": "519fb268390777e6012b3a4b65a9daaedb6e769e1fe3b02147b4bee96bdf1b15",
    "sha256": "36bed7104f5200c041965078c87e4a701c7ac8cf0d33023197b950a72f6067e0",
}


def log(m: str) -> None:
    if not os.environ.get("NOVA_TEST_QUIET"):
        print(f"[spectroscope {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure ────────────────────────────────────────────────────────────────────

def score_self_retrieval(mem_id: str, hits: list) -> dict:
    """One sampled memory vs. its own recall. -> {"id", "status": ok|miss|low_cosine, "cos"}."""
    own = next((h for h in hits if h.get("id") == mem_id), None)
    cos = None if own is None else float(own.get("score") or 0.0)
    if cos is None or cos == FTS_ONLY:   # found only by the text leg: its vector was not near
        return {"id": mem_id, "status": "miss", "cos": None}
    return {"id": mem_id, "status": "ok" if cos >= COS_MIN else "low_cosine", "cos": cos}


def compare_known(results: dict, expected: dict = EXPECTED, peer: dict | None = None) -> dict:
    """-> {"wrong": [test...], "peer_disagrees": [test...]} for one host's suite output."""
    wrong = sorted(k for k in expected if results.get(k) != expected[k])
    peer_dis = sorted(k for k in expected if peer is not None and results.get(k) != peer.get(k))
    return {"wrong": wrong, "peer_disagrees": peer_dis}


# ── external (each retries with backoff via W.retry, then fails open) ──────

def _http_json(path: str, body: dict | None = None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MEMORY_URL + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def memory_get(path: str, body: dict | None = None):
    """Memory-server call with 3x backoff. None when it kept failing (fail open)."""
    return W.retry(_http_json, path, body, tag="spectroscope") or None


def _ssh_suite(host: str):
    r = subprocess.run(["ssh", *SSH_OPTS, host, "python3 -c " + shlex.quote(SUITE)],
                       capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def run_local_suite() -> dict:
    out = []
    exec(SUITE, {"print": out.append})  # noqa: S102 — fixed in-source constant, never external input
    return json.loads(out[-1])


def run_remote_suite(host: str = REMOTE):
    """Suite output from `host`, or None if unreachable after retries (fail open)."""
    return W.retry(_ssh_suite, host, tag="spectroscope") or None


# ── checks ──────────────────────────────────────────────────────────────────

def check_memory(sample: int) -> dict:
    got = memory_get(f"/random?n={int(sample)}")
    mems = (got or {}).get("memories") or []
    if not mems:
        return {"check": "memory_integrity", "host": "memory-server", "n": 0, "mismatches": 0,
                "detail": {"unreachable": True}}
    scored = []
    for i in range(0, len(mems), BATCH):
        chunk = mems[i:i + BATCH]
        out = memory_get("/recall_batch", {"queries": [{"q": m["text"], "n": TOP_N} for m in chunk]})
        results = (out or {}).get("results") or []
        if len(results) != len(chunk):
            continue  # ponytail: a failed batch is skipped (fail open), not counted as misses
        scored += [score_self_retrieval(m["id"], r.get("memories") or []) for m, r in zip(chunk, results)]
    bad = [s for s in scored if s["status"] != "ok"]
    return {"check": "memory_integrity", "host": "memory-server", "n": len(scored),
            "mismatches": len(bad),
            "detail": {"method": "self_retrieval", "cos_min": COS_MIN, "top_n": TOP_N,
                       "flagged": bad[:50]}}  # ids + cosines only, never memory text


def check_known_answer(remote: str = REMOTE) -> list:
    local_host = socket.gethostname().split(".")[0]
    local, peer = run_local_suite(), run_remote_suite(remote)
    rows = []
    for host, res, other in ((local_host, local, peer), (remote, peer, local)):
        if res is None:
            rows.append({"check": "known_answer", "host": host, "n": 0, "mismatches": 0,
                         "detail": {"unreachable": True}})
            continue
        cmp = compare_known(res, peer=other)
        rows.append({"check": "known_answer", "host": host, "n": len(EXPECTED),
                     "mismatches": len(set(cmp["wrong"]) | set(cmp["peer_disagrees"])),
                     "detail": {**cmp, "peer": None if other is None else "compared"}})
    return rows


# ── persistence ─────────────────────────────────────────────────────────────

def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"query failed: {e}")
        return []


def write_rows(cur, rows: list) -> int:
    from nova_buick8_log import log_unexplained
    day = datetime.now().strftime("%Y-%m-%d")
    for r in rows:
        cur.execute('INSERT INTO spectroscope_runs ("check", host, n, mismatches, detail) '
                    "VALUES (%s,%s,%s,%s,%s::jsonb)",
                    (r["check"], r["host"], r["n"], r["mismatches"], json.dumps(r["detail"])))
        if r["mismatches"]:
            log_unexplained("substrate_mismatch", f"{r['check']}:{r['host']}",
                            f"Spectroscope: {r['mismatches']} of {r['n']} {r['check']} results on "
                            f"{r['host']} no longer match their known spectrum",
                            evidence=r["detail"], occurrence_key=day, source="spectroscope", cur=cur)
    return len(rows)


def run(sample: int = 200, dry: bool = False) -> list:
    rows = [check_memory(sample)] + check_known_answer()
    for r in rows:
        log(f"{r['check']:<17} {r['host']:<14} n={r['n']:<4} mismatches={r['mismatches']} "
            f"{json.dumps(r['detail'], default=str)[:300]}")
    if dry:
        log("dry run: nothing written")
        return rows
    conn = W.connect()
    try:
        cur = conn.cursor()
        ensure_schema(cur)
        write_rows(cur, rows)
    finally:
        conn.close()
    return rows


def show() -> int:
    conn = W.connect()
    try:
        rows = _q(conn.cursor(), 'SELECT ts, "check", host, n, mismatches, detail FROM spectroscope_runs '
                                 "ORDER BY ts DESC LIMIT 20")
    finally:
        conn.close()
    for ts, chk, host, n, mm, det in rows:
        print(f"{ts:%Y-%m-%d %H:%M}  {chk:<17} {host:<14} n={n:<4} mismatches={mm}  "
              f"{json.dumps(det, default=str)[:160]}")
    return 0


def selftest() -> int:
    assert run_local_suite() == EXPECTED
    assert compare_known(EXPECTED) == {"wrong": [], "peer_disagrees": []}
    bad = dict(EXPECTED, sha256="0")
    assert compare_known(bad, peer=EXPECTED) == {"wrong": ["sha256"], "peer_disagrees": ["sha256"]}
    assert score_self_retrieval("a", [{"id": "a", "score": 1.0}])["status"] == "ok"
    assert score_self_retrieval("a", [{"id": "a", "score": 0.9}])["status"] == "low_cosine"
    assert score_self_retrieval("a", [{"id": "b", "score": 1.0}])["status"] == "miss"
    assert score_self_retrieval("a", [{"id": "a", "score": 0.5}])["status"] == "miss"
    print("spectroscope selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="run both checks and record them")
    ap.add_argument("--dry-run", action="store_true", help="with --run: read and print, write nothing")
    ap.add_argument("--sample", type=int, default=200, help="memories to sample (default 200)")
    ap.add_argument("--show", action="store_true", help="last 20 recorded results")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.show:
        return show()
    if a.run:
        run(max(1, min(a.sample, 2000)), dry=a.dry_run)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
