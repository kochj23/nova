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

import nova_dsn as _nova_dsn  # noqa: E402
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_ops_writer
from nova_notify import notify
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN, LOG_DEBUG

# Paths are env-overridable so a second scheduler (e.g. nova-scheduler-core on the
# Linux cluster) can run its own task set/state without clobbering .6's. Defaults
# preserve the original single-scheduler behavior on the Mac Studio.
CONFIG_PATH = Path(os.environ.get("NOVA_SCHED_CONFIG") or Path.home() / ".openclaw/config/scheduler.yaml")
STATE_PATH = Path(os.environ.get("NOVA_SCHED_STATE") or Path.home() / ".openclaw/config/scheduler_state.json")
HEARTBEAT_FILE = Path(os.environ.get("NOVA_SCHED_HEARTBEAT") or Path.home() / ".openclaw/config/scheduler_heartbeat")
SCRIPTS_DIR = Path(os.environ.get("NOVA_SCHED_SCRIPTS") or Path.home() / ".openclaw/scripts")

# Host this scheduler runs on — surfaced in every task-failure alert title.
SCHED_HOST = getattr(nova_config, "NOVA_HOST", "192.168.1.6")


def _should_alert_failure(n: int) -> bool:
    """Exponential-backoff cadence for repeated task failures: alert at
    consecutive-failure counts 1, 2, 4, 8, 16, 32, 64, 128, ... and never in
    between. Replaces the old fixed (3,10,50,100) set, which spammed at each
    fixed step; powers of two thin the cadence out automatically the longer a
    task stays broken, so a persistently dead task pages ever less often."""
    return n >= 1 and (n & (n - 1)) == 0


# Alert-flood controls (2026-09-13): at most one failure alert per task per
# hour, and after QUARANTINE_THRESHOLD consecutive failures the task is
# dead-lettered — unscheduled until scheduler restart or a config reload that
# touches it — with a single clear alert instead of an endless failure stream.
ALERT_MIN_INTERVAL_S = 3600
QUARANTINE_THRESHOLD = 10


# ── Catch-up re-runs (#26, 2026-10-15) ──────────────────────────────────────
# A cron task that fires less than daily and whose latest scheduled run died on an
# ENVIRONMENT error (missing module, missing interpreter, NAS path gone, PG refusing
# connections) otherwise waits a whole week/month/quarter for its next chance — on
# 2026-10-11/12 the Studio's python lost psycopg2 and every weekly organ missed its slot.
# Such a slot is re-run once the environment probes healthy again: at most `max_attempts`
# per slot, `min_spacing_s` apart, recorded with was_retry=true / trigger='catch_up'.
# Config: scheduler.catch_up: {enabled, max_attempts, min_spacing_s, min_period_s}.
CATCH_UP_DEFAULTS = {"enabled": True, "max_attempts": 3, "min_spacing_s": 1800,
                     "min_period_s": 30 * 3600, "max_per_check": 3}
CATCH_UP_CHECK_EVERY_S = 60

_ENV_PATTERNS = (
    ("module", re.compile(r"ModuleNotFoundError|No module named|missing dependency", re.I)),
    ("import", re.compile(r"\bImportError\b")),
    ("interpreter", re.compile(r"No such file or directory: '[^']*/(?:python[\d.]*|zsh|bash|sh)'")),
    ("pg", re.compile(r"(?:connection to server|could not connect to server|Connect call failed)"
                      r"[^\n]*(?:Connection refused|\b5432\b|\b5434\b|\b6432\b)"
                      r"|Connection refused[^\n]*(?:5432|5434|6432)", re.I)),
    ("path", re.compile(r"can't open file|No such file or directory|Permission denied|"
                        r"Operation not permitted|Stale (?:NFS )?file handle|"
                        r"Transport endpoint is not connected", re.I)),
)
_MODULE_RE = re.compile(r"No module named '([A-Za-z_][\w.]*)'")
_QUOTED_PATH_RE = re.compile(r"'(/[^'\n]+)'")
_cron_period_cache: dict = {}


def classify_env_error(text: str) -> str:
    """Environment-error class of a failure's error tail ('' = not an environment error).
    Only these are worth a catch-up: the script itself is fine, the host around it was not."""
    for kind, pat in _ENV_PATTERNS:
        if pat.search(text or ""):
            return kind
    return ""


def cron_period_s(expr: str, tz_name: str = "America/Los_Angeles") -> float:
    """Shortest gap between consecutive fires of a 5-field cron expression (cached).
    Expressions with day-of-month, month and day-of-week all '*' fire at least daily, so
    they short-circuit without walking the calendar."""
    f = expr.split()
    if len(f) != 5:
        return 0.0
    if f[2] == "*" and f[3] == "*" and f[4] == "*":
        return 0.0  # fires every day (or more often) — never a catch-up candidate
    key = (expr, tz_name)
    if key not in _cron_period_cache:
        t = next_cron_time(expr, time.time(), tz_name)
        gaps = []
        for _ in range(4):
            n = next_cron_time(expr, t, tz_name)
            gaps.append(n - t)
            t = n
        _cron_period_cache[key] = min(gaps)
    return _cron_period_cache[key]


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
    _retry_pending: bool = False
    # Catch-up (#26): the missed cron slot whose run failed on an ENVIRONMENT error, how many
    # catch-up attempts it has had, and when the last one started. Persisted in the state file.
    catch_up_slot: float = 0
    catch_up_attempts: int = 0
    catch_up_last: float = 0
    catch_up_kind: str = ""
    catch_up_error: str = ""  # error tail that classified the slot (the probe reads it)
    _slot: float = 0  # scheduled slot of the run in flight (a 60s retry inherits it)


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
    group: str = ""
    gpu_heavy: bool = False
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
        # cron day-of-week is Sunday=0..Saturday=6; Python's dt.weekday() is Monday=0..Sunday=6.
        # Using dt.weekday() directly against a cron field shifts every weekday-constrained job
        # one day early (a "* * 0" meant for Sunday actually fires Monday) -- harmless for tasks
        # that just generate whatever's due, but silently kills any task with its own internal
        # day-gate expecting the day cron claims to run it on (e.g. nova_meta_analysis.py's
        # "first Sunday of the month" check, which the scheduler was invoking on Mondays).
        cron_dow = (dt.weekday() + 1) % 7
        if (matches(fields[0], dt.minute, 59) and
            matches(fields[1], dt.hour, 23) and
            matches(fields[2], dt.day, 31) and
            matches(fields[3], dt.month, 12) and
            matches(fields[4], cron_dow, 6)):
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
            group=tcfg.get("group", ""),
            gpu_heavy=tcfg.get("gpu_heavy", False),
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
        self._prev_heartbeat_state = None  # (healthy, total, failing_ids) from last heartbeat
        self._last_forced_heartbeat = 0    # timestamp of last proof-of-life heartbeat
        self._last_task_alert: dict = {}   # task_id -> ts of last failure alert (1h rate limit)
        self._quarantined: set = set()     # dead-lettered task ids (in-memory; cleared on restart)
        self._last_catch_up_check = 0.0

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
                        s._retry_pending = sdata.get("_retry_pending", False)
                        s.catch_up_slot = sdata.get("catch_up_slot", 0)
                        s.catch_up_attempts = sdata.get("catch_up_attempts", 0)
                        s.catch_up_last = sdata.get("catch_up_last", 0)
                        s.catch_up_kind = sdata.get("catch_up_kind", "")
                        s.catch_up_error = sdata.get("catch_up_error", "")
            except Exception:
                pass
        self._adopt_shared_last_runs()

    def _adopt_shared_last_runs(self):
        """Under leader election (nova_leader_wrapper exports NOVA_LEADER_NAME) the leader and the standby run
        the SAME task set but keep separate state files. On takeover, adopt the newest run of each task from
        scheduler_runs: last_run = max(local, shared) and, when that shared run succeeded, consecutive_failures=0.
        Without this (2026-10-18 and 10-21): the standby took over with a days-old state file, fired every
        interval task at once (a burst of ~100 sudo/TPM secret decrypts -> UniFi key lookups timed out), and its
        stale failure counts quarantined tasks on their first failure ("12 consecutive failures"); the handback
        to nova-core re-ran journal_essay's 09:00 slot the standby had already published. Best-effort."""
        if not os.environ.get("NOVA_LEADER_NAME") or not self.tasks:
            return
        try:
            import psycopg2
            conn = psycopg2.connect(_nova_dsn.pg_dsn("nova_ops", "connect_timeout=8"))
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT DISTINCT ON (task_id) task_id, task_script, started_at, status "
                                "FROM scheduler_runs WHERE task_id = ANY(%s) AND status <> 'running' "
                                "AND started_at > (extract(epoch FROM now()) - 8*86400) * 1000 "
                                "ORDER BY task_id, started_at DESC", (list(self.tasks),))
                    rows = cur.fetchall()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log(f"shared last_run adoption skipped: {e}", level=LOG_WARN, source="scheduler")
            return
        adopted = 0
        for tid, script, started_ms, status in rows:
            t = self.tasks[tid]
            if script == t.script and started_ms and started_ms / 1000 > t.state.last_run:
                t.state.last_run = started_ms / 1000
                if status == "success":
                    t.state.consecutive_failures = 0
                    t.state._retry_pending = False
                adopted += 1
        if adopted:
            log(f"adopted shared last_run for {adopted} task(s) from scheduler_runs", level=LOG_INFO, source="scheduler")

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
                "_retry_pending": task.state._retry_pending,
                "catch_up_slot": task.state.catch_up_slot,
                "catch_up_attempts": task.state.catch_up_attempts,
                "catch_up_last": task.state.catch_up_last,
                "catch_up_kind": task.state.catch_up_kind,
                "catch_up_error": task.state.catch_up_error,
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
                # A task that has run before may catch a slot missed in the last 24h; a NEW task waits for its
                # first real slot (2026-10-15: a never-run nightly job fired at registration, 4h early).
                ref = max(task.state.last_run, now - 86400) if task.state.last_run else now
                task.state.next_run = next_cron_time(task._cron_expr, ref, tz)

    def _advance_next_run(self, task):
        now = time.time()
        tz = self.sched_cfg.get("tz", "America/Los_Angeles")
        if task._interval_s:
            task.state.next_run = now + task._interval_s
        elif task._cron_expr:
            task.state.next_run = next_cron_time(task._cron_expr, now, tz)

    # ── Sleep-aware scheduling ──────────────────────────────────────────

    _sleep_observation_date: str = ""  # tracks once-per-day observation

    def _get_sleep_metrics(self):
        """Check last night's sleep from health data."""
        try:
            health_dir = Path.home() / ".openclaw/private/health"
            # Try today's file first, then latest
            today_file = health_dir / f"{datetime.now().strftime('%Y-%m-%d')}.json"
            latest_file = health_dir / "latest.json"
            target = today_file if today_file.exists() else latest_file
            if not target.exists():
                return {"hours_slept": 7}
            data = json.loads(target.read_text())
            sleep_hours = data.get("sleep", data.get("sleep_hours", 7))
            if isinstance(sleep_hours, list):
                sleep_hours = sleep_hours[0] if sleep_hours else 7
            return {"hours_slept": float(sleep_hours), "date": data.get("date", "")}
        except Exception:
            return {"hours_slept": 7}

    # ── Execution ────────────────────────────────────────────────────────

    async def execute_task(self, task: Task, trigger: str = "schedule"):
        """trigger: 'schedule' (normal slot or its 60s retry), 'catch_up' (#26) or 'manual' (/run)."""
        task.state.running = True
        self._running_count += 1
        start = time.time()
        run_id = str(uuid.uuid4())
        catch_up = trigger == "catch_up"
        if catch_up:
            scheduled_at_ms = int(task.state.catch_up_slot * 1000)  # the missed slot it stands in for
        else:
            scheduled_at_ms = int(task.state.next_run * 1000)
            if trigger == "schedule" and not task.state._retry_pending:
                task.state._slot = task.state.next_run
        started_at_ms   = int(start * 1000)
        was_retry = bool(task.state._retry_pending) or catch_up

        nova_ops_writer.record_run_start(
            run_id=run_id,
            task_id=task.id,
            task_script=task.script,
            task_group=task.group or "",
            scheduled_at_ms=scheduled_at_ms,
            started_at_ms=started_at_ms,
            consecutive_failures=task.state.consecutive_failures,
            run_count=task.state.run_count,
            was_retry=was_retry,
            trigger=trigger,
            sched_host=SCHED_HOST,
        )

        cmd = self._task_cmd(task)
        env = self._task_env(task)

        _run_status = "success"
        _stdout_tail = ""
        _error_tail  = ""
        _retry_recovered = False

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
                _run_status = "timeout"
                # Sentinel exit code for timeouts (matches shell's 128+SIGTERM
                # convention) so a timed-out run isn't recorded with the prior
                # run's stale exit code.
                task.state.last_exit_code = 124
                raise TimeoutError(f"Timed out after {task.timeout}s")

            task.state.last_exit_code = proc.returncode
            task.state.last_duration = time.time() - start
            _stdout_tail = stdout.decode(errors="replace")[-200:].strip() if stdout else ""

            if proc.returncode != 0:
                _run_status = "failure"
                task.state.last_error = (stderr.decode(errors="replace"))[-500:]
                if not task.state.last_error.strip() and stdout:
                    # many scripts log their failure reason to stdout and exit 1 — without this the
                    # run row / Slack alert had an empty error (cluster_render, wifi_scan, unifi_stalker)
                    task.state.last_error = stdout.decode(errors="replace")[-500:]
                _error_tail = task.state.last_error
                if (not catch_up and not task.state._retry_pending
                        and task.state.consecutive_failures == 0):
                    # First failure — schedule a retry in 60s instead of counting it
                    task.state._retry_pending = True
                    task.state.next_run = time.time() + 60
                    log(f"RETRY {task.id} in 60s (first failure, exit={proc.returncode})",
                        level=LOG_WARN, source="scheduler")
                    # Don't increment consecutive_failures yet — give it one more shot
                else:
                    # Retry also failed, or already had prior failures — count it
                    task.state.consecutive_failures += 1
                    task.state._retry_pending = False
                    log(f"FAIL {task.id} exit={proc.returncode} ({task.state.last_duration:.1f}s)",
                        level=LOG_ERROR, source="scheduler")
                    self._total_failures += 1
                    if _should_alert_failure(task.state.consecutive_failures):
                        await self._slack_alert(f":x: *{task.id}* — {task.state.consecutive_failures} consecutive failures. "
                                               f"Last error: {task.state.last_error[:200]}",
                                               task_id=task.id)
            else:
                _run_status = "success"
                if task.state._retry_pending:
                    _retry_recovered = True
                    log(f"RETRY OK {task.id} — recovered on retry",
                        level=LOG_INFO, source="scheduler")
                elif task.state.consecutive_failures > 0:
                    log(f"RECOVERED {task.id} after {task.state.consecutive_failures} failures",
                        level=LOG_INFO, source="scheduler")
                task.state.consecutive_failures = 0
                task.state._retry_pending = False
                task.state.last_error = ""
                if task.id in self._quarantined:
                    # A successful (e.g. manual /run) execution clears dead-letter.
                    self._quarantined.discard(task.id)
                    log(f"UNQUARANTINE {task.id} — succeeded, rescheduling",
                        level=LOG_INFO, source="scheduler")

        except Exception as e:
            if _run_status == "success":  # not already set to timeout/failure above
                _run_status = "failure"
            task.state.consecutive_failures += 1
            task.state.last_error = str(e)
            _error_tail = str(e)[:500]
            task.state.last_duration = time.time() - start
            self._total_failures += 1
            log(f"ERROR {task.id}: {e}", level=LOG_ERROR, source="scheduler")
            if _should_alert_failure(task.state.consecutive_failures):
                await self._slack_alert(f":x: *{task.id}* — {e}", task_id=task.id)

        finally:
            ended_at_ms  = int(time.time() * 1000)
            duration_ms  = ended_at_ms - started_at_ms

            nova_ops_writer.record_run_end(
                run_id=run_id,
                ended_at_ms=ended_at_ms,
                duration_ms=duration_ms,
                exit_code=task.state.last_exit_code,
                status=_run_status,
                error_tail=_error_tail,
                stdout_tail=_stdout_tail,
                retry_recovered=_retry_recovered,
            )

            try:
                self._note_catch_up(task, trigger, _run_status, _error_tail)
            except Exception as e:  # noqa: BLE001 — bookkeeping must never break a run
                log(f"catch-up bookkeeping failed for {task.id}: {e}", level=LOG_WARN, source="scheduler")

            task.state.running = False
            task.state.pid = 0
            task.state.last_run = start
            task.state.run_count += 1
            self._running_count -= 1
            self._total_runs += 1
            # Dead-letter: after QUARANTINE_THRESHOLD consecutive failures stop
            # scheduling this task entirely and say so exactly once (bypasses the
            # per-task rate limit via its own dedup key). Cleared by scheduler
            # restart, a config reload touching the task, or a successful run.
            if (task.state.consecutive_failures >= QUARANTINE_THRESHOLD
                    and task.id not in self._quarantined):
                self._quarantined.add(task.id)
                log(f"QUARANTINE {task.id} — {task.state.consecutive_failures} consecutive "
                    f"failures; unscheduled until scheduler restart or config change",
                    level=LOG_ERROR, source="scheduler")
                if self.slack_cfg.get("alerts", True):
                    await self._slack_post(
                        f":octagonal_sign: *[{SCHED_HOST}] {task.id} QUARANTINED* — "
                        f"{task.state.consecutive_failures} consecutive failures. Task is "
                        f"unscheduled until scheduler restart or config change. "
                        f"Last error: {task.state.last_error[:200]}",
                        level="warning", dedup_key=f"scheduler-quarantine-{task.id}")
            # Don't overwrite next_run if a retry was just scheduled
            if not task.state._retry_pending:
                self._advance_next_run(task)

    def _task_cmd(self, task: Task) -> list:
        script_path = SCRIPTS_DIR / task.script
        if task.script.endswith(".py"):
            return [self.sched_cfg.get("python", "/opt/homebrew/bin/python3"), str(script_path)] + task.args
        if task.script.endswith(".sh"):
            return [self.sched_cfg.get("shell", "/bin/zsh"), str(script_path)] + task.args
        return [str(script_path)] + task.args

    def _task_env(self, task: Task) -> dict:
        env = {**os.environ}
        env["PATH"] = self.sched_cfg.get("env", {}).get("PATH", env.get("PATH", ""))
        env["PYTHONPATH"] = self.sched_cfg.get("env", {}).get("PYTHONPATH", str(SCRIPTS_DIR))
        env.update(task.env)
        return env

    # ── Catch-up re-runs (#26) ───────────────────────────────────────────

    def _catch_up_cfg(self) -> dict:
        raw = self.sched_cfg.get("catch_up", {})
        if isinstance(raw, bool):
            raw = {"enabled": raw}
        cfg = {**CATCH_UP_DEFAULTS, **(raw or {})}
        if os.environ.get("NOVA_SCHED_CATCH_UP", "").lower() in ("0", "false", "off", "no"):
            cfg["enabled"] = False
        return cfg

    def _catch_up_eligible_task(self, task: Task) -> bool:
        """Only cron tasks that fire less than ~daily: frequent tasks heal on their own next slot."""
        if not task._cron_expr:
            return False
        tz = self.sched_cfg.get("tz", "America/Los_Angeles")
        return cron_period_s(task._cron_expr, tz) >= self._catch_up_cfg()["min_period_s"]

    @staticmethod
    def _clear_catch_up(st: TaskState):
        st.catch_up_slot = 0
        st.catch_up_attempts = 0
        st.catch_up_last = 0
        st.catch_up_kind = ""
        st.catch_up_error = ""

    def _note_catch_up(self, task: Task, trigger: str, status: str, error: str):
        """Update a task's missed-slot record from the outcome of a run."""
        st = task.state
        if status == "success":
            if st.catch_up_slot:
                log(f"CATCH-UP {'OK' if trigger == 'catch_up' else 'CLEARED'} {task.id} — slot "
                    f"{datetime.fromtimestamp(st.catch_up_slot):%Y-%m-%d %H:%M} satisfied "
                    f"({trigger})", level=LOG_INFO, source="scheduler")
            self._clear_catch_up(st)
            return
        if trigger == "catch_up":
            new_kind = classify_env_error(error)
            if new_kind:
                st.catch_up_kind, st.catch_up_error = new_kind, (error or "")[-500:]
            if st.catch_up_attempts >= self._catch_up_cfg()["max_attempts"]:
                log(f"CATCH-UP GAVE UP {task.id} — {st.catch_up_attempts} attempts for the missed slot",
                    level=LOG_WARN, source="scheduler")
            return
        if trigger != "schedule":
            return
        kind = classify_env_error(error)
        if not kind or not self._catch_up_eligible_task(task):
            self._clear_catch_up(st)  # the newest slot failed for a non-environment reason
            return
        slot = st._slot or st.next_run
        if st.catch_up_slot != slot:
            self._clear_catch_up(st)
            st.catch_up_slot = slot
        st.catch_up_kind, st.catch_up_error = kind, (error or "")[-500:]

    def _env_probe(self, task: Task, kind: str, error: str = "") -> tuple:
        """Has the environment that broke this task recovered? -> (ok, detail). Cheap, read-only."""
        try:
            if kind == "module":
                m = _MODULE_RE.search(error or "")
                if not m:
                    return True, "module error without a module name — spacing is the only guard"
                mod = m.group(1).split(".")[0]
                py = self.sched_cfg.get("python", "/opt/homebrew/bin/python3")
                r = subprocess.run([py, "-c", f"import {mod}"], env=self._task_env(task),
                                   cwd=str(SCRIPTS_DIR), capture_output=True, timeout=30)
                return r.returncode == 0, f"import {mod}: {'ok' if r.returncode == 0 else 'still failing'}"
            if kind == "interpreter":
                interp = self._task_cmd(task)[0]
                ok = os.access(interp, os.X_OK)
                return ok, f"interpreter {interp}: {'present' if ok else 'missing'}"
            if kind == "path":
                m = _QUOTED_PATH_RE.search(error or "")
                target = Path(m.group(1)) if m else SCRIPTS_DIR / task.script
                script = SCRIPTS_DIR / task.script
                ok = os.access(script, os.R_OK) and os.path.exists(target)
                return ok, f"{target}: {'reachable' if ok else 'still missing/denied'}"
            if kind == "pg":
                host = os.environ.get("NOVA_PG_HOST", _nova_dsn.DEFAULT_HOST)
                with socket.create_connection((host, 5432), timeout=5):
                    pass
                return True, f"PG {host}:5432 accepting connections"
            return True, f"no specific probe for {kind or 'unknown'} — spacing is the only guard"
        except Exception as e:  # noqa: BLE001 — a failed probe just means "not yet"
            return False, f"probe failed: {str(e)[:120]}"

    def catch_up_report(self, now: float = None, probe: bool = True) -> list:
        """Every task holding a missed slot, and whether it WOULD be caught up right now.
        -> [{task, slot, kind, attempts, would, reason}]. Read-only (the dry-run uses it too)."""
        now = time.time() if now is None else now
        cfg = self._catch_up_cfg()
        out = []
        for task in self.tasks.values():
            st = task.state
            if not st.catch_up_slot:
                continue
            row = {"task": task.id, "slot": st.catch_up_slot, "kind": st.catch_up_kind,
                   "attempts": st.catch_up_attempts, "would": False, "reason": ""}
            out.append(row)
            if not cfg["enabled"]:
                row["reason"] = "catch-up disabled"
            elif not task.enabled or task.id in self._quarantined:
                row["reason"] = "task disabled/quarantined"
            elif not self._catch_up_eligible_task(task):
                row["reason"] = "not a less-than-daily cron task"
            elif st.running or st._retry_pending:
                row["reason"] = "running or regular retry pending"
            elif st.catch_up_attempts >= cfg["max_attempts"]:
                row["reason"] = f"gave up after {st.catch_up_attempts} attempts"
            elif now - max(st.catch_up_last, st.last_run) < cfg["min_spacing_s"]:
                wait = cfg["min_spacing_s"] - (now - max(st.catch_up_last, st.last_run))
                row["reason"] = f"spacing: next attempt in {wait / 60:.0f}m"
            elif st.next_run - now < cfg["min_spacing_s"]:
                row["reason"] = "next regular slot is imminent"
            elif probe:
                ok, detail = self._env_probe(task, st.catch_up_kind, st.catch_up_error)
                row["would"], row["reason"] = ok, detail
            else:
                row["would"], row["reason"] = True, "eligible (environment not probed)"
        return out

    async def _run_catch_ups(self, now: float, max_conc: int):
        rows = await asyncio.to_thread(self.catch_up_report, now)
        budget = int(self._catch_up_cfg()["max_per_check"])  # no thundering herd after an outage
        for row in rows:
            task = self.tasks.get(row["task"])
            if not row["would"] or task is None or task.state.running:
                continue
            if self._running_count >= max_conc or budget <= 0:
                break
            if task.group and any(t.state.running for t in self.tasks.values()
                                  if t.group == task.group and t.id != task.id):
                continue
            if task.gpu_heavy and any(t.state.running and t.gpu_heavy for t in self.tasks.values()
                                      if t.id != task.id):
                continue
            budget -= 1
            task.state.catch_up_attempts += 1
            task.state.catch_up_last = now
            log(f"CATCH-UP {task.id} attempt {task.state.catch_up_attempts} for missed slot "
                f"{datetime.fromtimestamp(task.state.catch_up_slot):%Y-%m-%d %H:%M} "
                f"({task.state.catch_up_kind}: {row['reason']})", level=LOG_WARN, source="scheduler")
            asyncio.create_task(self.execute_task(task, trigger="catch_up"))

    async def seed_catch_up_from_history(self, now: float = None):
        """Rebuild missed-slot records from scheduler_runs, so a slot that failed before this
        code (or before a restart that lost state) is still caught up. Best-effort."""
        now = time.time() if now is None else now
        todo = [t for t in self.tasks.values()
                if t.enabled and not t.state.catch_up_slot and self._catch_up_eligible_task(t)]
        if not todo or not self._catch_up_cfg()["enabled"]:
            return 0
        try:
            import asyncpg
            conn = await asyncpg.connect(_nova_dsn.pg_url("nova_ops"), timeout=10)
        except Exception as e:  # noqa: BLE001
            log(f"catch-up seed skipped (no PG/asyncpg): {e}", level=LOG_WARN, source="scheduler")
            return 0
        seeded = 0
        try:
            for task in todo:
                try:
                    rows = await conn.fetch(
                        "SELECT scheduled_at, started_at, status, error_tail, was_retry, "
                        "COALESCE(\"trigger\", 'schedule') AS trig FROM scheduler_runs "
                        "WHERE task_id=$1 AND (sched_host IS NULL OR sched_host=$2) "
                        "AND status <> 'running' ORDER BY started_at DESC LIMIT 10",
                        task.id, str(SCHED_HOST))
                except Exception:  # noqa: BLE001 — pre-migration table: no trigger/sched_host
                    rows = await conn.fetch(
                        "SELECT scheduled_at, started_at, status, error_tail, was_retry, "
                        "'schedule' AS trig FROM scheduler_runs WHERE task_id=$1 "
                        "AND status <> 'running' ORDER BY started_at DESC LIMIT 10", task.id)
                rec = self._missed_slot_from_rows(task, [dict(r) for r in rows], now)
                if rec:
                    st = task.state
                    (st.catch_up_slot, st.catch_up_attempts, st.catch_up_last,
                     st.catch_up_kind, st.catch_up_error) = rec
                    seeded += 1
        finally:
            await conn.close()
        if seeded:
            log(f"catch-up: seeded {seeded} missed slot(s) from run history", level=LOG_INFO,
                source="scheduler")
        return seeded

    def _missed_slot_from_rows(self, task: Task, rows: list, now: float):
        """rows newest-first. -> (slot, attempts, last_attempt, kind, error) or None."""
        if not rows or rows[0]["status"] == "success":
            return None
        kind = classify_env_error(rows[0]["error_tail"] or "")
        if not kind:
            return None
        slot_row = next((r for r in rows if r["trig"] == "schedule" and not r["was_retry"]), None)
        if slot_row is None:
            return None
        slot = slot_row["scheduled_at"] / 1000.0
        tz = self.sched_cfg.get("tz", "America/Los_Angeles")
        if next_cron_time(task._cron_expr, slot + 1, tz) <= now:
            return None  # a newer slot has already come and gone — this one is stale
        tries = [r for r in rows if r["trig"] == "catch_up" and r["scheduled_at"] == slot_row["scheduled_at"]]
        last = max((r["started_at"] for r in tries), default=0) / 1000.0
        return slot, len(tries), last, kind, (rows[0]["error_tail"] or "")[-500:]

    # ── Slack ────────────────────────────────────────────────────────────

    async def _slack_post(self, text, level="info", category="scheduler", dedup_key=None):
        # Migrated to the central notification bus. Declares intent (level +
        # category) instead of a channel; the notifier daemon routes/dedups.
        import asyncio
        lines = str(text).split("\n")
        title = lines[0]
        for sc in (":x:", ":heartbeat:", ":rocket:", ":sunrise:", ":octagonal_sign:"):
            title = title.replace(sc, "")
        title = title.replace("*", "").strip()
        body = "\n".join(l.strip() for l in lines[1:]).strip() or None
        await asyncio.to_thread(
            notify, title, body, level, category, None, dedup_key,
        )

    async def _slack_alert(self, text, task_id=None):
        if self.slack_cfg.get("alerts", True):
            # Per-task rate limit: at most one failure alert per task per hour,
            # on top of the power-of-two backoff cadence (2026-09-13).
            if task_id:
                now = time.time()
                if now - self._last_task_alert.get(task_id, 0) < ALERT_MIN_INTERVAL_S:
                    return
                self._last_task_alert[task_id] = now
            # Host prominent in the title so the alert is identifiable at a glance.
            text = f":x: *[{SCHED_HOST}] {task_id}* — " + text.split("—", 1)[1].strip() \
                if task_id and "—" in text else f"[{SCHED_HOST}] " + text
            # Per-task dedup_key so distinct failing tasks don't collapse into each
            # other (the old shared key meant task B's failure suppressed task A's).
            dk = f"scheduler-task-failure-{task_id}" if task_id else "scheduler-task-failure"
            await self._slack_post(text, level="warning", dedup_key=dk)

    async def _heartbeat(self):
        healthy = sum(1 for t in self.tasks.values() if t.state.consecutive_failures == 0 and t.enabled)
        total = sum(1 for t in self.tasks.values() if t.enabled)
        running = sum(1 for t in self.tasks.values() if t.state.running)
        uptime_h = (time.time() - self._start_time) / 3600

        failing = [t for t in self.tasks.values() if t.state.consecutive_failures >= 3 and t.enabled]
        failing_ids = frozenset(t.id for t in failing)
        fail_str = ""
        if failing:
            fail_str = "\n  :x: Failing: " + ", ".join(t.id for t in failing)

        # Build current state snapshot for comparison
        current_state = (healthy, total, failing_ids)

        # Quiet hours: suppress heartbeat between 10pm-8am unless there are NEW failures
        tz = ZoneInfo(self.sched_cfg.get("tz", "America/Los_Angeles"))
        local_hour = datetime.now(tz).hour
        in_quiet_hours = local_hour >= 22 or local_hour < 8

        if in_quiet_hours:
            # Only post during quiet hours if there are new failures since last heartbeat
            new_failures = [t for t in failing
                           if t.state.last_run > self._last_heartbeat]
            if not new_failures:
                return

        # During daytime: suppress heartbeat if nothing changed since last time.
        # Still post at least every 4 hours as proof-of-life.
        now = time.time()
        proof_of_life_interval = 4 * 3600  # 4 hours
        state_changed = (self._prev_heartbeat_state is None or
                         current_state != self._prev_heartbeat_state)
        new_failures_since = any(t.state.last_run > self._last_heartbeat for t in failing)
        force_proof_of_life = (now - self._last_forced_heartbeat) >= proof_of_life_interval

        if not in_quiet_hours and not state_changed and not new_failures_since and not force_proof_of_life:
            # Nothing changed, no new failures, and not time for proof-of-life yet
            return

        self._prev_heartbeat_state = current_state
        if force_proof_of_life or state_changed or new_failures_since:
            self._last_forced_heartbeat = now

        await self._slack_post(
            f":heartbeat: *Scheduler Heartbeat*\n"
            f"  Tasks: {healthy}/{total} healthy, {running} running\n"
            f"  Runs: {self._total_runs} total, {self._total_failures} failures\n"
            f"  Uptime: {uptime_h:.1f}h"
            f"{fail_str}",
            level="info", dedup_key="scheduler-heartbeat",
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
                        "group": t.group,
                        "gpu_heavy": t.gpu_heavy,
                        "running": t.state.running,
                        "last_run": t.state.last_run,
                        "next_run": t.state.next_run,
                        "last_duration": round(t.state.last_duration, 1),
                        "last_exit_code": t.state.last_exit_code,
                        "consecutive_failures": t.state.consecutive_failures,
                        "run_count": t.state.run_count,
                        "quarantined": tid in self._quarantined,
                    }
                body = json.dumps(tasks_data, indent=2).encode()
            elif path.startswith("/run/"):
                task_id = path[5:]
                if task_id in self.tasks:
                    asyncio.create_task(self.execute_task(self.tasks[task_id], trigger="manual"))
                    body = json.dumps({"queued": task_id}).encode()
                else:
                    body = json.dumps({"error": "unknown task"}).encode()
            elif path == "/runs" or path.startswith("/runs/"):
                # Serve run history from nova_ops.scheduler_runs
                try:
                    import asyncpg
                    conn = await asyncpg.connect(_nova_dsn.pg_url("nova_ops"))
                    try:
                        if path == "/runs":
                            # Last 50 runs across all tasks
                            rows = await conn.fetch(
                                "SELECT run_id, task_id, task_script, task_group, "
                                "started_at, ended_at, duration_ms, exit_code, status, "
                                "error_tail, was_retry, retry_recovered "
                                "FROM scheduler_runs ORDER BY started_at DESC LIMIT 50"
                            )
                        else:
                            tid = path[6:]
                            rows = await conn.fetch(
                                "SELECT run_id, task_id, task_script, task_group, "
                                "started_at, ended_at, duration_ms, exit_code, status, "
                                "error_tail, was_retry, retry_recovered "
                                "FROM scheduler_runs WHERE task_id=$1 "
                                "ORDER BY started_at DESC LIMIT 20",
                                tid,
                            )
                        body = json.dumps([dict(r) for r in rows], default=str).encode()
                    finally:
                        await conn.close()
                except Exception as e:
                    body = json.dumps({"error": str(e)}).encode()
            elif path == "/stats":
                # Aggregate stats per task from nova_ops view
                try:
                    import asyncpg
                    conn = await asyncpg.connect(_nova_dsn.pg_url("nova_ops"))
                    try:
                        rows = await conn.fetch(
                            "SELECT * FROM scheduler_task_stats ORDER BY total_runs DESC"
                        )
                        body = json.dumps([dict(r) for r in rows], default=str).encode()
                    finally:
                        await conn.close()
                except Exception as e:
                    body = json.dumps({"error": str(e)}).encode()
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
        self._last_forced_heartbeat = time.time()

        # Signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown)
        loop.add_signal_handler(signal.SIGHUP, self._reload)

        # Start HTTP status server — also acts as singleton lock
        port = self.sched_cfg.get("status_port", 37460)
        try:
            server = await asyncio.start_server(self._handle_http, "0.0.0.0", port)
            log(f"Status API on port {port}", level=LOG_INFO, source="scheduler")
        except OSError as e:
            if e.errno == 48:  # Address already in use — another scheduler is running
                log(f"Another scheduler already running on port {port} — exiting to prevent duplicates",
                    level=LOG_WARN, source="scheduler")
                self._running = False
                return
            log(f"Status API failed to start: {e}", level=LOG_WARN, source="scheduler")
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

        try:
            await self.seed_catch_up_from_history()
        except Exception as e:  # noqa: BLE001
            log(f"catch-up seed failed: {e}", level=LOG_WARN, source="scheduler")
        # First catch-up check waits a minute so regular overdue work starts first.
        self._last_catch_up_check = time.time()

        tick = self.sched_cfg.get("tick_interval", 1)
        max_conc = self.sched_cfg.get("max_concurrent", 6)
        hb_interval = self.sched_cfg.get("heartbeat_interval", 3600)

        _post_wake_stagger_until: float = 0.0  # wall time until stagger delay is over
        _stagger_slot: int = 0                  # which task slot we're on during stagger

        while self._running:
            now = time.time()

            # Detect sleep/wake (time jump)
            if now - self._last_tick > tick * 30:
                gap = now - self._last_tick
                log(f"Time jump: {gap:.0f}s gap — likely wake from sleep", level=LOG_WARN, source="scheduler")
                self._recalculate_next_runs()
                await self._slack_post(f":sunrise: *Scheduler resumed* — {gap/60:.0f}m gap, recalculating timers",
                                       level="info", dedup_key="scheduler-wake-resume")
                # Stagger post-wake task launches over 60s to prevent thundering herd.
                # Tasks that became overdue will be launched 5s apart instead of all at once.
                _post_wake_stagger_until = now + 60.0
                _stagger_slot = 0
            self._last_tick = now

            # Find due tasks
            for task in self.tasks.values():
                if (not task.enabled or task.id in self._quarantined
                        or task.state.next_run > now):
                    continue

                if task.state.running:
                    if task.overlap == "skip":
                        self._advance_next_run(task)
                    continue

                if self._running_count >= max_conc:
                    break

                # Group serialization: skip if another task in the same group is running
                if task.group:
                    group_busy = any(
                        t.state.running
                        for t in self.tasks.values()
                        if t.group == task.group and t.id != task.id
                    )
                    if group_busy:
                        continue  # will retry on next tick

                # GPU exclusion: only one gpu_heavy task at a time
                if task.gpu_heavy:
                    gpu_busy = any(
                        t.state.running and t.gpu_heavy
                        for t in self.tasks.values()
                        if t.id != task.id
                    )
                    if gpu_busy:
                        continue

                # Sleep-aware: defer GPU-heavy/LLM tasks when under-slept
                if task.gpu_heavy or task.group == "llm":
                    sleep = self._get_sleep_metrics()
                    if sleep["hours_slept"] < 6:
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        if self._sleep_observation_date != today_str:
                            self._sleep_observation_date = today_str
                            log(f"Sleep-aware: {sleep['hours_slept']:.1f}h sleep detected — "
                                f"deferring GPU-heavy/LLM tasks this cycle",
                                level=LOG_INFO, source="scheduler")
                            try:
                                nova_ops_writer.record_observation(
                                    "sleep_aware_defer",
                                    f"Deferring GPU-heavy tasks: {sleep['hours_slept']:.1f}h sleep"
                                )
                            except Exception:
                                pass
                        log(f"Sleep-aware: deferring {task.id} (GPU-heavy, "
                            f"{sleep['hours_slept']:.1f}h sleep)",
                            level=LOG_INFO, source="scheduler")
                        continue  # skip this cycle, will retry next tick

                # Post-wake stagger: delay each overdue task by 5s * slot
                if now < _post_wake_stagger_until:
                    stagger_delay = _stagger_slot * 5.0
                    task.state.next_run = now + stagger_delay
                    _stagger_slot += 1
                    if stagger_delay > 0:
                        continue  # will be picked up on the next tick(s)

                asyncio.create_task(self.execute_task(task))

            # Catch-up re-runs of missed less-than-daily slots (#26)
            if (now - self._last_catch_up_check >= CATCH_UP_CHECK_EVERY_S
                    and self._catch_up_cfg()["enabled"]):
                self._last_catch_up_check = now
                try:
                    await self._run_catch_ups(now, max_conc)
                except Exception as e:  # noqa: BLE001 — never let catch-up stall the loop
                    log(f"catch-up check failed: {e}", level=LOG_WARN, source="scheduler")

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
        await nova_ops_writer.close()
        if self.slack_cfg.get("startup", True):
            await self._slack_post(":octagonal_sign: *Nova Scheduler stopped*")

    def _shutdown(self):
        log("Received shutdown signal", level=LOG_INFO, source="scheduler")
        self._running = False

    def _reload(self):
        """Hot-reload config from YAML. Preserves runtime state of existing tasks."""
        log("SIGHUP received — reloading config", level=LOG_INFO, source="scheduler")
        try:
            _, new_slack, new_tasks = load_config()
            self.slack_cfg = new_slack

            added, removed, updated = [], [], []
            for tid, new_task in new_tasks.items():
                if tid not in self.tasks:
                    added.append(tid)
                    self.tasks[tid] = new_task
                    new_task.state.next_run = time.time()
                else:
                    old = self.tasks[tid]
                    if (old.timeout != new_task.timeout or old.schedule != new_task.schedule
                            or old.enabled != new_task.enabled or old.script != new_task.script):
                        updated.append(tid)
                    new_task.state = old.state
                    self.tasks[tid] = new_task

            for tid in list(self.tasks.keys()):
                if tid not in new_tasks:
                    if not self.tasks[tid].state.running:
                        removed.append(tid)
                        del self.tasks[tid]

            # Config change clears dead-letter for tasks that were added/updated.
            self._quarantined -= set(added) | set(updated)
            self._recalculate_next_runs()
            parts = []
            if added:
                parts.append(f"+{len(added)} new")
            if removed:
                parts.append(f"-{len(removed)} removed")
            if updated:
                parts.append(f"~{len(updated)} updated")
            summary = ", ".join(parts) if parts else "no changes"
            log(f"Config reloaded: {len(self.tasks)} tasks ({summary})",
                level=LOG_INFO, source="scheduler")
        except Exception as e:
            log(f"Config reload failed: {e}", level=LOG_ERROR, source="scheduler")


# ── Entry point ──────────────────────────────────────────────────────────────

def catch_up_dry_run(probe: bool = True) -> int:
    """Print which missed less-than-daily slots WOULD be caught up now. Runs nothing."""
    scheduler = NovaScheduler()
    scheduler.load()
    asyncio.run(scheduler.seed_catch_up_from_history())
    rows = scheduler.catch_up_report(probe=probe)
    cfg = scheduler._catch_up_cfg()
    print(f"catch-up {'ENABLED' if cfg['enabled'] else 'DISABLED'} — max {cfg['max_attempts']} attempts/slot, "
          f"spacing {cfg['min_spacing_s'] // 60}m, period >= {cfg['min_period_s'] / 3600:.0f}h ({CONFIG_PATH})")
    if not rows:
        print("no missed slots on record")
    for r in sorted(rows, key=lambda r: (not r["would"], r["task"])):
        print(f"  {'WOULD RUN' if r['would'] else 'blocked  '}  {r['task']:<28} slot "
              f"{datetime.fromtimestamp(r['slot']):%Y-%m-%d %H:%M}  {r['kind'] or '-':<11} "
              f"attempts {r['attempts']}  {r['reason']}")
    return 0


def main():
    if "--catch-up-dry-run" in sys.argv:
        sys.exit(catch_up_dry_run(probe="--no-probe" not in sys.argv))
    scheduler = NovaScheduler()
    scheduler.load()
    asyncio.run(scheduler.run())


if __name__ == "__main__":
    main()
