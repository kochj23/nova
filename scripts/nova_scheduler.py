#!/usr/bin/env python3
"""
nova_scheduler.py — Unified task scheduler for Nova.

One persistent daemon replaces 30+ fragile launchd StartInterval/Calendar
jobs. Survives sleep/wake by using wall-clock comparison, not timers.

Features:
  - Interval and cron scheduling with timezone support
  - Overlap prevention (skip/queue per task)
  - Concurrent subprocess execution (max 6)
  - Sleep/wake detection and recovery
  - Self-healing: overdue tasks are force-run
  - Slack alerts on failures, hourly heartbeat
  - HTTP status API on port 37460
  - State persistence across restarts
  - Graceful shutdown with SIGTERM handling

Written by Jordan Koch.
"""

import asyncio
import json
import os
import re
import signal
import sys
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN, LOG_DEBUG

CONFIG_PATH = Path.home() / ".openclaw/config/scheduler.yaml"
STATE_PATH = Path.home() / ".openclaw/config/scheduler_state.json"
HEARTBEAT_FILE = Path.home() / ".openclaw/config/scheduler_heartbeat"
SCRIPTS_DIR = Path.home() / ".openclaw/scripts"


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class TaskState:
    last_run: float = 0
    last_duration: float = 0
    last_exit_code: int = 0
    last_error: str = ""
    next_run: float = 0
    consecutive_failures: int = 0
    running: bool = False
    pid: int = 0
    run_count: int = 0


@dataclass
class Task:
    id: str
    script: str
    schedule: str
    timeout: int = 300
    overlap: str = "skip"
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    enabled: bool = True
    state: TaskState = field(default_factory=TaskState)

    # Parsed schedule
    _interval_s: float = 0
    _cron_expr: str = ""


# ── Schedule parsing ─────────────────────────────────────────────────────────

def parse_interval(s):
    """Parse 'every 5m' / 'every 4h' / 'every 30s' to seconds."""
    m = re.match(r"every\s+(\d+)\s*([smh])", s.strip())
    if not m:
        return 0
    val = int(m.group(1))
    unit = m.group(2)
    return val * {"s": 1, "m": 60, "h": 3600}[unit]


def parse_cron(s):
    """Extract cron expression from 'cron 0 23 * * *'."""
    m = re.match(r"cron\s+(.+)", s.strip())
    return m.group(1).strip() if m else ""


def next_cron_time(expr, after, tz_name="America/Los_Angeles"):
    """Compute next matching time for a 5-field cron expression."""
    tz = ZoneInfo(tz_name)
    fields = expr.split()
    if len(fields) != 5:
        return after + 3600

    def matches(field_str, value, max_val):
        if field_str == "*":
            return True
        for part in field_str.split(","):
            if "/" in part:
                base, step = part.split("/")
                start = 0 if base == "*" else int(base)
                if (value - start) % int(step) == 0 and value >= start:
                    return True
            elif "-" in part:
                lo, hi = map(int, part.split("-"))
                if lo <= value <= hi:
                    return True
            else:
                if int(part) == value:
                    return True
        return False

    dt = datetime.fromtimestamp(after, tz=tz) + timedelta(minutes=1)
    dt = dt.replace(second=0, microsecond=0)

    for _ in range(525600):  # max 1 year of minutes
        if (matches(fields[0], dt.minute, 59) and
            matches(fields[1], dt.hour, 23) and
            matches(fields[2], dt.day, 31) and
            matches(fields[3], dt.month, 12) and
            matches(fields[4], dt.weekday(), 6)):
            return dt.timestamp()
        dt += timedelta(minutes=1)

    return after + 86400  # fallback: 24h


# ── Config loading ───────────────────────────────────────────────────────────

def load_config():
    """Load scheduler config from YAML."""
    try:
        import yaml
    except ImportError:
        log("pyyaml not installed — run: pip3 install pyyaml", level=LOG_ERROR, source="scheduler")
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)

    sched_cfg = raw.get("scheduler", {})
    slack_cfg = raw.get("slack", {})
    tasks = {}

    for task_id, tcfg in raw.get("tasks", {}).items():
        t = Task(
            id=task_id,
            script=tcfg.get("script", ""),
            schedule=tcfg.get("schedule", ""),
            timeout=tcfg.get("timeout", 300),
            overlap=tcfg.get("overlap", "skip"),
            args=tcfg.get("args", []),
            env=tcfg.get("env", {}),
            enabled=tcfg.get("enabled", True),
        )
        # Parse schedule
        if t.schedule.startswith("every"):
            t._interval_s = parse_interval(t.schedule)
        elif t.schedule.startswith("cron"):
            t._cron_expr = parse_cron(t.schedule)
        tasks[task_id] = t

    return sched_cfg, slack_cfg, tasks


# ── Scheduler ────────────────────────────────────────────────────────────────

class NovaScheduler:
    def __init__(self):
        self.sched_cfg = {}
        self.slack_cfg = {}
        self.tasks: dict[str, Task] = {}
        self._running = False
        self._last_tick = 0
        self._last_heartbeat = 0
        self._last_state_save = 0
        self._running_count = 0
        self._start_time = 0
        self._total_runs = 0
        self._total_failures = 0

    def load(self):
        self.sched_cfg, self.slack_cfg, self.tasks = load_config()
        self._load_state()
        self._recalculate_next_runs()
        log(f"Loaded {len(self.tasks)} tasks from config", level=LOG_INFO, source="scheduler")

    def _load_state(self):
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text())
                for tid, sdata in data.get("tasks", {}).items():
                    if tid in self.tasks:
                        s = self.tasks[tid].state
                        s.last_run = sdata.get("last_run", 0)
                        s.last_duration = sdata.get("last_duration", 0)
                        s.last_exit_code = sdata.get("last_exit_code", 0)
                        s.consecutive_failures = sdata.get("consecutive_failures", 0)
                        s.run_count = sdata.get("run_count", 0)
            except Exception:
                pass

    def _save_state(self):
        data = {"saved_at": time.time(), "tasks": {}}
        for tid, task in self.tasks.items():
            data["tasks"][tid] = {
                "last_run": task.state.last_run,
                "last_duration": task.state.last_duration,
                "last_exit_code": task.state.last_exit_code,
                "consecutive_failures": task.state.consecutive_failures,
                "run_count": task.state.run_count,
                "next_run": task.state.next_run,
            }
        STATE_PATH.write_text(json.dumps(data, indent=2))
        HEARTBEAT_FILE.write_text(str(time.time()))

    def _recalculate_next_runs(self):
        now = time.time()
        tz = self.sched_cfg.get("tz", "America/Los_Angeles")
        for task in self.tasks.values():
            if task._interval_s:
                if task.state.last_run:
                    task.state.next_run = task.state.last_run + task._interval_s
                    if task.state.next_run < now:
                        task.state.next_run = now  # overdue, run immediately
                else:
                    task.state.next_run = now + 5  # first run in 5s
            elif task._cron_expr:
                ref = max(task.state.last_run, now - 86400)
                task.state.next_run = next_cron_time(task._cron_expr, ref, tz)

    def _advance_next_run(self, task):
        now = time.time()
        tz = self.sched_cfg.get("tz", "America/Los_Angeles")
        if task._interval_s:
            task.state.next_run = now + task._interval_s
        elif task._cron_expr:
            task.state.next_run = next_cron_time(task._cron_expr, now, tz)

    # ── Execution ────────────────────────────────────────────────────────

    async def execute_task(self, task: Task):
        task.state.running = True
        self._running_count += 1
        start = time.time()

        script_path = SCRIPTS_DIR / task.script
        if task.script.endswith(".py"):
            cmd = [self.sched_cfg.get("python", "/opt/homebrew/bin/python3"), str(script_path)] + task.args
        elif task.script.endswith(".sh"):
            cmd = [self.sched_cfg.get("shell", "/bin/zsh"), str(script_path)] + task.args
        else:
            cmd = [str(script_path)] + task.args

        env = {**os.environ}
        env["PATH"] = self.sched_cfg.get("env", {}).get("PATH", env.get("PATH", ""))
        env["PYTHONPATH"] = self.sched_cfg.get("env", {}).get("PYTHONPATH", str(SCRIPTS_DIR))
        env.update(task.env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=str(SCRIPTS_DIR),
            )
            task.state.pid = proc.pid

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=task.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(f"Timed out after {task.timeout}s")

            task.state.last_exit_code = proc.returncode
            task.state.last_duration = time.time() - start

            if proc.returncode != 0:
                task.state.consecutive_failures += 1
                task.state.last_error = (stderr.decode(errors="replace"))[-500:]
                log(f"FAIL {task.id} exit={proc.returncode} ({task.state.last_duration:.1f}s)",
                    level=LOG_ERROR, source="scheduler")
                self._total_failures += 1
                if task.state.consecutive_failures >= 3:
                    await self._slack_alert(f":x: *{task.id}* — {task.state.consecutive_failures} consecutive failures. "
                                           f"Last error: {task.state.last_error[:200]}")
            else:
                if task.state.consecutive_failures > 0:
                    log(f"RECOVERED {task.id} after {task.state.consecutive_failures} failures",
                        level=LOG_INFO, source="scheduler")
                task.state.consecutive_failures = 0
                task.state.last_error = ""

        except Exception as e:
            task.state.consecutive_failures += 1
            task.state.last_error = str(e)
            task.state.last_duration = time.time() - start
            self._total_failures += 1
            log(f"ERROR {task.id}: {e}", level=LOG_ERROR, source="scheduler")
            if task.state.consecutive_failures >= 3:
                await self._slack_alert(f":x: *{task.id}* — {e}")

        finally:
            task.state.running = False
            task.state.pid = 0
            task.state.last_run = start
            task.state.run_count += 1
            self._running_count -= 1
            self._total_runs += 1
            self._advance_next_run(task)

    # ── Slack ────────────────────────────────────────────────────────────

    async def _slack_post(self, text):
        token = nova_config.slack_bot_token()
        if not token:
            return
        channel = self.slack_cfg.get("channel", nova_config.SLACK_NOTIFY)
        try:
            payload = json.dumps({"channel": channel, "text": text}).encode()
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage", data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
            )
            await asyncio.get_event_loop().run_in_executor(None, urllib.request.urlopen, req)
        except Exception:
            pass

    async def _slack_alert(self, text):
        if self.slack_cfg.get("alerts", True):
            await self._slack_post(text)

    async def _heartbeat(self):
        healthy = sum(1 for t in self.tasks.values() if t.state.consecutive_failures == 0 and t.enabled)
        total = sum(1 for t in self.tasks.values() if t.enabled)
        running = sum(1 for t in self.tasks.values() if t.state.running)
        uptime_h = (time.time() - self._start_time) / 3600

        failing = [t for t in self.tasks.values() if t.state.consecutive_failures >= 3 and t.enabled]
        fail_str = ""
        if failing:
            fail_str = "\n  :x: Failing: " + ", ".join(t.id for t in failing)

        await self._slack_post(
            f":heartbeat: *Scheduler Heartbeat*\n"
            f"  Tasks: {healthy}/{total} healthy, {running} running\n"
            f"  Runs: {self._total_runs} total, {self._total_failures} failures\n"
            f"  Uptime: {uptime_h:.1f}h"
            f"{fail_str}"
        )

    # ── HTTP Status API ──────────────────────────────────────────────────

    async def _handle_http(self, reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            request = data.decode(errors="replace")
            path = request.split(" ")[1] if " " in request else "/"

            if path == "/health":
                body = b'{"ok":true}'
            elif path == "/status":
                body = json.dumps({
                    "status": "running",
                    "uptime_s": int(time.time() - self._start_time),
                    "tasks_total": len(self.tasks),
                    "tasks_running": self._running_count,
                    "total_runs": self._total_runs,
                    "total_failures": self._total_failures,
                }).encode()
            elif path == "/tasks":
                tasks_data = {}
                for tid, t in self.tasks.items():
                    tasks_data[tid] = {
                        "script": t.script,
                        "schedule": t.schedule,
                        "enabled": t.enabled,
                        "running": t.state.running,
                        "last_run": t.state.last_run,
                        "next_run": t.state.next_run,
                        "last_duration": round(t.state.last_duration, 1),
                        "last_exit_code": t.state.last_exit_code,
                        "consecutive_failures": t.state.consecutive_failures,
                        "run_count": t.state.run_count,
                    }
                body = json.dumps(tasks_data, indent=2).encode()
            elif path.startswith("/run/"):
                task_id = path[5:]
                if task_id in self.tasks:
                    asyncio.create_task(self.execute_task(self.tasks[task_id]))
                    body = json.dumps({"queued": task_id}).encode()
                else:
                    body = json.dumps({"error": "unknown task"}).encode()
            else:
                body = b'{"error":"not found"}'

            resp = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    # ── Main loop ────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        self._start_time = time.time()
        self._last_tick = time.time()
        self._last_heartbeat = time.time()
        self._last_state_save = time.time()

        # Signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown)

        # Start HTTP status server
        port = self.sched_cfg.get("status_port", 37460)
        try:
            server = await asyncio.start_server(self._handle_http, "127.0.0.1", port)
            log(f"Status API on port {port}", level=LOG_INFO, source="scheduler")
        except Exception as e:
            log(f"Status API failed to start: {e}", level=LOG_WARN, source="scheduler")

        interval_count = sum(1 for t in self.tasks.values() if t._interval_s)
        cron_count = sum(1 for t in self.tasks.values() if t._cron_expr)
        log(f"Scheduler started: {len(self.tasks)} tasks ({interval_count} interval, {cron_count} cron)",
            level=LOG_INFO, source="scheduler")

        if self.slack_cfg.get("startup", True):
            await self._slack_post(
                f":rocket: *Nova Scheduler Started*\n"
                f"  Tasks: {len(self.tasks)} ({interval_count} interval, {cron_count} cron)\n"
                f"  Max concurrent: {self.sched_cfg.get('max_concurrent', 6)}\n"
                f"  Status: http://127.0.0.1:{port}/tasks"
            )

        tick = self.sched_cfg.get("tick_interval", 1)
        max_conc = self.sched_cfg.get("max_concurrent", 6)
        hb_interval = self.sched_cfg.get("heartbeat_interval", 3600)

        while self._running:
            now = time.time()

            # Detect sleep/wake (time jump)
            if now - self._last_tick > tick * 30:
                gap = now - self._last_tick
                log(f"Time jump: {gap:.0f}s gap — likely wake from sleep", level=LOG_WARN, source="scheduler")
                self._recalculate_next_runs()
                await self._slack_post(f":sunrise: *Scheduler resumed* — {gap/60:.0f}m gap, recalculating timers")
            self._last_tick = now

            # Find due tasks
            for task in self.tasks.values():
                if not task.enabled or task.state.next_run > now:
                    continue

                if task.state.running:
                    if task.overlap == "skip":
                        self._advance_next_run(task)
                    continue

                if self._running_count >= max_conc:
                    break

                asyncio.create_task(self.execute_task(task))

            # Heartbeat
            if now - self._last_heartbeat > hb_interval:
                await self._heartbeat()
                self._last_heartbeat = now

            # Save state
            if now - self._last_state_save > 60:
                self._save_state()
                self._last_state_save = now

            await asyncio.sleep(tick)

        # Shutdown
        log("Scheduler shutting down", level=LOG_INFO, source="scheduler")
        self._save_state()
        if self.slack_cfg.get("startup", True):
            await self._slack_post(":octagonal_sign: *Nova Scheduler stopped*")

    def _shutdown(self):
        log("Received shutdown signal", level=LOG_INFO, source="scheduler")
        self._running = False


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    scheduler = NovaScheduler()
    scheduler.load()
    asyncio.run(scheduler.run())


if __name__ == "__main__":
    main()
