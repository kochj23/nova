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
import subprocess, os, sys


def claude_oauth_token() -> str | None:
    """The long-lived CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), resolved fail-safe
    across the fleet. Order: an explicit env override, then the macOS Keychain (.6), then a
    0600 file on the Linux nodes (delivered out-of-band). Returns None if none is available —
    callers then fall through to the CLI's own file credential, so behaviour is never broken,
    only improved: the long-lived token keeps `claude -p` authenticated even after the CLI's
    short-lived file credential expires nightly (verified: the env token overrides an expired
    file cred). 2026-08-12."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "claude-code-oauth-token", "-w"],
                capture_output=True, text=True, timeout=5)
            t = (r.stdout or "").strip()
            if t:
                return t
        except Exception:
            pass
    for p in ("/etc/nova/claude-oauth-token",
              os.path.expanduser("~/.config/nova/claude-oauth-token")):
        try:
            with open(p) as f:
                t = f.read().strip()
            if t:
                return t
        except Exception:
            pass
    return None


def claude_env(base: dict | None = None) -> dict:
    """Environment for a `claude -p` subprocess: HOME forced (launchd-safe, so `claude` finds
    ~/.claude) plus the long-lived OAuth token injected as CLAUDE_CODE_OAUTH_TOKEN when we can
    resolve one. Fail-safe: if no token is found the env is just the base + HOME, unchanged."""
    env = {**(base if base is not None else os.environ), "HOME": os.path.expanduser("~")}
    tok = claude_oauth_token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def claude_generate(user: str, system: str | None = None,
                    model: str = "sonnet", timeout: int = 240) -> str:
    prompt = user if not system else f"[System instructions]\n{system}\n\n[Task]\n{user}"
    cmd = ["claude", "-p"]
    if model:
        cmd += ["--model", model]
    # Prompt goes over stdin, not argv -- a big prompt (e.g. fishbowl channel churn data)
    # blows past the OS's execve() arg+env size limit as a CLI argument ("Argument list
    # too long"), silently aborting the generation. stdin has no such limit.
    # launchd-safe HOME + long-lived OAuth token so auth survives the nightly file-cred expiry.
    env = claude_env()
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
