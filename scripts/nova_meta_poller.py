#!/opt/homebrew/bin/python3
"""
nova_meta_poller.py — Daemon that collects Nova system metrics every 5 minutes
and inserts them into telemetry.nova_meta.

Metrics collected:
  memories_total, memories_today, ingest_rate_per_hour, api_cost_today,
  agent_runs_today, article_count_today, disk_used_gb, ollama_vram_gb,
  gateway_latency_ms, pg_size_gb, vector_count_by_source

Written by Jordan Koch.
"""

import sys
import os
import json
import time
import logging
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_config  # noqa: E402

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH = Path(str(Path.home()) + "/.openclaw/logs/meta_poller.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meta_poller")

# ── Constants ────────────────────────────────────────────────────────────────

POLL_INTERVAL = 300  # 5 minutes
MEMORY_STATS_URL = f"http://{nova_config.LAN_IP}:18790/stats"
OLLAMA_PS_URL = "http://127.0.0.1:11434/api/ps"
GATEWAY_HEALTH_URL = "http://127.0.0.1:18792/health"
API_COSTS_JSON = Path(str(Path.home()) + "/.openclaw/workspace/state/api_costs.json")
JOURNAL_CONTENT_DIR = Path("/Volumes/Data/xcode/nova-journal/content/")

# Disk mount points to monitor
DISK_MOUNTS = ["/", "/Volumes/Data", "/Volumes/MoreData"]


# ── Database helpers ─────────────────────────────────────────────────────────

def get_conn(dbname: str):
    """Get a psycopg2 connection to the specified database."""
    import psycopg2
    return psycopg2.connect(host="localhost", dbname=dbname, user="kochj")


def insert_metric(conn, metric: str, value: float, metadata: dict = None):
    """Insert a single metric row into telemetry.nova_meta."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry.nova_meta (ts, metric, value, metadata) VALUES (NOW(), %s, %s, %s)",
            (metric, value, json.dumps(metadata) if metadata else None),
        )
    conn.commit()


# ── Metric collectors ────────────────────────────────────────────────────────

def collect_memories_total() -> tuple[float, dict | None]:
    """GET http://192.168.1.6:18790/stats -> .count"""
    try:
        req = urllib.request.Request(MEMORY_STATS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return float(data.get("count", 0)), None
    except Exception as e:
        log.warning(f"memories_total failed: {e}")
        return None, None


def collect_memories_today() -> tuple[float, dict | None]:
    """Count memories created today from nova_memories."""
    try:
        conn = get_conn("nova_memories")
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM memories WHERE created_at >= CURRENT_DATE")
            count = cur.fetchone()[0]
        conn.close()
        return float(count), None
    except Exception as e:
        log.warning(f"memories_today failed: {e}")
        return None, None


def collect_ingest_rate_per_hour() -> tuple[float, dict | None]:
    """Count memories ingested in the last hour from nova_memories."""
    try:
        conn = get_conn("nova_memories")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM memories WHERE created_at >= NOW() - INTERVAL '1 hour'"
            )
            count = cur.fetchone()[0]
        conn.close()
        return float(count), None
    except Exception as e:
        log.warning(f"ingest_rate_per_hour failed: {e}")
        return None, None


def collect_api_cost_today() -> tuple[float, dict | None]:
    """Read api_costs.json for today's cost total."""
    try:
        if not API_COSTS_JSON.exists():
            return 0.0, {"note": "api_costs.json not found"}
        data = json.loads(API_COSTS_JSON.read_text())
        today_str = date.today().isoformat()
        # Try common structures: {"YYYY-MM-DD": cost} or {"daily": {"YYYY-MM-DD": cost}}
        if isinstance(data, dict):
            if today_str in data:
                return float(data[today_str]), None
            if "daily" in data and today_str in data["daily"]:
                return float(data["daily"][today_str]), None
            if "today" in data:
                return float(data["today"]), None
            if "total_today" in data:
                return float(data["total_today"]), None
        return 0.0, {"note": "no today entry found"}
    except Exception as e:
        log.warning(f"api_cost_today failed: {e}")
        return None, None


def collect_agent_runs_today() -> tuple[float, dict | None]:
    """Count claude_sessions created today from nova_ops."""
    try:
        conn = get_conn("nova_ops")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM claude_sessions WHERE created_at >= CURRENT_DATE"
            )
            count = cur.fetchone()[0]
        conn.close()
        return float(count), None
    except Exception as e:
        log.warning(f"agent_runs_today failed: {e}")
        return None, None


def collect_article_count_today() -> tuple[float, dict | None]:
    """Count files in nova-journal/content/ modified today."""
    try:
        if not JOURNAL_CONTENT_DIR.exists():
            return 0.0, {"note": "journal content dir not found"}
        today_start = datetime.combine(date.today(), datetime.min.time())
        count = 0
        for f in JOURNAL_CONTENT_DIR.rglob("*"):
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) >= today_start:
                count += 1
        return float(count), None
    except Exception as e:
        log.warning(f"article_count_today failed: {e}")
        return None, None


def collect_disk_used_gb() -> tuple[float, dict | None]:
    """Get disk usage for /, /Volumes/Data, /Volumes/MoreData."""
    metadata = {}
    total_used = 0.0
    for mount in DISK_MOUNTS:
        try:
            result = subprocess.run(
                ["df", "-g", mount], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    # df -g: Filesystem 1G-blocks Used Available Capacity Mounted
                    used_gb = float(parts[2])
                    metadata[mount] = used_gb
                    total_used += used_gb
        except Exception as e:
            log.warning(f"disk_used_gb failed for {mount}: {e}")
    return total_used, metadata if metadata else None


def collect_ollama_vram_gb() -> tuple[float, dict | None]:
    """GET http://127.0.0.1:11434/api/ps -> sum of size_vram fields."""
    try:
        req = urllib.request.Request(OLLAMA_PS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = data.get("models", [])
            total_vram = 0
            model_info = {}
            for m in models:
                vram = m.get("size_vram", 0)
                total_vram += vram
                model_info[m.get("name", "unknown")] = round(vram / (1024**3), 2)
            return round(total_vram / (1024**3), 2), model_info if model_info else None
    except Exception as e:
        log.warning(f"ollama_vram_gb failed: {e}")
        return None, None


def collect_gateway_latency_ms() -> tuple[float, dict | None]:
    """Time a request to the gateway health endpoint."""
    try:
        start = time.time()
        req = urllib.request.Request(GATEWAY_HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        elapsed_ms = (time.time() - start) * 1000
        return round(elapsed_ms, 2), None
    except Exception as e:
        log.warning(f"gateway_latency_ms failed: {e}")
        return None, None


def collect_pg_size_gb() -> tuple[float, dict | None]:
    """Get pg_database_size for nova_ops and nova_memories."""
    metadata = {}
    total_gb = 0.0
    for dbname in ("nova_ops", "nova_memories"):
        try:
            conn = get_conn(dbname)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_database_size(current_database()) / (1024.0^3)"
                )
                size_gb = float(cur.fetchone()[0])
                metadata[dbname] = round(size_gb, 3)
                total_gb += size_gb
            conn.close()
        except Exception as e:
            log.warning(f"pg_size_gb failed for {dbname}: {e}")
    return round(total_gb, 3), metadata if metadata else None


def collect_vector_count_by_source() -> tuple[float, dict | None]:
    """Top 10 sources by count from nova_memories."""
    try:
        conn = get_conn("nova_memories")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source, COUNT(*) as cnt
                FROM memories
                GROUP BY source
                ORDER BY cnt DESC
                LIMIT 10
            """)
            rows = cur.fetchall()
        conn.close()
        total = sum(r[1] for r in rows)
        sources = {r[0]: r[1] for r in rows}
        return float(total), sources
    except Exception as e:
        log.warning(f"vector_count_by_source failed: {e}")
        return None, None


# ── Main loop ────────────────────────────────────────────────────────────────

COLLECTORS = [
    ("memories_total", collect_memories_total),
    ("memories_today", collect_memories_today),
    ("ingest_rate_per_hour", collect_ingest_rate_per_hour),
    ("api_cost_today", collect_api_cost_today),
    ("agent_runs_today", collect_agent_runs_today),
    ("article_count_today", collect_article_count_today),
    ("disk_used_gb", collect_disk_used_gb),
    ("ollama_vram_gb", collect_ollama_vram_gb),
    ("gateway_latency_ms", collect_gateway_latency_ms),
    ("pg_size_gb", collect_pg_size_gb),
    ("vector_count_by_source", collect_vector_count_by_source),
]


def poll_once():
    """Run all collectors and insert metrics."""
    import psycopg2

    try:
        conn = get_conn("nova_ops")
    except Exception as e:
        log.error(f"Cannot connect to nova_ops for writing: {e}")
        return

    collected = 0
    for metric_name, collector_fn in COLLECTORS:
        try:
            value, metadata = collector_fn()
            if value is not None:
                insert_metric(conn, metric_name, value, metadata)
                collected += 1
        except Exception as e:
            log.error(f"Error collecting {metric_name}: {e}")

    conn.close()
    log.info(f"Poll complete: {collected}/{len(COLLECTORS)} metrics collected")


def main():
    log.info("nova_meta_poller starting (interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            poll_once()
        except Exception as e:
            log.error(f"Unhandled error in poll_once: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
