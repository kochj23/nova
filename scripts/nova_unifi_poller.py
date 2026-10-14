#!/opt/homebrew/bin/python3
"""
nova_unifi_poller.py — UniFi controller poller for per-client network statistics.

Polls the UniFi controller API every 5 minutes for:
  - Per-client stats (MAC, hostname, IP, rx/tx bytes, signal, channel, radio, uptime, wired)
  - AP health metrics (CPU, mem, connected clients)

Calculates bandwidth deltas between polls and inserts into:
  - telemetry.network — per-client network stats
  - telemetry.nova_meta — AP health metrics as 'unifi_ap_{name}_clients', etc.

UniFi Controller: 192.168.1.1 (UniFi OS style API)
Auth: macOS Keychain service "nova-unifi-controller" account "nova"

Written by Jordan Koch.
"""

import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
POLL_INTERVAL = 300  # 5 minutes
CONTROLLER_IP = "192.168.1.1"
CONTROLLER_BASE = f"https://{CONTROLLER_IP}"
SITE = "default"

# API endpoints (UniFi OS style)
LOGIN_URL = f"{CONTROLLER_BASE}/api/auth/login"
CLIENTS_URL = f"{CONTROLLER_BASE}/proxy/network/api/s/{SITE}/stat/sta"
DEVICES_URL = f"{CONTROLLER_BASE}/proxy/network/api/s/{SITE}/stat/device"

LOG_FILE = Path(str(Path.home()) + "/.openclaw/logs/unifi_poller.log")
import nova_dsn as _nova_dsn  # noqa: E402
DB_DSN = _nova_dsn.pg_dsn("nova_ops")

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("unifi_poller")

# ── SSL Context (disable verification for self-signed cert) ───────────────────

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# ── State ─────────────────────────────────────────────────────────────────────

_previous_poll: dict[str, dict] = {}  # mac -> {rx_bytes, tx_bytes, ts}
_session_cookie: str | None = None
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info(f"Received signal {signum}, shutting down...")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ── Keychain ──────────────────────────────────────────────────────────────────


UNIFI_KEY_NAME = "nova-unifi-api-key"                # admin key: writes (nova_unifi_ctl, toolkit House Arrest)
UNIFI_RO_KEY_NAME = "nova-unifi-api-key-readonly"    # OPTIONAL key of a View-Only UDM admin (BSRS ch.5)
_READONLY = False      # set True by read-only callers (this daemon, toolkit read commands) -> prefer the RO key
_ro_rejected = False   # the UDM refused the RO key (401/403) -> use the admin key for the rest of this process
_key_role = None       # "readonly" | "admin" — which key is loaded (never the key itself)


def _keychain_get(name: str):
    """Read a secret via `security` (macOS Keychain; on Linux the shim answers from nova.secrets)."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-a", "nova", "-s", name, "-w"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _get_unifi_api_key(readonly=None) -> str:
    """UniFi API key. Read-only callers get the View-Only admin's key when it exists (2026-10-13,
    least privilege) and fall back to the admin key, so nothing breaks before that key is created."""
    global _key_role
    if (_READONLY if readonly is None else readonly) and not _ro_rejected:
        key = _keychain_get(UNIFI_RO_KEY_NAME)
        if key:
            _key_role = "readonly"
            return key
    key = _keychain_get(UNIFI_KEY_NAME)
    if not key:
        log.error(f"Failed to get UniFi API key (service={UNIFI_KEY_NAME}, account=nova) from Keychain / fleet store")
        log.error(f"Run: security add-generic-password -a nova -s {UNIFI_KEY_NAME} -w YOUR_API_KEY")
        sys.exit(1)
    _key_role = "admin"
    return key


def _readonly_refused() -> bool:
    """Called on HTTP 401/403. If the RO key was in use, switch this process to the admin key and
    return True (caller re-logs in and retries) — a View-Only role missing a read permission must
    degrade to the old behaviour, loudly in the log, never break the collector."""
    global _ro_rejected
    if _key_role == "readonly" and not _ro_rejected:
        _ro_rejected = True
        log.warning(f"UDM refused the read-only key ({UNIFI_RO_KEY_NAME}); falling back to the admin key "
                    "for this process — grant the View-Only role the missing permission")
        return True
    return False


_api_key = None


# ── UniFi API ─────────────────────────────────────────────────────────────────

# ── Certificate pin (2026-10-13, unifi-toolkit security audit UT-04) ─────────
# The UDM's certificate is self-signed, so CA verification can't work and every call ran with
# CERT_NONE -- anyone on the LAN path could have taken the API key. Now the SHA-256 of the UDM's
# leaf certificate is pinned trust-on-first-use in service_config (unifi / udm_cert_sha256) and
# every connection through _opener fails CLOSED on a mismatch. Redirects are refused too (the API
# key header must never follow one). After a deliberate cert change:
#   nova_unifi_poller.py --repin
import hashlib            # noqa: E402
import http.client        # noqa: E402

PIN_SERVICE, PIN_KEY = "unifi", "udm_cert_sha256"
_pin_cache = None


class CertPinError(OSError):
    pass


def _udm_pin(observed: str) -> str:
    """The pinned fingerprint; pins `observed` if none is stored yet (TOFU). Unreadable PG fails closed."""
    global _pin_cache
    if _pin_cache:
        return _pin_cache
    import psycopg2
    from psycopg2.extras import Json
    with psycopg2.connect(DB_DSN, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (PIN_SERVICE, PIN_KEY))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES (%s,%s,%s,'unifi_poller TOFU') "
                        "ON CONFLICT (service, key) DO NOTHING", (PIN_SERVICE, PIN_KEY, Json(observed)))
            log.warning(f"UDM certificate pinned (trust on first use): sha256 {observed}")
            cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (PIN_SERVICE, PIN_KEY))
            row = cur.fetchone()
    _pin_cache = str(row[0])
    return _pin_cache


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        got = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        try:
            want = _udm_pin(got)
        except Exception as e:  # noqa: BLE001
            self.close()
            raise CertPinError(f"UDM certificate pin unreadable ({e}) -- refusing to send credentials") from e
        if got != want:
            self.close()
            raise CertPinError(f"UDM certificate CHANGED (pinned {want[:16]}…, got {got[:16]}…) -- refusing to send "
                               "credentials. If the cert was renewed on purpose: nova_unifi_poller.py --repin")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=ssl_ctx)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def repin() -> str:
    """Re-pin to the certificate the UDM presents right now (run only after a deliberate change)."""
    import psycopg2
    from psycopg2.extras import Json
    pem = ssl.get_server_certificate((CONTROLLER_IP, 443))
    fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    with psycopg2.connect(DB_DSN, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES (%s,%s,%s,'repin') "
                    "ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by='repin'",
                    (PIN_SERVICE, PIN_KEY, Json(fp)))
    return fp


_cookie_jar = CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookie_jar),
    _PinnedHTTPSHandler(),
    _NoRedirect(),
)


def _unifi_login() -> bool:
    """Load API key for UniFi controller auth. Returns True on success."""
    global _api_key
    _api_key = _get_unifi_api_key()
    if _api_key:
        log.info(f"Loaded UniFi API key ({_key_role})")
        return True
    log.error("No UniFi API key available")
    return False


def _unifi_get(url: str) -> dict | None:
    """GET a UniFi API endpoint. Returns parsed JSON or None on failure."""
    headers = {
        "User-Agent": "Nova-UniFi-Poller/1.0",
        "Accept": "application/json",
    }
    if _api_key:
        headers["X-API-Key"] = _api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = _opener.open(req, timeout=30)
        body = resp.read().decode("utf-8")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and _readonly_refused():
            return None                     # caller re-logs in (now with the admin key) and retries
        if e.code == 401:
            log.warning("Session expired, will re-authenticate")
            return None
        log.error(f"HTTP error fetching {url}: {e.code} {e.reason}")
        return None
    except Exception as e:
        log.error(f"Error fetching {url}: {e}")
        return None


def _fetch_clients() -> list[dict] | None:
    """Fetch all connected clients from the controller."""
    data = _unifi_get(CLIENTS_URL)
    if data is None:
        # Try re-login
        if _unifi_login():
            data = _unifi_get(CLIENTS_URL)
    if data is None:
        return None
    # UniFi API returns {"data": [...]} or {"meta": {...}, "data": [...]}
    if isinstance(data, dict):
        return data.get("data", [])
    return data


def _fetch_devices() -> list[dict] | None:
    """Fetch all UniFi network devices (APs, switches, gateways)."""
    data = _unifi_get(DEVICES_URL)
    if data is None:
        if _unifi_login():
            data = _unifi_get(DEVICES_URL)
    if data is None:
        return None
    if isinstance(data, dict):
        return data.get("data", [])
    return data


# ── Database ──────────────────────────────────────────────────────────────────


def _get_db():
    """Get a psycopg2 connection."""
    import psycopg2
    return psycopg2.connect(DB_DSN)


def _insert_client_stats(clients: list[dict], now: datetime) -> int:
    """Insert per-client stats into telemetry.network. Returns count inserted."""
    global _previous_poll
    import psycopg2
    import psycopg2.extras

    rows = []
    new_poll: dict[str, dict] = {}

    for c in clients:
        mac = c.get("mac", "").lower()
        if not mac:
            continue

        hostname = c.get("hostname") or c.get("name") or c.get("oui") or "unknown"
        ip = c.get("ip", "")
        rx_bytes = c.get("rx_bytes", 0) or 0
        tx_bytes = c.get("tx_bytes", 0) or 0
        signal_dbm = c.get("signal") or c.get("rssi")
        channel = c.get("channel")
        radio = c.get("radio")  # "na" (5GHz) or "ng" (2.4GHz)
        uptime_s = c.get("uptime", 0) or 0
        is_wired = c.get("is_wired", False)
        ap_name = c.get("last_uplink_name") or c.get("ap_name") or ""
        essid = c.get("essid") or ""

        # Store current values for delta calculation next poll
        new_poll[mac] = {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes, "ts": now}

        # If we have previous data, compute delta; otherwise use raw counters
        # (first poll will just store raw cumulative bytes, subsequent polls store deltas)
        if mac in _previous_poll:
            prev = _previous_poll[mac]
            # Handle counter resets (device reconnected)
            rx_delta = rx_bytes - prev["rx_bytes"] if rx_bytes >= prev["rx_bytes"] else rx_bytes
            tx_delta = tx_bytes - prev["tx_bytes"] if tx_bytes >= prev["tx_bytes"] else tx_bytes
        else:
            # First time seeing this client — store cumulative as-is
            rx_delta = rx_bytes
            tx_delta = tx_bytes

        rows.append((
            now, mac, hostname, ip, rx_delta, tx_delta,
            signal_dbm, channel, radio, uptime_s, is_wired, ap_name, essid
        ))

    _previous_poll = new_poll

    if not rows:
        return 0

    try:
        conn = _get_db()
        cur = conn.cursor()
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO telemetry.network
               (ts, client_mac, client_name, ip, rx_bytes, tx_bytes,
                signal_dbm, channel, radio, uptime_s, is_wired, ap_name, essid)
               VALUES %s""",
            rows,
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        )
        conn.commit()
        cur.close()
        conn.close()
        return len(rows)
    except Exception as e:
        log.error(f"DB insert (telemetry.network) failed: {e}")
        return 0


def _insert_ap_metrics(devices: list[dict], now: datetime) -> int:
    """Insert AP health metrics into telemetry.nova_meta. Returns count inserted."""
    import psycopg2
    import psycopg2.extras

    rows = []

    for dev in devices:
        # Only process APs (type "uap") and switches (type "usw")
        dev_type = dev.get("type", "")
        name = dev.get("name") or dev.get("model", "unknown")
        name_slug = name.lower().replace(" ", "_").replace("-", "_")

        if dev_type == "uap":
            # Access Point metrics
            sys_stats = dev.get("system-stats", dev.get("sys_stats", {}))
            cpu = sys_stats.get("cpu")
            mem = sys_stats.get("mem")
            num_sta = dev.get("num_sta", 0)
            user_num_sta = dev.get("user-num_sta", dev.get("user_num_sta", num_sta))

            if cpu is not None:
                rows.append((now, f"unifi_ap_{name_slug}_cpu", float(cpu), json.dumps({"device": name, "type": "uap"})))
            if mem is not None:
                rows.append((now, f"unifi_ap_{name_slug}_mem", float(mem), json.dumps({"device": name, "type": "uap"})))
            rows.append((now, f"unifi_ap_{name_slug}_clients", float(user_num_sta), json.dumps({"device": name, "type": "uap"})))

            # Per-radio stats if available
            radio_table = dev.get("radio_table_stats", [])
            for radio in radio_table:
                radio_name = radio.get("name", "")
                r_clients = radio.get("num_sta", 0)
                r_channel = radio.get("channel", 0)
                r_satisfaction = radio.get("satisfaction", 0)
                radio_slug = f"{name_slug}_{radio_name}"
                rows.append((now, f"unifi_ap_{radio_slug}_clients", float(r_clients), json.dumps({"device": name, "radio": radio_name})))
                if r_satisfaction:
                    rows.append((now, f"unifi_ap_{radio_slug}_satisfaction", float(r_satisfaction), json.dumps({"device": name, "radio": radio_name})))

        elif dev_type == "usw":
            # Switch metrics
            sys_stats = dev.get("system-stats", dev.get("sys_stats", {}))
            cpu = sys_stats.get("cpu")
            mem = sys_stats.get("mem")
            num_sta = dev.get("num_sta", 0)

            if cpu is not None:
                rows.append((now, f"unifi_sw_{name_slug}_cpu", float(cpu), json.dumps({"device": name, "type": "usw"})))
            if mem is not None:
                rows.append((now, f"unifi_sw_{name_slug}_mem", float(mem), json.dumps({"device": name, "type": "usw"})))
            rows.append((now, f"unifi_sw_{name_slug}_clients", float(num_sta), json.dumps({"device": name, "type": "usw"})))

        elif dev_type == "ugw" or dev_type == "udm":
            # Gateway/UDM metrics
            sys_stats = dev.get("system-stats", dev.get("sys_stats", {}))
            cpu = sys_stats.get("cpu")
            mem = sys_stats.get("mem")

            if cpu is not None:
                rows.append((now, f"unifi_gw_{name_slug}_cpu", float(cpu), json.dumps({"device": name, "type": dev_type})))
            if mem is not None:
                rows.append((now, f"unifi_gw_{name_slug}_mem", float(mem), json.dumps({"device": name, "type": dev_type})))

    if not rows:
        return 0

    try:
        conn = _get_db()
        cur = conn.cursor()
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO telemetry.nova_meta (ts, metric, value, metadata)
               VALUES %s""",
            rows,
            template="(%s, %s, %s, %s::jsonb)",
        )
        conn.commit()
        cur.close()
        conn.close()
        return len(rows)
    except Exception as e:
        log.error(f"DB insert (telemetry.nova_meta) failed: {e}")
        return 0


# ── Main Poll Loop ────────────────────────────────────────────────────────────


def _poll_once() -> None:
    """Execute a single poll cycle."""
    now = datetime.now(timezone.utc)
    log.info("Starting poll cycle...")

    # Fetch clients
    clients = _fetch_clients()
    if clients is None:
        log.error("Failed to fetch client list")
    else:
        count = _insert_client_stats(clients, now)
        wired = sum(1 for c in clients if c.get("is_wired", False))
        wireless = len(clients) - wired
        log.info(f"Clients: {len(clients)} total ({wireless} wireless, {wired} wired), {count} rows inserted")

    # Fetch devices (APs, switches, gateway)
    devices = _fetch_devices()
    if devices is None:
        log.error("Failed to fetch device list")
    else:
        count = _insert_ap_metrics(devices, now)
        log.info(f"Devices: {len(devices)} polled, {count} metric rows inserted")


def main():
    """Main daemon loop."""
    log.info(f"nova_unifi_poller v{VERSION} starting (interval={POLL_INTERVAL}s)")
    log.info(f"Controller: {CONTROLLER_BASE}")
    log.info(f"Logging to: {LOG_FILE}")

    # This daemon only ever GETs -> prefer the View-Only key when it exists (BSRS ch.5).
    global _READONLY
    _READONLY = True
    # Initial login
    if not _unifi_login():
        log.error("Initial authentication failed — will retry on first poll")

    poll_count = 0
    while not _shutdown:
        try:
            _poll_once()
            poll_count += 1
        except Exception as e:
            log.exception(f"Unhandled error in poll cycle: {e}")

        # Sleep in small increments to allow clean shutdown
        elapsed = 0
        while elapsed < POLL_INTERVAL and not _shutdown:
            time.sleep(5)
            elapsed += 5

    log.info(f"Shutdown complete after {poll_count} polls")


if __name__ == "__main__":
    if "--repin" in sys.argv:
        print(f"UDM certificate re-pinned: sha256 {repin()}")
        sys.exit(0)
    main()
