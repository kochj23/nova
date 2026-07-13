#!/opt/homebrew/bin/python3
"""Stream a running Strix pentest's real findings into #nova-info, then post the
executive summary + close the maintenance window on exit.

Usage: nova_strix_monitor.py <run_log_path_on_.2> <procmatch>
  run_log_path : e.g. /home/kochj/strix_runs/<name>/strix.log  (the REAL output,
                 not the stdout banner file)
  procmatch    : pgrep -f pattern to detect the run is still alive (e.g. '3000')
"""
import subprocess, time, sys, os, re
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_notify, nova_maintenance

HOST = "kochj@192.168.1.2"
RUN_LOG = sys.argv[1] if len(sys.argv) > 1 else "/home/kochj/strix_runs/latest/strix.log"
PROC = sys.argv[2] if len(sys.argv) > 2 else "3000"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Surface real findings; drop the openai.agents/DEBUG tracing firehose.
KEEP = re.compile(r"vulnerab|severity|critical|high\b|medium\b|CVE-|exploit|IDOR|XSS|"
                  r"SQL inj|auth bypass|exposed|default cred|anonymous|api key|"
                  r"finding|report saved|executive_summary|no vulnerabilit", re.I)
DROP = re.compile(r"DEBUG|openai\.agents|Tracing is disabled|Not creating|Setting current|"
                  r"Resetting|Starting turn|Processing output|Calling LLM|conversation_id", re.I)

def ssh(cmd):
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, cmd],
                              capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""

def alive():
    return "yes" in ssh(f"pgrep -f '{PROC}' >/dev/null && echo yes")

seen, posted = set(), 0
POST_CAP = 30
while True:
    for ln in ssh(f"cat {RUN_LOG} 2>/dev/null").splitlines():
        c = ANSI.sub("", ln).strip()
        if len(c) < 12 or c in seen or DROP.search(c) or not KEEP.search(c):
            continue
        seen.add(c)
        if posted < POST_CAP:
            nova_notify.notify(f"🔎 Strix — {c[:280]}", level="info",
                               category="strix", source="nova_strix_monitor")
            posted += 1
    if not alive():
        break
    time.sleep(30)

# ── completion: pull the executive summary out of the run log ──
summ = ssh(f"sed 's/\\x1b\\[[0-9;]*m//g' {RUN_LOG} | grep -oE 'executive_summary[\": ]+[^}}]+' | tail -1 | cut -c1-500")
summ = summ.strip() or "(no executive summary captured — see run dir on .2)"
nova_notify.notify("✅ Strix run — COMPLETE", level="info", category="strix",
                   source="nova_strix_monitor",
                   body=f"{posted} findings streamed. Summary: {summ[:400]}")
try:
    nova_maintenance.stop()
    nova_notify.notify("🔓 Maintenance window closed — alerting normal", level="info",
                       category="strix", source="nova_strix_monitor")
except Exception:
    pass
print(f"monitor done — {posted} findings")
