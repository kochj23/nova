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
import subprocess, os, sys, shutil

# Where `claude` lives across the fleet. launchd / systemd / scheduler children often run with a
# minimal PATH (the core scheduler: /usr/local/bin:/usr/bin:/bin), so a bare "claude" argv fails
# with [Errno 2] even though the CLI is installed — that is how 16 articles died between
# 2026-10-18 16:36 and 10-19 09:27 (core leader failed over to nova-core3, whose CLI is the
# per-user install in ~/.local/bin; .6's Homebrew link was also mid-update). Checked in order
# after $NOVA_CLAUDE_BIN and shutil.which().
CLAUDE_KNOWN_PATHS = (
    "/opt/homebrew/bin/claude",          # .6 / Apple Silicon Homebrew (npm global link)
    "/usr/local/bin/claude",             # nova-core (npm global, root-owned link)
    "~/.local/bin/claude",               # nova-core3 native installer (per-user)
    "~/.claude/local/claude",            # `claude migrate-installer` local install
    "~/.npm-global/bin/claude",
    "/usr/bin/claude",
)


class ClaudeCLIMissing(FileNotFoundError):
    """The Claude Code CLI is not installed / not reachable from this process."""


def resolve_claude_bin() -> str:
    """Absolute path of an executable `claude`, or raise ClaudeCLIMissing with an actionable
    message (host, PATH, every location tried, how to fix). Never returns a bare name."""
    tried = []
    override = os.environ.get("NOVA_CLAUDE_BIN", "").strip()
    cands = ([override] if override else []) + [shutil.which("claude") or ""] + list(CLAUDE_KNOWN_PATHS)
    for c in cands:
        if not c:
            continue
        p = os.path.expanduser(c)
        tried.append(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    import socket
    raise ClaudeCLIMissing(
        f"Claude Code CLI not found on {socket.gethostname()} — tried {', '.join(dict.fromkeys(tried))} "
        f"(PATH={os.environ.get('PATH', '')}). Fix: reinstall (`npm i -g @anthropic-ai/claude-code` or the "
        f"native installer), or set NOVA_CLAUDE_BIN=/path/to/claude, then re-run the job.")


def claude_oauth_token() -> str | None:
    """The long-lived CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), resolved fail-safe
    across the fleet. Order: an explicit env override, then the FLEET SECRET STORE (1Password
    vault "Nova" item `claude-code-oauth-token`, mirrored into nova.secrets — the source of
    truth, so a rotation reaches every node with no file to push), then the macOS Keychain
    (.6), then a 0600 file on the Linux nodes (a legacy cache that goes stale: .5's copy was
    18 days old on 2026-10-21). Returns None if none is available — callers then fall through
    to the CLI's own file credential, so behaviour is never broken, only improved: the
    long-lived token keeps `claude -p` authenticated even after the CLI's short-lived file
    credential expires (verified: the env token overrides an expired file cred, which is how
    nova-core2/.86's 2026-10-03-expired ~/.claude/.credentials.json stopped mattering).
    2026-08-12; fleet-store source added 2026-10-21."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    # Fleet store / 1Password: works under systemd and cron (nova_secrets._load falls back to
    # the sealed systemd credential), so the scheduler path is not tied to a login shell.
    try:
        sys.path[:0] = [p for p in ("/opt/nova", os.path.expanduser("~/.openclaw/scripts"))
                        if p not in sys.path]
        from nova_secrets import get_secret  # noqa: PLC0415
        t = (get_secret("claude-code-oauth-token") or "").strip()
        if t:
            return t
    except Exception:
        pass
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
    cmd = [resolve_claude_bin(), "-p"]   # ClaudeCLIMissing (a FileNotFoundError) if absent
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
