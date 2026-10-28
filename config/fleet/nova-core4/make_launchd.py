#!/usr/bin/env python3
"""Generate nova-core4 (M1, macOS) LaunchDaemons — macOS port of the Linux core4 systemd roles.
Usage: python3 make_launchd.py OUTDIR; then on core4: sudo install -o root -g wheel -m 644 OUTDIR/<f> /Library/LaunchDaemons/
       and sudo launchctl bootstrap system /Library/LaunchDaemons/<f>  (cold standbys: install only).
Resident: vault-to-keychain, nasdata mount, share mount, mesh agent, hue, gateway standby, ollama,
pgbouncer, minion agent, LLM probe.
Cold standbys (Disabled=true; failover = launchctl enable + bootstrap): memory-server,
scheduler-core, snmp-poller, syslog."""
import plistlib, sys
from pathlib import Path

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
U, H = "kochj23", "/Users/kochj23"
OC = f"{H}/.openclaw"; S = f"{OC}/scripts"; LOG = f"{OC}/logs"; PY = f"{OC}/venv/bin/python"
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
BASE_ENV = {"HOME": H, "PATH": PATH, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
# macOS 26 Local Network privacy blocks LAN connects from launchd jobs that run as a user
# ("No route to host" to everything but the DNS server; tested 2026-10-27). Root daemons are
# exempt, so LAN-talking roles run as root with HOME=kochj23 — kochj23 already has NOPASSWD sudo,
# so this adds no privilege. Same trap that killed the .7 PG replica in July (agent_docs data-platform).
LAN_ROOT = True


def job(label, args=None, *, user=True, root=False, env=None, wd=None, log=None, keepalive=True,
        interval=None, disabled=False, **extra):
    d = {"Label": label, "ProgramArguments": args, "RunAtLoad": True}
    if user:
        if not root:
            d.update(UserName=U, GroupName="staff")
        d["EnvironmentVariables"] = {**BASE_ENV, **(env or {})}
    elif env:
        d["EnvironmentVariables"] = env
    if wd: d["WorkingDirectory"] = wd
    if keepalive: d.update(KeepAlive=True, ThrottleInterval=10)
    if interval: d["StartInterval"] = interval
    if disabled: d["Disabled"] = True
    lp = log or f"{LOG}/{label.split('.')[-1]}.log"
    d["StandardOutPath"] = d["StandardErrorPath"] = lp
    d.update(extra)
    (OUT / f"{label}.plist").write_bytes(plistlib.dumps(d))


# --- plumbing (as on nova-core6 .252) ---
job("net.digitalnoise.nova-vault-to-keychain", ["/usr/bin/python3", "/usr/local/bin/nova_vault_to_keychain.py"],
    user=False, keepalive=False, interval=3600, log="/var/log/nova-vault-to-keychain.log")
job("net.digitalnoise.nova-nasdata-mount", ["/bin/bash", f"{OC}/bin/nova_nasdata_mount.sh", "/Volumes/Data"],
    user=False, keepalive=False, interval=60, log="/var/log/nova-nasdata-mount.log")
job("net.digitalnoise.daemon.nova-mac-share-mount", ["/usr/bin/python3", f"{S}/nova_mac_share_mount.py"],
    interval=120, log=f"{LOG}/nova_mac_share_mount.log", ProcessType="Background")
job("net.digitalnoise.nova-mesh-agent", root=LAN_ROOT, args=[PY, f"{S}/nova_mesh_agent.py"], log=f"{LOG}/mesh-agent.log")

# --- resident roles ---
job("net.digitalnoise.nova-hue", root=LAN_ROOT, args=[PY, f"{S}/nova_hue_poller.py"], wd=S, log=f"{LOG}/hue.log")
job("net.digitalnoise.nova-gateway-v2", root=LAN_ROOT, args=[PY, f"{S}/nova_gateway_v2.py"], wd=S,
    env={"NOVA_GW_STANDBY": "1", "NOVA_LOG_LEVEL": "info"}, log=f"{LOG}/nova_gateway_v2.log")
# ponytail: brew ollama, not the hand-untarred one .252 uses; `brew upgrade ollama` + kickstart to update.
job("net.digitalnoise.ollama", ["/opt/homebrew/bin/ollama", "serve"], log=f"{LOG}/ollama.log", Nice=10,
    env={"OLLAMA_HOST": "0.0.0.0", "OLLAMA_MODELS": f"{H}/.ollama/models", "OLLAMA_KEEP_ALIVE": "-1",
         "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_CONTEXT_LENGTH": "8192", "OLLAMA_FLASH_ATTENTION": "1"})

# pgbouncer refuses to run as root; as kochj23 it can only reach .2 (the DNS server is LNP-exempt)
job("net.digitalnoise.pgbouncer", ["/opt/homebrew/bin/pgbouncer", "/opt/homebrew/etc/pgbouncer.ini"],
    log=f"{LOG}/pgbouncer.stdout.log")

# minion + LLM probe: LaunchAgents elsewhere, root daemons here (Local Network privacy, above)
job("net.digitalnoise.nova-minion", root=LAN_ROOT, args=["/usr/bin/python3", f"{H}/.nova-minion/nova_minion_agent.py"],
    wd=f"{H}/.nova-minion", log=f"{H}/.nova-minion/agent.log", ProcessType="Background")
job("net.digitalnoise.nova-llm-probe", root=LAN_ROOT, args=["/usr/bin/python3", f"{H}/.nova-probe/nova_llm_probe.py"],
    log=f"{H}/.nova-probe/probe.log", Nice=10, ProcessType="Background")

# --- COLD standbys for nova-core .2 (runbook-core4-standby) ---
job("net.digitalnoise.nova-memory-server", root=LAN_ROOT, args=[PY, f"{OC}/memory_server.py"], wd=OC, disabled=True)
job("net.digitalnoise.nova-scheduler-core",
    [PY, f"{S}/nova_leader_wrapper.py", "--name", "nova-scheduler-core", "--", PY, f"{S}/nova_scheduler.py"],
    root=LAN_ROOT, wd=S, disabled=True, env={
        "PYTHONPATH": S, "NOVA_SCHED_SCRIPTS": S,
        "NOVA_SCHED_CONFIG": "/Volumes/nas/nova-fs/config/scheduler-core.yaml",
        "NOVA_SCHED_STATE": f"{OC}/config/scheduler-core_state.json",
        "NOVA_SCHED_HEARTBEAT": f"{OC}/config/scheduler-core_heartbeat"})
job("net.digitalnoise.nova-snmp-poller", root=LAN_ROOT, args=[PY, f"{S}/nova_snmp_poller.py"], wd=S, disabled=True)
job("net.digitalnoise.nova-syslog", root=LAN_ROOT, args=[PY, f"{S}/nova_syslog_server.py"], wd=S, disabled=True)

for p in sorted(OUT.glob("*.plist")):
    plistlib.loads(p.read_bytes())  # round-trip check
    print(p.name)
