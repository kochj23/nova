#!/usr/bin/env python3
"""nova_memory_verify.py — wish #59: physical-world memories carry a last-verified date and lose to a probe.

A memory that says "192.168.1.190 is the Mac mini" is a claim about the world, and the world can be asked.
Nightly: for memories whose text names a LAN IP next to a device/host name, cross-check against what the
network says now (telemetry.known_devices ip->client_name, node_status node_ip->node_name):
  agree    -> metadata.verified_at / verified_against (the probe source)
  disagree -> metadata.contradicted_at / truth, importance_score halved (the memory loses to the probe),
              one WARNING per memory via nova_notify (dedup by memory id), so a wrong belief is visible
  unknown  -> left alone (no probe can speak to it)
Names come only from the truth tables (lowercase, >= 4 chars); a claim is "IP within WINDOW chars of a name".
Why: ".190 was the Bose soundbar for weeks; the network knew; the memory won." Approved 2026-10-04.
--dry-run --limit N --selftest
"""
import re, sys, os, json
import psycopg2

OPS = os.environ.get("NOVA_OPS_DSN", "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
MEM = os.environ.get("NOVA_MEM_DSN", "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj")
IP_RE = re.compile(r"\b192\.168\.1\.(\d{1,3})\b")
WINDOW = 60
NOISY = ("nova_articles", "intelligence", "syslog", "alerts", "nova_journal", "security", "incident")
GENERIC = {"unknown", "none", "null", "apple", "device", "localhost", "server", "client"}
RECHECK_DAYS = 7
MARK_VERIFIED = ("UPDATE memories SET metadata = coalesce(metadata,'{}'::jsonb) || %s::jsonb "
                 "|| jsonb_build_object('verified_at', now()::text) WHERE id=%s")
MARK_CONTRADICTED = ("UPDATE memories SET metadata = coalesce(metadata,'{}'::jsonb) || jsonb_build_object("
                     "'contradicted_at', now()::text, 'contradicted_by', 'node_status+known_devices', 'truth', %s::jsonb, 'claims', %s::jsonb), "
                     "importance_score = greatest(0.05, coalesce(importance_score, 0.5) * 0.5) WHERE id=%s")

def log(m): print(f"[memory-verify] {m}", flush=True)

def truth_map(oc):
    """ip -> set(names); name -> set(ips). Pure data from the probes."""
    ip_names = {}
    oc.execute("SELECT host(node_ip), node_name FROM node_status WHERE node_ip IS NOT NULL")
    rows = oc.fetchall()
    oc.execute("SELECT ip::text, client_name FROM telemetry.known_devices WHERE ip IS NOT NULL AND client_name IS NOT NULL")
    rows += oc.fetchall()
    for ip, name in rows:
        n = (name or "").strip().lower()
        # a plain short word ("office", "kitchen") is a room, not a device name; keep hyphenated/numbered/long names
        if len(n) >= 4 and n not in GENERIC and (any(ch in n for ch in "-_.0123456789") or len(n) >= 8):
            ip_names.setdefault(ip, set()).add(n)
    name_ips = {}
    for ip, names in ip_names.items():
        for n in names:
            name_ips.setdefault(n, set()).add(ip)
    return ip_names, name_ips

def judge(text, ip_names, name_ips, window=WINDOW):
    """-> ('verified', [(ip,name)]) | ('contradicted', [(ip,name,truth_ips)]) | ('unknown', [])  Pure."""
    low = text.lower()
    verified, contradicted = [], []
    for m in IP_RE.finditer(low):
        ip = "192.168.1." + m.group(1)
        lo, hi = max(0, m.start() - window), min(len(low), m.end() + window)
        near = low[lo:hi]
        for name, ips in name_ips.items():
            if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", near):      # whole name: 'nova-core' is not 'nova-core8'
                if ip in ips:
                    verified.append((ip, name))
                elif ip in ip_names and not any(t in low for t in ips):
                    # the probe knows this IP and it is NOT this name — and the text never gives the name its
                    # real address either (a memory listing ".6 and nova-core (.2)" is a list, not a misbelief)
                    contradicted.append((ip, name, sorted(ips)))
    if contradicted:
        return "contradicted", contradicted
    if verified:
        return "verified", verified
    return "unknown", []

def main():
    dry = "--dry-run" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 400
    oc_conn = psycopg2.connect(OPS, connect_timeout=8); oc_conn.autocommit = True; oc = oc_conn.cursor()
    mc_conn = psycopg2.connect(MEM, connect_timeout=8); mc_conn.autocommit = True; mc = mc_conn.cursor()
    ip_names, name_ips = truth_map(oc)
    log(f"truth: {len(ip_names)} IPs, {len(name_ips)} names")
    mc.execute("""SELECT id, text, source FROM memories
                  WHERE text ~ '192\\.168\\.1\\.[0-9]+' AND source NOT ILIKE ALL(%s)
                  AND coalesce((metadata->>'verified_at')::timestamptz, 'epoch') < now() - interval '%s days'
                  AND metadata->>'contradicted_at' IS NULL
                  ORDER BY created_at DESC LIMIT %s""", (["%" + n + "%" for n in NOISY], RECHECK_DAYS, limit))
    rows = mc.fetchall()
    counts = {"verified": 0, "contradicted": 0, "unknown": 0}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from nova_notify import notify
    except Exception:  # noqa: BLE001
        notify = None
    for mid, text, source in rows:
        verdict, ev = judge(text, ip_names, name_ips)
        counts[verdict] += 1
        if verdict == "unknown" or dry:
            if verdict != "unknown": log(f"would mark {verdict}: {mid} {ev[:2]}")
            continue
        if verdict == "verified":
            meta = {"verified_against": "node_status+known_devices", "verified_pairs": [list(p) for p in ev[:4]]}
            mc.execute(MARK_VERIFIED, (json.dumps(meta), mid))
        else:
            truth = {ip: sorted(ip_names.get(ip, [])) for ip, _, _ in ev}
            mc.execute(MARK_CONTRADICTED, (json.dumps(truth), json.dumps([list(p[:2]) for p in ev[:4]]), mid))
            ip, name, ips = ev[0]
            log(f"CONTRADICTED {mid} ({source}): says {ip} ~ '{name}', network says {ip} = {truth.get(ip)} and '{name}' = {ips}")
            if notify:
                notify("Memory lost to a probe", f"A memory ({source}) puts `{name}` at {ip}; the network says {ip} is "
                       f"{', '.join(truth.get(ip) or ['nobody'])} and `{name}` is at {', '.join(ips)}. Halved its weight, marked contradicted.\n"
                       f"> {text[:220]}", level="warning", category="memory", source="nova_memory_verify",
                       dedup_key=f"memverify:{mid}", meta={"memory_id": mid, "truth": truth})
    log(f"checked {len(rows)}: {counts}")

def selftest():
    ip_names = {"192.168.1.190": {"bose soundbar"}, "192.168.1.77": {"mac-mini", "jordans-mac-mini"}, "192.168.1.6": {"mac-studio"}}
    name_ips = {}
    for ip, ns in ip_names.items():
        for n in ns: name_ips.setdefault(n, set()).add(ip)
    assert judge("the fleet mac-mini is 192.168.1.190 now", ip_names, name_ips)[0] == "contradicted"
    assert judge("mac-studio (192.168.1.6) runs the scheduler", ip_names, name_ips)[0] == "verified"
    assert judge("192.168.1.99 is the printer", ip_names, name_ips)[0] == "unknown"          # probe has no opinion on .99
    assert judge("see 192.168.1.6 for the control plane " + "x" * 80 + " mac-mini", ip_names, name_ips)[0] == "unknown"  # far mention ignored
    assert judge("no addresses here", ip_names, name_ips) == ("unknown", [])
    name_ips["nova-core"] = {"192.168.1.2"}; ip_names["192.168.1.2"] = {"nova-core"}
    assert judge("192.168.1.6 is nova-core8, the Studio", ip_names, name_ips)[0] == "unknown"   # substring is not a claim
    assert judge("nova-core (192.168.1.2) runs the gateway", ip_names, name_ips)[0] == "verified"
    assert judge("nova-core (192.168.1.2) and the Studio 192.168.1.6 both run it", ip_names, name_ips)[0] == "verified"   # a list, not a misbelief
    assert judge("nova-core is 192.168.1.6 now", ip_names, name_ips)[0] == "contradicted"
    print("selftest ok")

if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
