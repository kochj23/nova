"""
nova_homekit_client.py — authenticated client for the NovaHomeKit bridge (127.0.0.1:37433).

NovaHomeKit commit 51e7a91 made the bridge require a shared secret on EVERY request
(`Authorization: Bearer <token>`), POST-only on state-changing endpoints, and a loopback
Host header. Every Nova client of :37433 goes through here so the token is loaded one way.

Token sources, in order (never hardcoded, never logged):
  1. env NOVAHOMEKIT_TOKEN
  2. macOS Keychain  service=novahomekit-token account=nova   (via nova_config._keychain,
     which also falls back to the fleet secret store on Linux)
  3. ~/.config/nova/novahomekit-token (0600) — the file the Swift app itself reads; the app is
     a LaunchAgent and cannot prompt for Keychain access, so the provisioner writes both.

Provision / rotate:  python3 nova_homekit_client.py --provision   (then restart NovaHomeKit)

Written by Jordan Koch.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HK_BASE = "http://127.0.0.1:37433"
TOKEN_SERVICE = "novahomekit-token"
TOKEN_ACCOUNT = "nova"
TOKEN_ENV = "NOVAHOMEKIT_TOKEN"
TOKEN_FILE = Path.home() / ".config" / "nova" / "novahomekit-token"

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 1.0

_token_cache: str | None = None


def _log(msg: str) -> None:
    print(f"[nova_homekit_client] {msg}", file=sys.stderr)


def _read_token_file() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def load_token(refresh: bool = False) -> str:
    """Return the bridge token ('' if none is provisioned). Cached per process."""
    global _token_cache
    if _token_cache is not None and not refresh:
        return _token_cache
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if not tok:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from nova_config import _keychain
            tok = _keychain(TOKEN_SERVICE, account=TOKEN_ACCOUNT, required=False) or ""
        except Exception:
            tok = ""
    if not tok:
        tok = _read_token_file()
    _token_cache = tok
    return tok


def auth_headers(extra: dict | None = None) -> dict:
    """Headers for a NovaHomeKit request. Never raises; without a token the bridge will 401."""
    h = {"Accept": "application/json"}
    tok = load_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if extra:
        h.update(extra)
    return h


def _url(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return HK_BASE + (path_or_url if path_or_url.startswith("/") else "/" + path_or_url)


def request(path_or_url: str, method: str = "GET", timeout: float = 15,
            retries: int = RETRY_ATTEMPTS, backoff: float = RETRY_BACKOFF_S) -> bytes:
    """Authenticated request with retry + exponential backoff. Raises the last error after
    `retries` attempts (never fails silently). A 401 reloads the token once (rotation)."""
    url = _url(path_or_url)
    last: Exception | None = None
    reloaded = False
    attempt = 0
    while attempt < max(1, retries):
        attempt += 1
        req = urllib.request.Request(url, method=method, headers=auth_headers(),
                                     data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 401 and not reloaded:
                reloaded = True
                load_token(refresh=True)
                attempt -= 1          # a token reload is not a spent attempt
                continue
            if 400 <= e.code < 500:   # other client errors won't fix themselves
                break
        except Exception as e:        # URLError, timeout, connection reset
            last = e
        if attempt < retries:
            time.sleep(backoff * (2 ** (attempt - 1)))
    _log(f"{method} {url.split('?')[0]} failed after {attempt} attempt(s): {last}")
    raise last if last else RuntimeError("NovaHomeKit request failed")


def get_json(path: str = "/api/accessories", timeout: float = 15, retries: int = RETRY_ATTEMPTS):
    return json.loads(request(path, timeout=timeout, retries=retries))


def provision(force: bool = False) -> str:
    """Create (or reuse) the token in Keychain and mirror it to the 0600 file the app reads.
    Returns 'created' / 'existing'. Prints nothing secret."""
    import subprocess
    tok = "" if force else load_token(refresh=True)
    state = "existing"
    if not tok:
        tok = secrets.token_urlsafe(32)
        state = "created"
    subprocess.run(["security", "add-generic-password", "-a", TOKEN_ACCOUNT, "-s", TOKEN_SERVICE,
                    "-w", tok, "-U"], check=True, capture_output=True)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(TOKEN_FILE.parent, stat.S_IRWXU)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    load_token(refresh=True)
    return state


if __name__ == "__main__":
    if "--provision" in sys.argv:
        print(f"token {provision(force='--rotate' in sys.argv)} (Keychain + {TOKEN_FILE.name})")
    else:
        s = get_json("/api/status")
        print(json.dumps(s, indent=2))
