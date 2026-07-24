#!/usr/bin/env python3
"""
nova_unas_client.py — UniFi UNAS Pro 8 API client.

API key authentication via macOS Keychain.
Handles SSL (self-signed cert), retry logic, and all endpoint access.

Device: UNASPRO8 at 192.168.1.69, firmware 4.0.3
Keychain: service="nova", account="nova"  (X-API-Key header)

PRIVACY: All UNAS data is local-only. Never routed to cloud LLMs.

Written by Jordan Koch.
"""

import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

UNAS_HOST = "https://192.168.1.69"
KEYCHAIN_SERVICE = "nova"
KEYCHAIN_ACCOUNT = "nova"
KEYCHAIN_LOGIN_SERVER = "UNAS-Pro-8._smb._tcp.local"
KEYCHAIN_LOGIN_ACCOUNT = "kochj"
DEFAULT_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds between retries

# Self-signed cert on UNAS OS
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Session token cache (avoids re-login on every request)
_session_token: str | None = None


# ── Keychain ─────────────────────────────────────────────────────────────────

def _load_api_key() -> str | None:
    """Load UNAS API key: macOS Keychain, or the fleet pgcrypto secret store on
    Linux hosts (nova_config._keychain already implements this fallback chain)."""
    try:
        import nova_config
        key = nova_config._keychain(KEYCHAIN_SERVICE, account=KEYCHAIN_ACCOUNT, required=False)
        if key:
            return key
    except Exception:
        pass
    return None


def _load_login_password() -> str | None:
    """Load UNAS user password from macOS Keychain (internet password)."""
    try:
        result = subprocess.run(
            ["security", "find-internet-password",
             "-s", KEYCHAIN_LOGIN_SERVER,
             "-a", KEYCHAIN_LOGIN_ACCOUNT,
             "-w"],
            capture_output=True, text=True
        )
        pw = result.stdout.strip()
        if pw:
            return pw
    except Exception:
        pass
    return None


def _get_session_token() -> str | None:
    """Get a session token via login, caching for reuse."""
    global _session_token
    if _session_token:
        return _session_token

    password = _load_login_password()
    if not password:
        return None

    url = UNAS_HOST + "/api/auth/login"
    body = json.dumps({"username": KEYCHAIN_LOGIN_ACCOUNT, "password": password}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=DEFAULT_TIMEOUT) as resp:
            # Session token comes back as a cookie or in response headers
            cookies = resp.headers.get_all("Set-Cookie") or []
            for cookie in cookies:
                if "TOKEN" in cookie.upper() or "session" in cookie.lower():
                    token = cookie.split(";")[0]
                    _session_token = token
                    return token
            # Some UNAS firmware returns token in response body
            raw = resp.read()
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("token"):
                    _session_token = data["token"]
                    return _session_token
    except Exception:
        pass
    return None


# ── HTTP core ─────────────────────────────────────────────────────────────────

def _request(path: str, params: dict[str, str] | None = None,
             method: str = "GET", body: dict | None = None,
             retries: int = MAX_RETRIES) -> dict[str, Any]:
    """
    Make an authenticated request to the UNAS Pro API with retry logic.
    Returns parsed JSON dict. Raises UNASError on unrecoverable failure.
    """
    api_key = _load_api_key()
    if not api_key:
        raise UNASError("UNAS API key not found in Keychain "
                        f"(service={KEYCHAIN_SERVICE!r}, account={KEYCHAIN_ACCOUNT!r}). "
                        "Run: security add-generic-password -a nova -s nova -w YOUR_KEY")

    url = UNAS_HOST + path
    if params:
        query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{query}"

    data = json.dumps(body).encode() if body else None
    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Drive proxy endpoints need session-based auth
    if "/proxy/" in path:
        session_token = _get_session_token()
        if session_token:
            headers["Cookie"] = session_token

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=DEFAULT_TIMEOUT) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise UNASError(f"Authentication failed ({exc.code}) on {path} — check API key in Keychain")
            if exc.code == 404:
                raise UNASError(f"Endpoint not found: {path}")
            last_exc = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc

        if attempt < retries:
            time.sleep(RETRY_DELAY * attempt)  # exponential-ish backoff

    raise UNASError(f"UNAS request failed after {retries} attempts: {last_exc}") from last_exc


# ── Public API ────────────────────────────────────────────────────────────────

class UNASClient:
    """High-level UNAS Pro API client. All methods return plain dicts."""

    # ── System ────────────────────────────────────────────────────────────────

    def system_info(self) -> dict:
        """Device identity, firmware, cloud state, network connectivity."""
        return _request("/api/system")

    # ── Storage ───────────────────────────────────────────────────────────────

    def storage_summary(self) -> dict:
        """High-level storage health status and quota totals."""
        resp = _request("/proxy/drive/api/v1/systems/storage", params={"type": "detail"})
        return resp.get("data", {})

    def storage_basic(self) -> dict:
        """Basic storage health (status, disk slot info)."""
        resp = _request("/proxy/drive/api/v1/systems/storage")
        return resp.get("data", {})

    # ── Shares ────────────────────────────────────────────────────────────────

    def shared_drives(self) -> list[dict]:
        """All shared drives with usage, encryption, snapshot, and member info."""
        resp = _request("/proxy/drive/api/v1/shared")
        return resp.get("data", [])

    def shared_drive(self, drive_id: str) -> dict:
        """Single shared drive details by ID."""
        resp = _request(f"/proxy/drive/api/v1/shared/{drive_id}")
        return resp.get("data", {})

    # ── Health snapshot ───────────────────────────────────────────────────────

    def health_snapshot(self) -> dict:
        """
        Composite health dict combining system info, storage, and shares.
        This is what nova_unas_monitor.py and NovaControl both read.
        """
        info = self.system_info()
        try:
            storage = self.storage_summary()
        except UNASError:
            storage = {}
        try:
            shares = self.shared_drives()
        except UNASError:
            shares = []

        total_bytes = storage.get("totalQuota", 0)
        usage = storage.get("usage", {})
        used_bytes = usage.get("sharedDrives", 0) + usage.get("system", 0)
        free_bytes = total_bytes - used_bytes
        used_pct = round((used_bytes / total_bytes * 100), 1) if total_bytes > 0 else 0

        return {
            "device": {
                "model": info.get("hardware", {}).get("shortname", "UNASPRO8"),
                "name": info.get("name", "UNAS Pro 8"),
                "mac": info.get("mac", ""),
                "state": info.get("deviceState", "unknown"),
                "cloud_connected": info.get("cloudConnected", False),
                "has_internet": info.get("hasInternet", False),
            },
            "storage": {
                "status": storage.get("status", "unknown"),
                "total_bytes": total_bytes,
                "used_bytes": used_bytes,
                "free_bytes": free_bytes,
                "used_pct": used_pct,
                "total_tb": round(total_bytes / 1e12, 2),
                "free_tb": round(free_bytes / 1e12, 2),
                "needs_more_disk": storage.get("diskInfo", {}).get("needMoreDisk", False),
            },
            "shares": [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "used_bytes": s.get("usage", 0),
                    "used_tb": round(s.get("usage", 0) / 1e12, 2),
                    "encryption": s.get("encryptionStatus"),
                    "quota": s.get("quota"),
                }
                for s in shares
            ],
            "timestamp": time.time(),
        }

    def ping(self) -> bool:
        """Returns True if device is reachable and API key is valid."""
        try:
            info = self.system_info()
            return bool(info.get("hardware"))
        except UNASError:
            return False


# ── Exceptions ────────────────────────────────────────────────────────────────

class UNASError(Exception):
    pass


# ── urllib.parse needed for params ────────────────────────────────────────────
import urllib.parse  # noqa: E402  (imported at use site above, explicit here for clarity)
