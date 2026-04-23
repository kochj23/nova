#!/usr/bin/env python3
"""
nova_synology_monitor.py — Synology NAS monitoring via DSM 7 API.

Session-based auth via SYNO.API.Auth. Credentials stored in macOS Keychain.
Checks system health, storage, disks, backups, services, security, network.
Posts problems to Slack, stores status in vector memory.

PRIVACY: All NAS intents are PRIVATE — local only, never OpenRouter.

Target: Synology RS1221+ at 192.168.1.11 running DSM 7.2.2

Usage:
  python3 nova_synology_monitor.py                    # Full health check (default)
  python3 nova_synology_monitor.py --status            # System status summary
  python3 nova_synology_monitor.py --storage           # Volume/RAID health & usage
  python3 nova_synology_monitor.py --disks             # Per-disk SMART & temps
  python3 nova_synology_monitor.py --backups           # Hyper Backup task status
  python3 nova_synology_monitor.py --services          # Running packages/services
  python3 nova_synology_monitor.py --security          # Failed logins, advisories
  python3 nova_synology_monitor.py --network           # Network interface status
  python3 nova_synology_monitor.py --shares            # Shared folder listing
  python3 nova_synology_monitor.py --ups               # UPS status
  python3 nova_synology_monitor.py --problems          # Only show detected problems
  python3 nova_synology_monitor.py --snapshot          # Save daily NAS snapshot
  python3 nova_synology_monitor.py --trends            # 7-day trend comparison
  python3 nova_synology_monitor.py --json              # Full status as JSON (for Nova)

Written by Jordan Koch.
"""

import json
import ssl
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

NAS_HOST = "https://192.168.1.11:5001"
NAS_API = f"{NAS_HOST}/webapi/entry.cgi"
STATE_DIR = Path.home() / ".openclaw/workspace/state"
STATE_FILE = STATE_DIR / "nova_synology_state.json"
SNAPSHOT_FILE = STATE_DIR / "synology_snapshots.json"

# Thresholds
RAM_WARN_PCT = 75       # RAM usage warning threshold
RAM_CRIT_PCT = 90       # RAM usage critical threshold
CPU_WARN_PCT = 80       # CPU usage warning threshold
DISK_TEMP_WARN_C = 45   # HDD temperature warning
NVME_TEMP_WARN_C = 60   # NVMe temperature warning (cache drives run hotter)
DISK_TEMP_CRIT_C = 55   # HDD temperature critical
NVME_TEMP_CRIT_C = 70   # NVMe temperature critical
VOLUME_USAGE_WARN = 80  # Volume % full warning
VOLUME_USAGE_CRIT = 90  # Volume % full critical

# SSL context — self-signed cert on DSM
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def log(msg):
    print(f"[nova_synology {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Credentials ─────────────────────────────────────────────────────────────

def _get_credential(service):
    """Load a credential from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova",
             "-s", service, "-w"],
            capture_output=True, text=True
        )
        val = result.stdout.strip()
        if val:
            return val
    except Exception:
        pass
    log(f"ERROR: Keychain entry '{service}' not found")
    log(f"Run: security add-generic-password -a nova -s {service} -w YOUR_VALUE")
    return None


def get_credentials():
    """Load NAS username and password from Keychain."""
    username = _get_credential("nova-synology-username")
    password = _get_credential("nova-synology-password")
    if not username or not password:
        return None, None
    return username, password


# ── Session management ──────────────────────────────────────────────────────

class SynoSession:
    """Manages a DSM API session with login/logout lifecycle."""

    def __init__(self):
        self.sid = None
        self._retried_login = False

    def login(self):
        """Authenticate and get session ID."""
        username, password = get_credentials()
        if not username:
            return False

        params = {
            "api": "SYNO.API.Auth",
            "version": "7",
            "method": "login",
            "account": username,
            "passwd": password,
            "format": "sid",
        }
        data = self._raw_get(params)
        if data and data.get("success"):
            self.sid = data["data"]["sid"]
            return True
        error_code = data.get("error", {}).get("code", "?") if data else "no response"
        log(f"Login failed (error: {error_code})")
        return False

    def logout(self):
        """End the session."""
        if not self.sid:
            return
        try:
            params = {
                "api": "SYNO.API.Auth",
                "version": "7",
                "method": "logout",
                "_sid": self.sid,
            }
            self._raw_get(params)
        except Exception:
            pass
        self.sid = None

    def query(self, api, version, method, extra_params=None):
        """Execute an authenticated API query. Returns data dict or None."""
        if not self.sid:
            log(f"Not logged in — cannot query {api}")
            return None

        params = {
            "api": api,
            "version": str(version),
            "method": method,
            "_sid": self.sid,
        }
        if extra_params:
            params.update(extra_params)

        data = self._raw_get(params)
        if data and data.get("success"):
            return data.get("data", {})

        error_code = data.get("error", {}).get("code", "?") if data else "no response"

        # Error 119 = SID not valid (session expired/invalidated) — re-login once
        if error_code == 119 and not getattr(self, "_retried_login", False):
            log(f"Session invalidated (error 119) — re-authenticating...")
            self._retried_login = True
            self.sid = None
            if self.login():
                return self.query(api, version, method, extra_params)
            log("Re-login failed after session invalidation")
            return None

        # Don't log for expected "API not found" errors when probing optional APIs
        if error_code not in (101, 102, 103, 104, "?"):
            log(f"API error {api} method={method}: code={error_code}")
        return None

    def _raw_get(self, params):
        """Execute a raw GET request to the DSM API."""
        qs = urllib.parse.urlencode(params)
        url = f"{NAS_API}?{qs}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
                return json.loads(r.read())
        except Exception as e:
            log(f"HTTP error: {e}")
            return None

    def __enter__(self):
        if not self.login():
            raise ConnectionError("Cannot authenticate to Synology DSM")
        return self

    def __exit__(self, *args):
        self.logout()


# ── Slack & Vector Memory ──────────────────────────────────────────────────

def slack_post(text, channel=None):
    nova_config.post_both(text, slack_channel=channel or nova_config.SLACK_CHAN)


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "infrastructure",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            f"{VECTOR_URL}?async=1", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ── State file helpers ─────────────────────────────────────────────────────

def _load_json(path):
    """Load a JSON state file, returning empty dict on missing/corrupt."""
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log(f"Warning: corrupt state file {path}: {e}")
    return {}


def _save_json(path, data):
    """Atomically write JSON state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.rename(path)


# ── Data collection ────────────────────────────────────────────────────────

def get_system_info(session):
    """SYNO.Core.System v3 — model, firmware, uptime, RAM, serial."""
    return session.query("SYNO.Core.System", 3, "info")


def get_utilization(session):
    """SYNO.Core.System.Utilization v1 — CPU, memory, network, disk I/O."""
    return session.query("SYNO.Core.System.Utilization", 1, "get")


def get_storage(session):
    """SYNO.Storage.CGI.Storage v1 — volumes, disks, RAID status, SMART."""
    return session.query("SYNO.Storage.CGI.Storage", 1, "load_info")


def get_backup_tasks(session):
    """SYNO.Backup.Task — Hyper Backup task list and status."""
    # Try multiple versions — Hyper Backup API version varies by DSM
    for ver in (1, 2, 3):
        data = session.query("SYNO.Backup.Task", ver, "list")
        if data is not None:
            return data
    # Alternative API name used in some DSM versions
    for ver in (1, 2):
        data = session.query("SYNO.Backup.Repository", ver, "list")
        if data is not None:
            return data
    return None


def get_services(session):
    """Installed packages with running status, plus system services."""
    # Packages with status info
    packages = session.query(
        "SYNO.Core.Package", 1, "list",
        extra_params={"additional": '["status","startable"]'}
    )
    # System services (AFP, SMB, etc.)
    services = session.query("SYNO.Core.Service", 2, "get")
    return {"packages": packages, "services": services}


def get_security_scan(session):
    """SYNO.Core.SecurityScan.Status — security scan results."""
    return session.query("SYNO.Core.SecurityScan.Status", 1, "get")


def get_system_status(session):
    """SYNO.Core.System.Status — system warnings."""
    return session.query("SYNO.Core.System.Status", 1, "get")


def get_network(session):
    """SYNO.Core.Network + Interface list — network config and interfaces."""
    config = session.query("SYNO.Core.Network", 1, "get")
    interfaces = session.query("SYNO.Core.Network.Interface", 1, "list")
    return {"config": config, "interfaces": interfaces}


def get_shares(session):
    """SYNO.Core.Share — shared folders."""
    data = session.query("SYNO.Core.Share", 1, "list")
    if data is not None:
        return data
    return session.query("SYNO.Core.Share", 1, "get")


def get_ups(session):
    """SYNO.Core.ExternalDevice.UPS — UPS status."""
    return session.query("SYNO.Core.ExternalDevice.UPS", 1, "get")


def get_connection_log(session):
    """SYNO.Core.SyslogClient.Log — failed login attempts."""
    # Try connection logs
    data = session.query("SYNO.Core.CurrentConnection", 1, "list")
    return data


# ── Formatting helpers ─────────────────────────────────────────────────────

def _fmt_bytes(n):
    """Format bytes to human-readable."""
    if n is None:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} EB"


def _fmt_uptime(value):
    """Format uptime to human-readable string.
    DSM returns up_time as 'HH:MM:SS' string (total hours, not capped at 24).
    """
    if not value:
        return "unknown"
    if isinstance(value, str) and ":" in value:
        # DSM format: "610:53:47" = total_hours:minutes:seconds
        parts = value.split(":")
        try:
            total_hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
            days = total_hours // 24
            hours = total_hours % 24
            result = []
            if days:
                result.append(f"{days}d")
            if hours:
                result.append(f"{hours}h")
            if minutes:
                result.append(f"{minutes}m")
            return " ".join(result) if result else "<1m"
        except (ValueError, IndexError):
            return str(value)
    # Fallback: treat as seconds
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        return str(value)
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "<1m"


def _pct_bar(pct, width=20):
    """Simple text percentage bar."""
    filled = int(pct / 100 * width)
    return f"[{'#' * filled}{'.' * (width - filled)}] {pct:.1f}%"


# ── 1. System status (--status) ────────────────────────────────────────────

def cmd_status(session):
    """Display system status: model, firmware, uptime, CPU, RAM, temps."""
    sysinfo = get_system_info(session)
    util = get_utilization(session)

    if not sysinfo and not util:
        log("Cannot retrieve system info")
        return None

    lines = [f"*Synology NAS Status — {NOW.strftime('%I:%M %p')}*"]

    if sysinfo:
        model = sysinfo.get("model", "Unknown")
        firmware = sysinfo.get("firmware_ver", "Unknown")
        uptime = sysinfo.get("up_time", "")
        ram_mb = sysinfo.get("ram_size", 0)
        serial = sysinfo.get("serial", "N/A")
        temp = sysinfo.get("sys_temp", sysinfo.get("temperature", 0))
        temp_warn = sysinfo.get("sys_tempwarn", sysinfo.get("temperature_warning", False))
        cpu_vendor = sysinfo.get("cpu_vendor", "")
        cpu_series = sysinfo.get("cpu_series", "")
        cpu_cores = sysinfo.get("cpu_cores", "")
        cpu_clock = sysinfo.get("cpu_clock_speed", 0)

        lines.append(f"  Model: {model}")
        lines.append(f"  Firmware: {firmware}")
        lines.append(f"  Serial: {serial}")
        lines.append(f"  Uptime: {_fmt_uptime(uptime)}")
        if cpu_vendor:
            lines.append(f"  CPU: {cpu_vendor} {cpu_series} ({cpu_cores} cores, {cpu_clock} MHz)")
        if ram_mb:
            lines.append(f"  Installed RAM: {ram_mb} MB")
        if temp:
            warn_str = " !! WARN" if temp_warn else ""
            lines.append(f"  System Temp: {temp} C{warn_str}")
        # USB devices
        usb_devs = sysinfo.get("usb_dev", [])
        if usb_devs:
            lines.append(f"  USB Devices: {len(usb_devs)}")
            for usb in usb_devs:
                lines.append(f"    {usb.get('producer', '?')} {usb.get('product', '?')}")
        # PCI slots
        pci = sysinfo.get("external_pci_slot_info", [])
        if pci:
            for slot in pci:
                lines.append(f"  PCI Slot {slot.get('slot', '?')}: {slot.get('cardName', '?')}")

    if util:
        # CPU
        cpu = util.get("cpu", {})
        if cpu:
            user = cpu.get("user_load", 0)
            sys_load = cpu.get("system_load", 0)
            other = cpu.get("other_load", 0)
            total_cpu = user + sys_load + other
            lines.append(f"  CPU: {total_cpu}% (user {user}% / sys {sys_load}%)")

        # Memory (values from DSM are in KB)
        mem = util.get("memory", {})
        if mem:
            total_kb = mem.get("memory_size", 0)
            avail_kb = mem.get("avail_real", 0)
            cached_kb = mem.get("cached", 0)
            buffer_kb = mem.get("buffer", 0)
            real_usage_pct = mem.get("real_usage", 0)
            if total_kb > 0:
                used_kb = total_kb - avail_kb
                pct = (used_kb / total_kb) * 100
                lines.append(
                    f"  RAM: {used_kb // 1024} / {total_kb // 1024} MB ({pct:.0f}%) "
                    f"— kernel reports {real_usage_pct}% real usage"
                )
                lines.append(
                    f"    (cache: {cached_kb // 1024} MB, buffers: {buffer_kb // 1024} MB)"
                )

        # Network throughput
        nw = util.get("network", [])
        if nw:
            for iface in nw:
                name = iface.get("device", "?")
                tx = iface.get("tx", 0)
                rx = iface.get("rx", 0)
                if tx > 0 or rx > 0:
                    lines.append(f"  Net {name}: TX {_fmt_bytes(tx)}/s  RX {_fmt_bytes(rx)}/s")

    report = "\n".join(lines)
    print(report)
    return {"sysinfo": sysinfo, "utilization": util}


# ── 2. Storage health (--storage) ──────────────────────────────────────────

def cmd_storage(session):
    """Display volume status, usage, RAID health."""
    storage = get_storage(session)
    if not storage:
        log("Cannot retrieve storage info")
        return None

    lines = [f"*Synology Storage Health — {NOW.strftime('%I:%M %p')}*"]

    # Volumes
    volumes = storage.get("volumes", storage.get("vol_info", []))
    if volumes:
        lines.append("")
        lines.append("*Volumes:*")
        for vol in volumes:
            vol_id = vol.get("id", vol.get("vol_path", "?"))
            status = vol.get("status", "unknown")
            fs_type = vol.get("fs_type", "?")

            # Size info — DSM reports in various formats
            size_total = vol.get("size", {})
            if isinstance(size_total, dict):
                total_bytes = int(size_total.get("total", "0"))
                used_bytes = int(size_total.get("used", "0"))
            else:
                total_bytes = int(vol.get("vol_size", vol.get("total_size", 0)))
                used_bytes = int(vol.get("used_size", 0))

            if total_bytes > 0:
                pct = (used_bytes / total_bytes) * 100
                total_tb = total_bytes / (1024 ** 4)
                used_tb = used_bytes / (1024 ** 4)
                free_tb = (total_bytes - used_bytes) / (1024 ** 4)
                status_icon = "!!" if status != "normal" else ""
                lines.append(
                    f"  {vol_id}: {status.upper()} {status_icon} ({fs_type})"
                )
                lines.append(
                    f"    Used: {used_tb:.1f} / {total_tb:.1f} TB ({pct:.1f}%) "
                    f"— {free_tb:.1f} TB free"
                )
                lines.append(f"    {_pct_bar(pct)}")
            else:
                lines.append(f"  {vol_id}: {status.upper()} ({fs_type})")

    # RAID / Storage Pools
    pools = storage.get("storagePools", [])
    if pools:
        lines.append("")
        lines.append("*Storage Pools / RAID:*")
        for pool in pools:
            pool_id = pool.get("id", "?")
            desc = pool.get("desc", "")
            status = pool.get("status", "unknown")
            device_type = pool.get("device_type", "?")
            disk_ids = pool.get("disks", [])
            disk_str = f" — disks: {', '.join(disk_ids)}" if disk_ids else ""
            cache_disks = pool.get("cache_disks", [])
            cache_str = f" — cache: {', '.join(cache_disks)}" if cache_disks else ""

            status_icon = "!!" if status != "normal" else ""
            label = f" ({desc})" if desc else ""
            lines.append(f"  {pool_id}{label}: {device_type} — {status.upper()} {status_icon}{disk_str}{cache_str}")

            # Scrubbing status
            scrub = pool.get("scrubbingStatus", "")
            if scrub:
                lines.append(f"    Last scrub: {scrub}")

    # SSD Caches
    ssd_caches = storage.get("ssdCaches", [])
    if ssd_caches:
        lines.append("")
        lines.append("*SSD Cache:*")
        for cache in ssd_caches:
            cache_id = cache.get("id", "?")
            status = cache.get("status", "unknown")
            mode = cache.get("mode", "?")
            hit_rate = cache.get("hit_rate", "?")
            device_type = cache.get("device_type", "?")
            disks = cache.get("disks", [])
            mount_vol = cache.get("mountSpaceId", cache.get("path", "?"))
            size_info = cache.get("size", {})
            total = int(size_info.get("total", 0))
            total_gb = total / (1024 ** 3) if total else 0

            disk_str = ", ".join(disks) if disks else "?"
            status_icon = "!!" if status != "normal" else ""
            lines.append(
                f"  {cache_id}: {device_type} ({mode} cache) — {status.upper()} {status_icon}"
            )
            lines.append(f"    Disks: {disk_str} | Mounted on: {mount_vol}")
            lines.append(f"    Size: {total_gb:.0f} GB | Hit rate: {hit_rate}%")

    report = "\n".join(lines)
    print(report)
    return storage


# ── 3. Disk details (--disks) ──────────────────────────────────────────────

def cmd_disks(session):
    """Display per-disk SMART data, temperatures, health predictions."""
    storage = get_storage(session)
    if not storage:
        log("Cannot retrieve storage/disk info")
        return None

    disks = storage.get("disks", storage.get("disk_info", []))
    if not disks:
        log("No disk information available")
        return None

    lines = [f"*Synology Disk Details — {NOW.strftime('%I:%M %p')}*"]
    lines.append(f"  Total disks: {len(disks)}")
    lines.append("")

    for disk in disks:
        disk_id = disk.get("id", "?")
        model = disk.get("model", "Unknown").strip()
        vendor = disk.get("vendor", "")
        serial = disk.get("serial", disk.get("ui_serial", "N/A"))
        temp = disk.get("temp", 0)
        status = disk.get("status", "unknown")
        smart_status = disk.get("smart_status", "unknown")
        disk_type = disk.get("diskType", "")
        is_ssd = disk.get("isSsd", False)
        size_bytes = int(disk.get("size_total", 0))
        firmware = disk.get("firm", "")
        unc = disk.get("unc", 0)
        used_by = disk.get("used_by", "")
        long_name = disk.get("longName", disk.get("name", ""))
        remain_life = disk.get("remain_life", {})

        # Determine if NVMe (cache) or SATA
        is_nvme = "nvme" in disk_id.lower() or "NVMe" in disk_type

        status_icon = ""
        if status != "normal":
            status_icon = " !!"

        size_str = ""
        if size_bytes > 0:
            size_tb = size_bytes / (1024 ** 4)
            if size_tb >= 1:
                size_str = f" ({size_tb:.1f} TB)"
            else:
                size_gb = size_bytes / (1024 ** 3)
                size_str = f" ({size_gb:.0f} GB)"

        vendor_model = f"{vendor} {model}".strip()
        disk_label = f"{disk_type}" if disk_type else ("NVMe" if is_nvme else "SATA")
        lines.append(f"  *{disk_id}* ({long_name}) — {vendor_model}{size_str} [{disk_label}]")
        lines.append(f"    Status: {status.upper()}{status_icon} | SMART: {smart_status} | FW: {firmware}")
        lines.append(f"    Temp: {temp} C | Serial: {serial} | Pool: {used_by}")

        # Remaining life for SSDs
        if is_ssd and remain_life:
            life_val = remain_life.get("value", -1)
            if life_val >= 0:
                lines.append(f"    Remaining Life: {life_val}%")

        # Uncorrectable errors
        if unc and unc > 0:
            lines.append(f"    !! Uncorrectable errors: {unc}")

        # SMART attributes if available
        smart_attrs = disk.get("smart_attr", [])
        if smart_attrs:
            # Pick key SMART attributes
            interesting = {
                5: "Reallocated Sectors",
                9: "Power-On Hours",
                187: "Reported Uncorrectable",
                188: "Command Timeout",
                194: "Temperature",
                197: "Current Pending Sector",
                198: "Offline Uncorrectable",
                199: "UDMA CRC Error Count",
            }
            found_any = False
            for attr in smart_attrs:
                attr_id = attr.get("id", attr.get("attr_id", 0))
                if attr_id in interesting:
                    raw = attr.get("raw_value", attr.get("raw", "?"))
                    worst = attr.get("worst", "?")
                    current = attr.get("current", "?")
                    name = interesting.get(attr_id, f"Attr {attr_id}")
                    # Flag non-zero bad-sector counts
                    flag = ""
                    if attr_id in (5, 187, 197, 198) and str(raw) != "0":
                        flag = " !!"
                    if not found_any:
                        lines.append("    SMART highlights:")
                        found_any = True
                    lines.append(f"      {name}: raw={raw} current={current} worst={worst}{flag}")

        lines.append("")

    report = "\n".join(lines)
    print(report)
    return disks


# ── 4. Backup status (--backups) ───────────────────────────────────────────

def cmd_backups(session):
    """Display Hyper Backup task status."""
    data = get_backup_tasks(session)

    lines = [f"*Synology Backup Status — {NOW.strftime('%I:%M %p')}*"]

    if data is None:
        lines.append("  Hyper Backup API not available (package may not be installed)")
        print("\n".join(lines))
        return None

    tasks = data.get("task_list", data.get("data", []))
    if isinstance(data, list):
        tasks = data

    if not tasks:
        lines.append("  No backup tasks configured")
        print("\n".join(lines))
        return data

    lines.append(f"  Tasks: {len(tasks)}")
    lines.append("")

    for task in tasks:
        name = task.get("name", task.get("task_name", "Unknown Task"))
        status = task.get("status", task.get("state", "unknown"))
        last_result = task.get("last_result", task.get("result", "unknown"))
        last_time = task.get("last_bkp_time", task.get("last_backup_time", ""))
        next_time = task.get("next_bkp_time", task.get("next_backup_time", ""))
        progress = task.get("progress", "")
        target = task.get("target", task.get("repo_name", ""))

        status_icon = ""
        if "fail" in str(last_result).lower() or "error" in str(status).lower():
            status_icon = " !!"
        elif "warn" in str(last_result).lower():
            status_icon = " !"

        lines.append(f"  *{name}*{status_icon}")
        lines.append(f"    Status: {status} | Last result: {last_result}")
        if target:
            lines.append(f"    Target: {target}")
        if last_time:
            lines.append(f"    Last backup: {last_time}")
        if next_time:
            lines.append(f"    Next scheduled: {next_time}")
        if progress:
            lines.append(f"    Progress: {progress}")
        lines.append("")

    report = "\n".join(lines)
    print(report)
    return data


# ── 5. Services (--services) ──────────────────────────────────────────────

def cmd_services(session):
    """Display installed packages and system services with status."""
    data = get_services(session)

    lines = [f"*Synology Services — {NOW.strftime('%I:%M %p')}*"]

    # Installed packages
    pkg_data = data.get("packages") if data else None
    if pkg_data:
        packages = pkg_data.get("packages", [])
        running_pkgs = []
        stopped_pkgs = []

        for pkg in packages:
            name = pkg.get("name", pkg.get("id", "?"))
            version = pkg.get("version", "")
            additional = pkg.get("additional", {})
            status = additional.get("status", "unknown")
            startable = additional.get("startable", False)

            entry = f"{name}"
            if version:
                entry += f" (v{version})"

            if status == "running":
                running_pkgs.append(entry)
            else:
                stopped_pkgs.append(f"{entry} [{status}]")

        lines.append(f"  Packages — Running: {len(running_pkgs)} | Stopped: {len(stopped_pkgs)}")
        lines.append("")

        if running_pkgs:
            lines.append("*Running Packages:*")
            for s in sorted(running_pkgs):
                lines.append(f"  + {s}")

        if stopped_pkgs:
            lines.append("")
            lines.append("*Stopped Packages:*")
            for s in sorted(stopped_pkgs):
                lines.append(f"  - {s}")
    else:
        lines.append("  Package listing not available")

    # System services (AFP, SMB, SSH, etc.)
    svc_data = data.get("services") if data else None
    if svc_data:
        svc_list = svc_data.get("service", [])
        if svc_list:
            enabled_svcs = []
            disabled_svcs = []
            for svc in svc_list:
                name = svc.get("display_name", svc.get("service_id", "?"))
                status = svc.get("enable_status", "unknown")
                if status == "enabled":
                    enabled_svcs.append(name)
                else:
                    disabled_svcs.append(f"{name} [{status}]")

            lines.append("")
            lines.append(f"*System Services — Enabled: {len(enabled_svcs)} | Disabled: {len(disabled_svcs)}*")
            for s in sorted(enabled_svcs):
                lines.append(f"  + {s}")
            if disabled_svcs:
                for s in sorted(disabled_svcs):
                    lines.append(f"  - {s}")

    report = "\n".join(lines)
    print(report)
    return data


# ── 6. Security (--security) ──────────────────────────────────────────────

def cmd_security(session):
    """Display security scan results and failed login info."""
    lines = [f"*Synology Security — {NOW.strftime('%I:%M %p')}*"]

    # Security scan
    scan = get_security_scan(session)
    if scan:
        last_scan = scan.get("last_scan_time", scan.get("lastScanTime", "never"))
        score = scan.get("score", scan.get("totalScore", "N/A"))
        progress = scan.get("progress", "")
        is_scanning = scan.get("is_scanning", scan.get("running", False))

        lines.append(f"  Security Score: {score}")
        lines.append(f"  Last Scan: {last_scan}")
        if is_scanning:
            lines.append(f"  Scan in progress: {progress}")

        # Individual check items
        items = scan.get("items", scan.get("ruleItems", []))
        if items:
            fail_items = [i for i in items if i.get("status") in ("risk", "fail", "warning")]
            if fail_items:
                lines.append("")
                lines.append("  *Security Issues:*")
                for item in fail_items:
                    name = item.get("name", item.get("title", "?"))
                    status = item.get("status", "?")
                    category = item.get("category", "")
                    lines.append(f"    !! {name}: {status} [{category}]")
            else:
                lines.append("  All security checks passed")

    # System status (warnings)
    sys_status = get_system_status(session)
    if sys_status:
        is_sys_warn = sys_status.get("is_system_warning", False)
        if is_sys_warn:
            lines.append("")
            lines.append("  !! System warning flag is set")

    # Current connections (login sessions)
    conn = get_connection_log(session)
    if conn:
        connections = conn.get("items", conn.get("users", []))
        if isinstance(conn, list):
            connections = conn
        if connections:
            lines.append("")
            lines.append(f"  *Active Connections: {len(connections)}*")
            for c in connections[:10]:
                user = c.get("who", c.get("user", "?"))
                from_ip = c.get("from", c.get("ip", "?"))
                conn_type = c.get("type", "?")
                lines.append(f"    {user} from {from_ip} ({conn_type})")

    if not scan and not sys_status and not conn:
        lines.append("  Security scan API not available")

    report = "\n".join(lines)
    print(report)
    return {"scan": scan, "sys_status": sys_status}


# ── 7. Network (--network) ────────────────────────────────────────────────

def cmd_network(session):
    """Display network interface status and throughput."""
    net_data = get_network(session)
    util_data = get_utilization(session)

    lines = [f"*Synology Network — {NOW.strftime('%I:%M %p')}*"]

    config = net_data.get("config") if net_data else None
    interfaces = net_data.get("interfaces") if net_data else None

    # Interfaces
    if interfaces:
        # interfaces is a list from the API
        iface_list = interfaces if isinstance(interfaces, list) else []
        if iface_list:
            lines.append("")
            lines.append("*Interfaces:*")
            for iface in iface_list:
                name = iface.get("ifname", "?")
                ip = iface.get("ip", "")
                mask = iface.get("mask", "")
                speed = iface.get("speed", 0)
                status = iface.get("status", "unknown")
                iface_type = iface.get("type", "")
                use_dhcp = iface.get("use_dhcp", False)

                if speed and speed > 0:
                    if speed >= 1000:
                        speed_str = f"{speed // 1000} Gbps"
                    else:
                        speed_str = f"{speed} Mbps"
                else:
                    speed_str = "no link"

                dhcp_str = "DHCP" if use_dhcp else "static"
                ip_str = f"{ip}/{mask}" if ip and not ip.startswith("169.254") else ip or "no IP"

                status_icon = ""
                if status != "connected":
                    status_icon = " (down)"

                lines.append(f"  {name}: {ip_str} — {speed_str} — {status}{status_icon} [{dhcp_str}]")

    # Config (DNS, gateway)
    if config:
        dns1 = config.get("dns_primary", "")
        dns2 = config.get("dns_secondary", "")
        gw = config.get("gateway", "")
        hostname = config.get("server_name", "")

        lines.append("")
        if hostname:
            lines.append(f"  Hostname: {hostname}")
        if gw:
            lines.append(f"  Gateway: {gw}")
        if dns1:
            dns_str = dns1
            if dns2:
                dns_str += f", {dns2}"
            lines.append(f"  DNS: {dns_str}")

    # Real-time throughput from utilization
    if util_data:
        nw = util_data.get("network", [])
        if nw:
            lines.append("")
            lines.append("*Throughput:*")
            for iface in nw:
                name = iface.get("device", "?")
                tx = iface.get("tx", 0)
                rx = iface.get("rx", 0)
                if tx > 0 or rx > 0:
                    lines.append(f"  {name}: TX {_fmt_bytes(tx)}/s  RX {_fmt_bytes(rx)}/s")

    if not config and not interfaces and not util_data:
        lines.append("  Network API not available")

    report = "\n".join(lines)
    print(report)
    return net_data


# ── Shared folders (--shares) ─────────────────────────────────────────────

def cmd_shares(session):
    """Display shared folders."""
    data = get_shares(session)

    lines = [f"*Synology Shared Folders — {NOW.strftime('%I:%M %p')}*"]

    if data is None:
        lines.append("  Share listing API not available")
        print("\n".join(lines))
        return None

    shares = data.get("shares", data.get("items", []))
    if isinstance(data, list):
        shares = data

    if not shares:
        lines.append("  No shares returned")
        print("\n".join(lines))
        return data

    lines.append(f"  Total: {len(shares)}")
    lines.append("")

    for share in shares:
        name = share.get("name", "?")
        path = share.get("vol_path", share.get("path", ""))
        desc = share.get("desc", share.get("description", ""))
        encrypted = share.get("is_encrypted", share.get("encryption", False))
        recyclable = share.get("enable_recycle_bin", False)

        flags = []
        if encrypted:
            flags.append("encrypted")
        if recyclable:
            flags.append("recycle-bin")
        flags_str = f" [{', '.join(flags)}]" if flags else ""

        desc_str = f" — {desc}" if desc else ""
        lines.append(f"  {name}: {path}{desc_str}{flags_str}")

    report = "\n".join(lines)
    print(report)
    return data


# ── UPS (--ups) ───────────────────────────────────────────────────────────

def cmd_ups(session):
    """Display UPS status."""
    data = get_ups(session)

    lines = [f"*Synology UPS Status — {NOW.strftime('%I:%M %p')}*"]

    if data is None:
        lines.append("  UPS not connected or API not available")
        print("\n".join(lines))
        return None

    model = data.get("model", data.get("ups_model", "Unknown"))
    status = data.get("status", data.get("ups_status", "unknown"))
    charge = data.get("battery_charge", data.get("charge", "?"))
    runtime = data.get("battery_runtime", data.get("runtime", "?"))
    load = data.get("load", data.get("ups_load", "?"))

    lines.append(f"  Model: {model}")
    lines.append(f"  Status: {status}")
    lines.append(f"  Battery: {charge}% | Runtime: {runtime}s")
    lines.append(f"  Load: {load}%")

    report = "\n".join(lines)
    print(report)
    return data


# ── Problem detection ──────────────────────────────────────────────────────

def find_problems(sysinfo, utilization, storage):
    """Analyze all collected data for problems. Returns list of problem dicts."""
    problems = []

    # ── System-level checks ──
    if sysinfo:
        temp = sysinfo.get("sys_temp", sysinfo.get("temperature", 0))
        temp_warn = sysinfo.get("sys_tempwarn", sysinfo.get("temperature_warning", False))
        if temp_warn:
            problems.append({
                "severity": "high",
                "category": "temperature",
                "message": f"System temperature: {temp} C (DSM warning flag set)",
            })
        elif temp and temp > 65:
            problems.append({
                "severity": "medium",
                "category": "temperature",
                "message": f"System temperature elevated: {temp} C",
            })

    # ── CPU checks ──
    if utilization:
        cpu = utilization.get("cpu", {})
        if cpu:
            user = cpu.get("user_load", 0)
            sys_load = cpu.get("system_load", 0)
            total_cpu = user + sys_load
            if total_cpu > CPU_WARN_PCT:
                problems.append({
                    "severity": "medium",
                    "category": "cpu",
                    "message": f"CPU usage: {total_cpu}% (threshold: {CPU_WARN_PCT}%)",
                })

        # ── Memory checks (values in KB) ──
        # DSM reports real_usage which excludes cache/buffers — use that for alerts.
        # Linux aggressively caches to RAM; raw used/total is misleading.
        mem = utilization.get("memory", {})
        if mem:
            total_kb = mem.get("memory_size", 0)
            avail_kb = mem.get("avail_real", 0)
            real_usage = mem.get("real_usage", 0)  # DSM's calculated real usage %
            if total_kb > 0:
                used_kb = total_kb - avail_kb
                total_mb = total_kb // 1024
                used_mb = used_kb // 1024
                # Use DSM's real_usage if available (excludes cache/buffers)
                pct = real_usage if real_usage > 0 else (used_kb / total_kb) * 100
                if pct >= RAM_CRIT_PCT:
                    problems.append({
                        "severity": "high",
                        "category": "memory",
                        "message": f"RAM critically high: {pct:.0f}% real usage ({used_mb}/{total_mb} MB allocated)",
                    })
                elif pct >= RAM_WARN_PCT:
                    problems.append({
                        "severity": "medium",
                        "category": "memory",
                        "message": f"RAM elevated: {pct:.0f}% real usage ({used_mb}/{total_mb} MB allocated)",
                    })

    # ── Storage checks ──
    if storage:
        # Volume status
        volumes = storage.get("volumes", storage.get("vol_info", []))
        for vol in volumes:
            vol_id = vol.get("id", vol.get("vol_path", "?"))
            status = vol.get("status", "unknown")
            if status not in ("normal", "healthy"):
                problems.append({
                    "severity": "high",
                    "category": "volume",
                    "message": f"Volume {vol_id} status: {status.upper()} (degraded/crashed)",
                })

            # Volume usage
            size_info = vol.get("size", {})
            if isinstance(size_info, dict):
                total_bytes = int(size_info.get("total", "0"))
                used_bytes = int(size_info.get("used", "0"))
            else:
                total_bytes = int(vol.get("vol_size", vol.get("total_size", 0)))
                used_bytes = int(vol.get("used_size", 0))

            if total_bytes > 0:
                pct = (used_bytes / total_bytes) * 100
                if pct >= VOLUME_USAGE_CRIT:
                    problems.append({
                        "severity": "high",
                        "category": "storage",
                        "message": f"Volume {vol_id}: {pct:.1f}% full (critical threshold: {VOLUME_USAGE_CRIT}%)",
                    })
                elif pct >= VOLUME_USAGE_WARN:
                    problems.append({
                        "severity": "medium",
                        "category": "storage",
                        "message": f"Volume {vol_id}: {pct:.1f}% full (warning threshold: {VOLUME_USAGE_WARN}%)",
                    })

        # RAID / Storage Pool status
        pools = storage.get("storagePools", storage.get("raid_info", []))
        for pool in pools:
            pool_id = pool.get("id", pool.get("raidPath", "?"))
            status = pool.get("status", "unknown")
            if status not in ("normal", "healthy"):
                problems.append({
                    "severity": "high",
                    "category": "raid",
                    "message": f"Storage pool {pool_id} status: {status.upper()} (RAID degraded/rebuilding)",
                })

        # Disk health
        disks = storage.get("disks", [])
        for disk in disks:
            disk_id = disk.get("id", "?")
            status = disk.get("status", "unknown")
            temp = disk.get("temp", 0)
            smart_status = disk.get("smart_status", "unknown")
            exceed_bad = disk.get("exceed_bad_sector_thr", False)
            disk_type = disk.get("diskType", "")
            is_nvme = "nvme" in disk_id.lower() or "NVMe" in disk_type or disk.get("isSsd", False)

            # Disk status
            if status not in ("normal", "healthy", "initialized"):
                problems.append({
                    "severity": "high",
                    "category": "disk",
                    "message": f"Disk {disk_id} status: {status.upper()}",
                })

            # SMART
            if smart_status not in ("normal", "safe", "OK", "unknown"):
                problems.append({
                    "severity": "high",
                    "category": "disk",
                    "message": f"Disk {disk_id} SMART: {smart_status}",
                })

            # Bad sectors
            if exceed_bad:
                problems.append({
                    "severity": "high",
                    "category": "disk",
                    "message": f"Disk {disk_id}: exceeded bad sector threshold",
                })

            # Temperature
            if temp:
                if is_nvme:
                    if temp >= NVME_TEMP_CRIT_C:
                        problems.append({
                            "severity": "high",
                            "category": "temperature",
                            "message": f"NVMe {disk_id}: {temp} C (critical threshold: {NVME_TEMP_CRIT_C} C)",
                        })
                    elif temp >= NVME_TEMP_WARN_C:
                        problems.append({
                            "severity": "medium",
                            "category": "temperature",
                            "message": f"NVMe {disk_id}: {temp} C (warning threshold: {NVME_TEMP_WARN_C} C)",
                        })
                else:
                    if temp >= DISK_TEMP_CRIT_C:
                        problems.append({
                            "severity": "high",
                            "category": "temperature",
                            "message": f"Disk {disk_id}: {temp} C (critical threshold: {DISK_TEMP_CRIT_C} C)",
                        })
                    elif temp >= DISK_TEMP_WARN_C:
                        problems.append({
                            "severity": "medium",
                            "category": "temperature",
                            "message": f"Disk {disk_id}: {temp} C (warning threshold: {DISK_TEMP_WARN_C} C)",
                        })

    return problems


# ── Full health check (default) ───────────────────────────────────────────

def full_check(session):
    """Run all checks, post ONLY problems to Slack, store summary in vector memory."""
    log("Running Synology NAS health check...")

    sysinfo = get_system_info(session)
    utilization = get_utilization(session)
    storage = get_storage(session)

    if not sysinfo and not utilization and not storage:
        log("Could not reach Synology DSM API — all queries returned None")
        slack_post(
            "*Synology Monitor*\n"
            "  DSM API returned no data (session may have been invalidated).\n"
            "  NAS host 192.168.1.11 may still be reachable — check next cycle."
        )
        return

    problems = find_problems(sysinfo, utilization, storage)

    # Log summary
    model = sysinfo.get("model", "RS1221+") if sysinfo else "RS1221+"
    firmware = sysinfo.get("firmware_ver", "?") if sysinfo else "?"

    cpu_pct = 0
    ram_pct = 0
    if utilization:
        cpu = utilization.get("cpu", {})
        cpu_pct = cpu.get("user_load", 0) + cpu.get("system_load", 0)
        mem = utilization.get("memory", {})
        total_kb = mem.get("memory_size", 0)
        avail_kb = mem.get("avail_real", 0)
        if total_kb > 0:
            ram_pct = ((total_kb - avail_kb) / total_kb) * 100

    vol_summary = ""
    if storage:
        volumes = storage.get("volumes", storage.get("vol_info", []))
        vol_parts = []
        for vol in volumes:
            vol_id = vol.get("id", vol.get("vol_path", "?"))
            status = vol.get("status", "?")
            vol_parts.append(f"{vol_id}={status}")
        vol_summary = ", ".join(vol_parts)

    log(f"Model: {model} | DSM: {firmware} | CPU: {cpu_pct}% | RAM: {ram_pct:.0f}%")
    log(f"Volumes: {vol_summary}")
    log(f"Problems: {len(problems)}")

    # Post to Slack ONLY if there are problems
    if problems:
        lines = [f"*Synology NAS Alert — {model} — {NOW.strftime('%I:%M %p')}*"]
        high = [p for p in problems if p["severity"] == "high"]
        med = [p for p in problems if p["severity"] == "medium"]
        low = [p for p in problems if p["severity"] == "low"]

        if high:
            lines.append("*Critical:*")
            for p in high:
                lines.append(f"  !! {p['message']}")
        if med:
            lines.append("*Warnings:*")
            for p in med:
                lines.append(f"  ! {p['message']}")
        if low:
            for p in low:
                lines.append(f"  {p['message']}")

        report = "\n".join(lines)
        slack_post(report)

    # Also check for backup problems (non-blocking if API unavailable)
    backup_data = get_backup_tasks(session)
    if backup_data:
        tasks = backup_data.get("task_list", backup_data.get("data", []))
        if isinstance(backup_data, list):
            tasks = backup_data
        for task in (tasks or []):
            last_result = str(task.get("last_result", task.get("result", ""))).lower()
            name = task.get("name", task.get("task_name", "Unknown"))
            if "fail" in last_result or "error" in last_result:
                problems.append({
                    "severity": "high",
                    "category": "backup",
                    "message": f"Backup '{name}' last result: {last_result}",
                })
                slack_post(f"*Backup Alert*\n  !! Task '{name}' failed: {last_result}")

    # Store in vector memory
    summary = (
        f"NAS health check {TODAY} {NOW.strftime('%H:%M')}: "
        f"{model} DSM {firmware}, CPU {cpu_pct}%, RAM {ram_pct:.0f}%, "
        f"volumes: {vol_summary}, {len(problems)} problems"
    )
    vector_remember(summary, {"date": TODAY, "type": "nas_health"})

    # Save state
    state = {
        "last_check": NOW.isoformat(),
        "model": model,
        "firmware": firmware,
        "cpu_pct": cpu_pct,
        "ram_pct": round(ram_pct, 1),
        "problem_count": len(problems),
        "problems": [p["message"] for p in problems],
        "volumes": vol_summary,
    }
    _save_json(STATE_FILE, state)

    if not problems:
        log("All clear.")
    else:
        log(f"Found {len(problems)} problem(s) — posted to Slack.")


# ── JSON output (--json) ──────────────────────────────────────────────────

def cmd_json(session):
    """Full status dump as JSON for programmatic consumption."""
    result = {
        "timestamp": NOW.isoformat(),
        "host": NAS_HOST,
    }

    sysinfo = get_system_info(session)
    if sysinfo:
        result["system"] = sysinfo

    util = get_utilization(session)
    if util:
        result["utilization"] = util

    storage = get_storage(session)
    if storage:
        result["storage"] = storage

    problems = find_problems(sysinfo, util, storage)
    result["problems"] = problems
    result["problem_count"] = len(problems)

    print(json.dumps(result, indent=2, default=str))
    return result


# ── Snapshots & Trends ────────────────────────────────────────────────────

def cmd_snapshot(session):
    """Save a daily NAS snapshot for trend tracking."""
    sysinfo = get_system_info(session)
    util = get_utilization(session)
    storage = get_storage(session)

    if not sysinfo and not util and not storage:
        log("Cannot reach NAS for snapshot.")
        return

    problems = find_problems(sysinfo, util, storage)

    # Gather metrics
    cpu_pct = 0
    ram_pct = 0
    ram_real_pct = 0
    ram_used_mb = 0
    ram_total_mb = 0
    if util:
        cpu = util.get("cpu", {})
        cpu_pct = cpu.get("user_load", 0) + cpu.get("system_load", 0)
        mem = util.get("memory", {})
        total_kb = mem.get("memory_size", 0)
        avail_kb = mem.get("avail_real", 0)
        ram_real_pct = mem.get("real_usage", 0)
        if total_kb > 0:
            ram_pct = ((total_kb - avail_kb) / total_kb) * 100
            ram_used_mb = (total_kb - avail_kb) // 1024
            ram_total_mb = total_kb // 1024

    vol_data = []
    disk_temps = []
    if storage:
        for vol in storage.get("volumes", storage.get("vol_info", [])):
            vol_id = vol.get("id", vol.get("vol_path", "?"))
            status = vol.get("status", "?")
            size_info = vol.get("size", {})
            if isinstance(size_info, dict):
                total_bytes = int(size_info.get("total", "0"))
                used_bytes = int(size_info.get("used", "0"))
            else:
                total_bytes = int(vol.get("vol_size", vol.get("total_size", 0)))
                used_bytes = int(vol.get("used_size", 0))
            pct = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0
            vol_data.append({
                "id": vol_id,
                "status": status,
                "used_pct": round(pct, 1),
                "used_tb": round(used_bytes / (1024 ** 4), 2),
                "total_tb": round(total_bytes / (1024 ** 4), 2),
            })

        for disk in storage.get("disks", storage.get("disk_info", [])):
            disk_id = disk.get("id", disk.get("name", "?"))
            temp = disk.get("temp", disk.get("temperature", 0))
            status = disk.get("status", "?")
            if temp:
                disk_temps.append({"id": disk_id, "temp": temp, "status": status})

    snapshot = {
        "date": TODAY,
        "timestamp": NOW.isoformat(),
        "cpu_pct": cpu_pct,
        "ram_pct": round(ram_pct, 1),
        "ram_real_pct": ram_real_pct,
        "ram_used_mb": ram_used_mb,
        "ram_total_mb": ram_total_mb,
        "problem_count": len(problems),
        "volumes": vol_data,
        "disk_temps": disk_temps,
        "problems": [p["message"] for p in problems],
    }

    snapshots = _load_json(SNAPSHOT_FILE)
    if not isinstance(snapshots, dict):
        snapshots = {"snapshots": []}
    if "snapshots" not in snapshots:
        snapshots["snapshots"] = []

    # Replace today's snapshot if already exists
    snapshots["snapshots"] = [s for s in snapshots["snapshots"] if s.get("date") != TODAY]
    snapshots["snapshots"].append(snapshot)

    # Keep 90 days
    if len(snapshots["snapshots"]) > 90:
        snapshots["snapshots"] = snapshots["snapshots"][-90:]

    _save_json(SNAPSHOT_FILE, snapshots)

    print(f"*NAS Snapshot — {TODAY}*")
    print(f"  CPU: {cpu_pct}% | RAM: {ram_pct:.0f}% alloc, {ram_real_pct}% real ({ram_used_mb}/{ram_total_mb} MB)")
    for v in vol_data:
        print(f"  {v['id']}: {v['used_tb']:.1f} / {v['total_tb']:.1f} TB ({v['used_pct']}%) — {v['status']}")
    if disk_temps:
        temp_strs = [f"{d['id']}={d['temp']}C" for d in disk_temps]
        print(f"  Disk temps: {', '.join(temp_strs)}")
    print(f"  Problems: {len(problems)}")
    print(f"  Saved to: {SNAPSHOT_FILE}")

    vector_remember(
        f"NAS snapshot {TODAY}: CPU {cpu_pct}%, RAM {ram_pct:.0f}%, "
        f"{len(problems)} problems, {len(vol_data)} volumes",
        {"date": TODAY, "type": "nas_snapshot"}
    )


def cmd_trends(session):
    """Show 7-day NAS trend comparison."""
    snapshots = _load_json(SNAPSHOT_FILE)
    entries = snapshots.get("snapshots", [])
    if not entries:
        print("No snapshots recorded yet. Run --snapshot first.")
        return

    recent = entries[-7:]

    print(f"*NAS Trends (last {len(recent)} snapshots)*\n")
    print(f"  {'Date':<12} {'CPU':>6} {'RAM':>6} {'Problems':>8}")
    print(f"  {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 8}")

    for s in recent:
        dt = s.get("date", "?")
        cpu = s.get("cpu_pct", "?")
        ram = s.get("ram_pct", "?")
        probs = s.get("problem_count", "?")

        cpu_str = f"{cpu}%" if isinstance(cpu, (int, float)) else str(cpu)
        ram_str = f"{ram}%" if isinstance(ram, (int, float)) else str(ram)

        print(f"  {dt:<12} {cpu_str:>6} {ram_str:>6} {str(probs):>8}")

    # Volume trends
    if recent and recent[-1].get("volumes"):
        print("")
        print("  *Volume Usage Trend:*")
        for entry in recent:
            dt = entry.get("date", "?")
            vols = entry.get("volumes", [])
            vol_strs = [f"{v['id']}={v.get('used_pct', '?')}%" for v in vols]
            print(f"  {dt}: {', '.join(vol_strs)}")

    # Disk temp trends
    if recent and recent[-1].get("disk_temps"):
        print("")
        print("  *Disk Temperature Trend (latest):*")
        latest = recent[-1]
        for d in latest.get("disk_temps", []):
            print(f"    {d['id']}: {d['temp']} C ({d.get('status', '?')})")

    # Changes summary
    if len(recent) >= 2:
        first = recent[0]
        last = recent[-1]
        print(f"\n*Changes ({first.get('date', '?')} -> {last.get('date', '?')}):*")

        for metric, label in [
            ("cpu_pct", "CPU"),
            ("ram_pct", "RAM"),
            ("problem_count", "Problems"),
        ]:
            v1 = first.get(metric, 0)
            v2 = last.get(metric, 0)
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                diff = v2 - v1
                if abs(diff) > 0.1:
                    direction = "+" if diff > 0 else ""
                    unit = "%" if metric in ("cpu_pct", "ram_pct") else ""
                    print(f"  {label}: {v1}{unit} -> {v2}{unit} ({direction}{diff:.1f}{unit})")

        # Volume usage changes
        first_vols = {v["id"]: v.get("used_pct", 0) for v in first.get("volumes", [])}
        last_vols = {v["id"]: v.get("used_pct", 0) for v in last.get("volumes", [])}
        for vol_id in last_vols:
            if vol_id in first_vols:
                diff = last_vols[vol_id] - first_vols[vol_id]
                if abs(diff) > 0.1:
                    direction = "+" if diff > 0 else ""
                    print(f"  {vol_id}: {first_vols[vol_id]}% -> {last_vols[vol_id]}% ({direction}{diff:.1f}%)")


# ── Problems only (--problems) ────────────────────────────────────────────

def cmd_problems(session):
    """Display only detected problems."""
    sysinfo = get_system_info(session)
    util = get_utilization(session)
    storage = get_storage(session)
    problems = find_problems(sysinfo, util, storage)

    if problems:
        print(f"*Synology NAS Problems ({len(problems)})*")
        for p in problems:
            severity = p["severity"].upper()
            print(f"  [{severity}] {p['message']}")
    else:
        print("No problems detected.")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nova Synology NAS Monitor")
    parser.add_argument("--status", action="store_true", help="System status summary")
    parser.add_argument("--storage", action="store_true", help="Volume/RAID health & usage")
    parser.add_argument("--disks", action="store_true", help="Per-disk SMART & temps")
    parser.add_argument("--backups", action="store_true", help="Hyper Backup task status")
    parser.add_argument("--services", action="store_true", help="Running packages/services")
    parser.add_argument("--security", action="store_true", help="Security scan & failed logins")
    parser.add_argument("--network", action="store_true", help="Network interface status")
    parser.add_argument("--shares", action="store_true", help="Shared folder listing")
    parser.add_argument("--ups", action="store_true", help="UPS status")
    parser.add_argument("--problems", action="store_true", help="Only show detected problems")
    parser.add_argument("--snapshot", action="store_true", help="Save daily NAS snapshot")
    parser.add_argument("--trends", action="store_true", help="7-day trend comparison")
    parser.add_argument("--json", action="store_true", help="Full status as JSON")
    args = parser.parse_args()

    try:
        with SynoSession() as session:
            if args.status:
                cmd_status(session)
            elif args.storage:
                cmd_storage(session)
            elif args.disks:
                cmd_disks(session)
            elif args.backups:
                cmd_backups(session)
            elif args.services:
                cmd_services(session)
            elif args.security:
                cmd_security(session)
            elif args.network:
                cmd_network(session)
            elif args.shares:
                cmd_shares(session)
            elif args.ups:
                cmd_ups(session)
            elif args.problems:
                cmd_problems(session)
            elif args.snapshot:
                cmd_snapshot(session)
            elif args.trends:
                cmd_trends(session)
            elif args.json:
                cmd_json(session)
            else:
                full_check(session)
    except ConnectionError as e:
        log(f"FATAL: {e}")
        slack_post(f"*Synology Monitor*\n  !! Cannot authenticate to NAS: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL: {e}")
        slack_post(f"*Synology Monitor*\n  !! Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
