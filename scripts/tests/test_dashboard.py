"""
test_dashboard.py — Unit and mock tests for Nova Control dashboard server.

Covers collectors, history writes, and alert evaluation from:
    " + str(Path.home()) + "/.openclaw/apps/nova-control-web/server.py

All external dependencies (asyncpg, psql subprocesses, aiohttp, redis, psutil)
are mocked so tests run without live services.

Written by Jordan Koch.
"""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# We cannot import server.py directly because it has side effects at module
# level (FastAPI app creation, global state, psycopg2/psutil etc.).
# Instead we import individual functions after patching the heavy deps.
# ---------------------------------------------------------------------------

# Minimal stubs so the module loads without a live Postgres / Redis / psutil.
_PATCHES = []


def _start_patches():
    """Patch heavy imports so server.py can be imported safely."""
    global _PATCHES
    stubs = {
        "psycopg2": MagicMock(),
        "psutil": MagicMock(),
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "aiohttp": MagicMock(),
        "uvicorn": MagicMock(),
    }
    for mod_name, stub in stubs.items():
        p = patch.dict("sys.modules", {mod_name: stub})
        p.start()
        _PATCHES.append(p)


def _stop_patches():
    for p in _PATCHES:
        p.stop()
    _PATCHES.clear()


import sys

_start_patches()

# Now we can add server's parent to the path and import it
sys.path.insert(0, str(Path("" + str(Path.home()) + "/.openclaw/apps/nova-control-web")))

# Re-import psutil for the server module since we mocked it
import importlib

# We need to import the server module with the mocked dependencies
import server  # noqa: E402

_stop_patches()


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def tmp_state_files(tmp_path):
    """Create a temp directory tree that mirrors the workspace state layout."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def tmp_memory_dir(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    return memory_dir


@pytest.fixture
def tmp_journal_dir(tmp_path):
    dream_dir = tmp_path / "journal" / "dreams"
    dream_dir.mkdir(parents=True)
    return tmp_path / "journal"


# ======================================================================
# collect_postgresql()
# ======================================================================

class TestCollectPostgresql:
    """Test the collect_postgresql() collector."""

    @pytest.mark.asyncio
    async def test_returns_today_count_and_sources(self):
        """Verify collect_postgresql returns today_count and today_sources."""

        # We'll mock asyncio.create_subprocess_exec to return canned psql output
        call_count = 0
        expected_outputs = [
            b"1048576\n",            # db_size
            b"memories|5000\nmemory_links|200\nconsolidation_runs|10\n",  # tables
            b"12\n",                 # index_count
            b"42\n",                 # today_count
            b"slack|20\nemail|15\ndiscord|7\n",  # today_sources
        ]

        async def fake_subprocess(*args, **kwargs):
            nonlocal call_count
            mock_proc = AsyncMock()
            idx = min(call_count, len(expected_outputs) - 1)
            mock_proc.communicate = AsyncMock(return_value=(expected_outputs[idx], b""))
            mock_proc.returncode = 0
            call_count += 1
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            result = await server.collect_postgresql()

        assert result["status"] == "ok"
        assert result["today_count"] == 42
        assert isinstance(result["today_sources"], list)
        assert len(result["today_sources"]) == 3
        assert result["today_sources"][0]["source"] == "slack"
        assert result["today_sources"][0]["count"] == 20

    @pytest.mark.asyncio
    async def test_returns_error_on_exception(self):
        """Verify graceful error handling when psql fails."""
        async def fail_subprocess(*args, **kwargs):
            raise OSError("psql not found")

        with patch("asyncio.create_subprocess_exec", side_effect=fail_subprocess):
            result = await server.collect_postgresql()

        assert result["status"] == "error"
        assert "error" in result
        assert result["db_size_gb"] == 0


# ======================================================================
# collect_task_history()
# ======================================================================

class TestCollectTaskHistory:
    """Test collect_task_history() which queries nova_ops via asyncpg."""

    @pytest.mark.asyncio
    async def test_returns_status_counts(self):
        """Verify task history aggregates status counts from nova_ops."""
        mock_conn = AsyncMock()

        # First fetch: all-time status counts
        mock_conn.fetch = AsyncMock(side_effect=[
            [
                {"status": "succeeded", "cnt": 100},
                {"status": "failed", "cnt": 5},
                {"status": "timed_out", "cnt": 2},
            ],
            [
                {"status": "succeeded", "cnt": 30},
                {"status": "failed", "cnt": 1},
            ],
        ])
        mock_conn.close = AsyncMock()

        with patch.object(server.asyncpg, "connect", return_value=mock_conn):
            result = await server.collect_task_history()

        assert result["status"] == "ok"
        assert result["all_time"]["succeeded"] == 100
        assert result["all_time"]["failed"] == 5
        assert result["last_24h"]["succeeded"] == 30
        assert result["last_24h"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_returns_error_on_db_failure(self):
        """Verify error handling when asyncpg connection fails."""
        with patch.object(server.asyncpg, "connect", side_effect=Exception("connection refused")):
            result = await server.collect_task_history()

        assert result["status"] == "error"
        assert "connection refused" in result["error"]


# ======================================================================
# collect_flow_runs()
# ======================================================================

class TestCollectFlowRuns:
    """Test collect_flow_runs() flow status aggregation."""

    @pytest.mark.asyncio
    async def test_aggregates_flow_statuses(self):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {"status": "completed", "cnt": 50},
            {"status": "failed", "cnt": 3},
            {"status": "running", "cnt": 1},
        ])
        mock_conn.close = AsyncMock()

        with patch.object(server.asyncpg, "connect", return_value=mock_conn):
            result = await server.collect_flow_runs()

        assert result["status"] == "ok"
        assert result["flows"]["completed"] == 50
        assert result["flows"]["failed"] == 3
        assert result["flows"]["running"] == 1

    @pytest.mark.asyncio
    async def test_returns_empty_on_db_error(self):
        with patch.object(server.asyncpg, "connect", side_effect=Exception("timeout")):
            result = await server.collect_flow_runs()

        assert result["status"] == "error"
        assert result["flows"] == {}


# ======================================================================
# collect_app_watchdog()
# ======================================================================

class TestCollectAppWatchdog:
    """Test collect_app_watchdog() state file parsing."""

    @pytest.mark.asyncio
    async def test_parses_watchdog_state(self, tmp_state_files):
        """Verify correct parsing of app watchdog state JSON."""
        state_data = {
            "apps": {
                "37421": {"alive": True, "last_seen": time.time() - 10, "info": "OneOnOne OK"},
                "37422": {"alive": True, "last_seen": time.time() - 5, "info": "MLXCode OK"},
                "37423": {"alive": False, "last_seen": 0, "info": "NMAPScanner unreachable"},
                "infra_11434": {"alive": True, "last_seen": time.time(), "info": "Ollama OK"},
            },
            "restarts": [
                {"port": "37423", "ts": time.time() - 300, "reason": "port unreachable"},
            ],
        }
        state_file = tmp_state_files / "nova_app_watchdog_state.json"
        state_file.write_text(json.dumps(state_data))

        # Reset cache so we get a fresh result
        server._app_watchdog_ts = 0

        with patch.object(server, "APP_WATCHDOG_STATE", state_file):
            result = await server.collect_app_watchdog()

        assert result["status"] == "degraded"  # 2 up, 1 down
        assert result["up_count"] == 2
        assert result["total"] == 3  # infra_ ports are filtered out
        # Verify infra ports are excluded
        port_keys = [a["port"] for a in result["apps"]]
        assert "infra_11434" not in port_keys
        # Verify named mapping
        names = [a["name"] for a in result["apps"]]
        assert "OneOnOne" in names
        assert "MLXCode" in names
        assert "NMAPScanner" in names

    @pytest.mark.asyncio
    async def test_returns_unavailable_when_file_missing(self, tmp_state_files):
        """Verify graceful handling when state file doesn't exist."""
        missing = tmp_state_files / "nonexistent.json"
        server._app_watchdog_ts = 0
        with patch.object(server, "APP_WATCHDOG_STATE", missing):
            result = await server.collect_app_watchdog()
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_all_up_returns_ok(self, tmp_state_files):
        """When all apps are alive, status should be 'ok'."""
        state_data = {
            "apps": {
                "37421": {"alive": True, "last_seen": time.time(), "info": "OK"},
                "37422": {"alive": True, "last_seen": time.time(), "info": "OK"},
            },
            "restarts": [],
        }
        state_file = tmp_state_files / "nova_app_watchdog_state.json"
        state_file.write_text(json.dumps(state_data))
        server._app_watchdog_ts = 0
        with patch.object(server, "APP_WATCHDOG_STATE", state_file):
            result = await server.collect_app_watchdog()
        assert result["status"] == "ok"
        assert result["up_count"] == 2

    @pytest.mark.asyncio
    async def test_all_down_returns_down(self, tmp_state_files):
        """When all apps are dead, status should be 'down'."""
        state_data = {
            "apps": {
                "37421": {"alive": False, "last_seen": 0, "info": ""},
            },
            "restarts": [],
        }
        state_file = tmp_state_files / "nova_app_watchdog_state.json"
        state_file.write_text(json.dumps(state_data))
        server._app_watchdog_ts = 0
        with patch.object(server, "APP_WATCHDOG_STATE", state_file):
            result = await server.collect_app_watchdog()
        assert result["status"] == "down"
        assert result["up_count"] == 0


# ======================================================================
# collect_weather()
# ======================================================================

class TestCollectWeather:
    """Test collect_weather() memory file extraction."""

    @pytest.mark.asyncio
    async def test_extracts_weather_from_memory_file(self, tmp_memory_dir, tmp_state_files):
        """Verify weather text, temperature, and conditions are extracted."""
        today_str = time.strftime("%Y-%m-%d")
        today_file = tmp_memory_dir / f"{today_str}.md"
        today_file.write_text(
            "# Daily Notes\n\n"
            "## Weather Report\n"
            "Weather: Currently 78F and partly cloudy with light winds.\n"
            "High of 85F expected this afternoon.\n\n"
            "## Other stuff\n"
            "Moon: Waxing Gibbous (72% illuminated)\n"
        )

        # Sky watcher state file
        sky_state = tmp_state_files / "nova_sky_watcher_state.json"
        sky_state.write_text(json.dumps({
            "last_capture": "2026-05-01T10:30:00",
            "frames_today": 15,
            "sessions_today": ["morning", "afternoon"],
        }))

        server._weather_ts = 0  # Reset cache

        with patch.object(server, "SKY_WATCHER_STATE", sky_state), \
             patch.object(server, "MEMORY_DIR", tmp_memory_dir):
            result = await server.collect_weather()

        assert result["status"] == "ok"
        assert "weather_text" in result
        assert "78" in result["weather_text"]
        assert result.get("temp_f") == 78
        # "cloudy" appears before "partly cloudy" in server's condition list,
        # so it matches first on "partly cloudy" text.
        assert result.get("conditions") == "Cloudy"
        assert result.get("moon_phase") is not None
        assert "Waxing Gibbous" in result["moon_phase"]
        assert result["frames_today"] == 15

    @pytest.mark.asyncio
    async def test_returns_unavailable_when_no_files(self, tmp_memory_dir, tmp_state_files):
        """Verify unavailable status when no state or memory files exist."""
        missing_sky = tmp_state_files / "missing_sky.json"
        server._weather_ts = 0
        with patch.object(server, "SKY_WATCHER_STATE", missing_sky), \
             patch.object(server, "MEMORY_DIR", tmp_memory_dir):
            result = await server.collect_weather()
        assert result["status"] == "unavailable"


# ======================================================================
# collect_dream_status()
# ======================================================================

class TestCollectDreamStatus:
    """Test collect_dream_status() dream state assembly."""

    @pytest.mark.asyncio
    async def test_assembles_dream_state(self, tmp_path):
        """Verify dream status combines scheduler data, images, and journal."""
        # Set up dream video dir with images
        dream_vid_dir = tmp_path / "dream_videos"
        dream_vid_dir.mkdir()
        for i in range(3):
            img = dream_vid_dir / f"frame_{i}.png"
            img.write_text("fake png")

        # Set up dream journal
        dream_dir = tmp_path / "dreams"
        dream_dir.mkdir()
        (dream_dir / "2026-04-30.md").write_text("I dreamed of electric sheep " * 20)
        (dream_dir / "2026-05-01.md").write_text("A vast neural network hummed softly " * 10)

        # Mock current_state with scheduler task data
        mock_state = {
            "scheduler": {
                "tasks": {
                    "dream_pipeline": {
                        "last_run": time.time() - 3600,
                        "run_count": 45,
                        "consecutive_failures": 0,
                        "last_duration": 120.5,
                    }
                }
            }
        }

        server._dream_ts = 0
        original_current_state = server.current_state

        try:
            server.current_state = mock_state
            with patch.object(server, "DREAM_DIR", dream_dir), \
                 patch("pathlib.Path.home", return_value=tmp_path.parent):
                # Also patch the dream_video_dir path used inside the function
                dream_vid = Path.home() / ".openclaw" / "workspace" / "dream_videos"
                # Since the function constructs the path via Path.home(), we need
                # a different approach: patch the actual path object
                with patch.object(server, "DREAM_DIR", dream_dir):
                    # Manually construct what the function expects
                    result = await server.collect_dream_status()
        finally:
            server.current_state = original_current_state

        assert result["status"] == "ok"
        assert result["run_count"] == 45
        assert result["consecutive_failures"] == 0
        assert result["dream_entries"] == 2
        assert result["last_dream_file"] == "2026-05-01.md"

    @pytest.mark.asyncio
    async def test_degraded_on_failures(self, tmp_path):
        """Verify degraded status when dream pipeline has failures."""
        mock_state = {
            "scheduler": {
                "tasks": {
                    "dream_pipeline": {
                        "last_run": time.time() - 7200,
                        "run_count": 10,
                        "consecutive_failures": 3,
                        "last_duration": 0,
                    }
                }
            }
        }

        dream_dir = tmp_path / "dreams"
        dream_dir.mkdir()
        server._dream_ts = 0
        original = server.current_state
        try:
            server.current_state = mock_state
            with patch.object(server, "DREAM_DIR", dream_dir):
                result = await server.collect_dream_status()
        finally:
            server.current_state = original

        assert result["status"] == "degraded"
        assert result["consecutive_failures"] == 3


# ======================================================================
# collect_synology_state()
# ======================================================================

class TestCollectSynologyState:
    """Test collect_synology_state() NAS state parsing."""

    @pytest.mark.asyncio
    async def test_parses_synology_state(self, tmp_state_files):
        """Verify correct parsing of Synology state JSON."""
        state_data = {
            "last_check": "2026-05-01T10:00:00",
            "model": "DS920+",
            "firmware": "DSM 7.2-64570 Update 4",
            "cpu_pct": 12.5,
            "ram_pct": 45.3,
            "problem_count": 0,
            "problems": [],
            "volumes": "Volume 1: Normal (85% used), Volume 2: Normal (40% used)",
        }
        state_file = tmp_state_files / "nova_synology_state.json"
        state_file.write_text(json.dumps(state_data))

        with patch.object(server, "SYNOLOGY_STATE", state_file):
            result = await server.collect_synology_state()

        assert result["status"] == "ok"
        assert result["model"] == "DS920+"
        assert result["firmware"] == "DSM 7.2-64570 Update 4"
        assert result["cpu_pct"] == 12.5
        assert result["ram_pct"] == 45.3
        assert result["problem_count"] == 0

    @pytest.mark.asyncio
    async def test_returns_unavailable_when_missing(self, tmp_state_files):
        missing = tmp_state_files / "missing_synology.json"
        with patch.object(server, "SYNOLOGY_STATE", missing):
            result = await server.collect_synology_state()
        assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_returns_error_on_corrupt_json(self, tmp_state_files):
        bad_file = tmp_state_files / "bad_synology.json"
        bad_file.write_text("{corrupt json")
        with patch.object(server, "SYNOLOGY_STATE", bad_file):
            result = await server.collect_synology_state()
        assert result["status"] == "error"


# ======================================================================
# collect_healthkit_status()
# ======================================================================

class TestCollectHealthkitStatus:
    """Test collect_healthkit_status() launchd check logic."""

    @pytest.mark.asyncio
    async def test_running_with_log(self, tmp_path):
        """Verify status=ok when launchd reports running and log has timestamps."""
        log_file = tmp_path / "healthkit.log"
        log_file.write_text(
            "2026-05-01T09:00:00 Starting HealthKit sync...\n"
            "2026-05-01T09:00:05 Synced 120 records\n"
            "2026-05-01T10:00:00 Starting HealthKit sync...\n"
            "2026-05-01T10:00:03 Synced 85 records\n"
        )

        async def fake_subprocess(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"PID\tStatus\tLabel\n", b""))
            mock_proc.returncode = 0
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
             patch.object(server, "HEALTHKIT_LOG", log_file):
            result = await server.collect_healthkit_status()

        assert result["status"] == "ok"
        assert result["running"] is True
        assert result["last_sync"] == "2026-05-01T10:00:03"

    @pytest.mark.asyncio
    async def test_not_running(self):
        """Verify status=down when launchd returns non-zero."""
        async def fake_subprocess(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"Could not find service\n"))
            mock_proc.returncode = 3
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
             patch.object(server, "HEALTHKIT_LOG", Path("/nonexistent/healthkit.log")):
            result = await server.collect_healthkit_status()

        assert result["status"] == "down"
        assert result["running"] is False


# ======================================================================
# collect_homebridge_status()
# ======================================================================

class TestCollectHomebridgeStatus:
    """Test collect_homebridge_status() port + launchd check."""

    @pytest.mark.asyncio
    async def test_fully_up(self):
        """Verify ok when both launchd and port check succeed."""
        async def fake_subprocess(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"12345\t0\tnet.digitalnoise.homebridge\n", b""))
            mock_proc.returncode = 0
            return mock_proc

        async def fake_open_connection(host, port):
            reader = AsyncMock()
            writer = AsyncMock()
            writer.close = MagicMock()
            writer.wait_closed = AsyncMock()
            return reader, writer

        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
             patch("asyncio.open_connection", side_effect=fake_open_connection):
            result = await server.collect_homebridge_status()

        assert result["status"] == "ok"
        assert result["launchd"] is True
        assert result["port_reachable"] is True

    @pytest.mark.asyncio
    async def test_launchd_up_port_down(self):
        """Verify degraded when launchd is up but port is unreachable."""
        async def fake_subprocess(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"12345\t0\tnet.digitalnoise.homebridge\n", b""))
            mock_proc.returncode = 0
            return mock_proc

        async def fail_connect(*args, **kwargs):
            raise ConnectionRefusedError("port closed")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
             patch("asyncio.open_connection", side_effect=fail_connect):
            result = await server.collect_homebridge_status()

        assert result["status"] == "degraded"
        assert result["launchd"] is True
        assert result["port_reachable"] is False

    @pytest.mark.asyncio
    async def test_both_down(self):
        """Verify down when launchd is not loaded and port is unreachable."""
        async def fake_subprocess(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"Could not find service\n"))
            mock_proc.returncode = 3
            return mock_proc

        async def fail_connect(*args, **kwargs):
            raise ConnectionRefusedError("port closed")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
             patch("asyncio.open_connection", side_effect=fail_connect):
            result = await server.collect_homebridge_status()

        assert result["status"] == "down"
        assert result["launchd"] is False


# ======================================================================
# write_history_snapshot()
# ======================================================================

class TestWriteHistorySnapshot:
    """Test the write_history_snapshot() function that inserts into nova_ops."""

    @pytest.mark.asyncio
    async def test_writes_system_snapshot(self):
        """Verify snapshot inserts into dashboard_snapshots."""
        mock_cur = MagicMock()
        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cur

        # Temporarily replace app.state.history_db
        mock_app_state = MagicMock()
        mock_app_state.history_db = mock_db

        state = {
            "system": {
                "cpu_percent": 25.0,
                "memory": {"percent": 60.0, "used_gb": 128.5},
                "disks": {
                    "/": {"free_gb": 50.0, "percent": 75.0},
                    "/Volumes/Data": {"free_gb": 200.0, "percent": 40.0},
                },
            },
            "services": {
                "ollama": {"latency_ms": 12, "status": "up"},
                "searxng": {"latency_ms": 45, "status": "up"},
            },
            "postgresql": {"total_rows": 5000},
            "model_usage": {
                "by_provider": {
                    "ollama": {"cost": 0.0, "input_tokens": 50000, "output_tokens": 30000, "sessions": 5},
                }
            },
            "poll_duration_ms": 150,
        }

        with patch.object(server, "app") as mock_app:
            mock_app.state = mock_app_state
            await server.write_history_snapshot(state)

        # Verify cursor.execute was called multiple times
        assert mock_cur.execute.call_count >= 1
        mock_db.commit.assert_called_once()
        mock_cur.close.assert_called_once()

        # Check the first call is the snapshot insert
        first_call = mock_cur.execute.call_args_list[0]
        assert "dashboard_snapshots" in first_call[0][0]

    @pytest.mark.asyncio
    async def test_rollback_on_error(self):
        """Verify rollback is called when a write error occurs."""
        mock_db = MagicMock()
        mock_db.cursor.side_effect = Exception("cursor creation failed")

        with patch.object(server, "app") as mock_app:
            mock_app.state.history_db = mock_db
            # Should not raise
            await server.write_history_snapshot({"system": {}})

        mock_db.rollback.assert_called_once()


# ======================================================================
# evaluate_alerts()
# ======================================================================

class TestEvaluateAlerts:
    """Test evaluate_alerts() threshold logic."""

    def setup_method(self):
        """Reset global alert counters before each test."""
        server._service_down_counts.clear()
        server._cpu_high_count = 0

    def test_disk_critical_below_10gb(self):
        state = {
            "system": {
                "cpu_percent": 10,
                "memory": {"percent": 50},
                "disks": {
                    "/": {"free_gb": 5.2},
                    "/Volumes/Data": {"free_gb": 200.0},
                },
            },
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 0},
        }
        alerts = server.evaluate_alerts(state)
        disk_alerts = [a for a in alerts if a["category"] == "disk"]
        assert len(disk_alerts) == 1
        assert "5.2 GB free" in disk_alerts[0]["message"]
        assert disk_alerts[0]["severity"] == "critical"

    def test_memory_critical_above_90(self):
        state = {
            "system": {
                "cpu_percent": 10,
                "memory": {"percent": 95.3},
                "disks": {},
            },
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 0},
        }
        alerts = server.evaluate_alerts(state)
        mem_alerts = [a for a in alerts if a["category"] == "memory"]
        assert len(mem_alerts) == 1
        assert mem_alerts[0]["severity"] == "critical"

    def test_no_alerts_for_healthy_system(self):
        state = {
            "system": {
                "cpu_percent": 30,
                "memory": {"percent": 50},
                "disks": {"/": {"free_gb": 100}},
            },
            "services": {"ollama": {"status": "up"}},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 5},
        }
        alerts = server.evaluate_alerts(state)
        assert len(alerts) == 0

    def test_cpu_high_after_three_consecutive_polls(self):
        """CPU alert fires only after 3+ consecutive polls above 95%."""
        state = {
            "system": {
                "cpu_percent": 98.0,
                "memory": {"percent": 50},
                "disks": {},
            },
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 0},
        }

        # First two polls should not trigger
        alerts1 = server.evaluate_alerts(state)
        cpu1 = [a for a in alerts1 if a["category"] == "cpu"]
        assert len(cpu1) == 0

        alerts2 = server.evaluate_alerts(state)
        cpu2 = [a for a in alerts2 if a["category"] == "cpu"]
        assert len(cpu2) == 0

        # Third poll should trigger
        alerts3 = server.evaluate_alerts(state)
        cpu3 = [a for a in alerts3 if a["category"] == "cpu"]
        assert len(cpu3) == 1
        assert cpu3[0]["severity"] == "warning"

    def test_cpu_resets_when_drops_below_threshold(self):
        """CPU counter resets if a poll comes in below 95%."""
        high_state = {
            "system": {"cpu_percent": 97, "memory": {"percent": 50}, "disks": {}},
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 0},
        }
        low_state = {
            "system": {"cpu_percent": 40, "memory": {"percent": 50}, "disks": {}},
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 0},
        }

        server.evaluate_alerts(high_state)
        server.evaluate_alerts(high_state)
        # Now a low poll resets
        server.evaluate_alerts(low_state)
        # Next high poll starts from 1 again
        alerts = server.evaluate_alerts(high_state)
        cpu = [a for a in alerts if a["category"] == "cpu"]
        assert len(cpu) == 0  # Only 1 consecutive, not 3

    def test_service_down_after_five_polls(self):
        """Service alert fires after 5 consecutive down polls."""
        state = {
            "system": {"cpu_percent": 10, "memory": {"percent": 50}, "disks": {}},
            "services": {"searxng": {"status": "down"}},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 0},
        }

        for i in range(4):
            alerts = server.evaluate_alerts(state)
            svc = [a for a in alerts if a["category"] == "service"]
            assert len(svc) == 0, f"Should not alert on poll {i+1}"

        # Fifth poll should trigger
        alerts = server.evaluate_alerts(state)
        svc = [a for a in alerts if a["category"] == "service"]
        assert len(svc) == 1
        assert "searxng" in svc[0]["message"]

    def test_redis_queue_depth_warning(self):
        state = {
            "system": {"cpu_percent": 10, "memory": {"percent": 50}, "disks": {}},
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 75},
        }
        alerts = server.evaluate_alerts(state)
        redis_alerts = [a for a in alerts if a["category"] == "redis"]
        assert len(redis_alerts) == 1
        assert redis_alerts[0]["severity"] == "warning"

    def test_redis_queue_depth_critical(self):
        state = {
            "system": {"cpu_percent": 10, "memory": {"percent": 50}, "disks": {}},
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 150},
        }
        alerts = server.evaluate_alerts(state)
        redis_alerts = [a for a in alerts if a["category"] == "redis"]
        assert len(redis_alerts) == 1
        assert redis_alerts[0]["severity"] == "critical"

    def test_scheduler_task_consecutive_failures(self):
        state = {
            "system": {"cpu_percent": 10, "memory": {"percent": 50}, "disks": {}},
            "services": {},
            "gateway": {"status": "ok", "ok": True},
            "scheduler": {
                "status": "ok",
                "tasks": {
                    "dead_mans_switch": {"consecutive_failures": 0},
                    "memory_ingest": {"consecutive_failures": 5},
                },
            },
            "redis": {"ingest_queue_depth": 0},
        }
        alerts = server.evaluate_alerts(state)
        sched_alerts = [a for a in alerts if a["category"] == "scheduler"]
        assert len(sched_alerts) == 1
        assert "memory_ingest" in sched_alerts[0]["message"]
        assert "5 consecutive failures" in sched_alerts[0]["message"]

    def test_gateway_down_counted_as_service(self):
        state = {
            "system": {"cpu_percent": 10, "memory": {"percent": 50}, "disks": {}},
            "services": {},
            "gateway": {"status": "error", "ok": False},
            "scheduler": {"status": "ok", "tasks": {}},
            "redis": {"ingest_queue_depth": 0},
        }
        # Pump 5 polls
        for _ in range(5):
            server.evaluate_alerts(state)
        alerts = server.evaluate_alerts(state)
        svc = [a for a in alerts if a["category"] == "service"]
        gw_alerts = [a for a in svc if "gateway" in a["message"]]
        assert len(gw_alerts) >= 1


# ======================================================================
# Helper function tests
# ======================================================================

class TestHelpers:
    """Test utility/helper functions."""

    def test_bucket_ts_rounds_to_5min(self):
        ts = 1714500123.456  # some arbitrary timestamp
        bucketed = server._bucket_ts(ts)
        assert bucketed % 300 == 0
        assert bucketed <= ts
        assert ts - bucketed < 300

    def test_downsample_single(self):
        rows = [
            (1000.0, 10.0),
            (1100.0, 20.0),
            (1200.0, 30.0),
            (2000.0, 50.0),
        ]
        result = server._downsample_single(rows, "value")
        assert isinstance(result, list)
        # All rows with close timestamps should be bucketed together
        for item in result:
            assert "ts" in item
            assert "value" in item

    def test_downsample_memory(self):
        rows = [
            (1000.0, 50.0, 128.0),
            (1100.0, 55.0, 130.0),
        ]
        result = server._downsample_memory(rows)
        assert len(result) >= 1
        assert "memory_percent" in result[0]
        assert "memory_used_gb" in result[0]


# ======================================================================
# collect_traffic_flow()
# ======================================================================

class TestCollectTrafficFlow:
    """Test the synchronous collect_traffic_flow() function."""

    def setup_method(self):
        # Reset global offsets
        server._log_offset = 0
        server._prev_scheduler_runs = -1
        server._prev_ingest_depth = -1
        server._prev_task_total = -1

    def test_returns_all_flow_keys(self):
        result = server.collect_traffic_flow({}, {}, {}, {})
        expected_keys = {
            "slack", "discord", "signal", "imessage", "email",
            "ollama", "openrouter", "mlx_chat", "tinychat", "openwebui",
            "redis", "postgresql", "memory_server", "scheduler",
        }
        assert set(result.keys()) == expected_keys

    def test_scheduler_running_boosts_flow(self):
        sched = {
            "info": {"total_runs": 100, "tasks_running": 3},
        }
        # First call sets the baseline
        server.collect_traffic_flow(sched, {}, {}, {})
        # Second call with higher run count should show flow
        sched2 = {
            "info": {"total_runs": 110, "tasks_running": 2},
        }
        result = server.collect_traffic_flow(sched2, {}, {}, {})
        assert result["scheduler"] > 0

    def test_flow_values_clamped_to_one(self):
        """All flow values should be between 0 and 1."""
        result = server.collect_traffic_flow(
            {"info": {"total_runs": 99999, "tasks_running": 100}},
            {"ingest_queue_depth": 999},
            {"all_time": {"succeeded": 99999}},
            {"ollama": {"status": "up", "latency_ms": 5000}},
        )
        for key, val in result.items():
            assert 0 <= val <= 1.0, f"{key} = {val} is out of [0, 1] range"
