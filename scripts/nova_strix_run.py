#!/opt/homebrew/bin/python3
"""
nova_strix_run.py — reusable Strix pentest harness (used by the daily rotation + one-offs).

Opens a maintenance window (mutes the Wazuh alert flood), launches a SCOPED Strix run on
.2 via OpenRouter, ENFORCES A HARD max-runtime auto-kill (the thing that made run 1
loop forever), then posts a clean findings summary + Wazuh detection scorecard to
#nova-info and closes the window. Fragile IoT is forced to recon-only regardless of mode.

  nova_strix_run.py --targets http://192.168.1.2:3000 --label grafana --mode standard
  nova_strix_run.py --targets http://cam1,http://cam2 --label cameras --recon-only
"""
import subprocess, time, sys, os, re, argparse, signal
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_notify, nova_maintenance, nova_router

HOST = "kochj@192.168.1.2"
STRIX = "~/.strix/bin/strix"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

def ssh(cmd, timeout=40):
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST, cmd],
                              capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL).stdout
    except Exception:
        return ""

def strix_alive():
    return "yes" in ssh(r"pgrep -f '\.strix/bin/strix' >/dev/null && echo yes")

def force_kill():
    ssh(r"pkill -9 -f '\.strix/bin/strix'; "
        r"docker ps -q --filter ancestor=ghcr.io/usestrix/strix-sandbox:1.0.0 | xargs -r docker kill")

def wazuh_scorecard(minutes):
    q = (f"SELECT count(*) FROM security_events WHERE ts>now()-interval '{minutes+15} min' "
         "AND (full_log ILIKE '%grafana%' OR full_log ILIKE '%:3000%' OR dst_port IS NOT NULL "
         "OR array_to_string(rule_groups,',') ILIKE '%web%' OR rule_description ILIKE '%SQL%' "
         "OR rule_description ILIKE '%authenticat%' OR rule_description ILIKE '%inject%');")
    try:
        n = subprocess.run(["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_ops", "-tA", "-c", q],
                           capture_output=True, text=True, timeout=20).stdout.strip()
        return n or "?"
    except Exception:
        return "?"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True, help="comma-separated targets")
    ap.add_argument("--label", default="adhoc")
    ap.add_argument("--mode", default="standard", choices=["quick", "standard", "deep"])
    ap.add_argument("--recon-only", action="store_true", help="fragile IoT: no exploitation/fuzzing/writes")
    ap.add_argument("--max-min", type=int, default=45, help="hard runtime cap (force-kill after)")
    a = ap.parse_args()

    targets = [t.strip() for t in a.targets.split(",") if t.strip()]
    if not targets:
        print("no targets"); return
    tflags = " ".join(f"-t {t}" for t in targets)

    if a.recon_only:
        mode = "quick"
        instr = ("Authorized internal assessment of a private homelab owned and operated by the requester. RECON and vulnerability "
                 "IDENTIFICATION ONLY. Absolutely NO exploitation, NO fuzzing, NO writes/POST/PUT/DELETE, "
                 "NO destructive actions, NO DoS. These are FRAGILE IoT devices (cameras/printers/bridges) "
                 "that can crash or brick — probe gently, read-only, minimal requests.")
    else:
        mode = a.mode
        instr = ("Authorized internal security assessment of a private homelab owned and operated by the requester. Focus on "
                 "authentication, access control, default credentials, exposed admin/API, injection, "
                 "SSRF, secret exposure. NO destructive data modification, account changes, or DoS.")

    nova_maintenance.start(a.max_min + 10, f"strix rotation: {a.label}")
    nova_notify.notify(f"🛡️ Strix [{a.label}] — STARTING", level="info", category="strix",
                       source="nova_strix_run",
                       body=f"Targets: {', '.join(targets)} · mode {mode}"
                            f"{' · RECON-ONLY' if a.recon_only else ''} · hard cap {a.max_min}m")

    # HARD BACKSTOP: no matter where we hang (a wedged ssh, a slow psql), this fires,
    # force-kills strix, closes the maintenance window, and exits. The window can NEVER
    # be left open. Fires a few minutes past the run cap.
    def _hard_timeout(signum, frame):
        force_kill()
        try:
            nova_notify.notify(f"⏱️ Strix [{a.label}] — HARD backstop fired, force-killed + window closed",
                               level="warning", category="strix", source="nova_strix_run")
        except Exception:
            pass
        try:
            nova_maintenance.stop()
        except Exception:
            pass
        os._exit(1)
    signal.signal(signal.SIGALRM, _hard_timeout)
    signal.alarm((a.max_min + 6) * 60)

    # Route through the Nova inference fabric (2026-07-11): point at the active/active router on
    # .2 (NOT a single node) with a model CLASS, so Strix is load-balanced across the heavy GPU
    # nodes and fails over if one dies. Local + free — keeps our own-network pentest topology
    # and findings off any cloud vendor.
    #
    # Use litellm's OPENAI-compatible provider against the router's /v1 endpoint, NOT the
    # ollama/ provider (2026-07-30): the bundled litellm builds the ollama URL as
    # {base}/v1/api/generate, a path the router does not serve, so warm-up 404'd every night
    # since 07-28 and litellm masked it as the misleading "backend .6:11434: Not Found". The
    # router's /v1/chat/completions handles the model CLASS directly (verified: conversation
    # -> qwen3:30b-a3b). Any OpenAI key value is accepted; the router does not check it.
    # To revert to metered cloud: STRIX_LLM='openrouter/anthropic/claude-sonnet-4.6' + OpenRouter key.
    logf = f"/tmp/strix_{a.label}.log"
    ssh(f"cd ~; export STRIX_LLM='openai/conversation'; "
        f"export OPENAI_API_BASE='{nova_router.base()}/v1'; "
        f"export OPENAI_API_KEY=${{OPENAI_API_KEY:-nova-router}}; "
        f"nohup {STRIX} -n -m {mode} {tflags} --instruction '{instr}' > {logf} 2>&1 & echo go")

    # discover the run dir from stdout
    rundir = ""
    for _ in range(8):
        time.sleep(6)
        out = ANSI.sub("", ssh(f"cat {logf} 2>/dev/null"))
        m = re.search(r"strix_runs/[A-Za-z0-9_-]+", out)
        if m:
            rundir = m.group(0); break
        if "Error" in out or "Traceback" in out:
            nova_notify.notify(f"❌ Strix [{a.label}] — failed to start (see {logf} on .2)",
                               level="warning", category="strix", source="nova_strix_run")
            nova_maintenance.stop(); return

    # monitor with a HARD deadline
    timed_out = False
    try:
        t0 = time.time()
        while strix_alive():
            if time.time() - t0 > a.max_min * 60:
                timed_out = True
                force_kill()
                nova_notify.notify(f"⏱️ Strix [{a.label}] — hit {a.max_min}m cap, force-killed",
                                   level="warning", category="strix", source="nova_strix_run")
                break
            time.sleep(30)

        # results from the structured CSV
        vlines, counts = [], {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        if rundir:
            csv = ssh(f"cat ~/{rundir}/vulnerabilities.csv 2>/dev/null")
            for row in csv.splitlines()[1:]:
                parts = row.split(",")
                if len(parts) >= 3:
                    sev = parts[2].strip().upper()
                    counts[sev] = counts.get(sev, 0) + 1
                    vlines.append(f"{sev}: {parts[1].strip()[:90]}")
        summary = " ".join(f"{k}×{v}" for k, v in counts.items() if v) or "no findings"
        detect = wazuh_scorecard(a.max_min)

        nova_notify.notify(
            f"{'⏱️' if timed_out else '✅'} Strix [{a.label}] — {'TIMED OUT' if timed_out else 'COMPLETE'} ({summary})",
            level="info", category="strix", source="nova_strix_run",
            body=("\n".join(f"• {v}" for v in vlines[:12]) or "No vulnerabilities found.")
                 + f"\n\nWazuh in-window events plausibly related: {detect}"
                 + (f"\nFull report: .2:~/{rundir}" if rundir else ""))
        print(f"done [{a.label}]: {summary}, timed_out={timed_out}")
    finally:
        # ALWAYS clean up. force_kill() previously only ran on timeout, so a CLEAN run left the
        # Strix docker sandbox container alive — that was the "window left open". Kill it here
        # every time, close the maintenance window, and os._exit so the wrapper can never hang
        # post-run. A short alarm guards a hang in cleanup itself; the maintenance window's own
        # TTL (opened for max_min+10) is the ultimate backstop.
        signal.alarm(45)
        try: force_kill()
        except Exception: pass
        try: nova_maintenance.stop()
        except Exception: pass
        signal.alarm(0)
        os._exit(2 if timed_out else 0)

if __name__ == "__main__":
    main()
