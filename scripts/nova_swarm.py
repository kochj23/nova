#!/usr/bin/env python3
"""nova_swarm.py -- distributed agent-swarm DISPATCHER (push model, runs on .6).
Takes a job -> decomposes it (capability-aware, via the cluster brain) -> fans subtasks to the
server-agents in parallel -> logs to swarm.tasks/jobs -> synthesizes -> records to telemetry.

Usage:
  nova_swarm.py "audit each host's security posture and flag issues"     # LLM decomposes + routes
  nova_swarm.py --each "assess this host's health"                        # same task to every node
"""
import concurrent.futures, subprocess, json, sys, time, urllib.request, uuid, os
import nova_router  # sibling module; resolves a healthy router (.2 primary -> .10 standby)

ROUTER = nova_router.chat_url()
AGENT_LOCAL = os.path.expanduser("~/.openclaw/scripts/nova_swarm_agent.py")
AGENT_REMOTE = "/tmp/nova_swarm_agent.py"
# name, ssh-host (or "local"), python, capability blurb (for routing)
NODES = [
    ("mac-studio", "local",         "/opt/homebrew/bin/python3", "M3 Ultra, 512GB, heavy reasoning + big-model inference + the memory store"),
    ("nova-core",  "192.168.1.2",   "python3",                   "Intel x86, the databases, security monitoring, always-on services"),
    ("nova-core2", "192.168.1.86",  "python3",                   "AMD Radeon GPU, media/transcode + GPU inference, lots of spare headroom"),
    ("mac-mini",   "192.168.1.190", "/opt/homebrew/bin/python3", "M4 Pro, 64GB, fast Apple-Silicon compute and inference"),
    ("tv-movies",  "192.168.1.7",   "/opt/homebrew/bin/python3", "M2 Pro, 32GB, media-adjacent + secondary inference"),
    ("nova-core5",  "192.168.1.10",  "python3",                   "tiny Intel NUC, 16GB, no GPU -- light lookups only"),
]
BYNAME = {n[0]: n for n in NODES}

def q(s):  # escape for psql single-quoted literal
    return (s or "").replace("'", "''")

def psql(sql):
    return subprocess.run(["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_ops", "-tAc", sql],
                          capture_output=True, text=True, timeout=30).stdout.strip()

def llm(system, user, model="code", max_tokens=1500):
    body = json.dumps({"model": model, "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}], "stream": False, "max_tokens": max_tokens}).encode()
    r = urllib.request.urlopen(urllib.request.Request(ROUTER, data=body,
        headers={"Content-Type": "application/json"}, method="POST"), timeout=180)
    return json.loads(r.read())["choices"][0]["message"]["content"]

def deploy():
    for name, host, _, _ in NODES:
        if host != "local":
            subprocess.run(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", AGENT_LOCAL,
                            f"kochj@{host}:{AGENT_REMOTE}"], capture_output=True, timeout=25)

def decompose(job):
    roster = "\n".join(f"- {n}: {cap}" for n, _, _, cap in NODES)
    system = ("You decompose a job into independent subtasks for a fleet of servers, each an agent that can "
              "run read-only shell diagnostics on ITS OWN host and reason. Output ONLY a JSON array of "
              "{\"node\":<one of the node names>, \"task\":<a concrete subtask for that node>}. Route each subtask "
              "to the most suitable node by capability. Use 3-6 subtasks. No prose, JSON only.")
    user = f"NODES:\n{roster}\n\nJOB: {job}\n\nJSON:"
    try:
        raw = llm(system, user)
        s = raw[raw.index("["):raw.rindex("]") + 1]
        items = json.loads(s)
        out = [(BYNAME[i["node"]], i["task"]) for i in items if i.get("node") in BYNAME and i.get("task")]
        if out:
            return out
    except Exception as e:
        print(f"  (decompose fell back to fan-to-all: {e})", file=sys.stderr)
    return [(n, job) for n in NODES]  # fallback: same task to every node

def run_agent(node, task):
    name, host, py, _ = node
    t0 = time.time()
    try:
        if host == "local":
            p = subprocess.run([py, AGENT_LOCAL], input=task, capture_output=True, timeout=240, text=True)
        else:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"kochj@{host}",
                                f"{py} {AGENT_REMOTE}"], input=task, capture_output=True, timeout=240, text=True)
        for line in reversed((p.stdout or "").strip().splitlines()):
            if line.strip().startswith("{"):
                r = json.loads(line.strip()); r["secs"] = round(time.time() - t0); r["node"] = name; return r
        return {"node": name, "assessment": f"(no result) {(p.stderr or '')[:120]}", "steps": 0, "secs": round(time.time()-t0)}
    except Exception as e:
        return {"node": name, "assessment": f"ERROR: {str(e)[:120]}", "steps": 0, "secs": round(time.time()-t0)}

def dispatch(job, fan_each=False):
    job_id = "swarm-" + uuid.uuid4().hex[:10]
    deploy()
    subtasks = [(n, job) for n in NODES] if fan_each else decompose(job)
    psql(f"INSERT INTO swarm.jobs (job_id, description, status) VALUES ('{job_id}', '{q(job)}', 'running');")
    for node, task in subtasks:
        psql(f"INSERT INTO swarm.tasks (job_id, node, prompt, status) VALUES ('{job_id}', '{node[0]}', '{q(task)}', 'running');")
    print(f"job {job_id}: {len(subtasks)} subtasks fanned across the fleet\n")
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(subtasks)) as ex:
        results = list(ex.map(lambda st: run_agent(st[0], st[1]), subtasks))
    wall = time.time() - t0
    for r in results:
        psql("UPDATE swarm.tasks SET status='done', result='{}', steps={}, finished_at=now() "
             "WHERE job_id='{}' AND node='{}' AND status='running';".format(
                 q(r.get('assessment', '')[:4000]), r.get('steps', 0), job_id, r.get('node')))
        print(f"[{r.get('node')}]  {r.get('steps')} tool-calls · {r.get('secs')}s")
        print("  " + (r.get('assessment', '') or '').replace("\n", " ")[:400] + "\n")
    findings = "\n".join(f"{r.get('node')}: {(r.get('assessment','') or '').replace(chr(10),' ')[:400]}" for r in results)
    try:
        synth = llm("You are the fleet coordinator. Synthesize these per-node agent findings into a clear, "
                    "skeptical summary. Note anything that needs human verification -- local-model agents can be "
                    "confidently wrong, so flag uncertain findings rather than asserting them.",
                    findings, model="conversation", max_tokens=800)
    except Exception as e:
        synth = f"(synthesis failed: {e})"
    print("=== SYNTHESIS ===\n" + synth)
    psql(f"UPDATE swarm.jobs SET status='done', synthesis='{q(synth[:4000])}', finished_at=now() WHERE job_id='{job_id}';")
    psql("INSERT INTO telemetry.events (ts, level, category, source, title, body) VALUES "
         "(now(), 'info', 'swarm', 'nova_swarm.py', 'Swarm job {}: {} agents, {:.0f}s', '{}');".format(
             job_id, len(subtasks), wall, q(job[:200])))
    return job_id

if __name__ == "__main__":
    args = sys.argv[1:]
    fan = "--each" in args
    args = [a for a in args if a != "--each"]
    if not args:
        print("usage: nova_swarm.py [--each] \"job description\""); sys.exit(1)
    dispatch(" ".join(args), fan_each=fan)
