#!/usr/bin/env python3
"""nova_cluster_soak.py — whole-cluster inference soak: every computer pushed AT ONCE for N minutes.

Jordan 2026-11-09: "do a full 10min load test on the entire cluster" (after the master-bedroom
10G fiber went in), then "restart the test by computer (not by LLM) ... peak and average numbers
... push the whole cluster plus .99 and come up with real true numbers".

  counting   every response is STREAMED and each token is counted the moment it arrives, per
             COMPUTER (all its LLM endpoints summed). A 1 s sampler records each computer's
             counter, so peak and average are measured rates, not per-request estimates.
  peak       best 10 s rolling window (1 s samples are too jittery to mean anything)
  average    total tokens / window
  push       workers per endpoint well above each server's parallel slots, so every computer
             is saturated; requests just queue past that (throughput is the ceiling)
  network    UniFi byte/error counters before/after on the bedroom<->rack 10G path
  results    printed, one row in nova_ops.loadtest_runs (json), summary to Slack #nova-digest

Usage: nova_cluster_soak.py [--minutes 10] [--only .6,.77] [--no-slack] [--dry-run] | --selftest
Run from the Studio (.6). Local-only: no cloud backend is touched.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROMPT = ("Explain, in a detailed technical paragraph, how a trunked radio system assigns "
          "talkgroups to voice channels and what happens when every channel is busy.")
NUM_PREDICT = 256
PEAK_WINDOW_S = 10

# (label, computer, kind, url, model, workers). Models are the ones RESIDENT on each node
# (live /api/ps 2026-11-09) so the test measures serving, not model swaps.
TARGETS = [
    (".6 ollama",   ".6 Studio M3 Ultra",   "ollama", "http://192.168.1.6:11434",   "qwen3:30b-a3b", 8),
    (".6 mlx",      ".6 Studio M3 Ultra",   "openai", "http://192.168.1.6:5050",    "Qwen/Qwen3-8B", 4),
    (".77 ollama",  ".77 Mac mini M4 Pro",  "ollama", "http://192.168.1.77:11434",  "qwen3:30b-a3b", 6),
    (".77 mlx",     ".77 Mac mini M4 Pro",  "openai", "http://192.168.1.77:5050",   "mlx-community/Qwen3-8B-4bit", 4),
    (".7 ollama",   ".7 Mac mini M2 Pro",   "ollama", "http://192.168.1.7:11434",   "qwen3:30b-a3b", 4),
    (".7 mlx",      ".7 Mac mini M2 Pro",   "openai", "http://192.168.1.7:5050",    "/Users/Shared/mlx-models/Qwen3-8B-4bit", 4),
    (".99 ollama",  ".99 office-2 M4",      "ollama", "http://192.168.1.99:11434",  "qwen3:30b-a3b", 6),
    (".86 ollama",  ".86 core2 Ryzen ROCm", "ollama", "http://192.168.1.86:11434",  "qwen3:8b", 4),
    (".150 ollama", ".150 core7 Ryzen 890M", "ollama", "http://192.168.1.150:11434", "qwen3:8b", 4),
    (".5 ollama",   ".5 core3 Ryzen",       "ollama", "http://192.168.1.5:11434",   "qwen3:8b", 4),
    (".252 ollama", ".252 core6 M1",        "ollama", "http://192.168.1.252:11434", "qwen3:8b", 3),
    (".250 ollama", ".250 core4 M1",        "ollama", "http://192.168.1.250:11434", "qwen3:8b", 3),
    (".10 embed",   ".10 core5 i5 (embed)", "embed",  "http://192.168.1.10:11434",  "nomic-embed-text", 8),
]

# UniFi ports on the bedroom <-> rack path (MB USW Pro 8 port 10 = the new fiber; agg port 3 = far end)
NET_PORTS = [("MB USW Pro 8 PoE", 10), ("Rack 15 - 8 Port Agg Switch", 3)]   # matched by UniFi device name


class Counter:
    """Thread-safe running token/request counters per computer."""
    def __init__(self, names):
        self.lock = threading.Lock()
        self.tokens = {n: 0 for n in names}
        self.done = {n: 0 for n in names}
        self.errors = {n: 0 for n in names}
        self.first_err = {}

    def add(self, name, field, n=1):
        with self.lock:
            getattr(self, field)[name] += n

    def snapshot(self):
        with self.lock:
            return dict(self.tokens)


def _req(url, body):
    return urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})


def stream_request(kind, base, model, on_token, timeout=300) -> int:
    """Run one request, calling on_token(n) as tokens arrive. Returns tokens received. Raises on error."""
    got = 0
    if kind == "embed":
        with urllib.request.urlopen(_req(f"{base}/api/embed", {"model": model, "input": PROMPT}), timeout=timeout) as r:
            r.read()
        on_token(1)
        return 1
    if kind == "ollama":
        body = {"model": model, "stream": True, "think": False,
                "messages": [{"role": "user", "content": PROMPT}],
                "options": {"num_predict": NUM_PREDICT, "temperature": 0.7}}
        with urllib.request.urlopen(_req(f"{base}/api/chat", body), timeout=timeout) as r:
            for line in r:
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("done"):
                    # the final line carries the exact count; settle any difference vs chunks seen
                    exact = d.get("eval_count")
                    if exact is not None and exact != got:
                        on_token(exact - got)
                        got = exact
                    break
                if (d.get("message") or {}).get("content"):
                    on_token(1); got += 1
        return got
    body = {"model": model, "stream": True, "max_tokens": NUM_PREDICT, "temperature": 0.7,
            "messages": [{"role": "user", "content": PROMPT + " /no_think"}]}
    with urllib.request.urlopen(_req(f"{base}/v1/chat/completions", body), timeout=timeout) as r:
        for line in r:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            d = json.loads(data)
            ch = (d.get("choices") or [{}])[0]
            if (ch.get("delta") or {}).get("content"):
                on_token(1); got += 1
    return got


def rates(samples: list[tuple[float, int]], window: float) -> dict:
    """Pure: [(t, cumulative tokens)] at ~1 s -> {avg, peak (best `window`-second rolling rate)}."""
    if len(samples) < 2:
        return {"avg": 0.0, "peak": 0.0}
    t0, c0 = samples[0]
    t1, c1 = samples[-1]
    avg = (c1 - c0) / (t1 - t0) if t1 > t0 else 0.0
    peak, j = 0.0, 0
    for i in range(len(samples)):
        while samples[i][0] - samples[j][0] > window:
            j += 1
        dt = samples[i][0] - samples[j][0]
        if dt >= window * 0.9:
            peak = max(peak, (samples[i][1] - samples[j][1]) / dt)
    return {"avg": round(avg, 1), "peak": round(max(peak, avg), 1)}


def unifi_ports() -> dict:
    import ssl
    import subprocess
    try:
        key = subprocess.run(["security", "find-generic-password", "-s", "nova-unifi-api-key", "-a", "nova", "-w"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        ctx = ssl._create_unverified_context()   # UDM self-signed, LAN only
        req = urllib.request.Request("https://192.168.1.1/proxy/network/api/s/default/stat/device",
                                     headers={"X-API-KEY": key})
        devs = json.loads(urllib.request.urlopen(req, timeout=15, context=ctx).read())["data"]
    except Exception as e:
        return {"error": str(e)}
    out = {}
    for name, idx in NET_PORTS:
        for d in devs:
            if d.get("name") == name:
                p = next((p for p in d.get("port_table", []) if p["port_idx"] == idx), {})
                out[f"{name} p{idx}"] = {k: p.get(k) for k in ("speed", "rx_bytes", "tx_bytes", "rx_errors", "tx_errors")}
    return out


def soak(targets, minutes: float) -> dict:
    window = minutes * 60
    print(f"warming {len(targets)} endpoints ...", flush=True)
    live = []
    for t in targets:
        t0 = time.time()
        try:
            stream_request(t[2], t[3], t[4], lambda n: None, timeout=300)
            live.append(t); print(f"  {t[0]:12} ok  {time.time() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"  {t[0]:12} FAIL {type(e).__name__}: {e}"[:120], flush=True)
    computers = list(dict.fromkeys(t[1] for t in live))
    embed_only = {t[1] for t in live if t[2] == "embed"} - {t[1] for t in live if t[2] != "embed"}
    ctr = Counter(computers)
    net0 = unifi_ports()
    start = time.time()
    deadline = start + window

    def worker(t):
        while time.time() < deadline:
            try:
                stream_request(t[2], t[3], t[4], lambda n: ctr.add(t[1], "tokens", n))
                ctr.add(t[1], "done")
            except Exception as e:
                ctr.add(t[1], "errors")
                ctr.first_err.setdefault(t[1], f"{t[0]}: {type(e).__name__}: {e}"[:120])
                time.sleep(2)

    threads = [threading.Thread(target=worker, args=(t,), daemon=True) for t in live for _ in range(t[5])]
    for th in threads:
        th.start()
    print(f"soak: {len(threads)} workers on {len(live)} endpoints / {len(computers)} computers for {minutes} min", flush=True)
    series = collections.defaultdict(list)          # computer -> [(t, cumulative tokens)]
    cluster = []
    next_print = start + 60
    while (now := time.time()) < deadline:
        snap = ctr.snapshot()
        for c, v in snap.items():
            series[c].append((now - start, v))
        cluster.append((now - start, sum(v for c, v in snap.items() if c not in embed_only)))
        if now >= next_print:
            last = {c: rates(s[-61:], PEAK_WINDOW_S)["avg"] for c, s in series.items()}
            print(f"  t+{int(now - start):>4}s  cluster {rates(cluster[-61:], PEAK_WINDOW_S)['avg']:>6} tok/s  | "
                  + "  ".join(f"{c.split()[0]}:{v:.0f}" for c, v in last.items()), flush=True)
            next_print += 60
        time.sleep(1)
    snap = ctr.snapshot()                            # the bell: count what arrived inside the window only
    for c, v in snap.items():
        series[c].append((window, v))
    cluster.append((window, sum(v for c, v in snap.items() if c not in embed_only)))
    net1 = unifi_ports()
    per = {c: {**rates(series[c], PEAK_WINDOW_S), "tokens": snap[c], "requests": ctr.done[c],
               "errors": ctr.errors[c], "first_err": ctr.first_err.get(c),
               "unit": "emb/s" if c in embed_only else "tok/s",
               "drift_pct": _drift(series[c])} for c in computers}
    return {"minutes": minutes, "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)),
            "workers": len(threads), "computers": per, "cluster": rates(cluster, PEAK_WINDOW_S),
            "cluster_tokens": cluster[-1][1], "skipped": [t[0] for t in targets if t not in live],
            "net_before": net0, "net_after": net1}


def _drift(s):
    """Last third vs first third average rate, % (negative = slowed down: heat, memory pressure)."""
    if len(s) < 6:
        return None
    n = len(s) // 3
    a, b = rates(s[:n + 1], PEAK_WINDOW_S)["avg"], rates(s[-n - 1:], PEAK_WINDOW_S)["avg"]
    return round((b - a) / a * 100, 1) if a else None


def net_lines(run):
    out = []
    for port, b in run["net_after"].items():
        a = run["net_before"].get(port, {})
        if isinstance(b, dict) and a and b.get("rx_bytes") is not None and a.get("rx_bytes") is not None:
            secs = run["minutes"] * 60
            mbps = lambda x: round(x * 8 / secs / 1e6, 1)
            out.append(f"{port}: {b['speed']} link, avg in {mbps(b['rx_bytes'] - a['rx_bytes'])} Mb/s, "
                       f"out {mbps(b['tx_bytes'] - a['tx_bytes'])} Mb/s, new rx errors "
                       f"{(b['rx_errors'] or 0) - (a['rx_errors'] or 0)}")
    return out


def report(run: dict) -> str:
    L = [f"CLUSTER SOAK {run['started']} — {run['minutes']:g} min, every computer at once ({run['workers']} workers)",
         f"{'computer':24} {'avg':>7} {'peak':>7} {'reqs':>5} {'err':>4} {'drift':>7}"]
    for c, s in sorted(run["computers"].items(), key=lambda kv: -kv[1]["avg"]):
        L.append(f"{c:24} {s['avg']:>7} {s['peak']:>7} {s['requests']:>5} {s['errors']:>4} "
                 f"{(str(s['drift_pct']) + '%') if s['drift_pct'] is not None else '-':>7}"
                 + ("  (emb/s)" if s["unit"] == "emb/s" else "") + (f"  {s['first_err']}" if s["first_err"] else ""))
    L.append(f"CLUSTER generation: avg {run['cluster']['avg']} tok/s, peak {run['cluster']['peak']} tok/s "
             f"({PEAK_WINDOW_S}s window), {run['cluster_tokens']:,} tokens")
    if run["skipped"]:
        L.append(f"skipped (warm-up failed): {', '.join(run['skipped'])}")
    L += net_lines(run)
    return "\n".join(L)


def slack(run: dict):
    try:
        import nova_config
        from nova_protect_monitor import slack_post
        rows = sorted(run["computers"].items(), key=lambda kv: -kv[1]["avg"])
        body = "\n".join(f"{c:24} avg {s['avg']:>6}  peak {s['peak']:>6} {s['unit']}"
                         + (f"  errors {s['errors']}" if s["errors"] else "") for c, s in rows)
        text = (f":rocket: *Cluster load test* — {run['minutes']:g} min, all {len(rows)} computers at once "
                f"({run['workers']} workers), {run['started']}\n"
                f"*Cluster generation: avg {run['cluster']['avg']} tok/s · peak {run['cluster']['peak']} tok/s* "
                f"({run['cluster_tokens']:,} tokens; tokens counted as they stream, peak = best {PEAK_WINDOW_S}s)\n"
                f"```{body}```\n" + "\n".join(net_lines(run))
                + (f"\nSkipped: {', '.join(run['skipped'])}" if run["skipped"] else ""))
        slack_post(text, channel=nova_config.SLACK_DIGEST)
        print("posted to Slack #nova-digest")
    except Exception as e:
        print(f"Slack post failed: {e}")


def save(run: dict):
    try:
        import nova_dsn
        c = nova_dsn.pg_connect(); c.autocommit = True
        with c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS loadtest_runs (id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), "
                        "kind text, minutes real, result jsonb)")
            cur.execute("INSERT INTO loadtest_runs (kind, minutes, result) VALUES ('cluster_soak', %s, %s) RETURNING id",
                        (run["minutes"], json.dumps(run)))
            print(f"saved loadtest_runs id {cur.fetchone()[0]}")
        c.close()
    except Exception as e:
        print(f"PG save failed: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--only", help="comma list of node prefixes, e.g. .6,.77")
    ap.add_argument("--no-slack", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="one streamed request per endpoint, no soak")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        flat = [(t, 100 * t) for t in range(0, 61)]                       # steady 100 tok/s
        assert rates(flat, 10) == {"avg": 100.0, "peak": 100.0}
        burst = [(t, 100 * t + (500 if t >= 30 else 0)) for t in range(0, 61)]   # +500 tokens at t=30
        r = rates(burst, 10)
        assert r["peak"] == 150.0 and abs(r["avg"] - 108.3) < 0.1, r
        assert rates([(0, 0)], 10) == {"avg": 0.0, "peak": 0.0}
        assert _drift([(t, 100 * t) for t in range(30)]) == 0.0
        print("selftest OK")
        return 0
    targets = [t for t in TARGETS if not a.only or any(t[0].startswith(p + " ") for p in a.only.split(","))]
    if a.dry_run:
        for t in targets:
            t0, n = time.time(), [0]
            try:
                got = stream_request(t[2], t[3], t[4], lambda k: n.__setitem__(0, n[0] + k))
                print(f"{t[0]:12} ok   {time.time() - t0:5.1f}s tokens={got} streamed={n[0]}")
            except Exception as e:
                print(f"{t[0]:12} FAIL {time.time() - t0:5.1f}s {type(e).__name__}: {e}"[:140])
        return 0
    run = soak(targets, a.minutes)
    print("\n" + report(run))
    save(run)
    if not a.no_slack:
        slack(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
