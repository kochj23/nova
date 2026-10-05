#!/usr/bin/env python3
"""nova_swarm_agent.py -- a swarm node-agent. Runs ON a server.
Thinks via the cluster inference router (tool-calling), ACTS via local read-only shell.
Reads the task from stdin, prints a JSON result: {"node","assessment","steps"}."""
import json, subprocess, urllib.request, socket, sys, os

ROUTER = os.environ.get("SWARM_ROUTER", "http://192.168.1.2:37475/v1/chat/completions")
MODEL  = os.environ.get("SWARM_MODEL", "code")   # qwen3-coder:30b -> reliable tool-calling
NODE   = socket.gethostname().split(".")[0]
MAX_STEPS = int(os.environ.get("SWARM_MAX_STEPS", "6"))

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a READ-ONLY shell command on THIS host to gather diagnostic info.",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
    },
}]
_BAD = ("rm ", "reboot", "shutdown", "mkfs", "dd ", ":(){", "> /", ">>", "kill ", "pkill", "systemctl stop",
        "systemctl restart", "docker kill", "docker stop", "docker rm", "truncate", "chmod -R", "chown -R")

def run_shell(cmd):
    if any(b in cmd for b in _BAD):
        return "BLOCKED (write/destructive command not allowed for a read-only diagnostic agent)"
    try:
        # diagnostics are pipelines by design; explicit sh -c argv after the _BAD screen
        r = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, timeout=20, text=True)
        return ((r.stdout or "") + (r.stderr or ""))[:1800] or "(no output)"
    except Exception as e:
        return f"error: {e}"

def llm(messages):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS, "stream": False}).encode()
    req = urllib.request.Request(ROUTER, data=body, headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=180).read())

def main():
    task = sys.stdin.read().strip() or "Assess this host's health."
    messages = [
        {"role": "system", "content":
            f"You are a diagnostic agent running ON host '{NODE}'. Use run_shell to inspect THIS host "
            "(uptime/load, disk usage, memory, top processes, failed services, recent errors). Keep commands "
            "read-only. After 2-4 tool calls, STOP and reply with a concise assessment prefixed exactly with "
            "'FINDINGS:' -- 2-4 sentences: is it healthy, and anything notable."},
        {"role": "user", "content": task},
    ]
    steps = 0
    for _ in range(MAX_STEPS):
        try:
            resp = llm(messages)
        except Exception as e:
            print(json.dumps({"node": NODE, "assessment": f"LLM error: {e}", "steps": steps})); return
        msg = resp.get("choices", [{}])[0].get("message", {})
        messages.append(msg)
        tcs = msg.get("tool_calls")
        if not tcs:
            print(json.dumps({"node": NODE, "assessment": (msg.get("content") or "").strip(), "steps": steps}))
            return
        for tc in tcs:
            steps += 1
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            out = run_shell(args.get("cmd", "echo no-cmd"))
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out})
    # ran out of steps -> force a summary
    messages.append({"role": "user", "content": "Stop investigating. Give your FINDINGS: now."})
    try:
        msg = llm(messages).get("choices", [{}])[0].get("message", {})
        print(json.dumps({"node": NODE, "assessment": (msg.get("content") or "").strip(), "steps": steps}))
    except Exception as e:
        print(json.dumps({"node": NODE, "assessment": f"(maxed steps) {e}", "steps": steps}))

if __name__ == "__main__":
    main()
