#!/usr/bin/env python3
"""
nova_ingest_daemon.py — Persistent daemon that polls ingest_jobs and executes queued ingests.

Architecture:
  - Runs as a launchd-managed daemon (single instance via PID file)
  - Polls nova_ops.ingest_jobs every 60 seconds for queued work
  - Executes ONE job at a time (GPU/memory contention protection)
  - Notifies Slack on job start and completion
  - Handles SIGTERM gracefully (lets current job finish)

Port/service: N/A (background daemon, no network interface)
PID file: /tmp/nova_ingest_daemon.pid

Run: python3 nova_ingest_daemon.py
Stop: kill $(cat /tmp/nova_ingest_daemon.pid)

Written by Jordan Koch.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

# ── Configuration ────────────────────────────────────────────────────────────

PID_FILE = Path("/tmp/nova_ingest_daemon.pid")
POLL_INTERVAL = 60  # seconds
INGEST_SCRIPT = Path.home() / ".openclaw/scripts/nova_ingest.py"
DB_DSN = "postgresql://kochj@192.168.1.6:5432/nova_ops"

LOG_DIR = Path.home() / ".openclaw/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ingest-daemon] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "nova_ingest_daemon.log"),
    ],
)
log = logging.getLogger("ingest-daemon")

# ── Globals ──────────────────────────────────────────────────────────────────

shutdown_requested = False
current_process: subprocess.Popen | None = None


# ── Signal Handling ──────────────────────────────────────────────────────────

def handle_sigterm(signum, frame):
    """Graceful shutdown — let the current job finish, then exit."""
    global shutdown_requested
    log.info("SIGTERM received — will exit after current job completes")
    shutdown_requested = True


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


# ── PID File Management ──────────────────────────────────────────────────────

def acquire_pid_lock() -> bool:
    """Ensure only one instance runs. Returns False if another daemon is alive."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is alive
            os.kill(old_pid, 0)
            log.error(f"Another instance running (PID {old_pid}). Exiting.")
            return False
        except (ProcessLookupError, ValueError):
            # Stale PID file — previous instance crashed
            log.warning("Stale PID file found, removing")
            PID_FILE.unlink(missing_ok=True)
        except PermissionError:
            log.error("PID file exists and process is running (permission denied). Exiting.")
            return False

    PID_FILE.write_text(str(os.getpid()))
    return True


def release_pid_lock():
    """Remove PID file on clean shutdown."""
    PID_FILE.unlink(missing_ok=True)


# ── Slack Notifications ──────────────────────────────────────────────────────

def notify(msg: str):
    """Post to #nova-notifications."""
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log.warning(f"Slack notification failed: {e}")


# ── Job Execution ────────────────────────────────────────────────────────────

async def claim_job(pool: asyncpg.Pool) -> dict | None:
    """Fetch and claim the next queued job. Returns row dict or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE ingest_jobs
            SET status = 'running', started_at = now(), pid = $1
            WHERE id = (
                SELECT id FROM ingest_jobs
                WHERE status = 'queued'
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            os.getpid(),
        )
    return dict(row) if row else None


async def complete_job(pool: asyncpg.Pool, job_id: int, success: bool,
                       memories_stored: int = 0, error: str = None):
    """Mark a job as completed or failed."""
    status = "completed" if success else "failed"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE ingest_jobs
            SET status = $1, completed_at = now(), memories_stored = $2, error = $3
            WHERE id = $4
            """,
            status, memories_stored, error, job_id,
        )


def run_ingest(job: dict) -> tuple[bool, int, str | None]:
    """Execute nova_ingest.py as a subprocess. Returns (success, memories_stored, error)."""
    global current_process

    mode = job["mode"]
    query = job["query"]
    vector = job["vector"] or mode
    target = job["target"] or 1000

    cmd = [
        sys.executable, str(INGEST_SCRIPT),
        mode, query,
        "--source", vector,
        "--target", str(target),
    ]

    log.info(f"Executing: {' '.join(cmd)}")

    try:
        current_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        stdout, _ = current_process.communicate()
        returncode = current_process.returncode
        current_process = None

        if returncode == 0:
            # Try to extract memories_stored from output
            stored = 0
            for line in (stdout or "").splitlines():
                if "stored" in line.lower() and any(c.isdigit() for c in line):
                    import re
                    nums = re.findall(r"(\d+)\s*(?:memories|vectors|stored)", line.lower())
                    if nums:
                        stored = int(nums[0])
                        break
            return True, stored, None
        else:
            # Capture last 500 chars of output as error
            error_msg = (stdout or "")[-500:].strip() or f"Exit code {returncode}"
            return False, 0, error_msg

    except Exception as e:
        current_process = None
        return False, 0, str(e)


# ── Main Loop ────────────────────────────────────────────────────────────────

async def daemon_loop():
    """Main polling loop."""
    log.info("Connecting to database...")
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    log.info("Ingest daemon started — polling every %ds", POLL_INTERVAL)
    notify(":gear: Ingest daemon started")

    try:
        while not shutdown_requested:
            job = await claim_job(pool)

            if job:
                job_id = job["id"]
                mode = job["mode"]
                query = job["query"]
                requester = job.get("requested_by", "unknown")

                log.info(f"Job #{job_id}: mode={mode} query='{query}' requested_by={requester}")
                notify(f":arrow_forward: Ingest job #{job_id} started: `{mode}` — _{query}_")

                # Run synchronously (one at a time)
                success, stored, error = await asyncio.to_thread(run_ingest, job)

                await complete_job(pool, job_id, success, stored, error)

                if success:
                    log.info(f"Job #{job_id} completed: {stored} memories stored")
                    notify(f":white_check_mark: Ingest job #{job_id} completed: {stored} memories stored")
                else:
                    log.error(f"Job #{job_id} failed: {error}")
                    notify(f":x: Ingest job #{job_id} failed: {error[:200] if error else 'unknown error'}")
            else:
                # No work — sleep until next poll (interruptible)
                for _ in range(POLL_INTERVAL):
                    if shutdown_requested:
                        break
                    await asyncio.sleep(1)

    finally:
        await pool.close()
        log.info("Daemon shutting down cleanly")
        notify(":stop_sign: Ingest daemon stopped")


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    if not acquire_pid_lock():
        sys.exit(1)

    try:
        asyncio.run(daemon_loop())
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        release_pid_lock()


if __name__ == "__main__":
    main()
