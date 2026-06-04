#!/usr/bin/env python3
"""
nova_syslog_server.py — Unified syslog receiver for Nova's network.

Receives syslog (RFC 3164/5424) from all network devices on UDP 1514,
stores in PostgreSQL, detects threats in real-time, and alerts via Slack
and journal security posts.

Services:
  - UDP syslog listener on 0.0.0.0:1514
  - HTTP health/stats API on 0.0.0.0:37462

Sources: UDM Pro, Synology NAS, UniFi switches/APs, Mac Minis, etc.

Written by Jordan Koch.
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import asyncpg
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
SYSLOG_PORT = 1514
HTTP_PORT = 37462
BIND_ADDR = "0.0.0.0"
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
LOG_FILE = Path.home() / ".openclaw/logs/nova_syslog.log"
QUEUE_MAX = 10_000
BATCH_SIZE = 100
BATCH_INTERVAL = 1.0
DEDUP_WINDOW = 300
RETENTION_DAYS = 90
THREAT_RETENTION_DAYS = 365
BRUTE_THRESHOLD = 5
BRUTE_WINDOW = 300
SCAN_THRESHOLD = 5
SCAN_WINDOW = 60

FACILITY_NAMES = {
    0: "kern", 1: "user", 2: "mail", 3: "daemon", 4: "auth", 5: "syslog",
    6: "lpr", 7: "news", 8: "uucp", 9: "cron", 10: "authpriv", 11: "ftp",
    16: "local0", 17: "local1", 18: "local2", 19: "local3",
    20: "local4", 21: "local5", 22: "local6", 23: "local7",
}

SEVERITY_NAMES = {
    0: "emerg", 1: "alert", 2: "crit", 3: "err",
    4: "warning", 5: "notice", 6: "info", 7: "debug",
}

# ── Threat Detection Rules ────────────────────────────────────────────────────

IPS_RULES = [
    (re.compile(r"ET WORM", re.IGNORECASE), "worm", "critical"),
    (re.compile(r"ET TROJAN", re.IGNORECASE), "trojan", "critical"),
    (re.compile(r"ET EXPLOIT", re.IGNORECASE), "exploit", "critical"),
    (re.compile(r"ET MALWARE", re.IGNORECASE), "malware", "critical"),
    (re.compile(r"ET ATTACK_RESPONSE", re.IGNORECASE), "attack_response", "critical"),
    (re.compile(r"GPL EXPLOIT", re.IGNORECASE), "exploit", "critical"),
    (re.compile(r"ET SCAN", re.IGNORECASE), "scan", "warning"),
    (re.compile(r"ET DNS", re.IGNORECASE), "dns_anomaly", "warning"),
    (re.compile(r"ET POLICY", re.IGNORECASE), "policy", "info"),
    (re.compile(r"ET INFO", re.IGNORECASE), "info", "info"),
]

FIREWALL_RE = re.compile(
    r"\[FW[-_]?(DROP|BLOCK|REJECT)\].*SRC=(\S+).*DST=(\S+)", re.IGNORECASE)
UNIFI_IPS_RE = re.compile(
    r"Intrusion Prevention.*Action:\s*(Block|Drop).*Signature:\s*(.+?)(?:\s*$|\s*Signature ID)", re.IGNORECASE)

AUTH_PATTERNS = [
    re.compile(r"Failed password for .+ from (\S+)"),
    re.compile(r"authentication failure.*rhost=(\S+)"),
    re.compile(r"Invalid user .+ from (\S+)"),
    re.compile(r"FAILED LOGIN .+ FROM (\S+)", re.IGNORECASE),
]

IP_RE = re.compile(r"SRC=(\d+\.\d+\.\d+\.\d+).*DST=(\d+\.\d+\.\d+\.\d+)")
PORT_RE = re.compile(r"SPT=(\d+).*DPT=(\d+)")

# ── Globals ───────────────────────────────────────────────────────────────────

_start_time = time.time()
_msg_count = 0
_threat_count = 0
_recent_alerts: dict[str, float] = {}
_auth_failures: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
_scan_events: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
_shutdown = False


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[nova_syslog {ts}] [{level}] {msg}"
    print(line, flush=True)


# ── Syslog Parser ─────────────────────────────────────────────────────────────

_RFC3164_RE = re.compile(
    r"<(\d{1,3})>"
    r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(\S+)\s+"
    r"(\S+?)(?:\[(\d+)\])?:\s*(.*)",
    re.DOTALL
)

_RFC5424_RE = re.compile(
    r"<(\d{1,3})>(\d)\s+"
    r"(\S+)\s+"
    r"(\S+)\s+"
    r"(\S+)\s+"
    r"(\S+)\s+"
    r"(\S+)\s+"
    r"(?:\[.*?\]\s*)?(.*)",
    re.DOTALL
)


def parse_syslog(data: bytes, addr: tuple) -> dict:
    """Parse a syslog message (RFC 3164 or 5424)."""
    try:
        text = data.decode("utf-8", errors="replace").strip()
    except Exception:
        return None

    if not text:
        return None

    result = {
        "source_ip": addr[0],
        "message": text,
        "hostname": None,
        "facility": None,
        "severity": None,
        "app_name": None,
        "proc_id": None,
        "msg_id": None,
        "timestamp": None,
    }

    # Try RFC 5424 first (version digit after PRI)
    m = _RFC5424_RE.match(text)
    if m:
        pri = int(m.group(1))
        result["facility"] = pri >> 3
        result["severity"] = pri & 0x07
        ts_str = m.group(3)
        result["hostname"] = m.group(4) if m.group(4) != "-" else None
        result["app_name"] = m.group(5) if m.group(5) != "-" else None
        result["proc_id"] = m.group(6) if m.group(6) != "-" else None
        result["msg_id"] = m.group(7) if m.group(7) != "-" else None
        result["message"] = m.group(8)
        if ts_str != "-":
            try:
                result["timestamp"] = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass
        return result

    # Try RFC 3164
    m = _RFC3164_RE.match(text)
    if m:
        pri = int(m.group(1))
        result["facility"] = pri >> 3
        result["severity"] = pri & 0x07
        result["hostname"] = m.group(3)
        result["app_name"] = m.group(4)
        result["proc_id"] = m.group(5)
        result["message"] = m.group(6)
        ts_str = m.group(2)
        try:
            now = datetime.now()
            parsed = datetime.strptime(ts_str, "%b %d %H:%M:%S")
            result["timestamp"] = parsed.replace(year=now.year, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
        return result

    # Fallback: just extract PRI if present
    pri_match = re.match(r"<(\d{1,3})>(.*)", text, re.DOTALL)
    if pri_match:
        pri = int(pri_match.group(1))
        result["facility"] = pri >> 3
        result["severity"] = pri & 0x07
        result["message"] = pri_match.group(2).strip()

    return result


# ── Threat Detection ──────────────────────────────────────────────────────────

def detect_threat(event: dict) -> dict | None:
    """Check if a syslog event contains a security threat. Returns threat metadata or None."""
    msg = event["message"]

    # IPS/IDS signature match
    for pattern, threat_name, severity in IPS_RULES:
        if pattern.search(msg):
            ip_m = IP_RE.search(msg)
            port_m = PORT_RE.search(msg)
            return {
                "threat_type": "ips",
                "signature": threat_name + ": " + msg[:200],
                "action": "blocked",
                "direction": "inbound",
                "src_addr": ip_m.group(1) if ip_m else None,
                "dst_addr": ip_m.group(2) if ip_m else None,
                "src_port": int(port_m.group(1)) if port_m else None,
                "dst_port": int(port_m.group(2)) if port_m else None,
                "severity_level": severity,
            }

    # UniFi IPS format
    m = UNIFI_IPS_RE.search(msg)
    if m:
        ip_m = IP_RE.search(msg)
        return {
            "threat_type": "ips",
            "signature": m.group(2).strip(),
            "action": m.group(1).lower(),
            "direction": "inbound",
            "src_addr": ip_m.group(1) if ip_m else None,
            "dst_addr": ip_m.group(2) if ip_m else None,
            "src_port": None,
            "dst_port": None,
            "severity_level": "critical",
        }

    # Firewall block from internal host
    m = FIREWALL_RE.search(msg)
    if m:
        src = m.group(2)
        dst = m.group(3)
        port_m = PORT_RE.search(msg)
        is_internal = src.startswith("192.168.1.")
        return {
            "threat_type": "firewall",
            "signature": f"FW {m.group(1)} {'internal' if is_internal else 'external'}",
            "action": m.group(1).lower(),
            "direction": "internal" if is_internal else "inbound",
            "src_addr": src,
            "dst_addr": dst,
            "src_port": int(port_m.group(1)) if port_m else None,
            "dst_port": int(port_m.group(2)) if port_m else None,
            "severity_level": "critical" if is_internal else "info",
        }

    # Auth failures
    for pattern in AUTH_PATTERNS:
        m = pattern.search(msg)
        if m:
            src_ip = m.group(1)
            now = time.time()
            _auth_failures[src_ip].append(now)
            recent = [t for t in _auth_failures[src_ip] if now - t < BRUTE_WINDOW]
            if len(recent) >= BRUTE_THRESHOLD:
                return {
                    "threat_type": "auth_failure",
                    "signature": f"Brute force: {len(recent)} failures from {src_ip} in {BRUTE_WINDOW}s",
                    "action": "detected",
                    "direction": "inbound",
                    "src_addr": src_ip,
                    "dst_addr": event.get("source_ip"),
                    "src_port": None,
                    "dst_port": None,
                    "severity_level": "warning",
                }
            return None

    return None


def should_alert(threat: dict, event: dict) -> bool:
    """Check dedup window — only alert once per (signature_prefix, src) per DEDUP_WINDOW."""
    sig_key = (threat["signature"][:60], threat.get("src_addr") or event.get("source_ip"))
    now = time.time()
    last = _recent_alerts.get(sig_key)
    if last and now - last < DEDUP_WINDOW:
        return False
    _recent_alerts[sig_key] = now
    return True


def should_alert_scan(threat: dict, event: dict) -> bool:
    """Rate-limit scan alerts — only fire if 5+ scans from same source in 60s."""
    src = threat.get("src_addr") or event.get("source_ip") or "unknown"
    now = time.time()
    _scan_events[src].append(now)
    recent = [t for t in _scan_events[src] if now - t < SCAN_WINDOW]
    return len(recent) >= SCAN_THRESHOLD


def format_alert(threat: dict, event: dict) -> str:
    """Format a Slack alert message."""
    type_labels = {
        "ips": "IPS Alert",
        "firewall": "Firewall Block",
        "auth_failure": "Brute Force Detected",
    }
    label = type_labels.get(threat["threat_type"], "Security Event")
    sig = threat["signature"][:120]
    hostname = event.get("hostname") or event.get("source_ip") or "unknown"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [f":rotating_light: *{label}*"]
    lines.append(f"  Device: {hostname}")
    lines.append(f"  Signature: {sig}")
    if threat.get("action"):
        lines.append(f"  Action: {threat['action'].title()}")
    if threat.get("src_addr"):
        lines.append(f"  Source: {threat['src_addr']}" +
                     (f":{threat['src_port']}" if threat.get("src_port") else ""))
    if threat.get("dst_addr"):
        lines.append(f"  Target: {threat['dst_addr']}" +
                     (f":{threat['dst_port']}" if threat.get("dst_port") else ""))
    if threat.get("direction"):
        lines.append(f"  Direction: {threat['direction'].title()}")
    lines.append(f"  Time: {ts}")
    return "\n".join(lines)


# ── UDP Protocol ──────────────────────────────────────────────────────────────

class SyslogProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def datagram_received(self, data: bytes, addr: tuple):
        global _msg_count
        _msg_count += 1
        event = parse_syslog(data, addr)
        if event:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop under pressure


# ── DB Writer Task ────────────────────────────────────────────────────────────

async def db_writer(queue: asyncio.Queue, pool: asyncpg.Pool):
    """Batch-insert syslog events into PostgreSQL."""
    batch = []
    last_flush = time.time()

    while not _shutdown or not queue.empty():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=BATCH_INTERVAL)
            batch.append(event)
        except asyncio.TimeoutError:
            pass

        if len(batch) >= BATCH_SIZE or (batch and time.time() - last_flush >= BATCH_INTERVAL):
            if batch:
                await _flush_batch(batch, pool)
                batch = []
                last_flush = time.time()

    if batch:
        await _flush_batch(batch, pool)


async def _flush_batch(batch: list, pool: asyncpg.Pool):
    """INSERT a batch of events."""
    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO syslog_events
                   (timestamp, hostname, facility, severity, app_name, proc_id, msg_id,
                    message, source_ip, threat_type, signature, action, direction,
                    src_addr, dst_addr, src_port, dst_port, alert_fired)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::inet,$10,$11,$12,$13,$14::inet,$15::inet,$16,$17,$18)""",
                [(
                    e.get("timestamp"),
                    e.get("hostname"),
                    e.get("facility"),
                    e.get("severity"),
                    e.get("app_name"),
                    e.get("proc_id"),
                    e.get("msg_id"),
                    e.get("message", ""),
                    e.get("source_ip"),
                    e.get("_threat", {}).get("threat_type") if e.get("_threat") else None,
                    e.get("_threat", {}).get("signature") if e.get("_threat") else None,
                    e.get("_threat", {}).get("action") if e.get("_threat") else None,
                    e.get("_threat", {}).get("direction") if e.get("_threat") else None,
                    e.get("_threat", {}).get("src_addr") if e.get("_threat") else None,
                    e.get("_threat", {}).get("dst_addr") if e.get("_threat") else None,
                    e.get("_threat", {}).get("src_port") if e.get("_threat") else None,
                    e.get("_threat", {}).get("dst_port") if e.get("_threat") else None,
                    e.get("_alert_fired", False),
                ) for e in batch]
            )
    except Exception as exc:
        log(f"DB write error: {exc}", "ERROR")


# ── Threat Detector Task ──────────────────────────────────────────────────────

async def threat_detector(queue: asyncio.Queue, db_queue: asyncio.Queue):
    """Check each event for threats and fire alerts."""
    global _threat_count

    while not _shutdown or not queue.empty():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        threat = detect_threat(event)
        if threat:
            _threat_count += 1
            event["_threat"] = threat
            severity = threat.get("severity_level", "info")

            fire_alert = False
            if severity == "critical":
                fire_alert = should_alert(threat, event)
            elif severity == "warning" and threat["threat_type"] == "scan":
                fire_alert = should_alert_scan(threat, event)
            elif severity == "warning":
                fire_alert = should_alert(threat, event)

            if fire_alert:
                event["_alert_fired"] = True
                alert_text = format_alert(threat, event)
                try:
                    nova_config.post_both(alert_text, slack_channel=nova_config.SLACK_NOTIFY)
                except Exception as exc:
                    log(f"Slack alert error: {exc}", "ERROR")

                if severity == "critical":
                    _fire_journal_alert(threat, event)

        # Forward to DB writer
        try:
            db_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _fire_journal_alert(threat: dict, event: dict):
    """Trigger a breaking journal security article."""
    try:
        trigger = f"IPS: {threat.get('signature', 'Unknown')[:80]}"
        hostname = event.get("hostname") or event.get("source_ip") or "unknown"
        details = (
            f"Threat detected on {hostname}. "
            f"Type: {threat.get('threat_type')}. "
            f"Action: {threat.get('action')}. "
            f"Source: {threat.get('src_addr') or 'unknown'}. "
            f"Direction: {threat.get('direction') or 'unknown'}."
        )
        subprocess.Popen([
            sys.executable,
            str(Path.home() / ".openclaw/scripts/nova_journal_security.py"),
            "breaking", trigger, details,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log(f"Journal alert error: {exc}", "ERROR")


# ── Retention Purge ───────────────────────────────────────────────────────────

async def retention_purge(pool: asyncpg.Pool):
    """Daily purge of old syslog events."""
    while not _shutdown:
        await asyncio.sleep(3600)
        try:
            async with pool.acquire() as conn:
                deleted_normal = await conn.execute(
                    "DELETE FROM syslog_events WHERE received_at < now() - interval '%s days' AND threat_type IS NULL" % RETENTION_DAYS
                )
                deleted_threat = await conn.execute(
                    "DELETE FROM syslog_events WHERE received_at < now() - interval '%s days' AND threat_type IS NOT NULL" % THREAT_RETENTION_DAYS
                )
                log(f"Retention purge: {deleted_normal}, {deleted_threat}")
        except Exception as exc:
            log(f"Purge error: {exc}", "ERROR")


# ── HTTP Health/Stats API ─────────────────────────────────────────────────────

async def handle_health(request):
    queue_size = request.app.get("_queue_size", 0)
    status = 200 if queue_size < 5000 else 503
    return web.json_response({
        "status": "ok" if status == 200 else "degraded",
        "version": VERSION,
        "uptime_s": int(time.time() - _start_time),
        "pid": os.getpid(),
        "queue_size": queue_size,
    }, status=status)


async def handle_stats(request):
    pool = request.app["pool"]
    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT count(*) FROM syslog_events")
            last_hour = await conn.fetchval(
                "SELECT count(*) FROM syslog_events WHERE received_at > now() - interval '1 hour'")
            threats_24h = await conn.fetchval(
                "SELECT count(*) FROM syslog_events WHERE threat_type IS NOT NULL AND received_at > now() - interval '24 hours'")
            top_sources = await conn.fetch(
                "SELECT hostname, count(*) as cnt FROM syslog_events "
                "WHERE received_at > now() - interval '24 hours' AND hostname IS NOT NULL "
                "GROUP BY hostname ORDER BY cnt DESC LIMIT 10")
            recent_threats = await conn.fetch(
                "SELECT received_at, hostname, signature, src_addr, action "
                "FROM syslog_events WHERE threat_type IS NOT NULL "
                "ORDER BY received_at DESC LIMIT 10")
    except Exception:
        total = last_hour = threats_24h = 0
        top_sources = recent_threats = []

    return web.json_response({
        "messages_total": total,
        "messages_last_hour": last_hour,
        "threats_last_24h": threats_24h,
        "threats_total_session": _threat_count,
        "messages_total_session": _msg_count,
        "top_sources": [{"hostname": r["hostname"], "count": r["cnt"]} for r in top_sources],
        "recent_threats": [
            {"ts": r["received_at"].isoformat(), "hostname": r["hostname"],
             "signature": r["signature"], "src": str(r["src_addr"]) if r["src_addr"] else None,
             "action": r["action"]}
            for r in recent_threats
        ],
    })


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _shutdown

    log(f"Nova Syslog Server v{VERSION} starting...")
    log(f"  UDP: {BIND_ADDR}:{SYSLOG_PORT}")
    log(f"  HTTP: {BIND_ADDR}:{HTTP_PORT}")
    log(f"  DB: {DB_DSN}")

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)
    log("PostgreSQL pool ready")

    # Queues
    ingest_queue = asyncio.Queue(maxsize=QUEUE_MAX)
    db_queue = asyncio.Queue(maxsize=QUEUE_MAX)

    # UDP listener
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: SyslogProtocol(ingest_queue),
        local_addr=(BIND_ADDR, SYSLOG_PORT),
    )
    log(f"UDP syslog listener active on :{SYSLOG_PORT}")

    # HTTP API
    app = web.Application()
    app["pool"] = pool
    app.router.add_get("/health", handle_health)
    app.router.add_get("/stats", handle_stats)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_ADDR, HTTP_PORT)
    await site.start()
    log(f"HTTP API active on :{HTTP_PORT}")

    # Shutdown handler
    def _signal_handler():
        global _shutdown
        _shutdown = True
        log("Shutdown signal received")

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Background tasks
    tasks = [
        asyncio.create_task(threat_detector(ingest_queue, db_queue)),
        asyncio.create_task(db_writer(db_queue, pool)),
        asyncio.create_task(retention_purge(pool)),
    ]

    # Queue size reporter for health endpoint
    async def _update_queue_size():
        while not _shutdown:
            app["_queue_size"] = ingest_queue.qsize() + db_queue.qsize()
            await asyncio.sleep(1)

    tasks.append(asyncio.create_task(_update_queue_size()))

    log("All systems go. Waiting for syslog messages...")
    nova_config.post_both(
        ":satellite: *Nova Syslog Server* online\n"
        f"  UDP :{SYSLOG_PORT} | HTTP :{HTTP_PORT}\n"
        f"  Threat detection active",
        slack_channel=nova_config.SLACK_NOTIFY,
    )

    # Run until shutdown
    while not _shutdown:
        await asyncio.sleep(1)

    # Graceful shutdown
    log("Shutting down...")
    transport.close()
    await runner.cleanup()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await pool.close()
    log("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
