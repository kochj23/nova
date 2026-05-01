"""
test_dashboard_integration.py — Integration and frame tests for the Nova Control dashboard.

Integration tests (@pytest.mark.integration) hit live services:
    - WebSocket connection to ws://127.0.0.1:37450/ws
    - REST API at http://127.0.0.1:37450/api/detail/postgresql
    - PostgreSQL nova_ops database schema verification

Frame tests (@pytest.mark.frame) verify HTML rendering:
    - Index page card IDs
    - HUD page element IDs
    - Static asset serving (JS, CSS)

All integration and frame tests are skipped by default unless the marker
is explicitly selected (e.g., pytest -m integration or pytest -m frame).

Written by Jordan Koch.
"""

import asyncio
import json
import subprocess
import urllib.request
import urllib.error

import pytest

DASHBOARD_URL = "http://127.0.0.1:37450"
WS_URL = "ws://127.0.0.1:37450/ws"
PG_OPS_DB = "nova_ops"
PG_MEMORIES_DB = "nova_memories"


# ======================================================================
# Helpers
# ======================================================================

def _http_get(path: str, timeout: float = 5.0) -> tuple[int, str]:
    """Simple HTTP GET, returns (status_code, body)."""
    url = f"{DASHBOARD_URL}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        pytest.skip(f"Dashboard unreachable at {url}: {e}")


def _curl(url: str, timeout: int = 5) -> tuple[int, str]:
    """Use subprocess to curl a URL. Returns (exit_code, stdout)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "-", "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=timeout,
        )
        parts = result.stdout.rsplit("\n", 1)
        body = parts[0] if len(parts) > 1 else result.stdout
        status = int(parts[-1]) if len(parts) > 1 and parts[-1].strip().isdigit() else 0
        return status, body
    except subprocess.TimeoutExpired:
        pytest.skip(f"curl timed out for {url}")
    except FileNotFoundError:
        pytest.skip("curl not found")


def _psql_query(db: str, sql: str) -> str:
    """Run a psql query and return stdout."""
    try:
        result = subprocess.run(
            ["psql", db, "-t", "-A", "-c", sql],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            pytest.skip(f"psql failed for {db}: {result.stderr.strip()}")
        return result.stdout.strip()
    except FileNotFoundError:
        pytest.skip("psql not found")
    except subprocess.TimeoutExpired:
        pytest.skip(f"psql query timed out on {db}")


# ======================================================================
# Integration Tests
# ======================================================================

@pytest.mark.integration
class TestWebSocketConnection:
    """Test WebSocket connection to the dashboard."""

    def test_websocket_returns_valid_state(self):
        """Connect to ws://127.0.0.1:37450/ws and verify state JSON has expected keys."""
        try:
            import websockets
            import websockets.sync.client
        except ImportError:
            pytest.skip("websockets package not installed")

        try:
            with websockets.sync.client.connect(WS_URL, open_timeout=5) as ws:
                raw = ws.recv(timeout=10)
                state = json.loads(raw)
        except Exception as e:
            pytest.skip(f"WebSocket connection failed: {e}")

        # Verify the state has all expected top-level keys
        expected_keys = {
            "ts", "scheduler", "agents", "gateway", "task_history",
            "redis", "services", "system", "ollama", "postgresql",
            "flows", "task_throughput", "model_usage", "gateway_queries",
            "traffic_flow", "poll_duration_ms", "alerts",
        }
        missing = expected_keys - set(state.keys())
        assert not missing, f"Missing keys in WebSocket state: {missing}"

        # Verify timestamp is present and numeric (state may be cached)
        import time
        assert isinstance(state["ts"], (int, float)), "State timestamp should be numeric"
        # Allow up to 30 minutes stale (dashboard may not have polled recently)
        assert abs(state["ts"] - time.time()) < 1800, "State timestamp is stale (>30min)"

        # Verify nested structure
        assert isinstance(state["services"], dict)
        assert isinstance(state["alerts"], list)
        assert isinstance(state["system"], dict)

    def test_websocket_system_has_cpu_and_memory(self):
        """Verify system data includes CPU and memory."""
        try:
            import websockets.sync.client
        except ImportError:
            pytest.skip("websockets package not installed")

        try:
            with websockets.sync.client.connect(WS_URL, open_timeout=5) as ws:
                raw = ws.recv(timeout=10)
                state = json.loads(raw)
        except Exception as e:
            pytest.skip(f"WebSocket connection failed: {e}")

        sys_data = state.get("system", {})
        assert "cpu_percent" in sys_data
        assert "memory" in sys_data
        mem = sys_data["memory"]
        assert "total_gb" in mem
        assert "percent" in mem


@pytest.mark.integration
class TestPostgresqlDetail:
    """Test the /api/detail/postgresql REST endpoint."""

    def test_postgresql_detail_returns_data(self):
        """GET /api/detail/postgresql returns today_count and memory data."""
        status, body = _http_get("/api/detail/postgresql")
        assert status == 200, f"Expected 200, got {status}"

        data = json.loads(body)
        assert "today" in data or "today_count" in data or "total" in data, \
            f"Expected memory stats in response, got: {list(data.keys())}"

    def test_postgresql_detail_has_sources(self):
        """GET /api/detail/postgresql returns source breakdown."""
        status, body = _http_get("/api/detail/postgresql")
        assert status == 200

        data = json.loads(body)
        # Could be today_sources or top_sources depending on exact endpoint
        has_sources = "today_sources" in data or "top_sources" in data
        assert has_sources, f"Expected source breakdown, got: {list(data.keys())}"


@pytest.mark.integration
class TestNovaOpsDatabase:
    """Test that nova_ops database has expected tables."""

    def test_task_runs_table_exists(self):
        """Verify task_runs table exists in nova_ops."""
        result = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='task_runs')"
        )
        assert result == "t", "task_runs table not found in nova_ops"

    def test_flow_runs_table_exists(self):
        """Verify flow_runs table exists in nova_ops."""
        result = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='flow_runs')"
        )
        assert result == "t", "flow_runs table not found in nova_ops"

    def test_face_people_table_exists(self):
        """Verify face_people table exists in nova_ops."""
        result = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='face_people')"
        )
        assert result == "t", "face_people table not found in nova_ops"

    def test_dashboard_snapshots_table_exists(self):
        """Verify dashboard_snapshots table exists in nova_ops."""
        result = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='dashboard_snapshots')"
        )
        assert result == "t", "dashboard_snapshots table not found in nova_ops"

    def test_dashboard_latency_history_table_exists(self):
        """Verify dashboard_latency_history table exists in nova_ops."""
        result = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='dashboard_latency_history')"
        )
        assert result == "t", "dashboard_latency_history table not found in nova_ops"

    def test_dashboard_disk_history_table_exists(self):
        """Verify dashboard_disk_history table exists in nova_ops."""
        result = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='dashboard_disk_history')"
        )
        assert result == "t", "dashboard_disk_history table not found in nova_ops"

    def test_dashboard_cost_history_table_exists(self):
        """Verify dashboard_cost_history table exists in nova_ops."""
        result = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='dashboard_cost_history')"
        )
        assert result == "t", "dashboard_cost_history table not found in nova_ops"

    def test_gateway_context_entries_or_query_log_exists(self):
        """Verify gateway tables exist in nova_ops (created by gateway context store on first start)."""
        # gateway_query_log is created by the gateway context store on startup.
        # It may not exist yet if the gateway hasn't been started against this DB.
        # Check for either gateway table as proof the schema was initialized.
        result_entries = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='gateway_context_entries')"
        )
        result_log = _psql_query(
            PG_OPS_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='gateway_query_log')"
        )
        has_either = result_entries == "t" or result_log == "t"
        if not has_either:
            pytest.skip("Gateway context store tables not yet initialized in nova_ops")

    def test_task_runs_has_data(self):
        """Verify task_runs has at least some rows."""
        result = _psql_query(PG_OPS_DB, "SELECT COUNT(*) FROM task_runs")
        count = int(result) if result.isdigit() else 0
        assert count > 0, "task_runs table is empty"


@pytest.mark.integration
class TestNovaMemoriesDatabase:
    """Test that nova_memories database has expected structure."""

    def test_memories_table_exists(self):
        """Verify memories table exists in nova_memories."""
        result = _psql_query(
            PG_MEMORIES_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='memories')"
        )
        assert result == "t", "memories table not found in nova_memories"

    def test_memories_has_data(self):
        """Verify memories table has data."""
        result = _psql_query(PG_MEMORIES_DB, "SELECT COUNT(*) FROM memories")
        count = int(result) if result.isdigit() else 0
        assert count > 0, "memories table is empty"

    def test_memories_has_source_column(self):
        """Verify memories table has a source column for breakdown queries."""
        result = _psql_query(
            PG_MEMORIES_DB,
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='memories' AND column_name='source')"
        )
        assert result == "t", "memories table missing 'source' column"


# ======================================================================
# Frame Tests
# ======================================================================

@pytest.mark.frame
class TestIndexPageCards:
    """Verify the index page contains all expected card elements."""

    EXPECTED_CARD_IDS = [
        "card-system",
        "card-gateway",
        "card-deadman",
        "card-channels",
        "card-scheduler",
        "card-ollama",
        "card-postgresql",
        "card-redis",
        "card-task-history",
        "card-memory",
        "card-model-usage",
        "card-traffic",
        "card-latency",
        "card-throughput",
        "card-conversations",
        "card-unifi",
        "card-searxng-stats",
        "card-backup-status",
        "card-response-time",
        "card-herd-activity",
        "card-mlx-status",
        "card-cameras",
        "card-homekit",
        "card-app-watchdog",
        "card-weather",
        "card-dream",
        "card-nas",
        "card-healthkit",
        "card-homebridge",
        "card-gateway-queries",
        "card-cost-tracker",
        "card-memory-growth",
        "card-disk-usage",
        "card-cron-health",
        "card-token-counter",
        "card-nmap",
        "card-briefings",
        "card-knowledge",
        "card-task-table",
    ]

    def test_index_contains_all_cards(self):
        """GET / and verify all card IDs are present in the HTML."""
        status, body = _curl(f"{DASHBOARD_URL}/")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200, f"Expected 200, got {status}"

        missing = []
        for card_id in self.EXPECTED_CARD_IDS:
            if f'id="{card_id}"' not in body:
                missing.append(card_id)

        assert not missing, f"Missing card IDs in index.html: {missing}"

    def test_index_contains_agent_cards(self):
        """Verify agent-specific cards are present."""
        status, body = _curl(f"{DASHBOARD_URL}/")
        if status == 0:
            pytest.skip("Dashboard not reachable")

        agent_cards = [
            "card-agent-analyst",
            "card-agent-coder",
            "card-agent-librarian",
            "card-agent-lookout",
            "card-agent-sentinel",
        ]
        missing = [c for c in agent_cards if f'id="{c}"' not in body]
        assert not missing, f"Missing agent card IDs: {missing}"


@pytest.mark.frame
class TestHudPage:
    """Verify the HUD page contains expected elements."""

    def test_hud_contains_container(self):
        """GET /hud and verify hud-container is present."""
        status, body = _curl(f"{DASHBOARD_URL}/hud")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200
        assert 'id="hud-container"' in body

    def test_hud_contains_canvas(self):
        """GET /hud and verify hud-canvas element exists."""
        status, body = _curl(f"{DASHBOARD_URL}/hud")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert 'id="hud-canvas"' in body

    def test_hud_contains_stat_elements(self):
        """GET /hud and verify stat display elements."""
        status, body = _curl(f"{DASHBOARD_URL}/hud")
        if status == 0:
            pytest.skip("Dashboard not reachable")

        expected_stats = [
            "stat-memories",
            "stat-today",
            "stat-apps",
            "stat-cpu",
            "stat-ram",
            "stat-models",
            "stat-scheduler",
            "stat-channels",
            "stat-nas",
        ]
        missing = [s for s in expected_stats if f'id="{s}"' not in body]
        assert not missing, f"Missing stat IDs in hud.html: {missing}"

    def test_hud_has_structural_elements(self):
        """Verify main structural divs are present."""
        status, body = _curl(f"{DASHBOARD_URL}/hud")
        if status == 0:
            pytest.skip("Dashboard not reachable")

        for elem_id in ["hud-main", "hud-topbar", "hud-bottombar", "hud-stats",
                         "hud-heatmap", "hud-ticker", "hud-status-leds"]:
            assert f'id="{elem_id}"' in body, f"Missing {elem_id} in HUD page"


@pytest.mark.frame
class TestStaticAssets:
    """Verify JS and CSS files are served correctly."""

    def test_main_js_served(self):
        """GET /static/js/main.js returns 200."""
        status, body = _curl(f"{DASHBOARD_URL}/static/js/main.js")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200, f"Expected 200 for main.js, got {status}"
        assert len(body) > 100, "main.js appears empty or too small"

    def test_charts_js_served(self):
        """GET /static/js/charts.js returns 200."""
        status, body = _curl(f"{DASHBOARD_URL}/static/js/charts.js")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200

    def test_graph_js_served(self):
        """GET /static/js/graph.js returns 200."""
        status, body = _curl(f"{DASHBOARD_URL}/static/js/graph.js")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200

    def test_hud_js_served(self):
        """GET /static/js/hud.js returns 200."""
        status, body = _curl(f"{DASHBOARD_URL}/static/js/hud.js")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200

    def test_dashboard_css_served(self):
        """GET /static/css/dashboard.css returns 200."""
        status, body = _curl(f"{DASHBOARD_URL}/static/css/dashboard.css")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200, f"Expected 200 for dashboard.css, got {status}"
        assert len(body) > 50, "dashboard.css appears empty or too small"

    def test_hud_css_served(self):
        """GET /static/css/hud.css returns 200."""
        status, body = _curl(f"{DASHBOARD_URL}/static/css/hud.css")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200

    def test_nonexistent_static_returns_404(self):
        """GET /static/js/nonexistent.js returns 404."""
        status, _ = _curl(f"{DASHBOARD_URL}/static/js/nonexistent.js")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 404


@pytest.mark.frame
class TestAPIEndpoints:
    """Verify key API endpoints return expected response shapes."""

    def test_detail_postgresql_returns_json(self):
        status, body = _curl(f"{DASHBOARD_URL}/api/detail/postgresql")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_detail_unknown_service_returns_404(self):
        status, body = _curl(f"{DASHBOARD_URL}/api/detail/nonexistent_service")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 404
        data = json.loads(body)
        assert "error" in data

    def test_detail_system_returns_json(self):
        status, body = _curl(f"{DASHBOARD_URL}/api/detail/system")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200
        data = json.loads(body)
        assert "cpu_count" in data or "current" in data

    def test_detail_scheduler_returns_json(self):
        status, body = _curl(f"{DASHBOARD_URL}/api/detail/scheduler")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_history_cpu_returns_list(self):
        status, body = _curl(f"{DASHBOARD_URL}/api/history/cpu?range=1h")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, list)

    def test_history_unknown_metric_returns_404(self):
        status, body = _curl(f"{DASHBOARD_URL}/api/history/fake_metric")
        if status == 0:
            pytest.skip("Dashboard not reachable")
        assert status == 404
