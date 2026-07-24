#!/opt/homebrew/bin/python3
"""
nova_claude_code.py — generate text via the Claude Code Max subscription (`claude -p`)
at zero per-token cost, for SHOWCASE content only (the daily article, essays).

Raises on any failure so the caller falls back to its normal OpenRouter path — so a
CLI/auth hiccup can never silently break a generation.

  from nova_claude_code import claude_generate
  try:
      text = claude_generate(user_prompt, system=sys_prompt, model="sonnet")
  except Exception:
      text = call_llm(...)   # existing cheap-OpenRouter fallback

DO NOT use for high-volume/agentic workloads (Strix, per-item loops): that risks the
Max plan's rate limits and is off-label for bulk automation.
"""
import subprocess, os

def claude_generate(user: str, system: str | None = None,
                    model: str = "sonnet", timeout: int = 240) -> str:
    prompt = user if not system else f"[System instructions]\n{system}\n\n[Task]\n{user}"
    cmd = ["claude", "-p"]
    if model:
        cmd += ["--model", model]
    # Prompt goes over stdin, not argv -- a big prompt (e.g. fishbowl channel churn data)
    # blows past the OS's execve() arg+env size limit as a CLI argument ("Argument list
    # too long"), silently aborting the generation. stdin has no such limit.
    # launchd-safe: force HOME so `claude` finds its auth (~/.claude)
    env = {**os.environ, "HOME": os.path.expanduser("~")}
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                       env=env)
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError(f"claude -p empty/failed (rc={r.returncode}): {(r.stderr or '')[:200]}")
    return out


if __name__ == "__main__":
    # self-check: prove the mechanism + the fallback contract
    import sys
    try:
        t = claude_generate("In one short sentence, describe a home lab.",
                            system="You are terse.", timeout=90)
        print("OK via claude -p:", t[:120])
    except Exception as e:
        print("claude -p failed (caller would fall back to OpenRouter):", e); sys.exit(1)
