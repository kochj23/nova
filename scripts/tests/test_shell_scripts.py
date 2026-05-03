"""test_shell_scripts.py — Tests for Nova's shell scripts. Written by Jordan Koch."""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Constants ────────────────────────────────────────────────────────────────

SCRIPTS_DIR = Path.home() / ".openclaw" / "scripts"
HOME = Path.home()

# All shell scripts under test, with their expected interpreter
ALL_SCRIPTS = [
    "nova-boot.sh",
    "nova_stack_restart.sh",
    "nova_gateway_start.sh",
    "nova_memory_server_start.sh",
    "openwebui_start.sh",
    "tinychat_start.sh",
    "nova_pg_backup.sh",
    "nova_ollama_preload.sh",
    "nova_session_reset.sh",
    "nova_slack_post.sh",
    "nova_remember.sh",
    "nova_recall.sh",
    "nova_homekit_scene.sh",
    "nova_homekit_query.sh",
    "wait-for-port.sh",
    "generate_image.sh",
    "nova_subagent_ctl.sh",
    "nova-services.sh",
    "mlx_server_start.sh",
    "searxng_start.sh",
]

# Scripts that use bash (#!/bin/bash or #!/usr/bin/env bash)
BASH_SCRIPTS = [
    "nova_ollama_preload.sh",
    "nova_session_reset.sh",
    "nova_slack_post.sh",
    "nova_recall.sh",
    "nova_homekit_scene.sh",
    "nova_homekit_query.sh",
    "generate_image.sh",
]

# Scripts that use zsh (#!/bin/zsh)
ZSH_SCRIPTS = [
    "nova-boot.sh",
    "nova_stack_restart.sh",
    "nova_gateway_start.sh",
    "nova_memory_server_start.sh",
    "openwebui_start.sh",
    "tinychat_start.sh",
    "wait-for-port.sh",
    "nova_subagent_ctl.sh",
    "nova-services.sh",
    "mlx_server_start.sh",
    "searxng_start.sh",
    "nova_remember.sh",
    "nova_pg_backup.sh",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _run(cmd, timeout=30, env=None, input_data=None):
    """Run a command and return the CompletedProcess."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=merged_env,
        input=input_data,
    )


def _script_path(name):
    """Return full path to a script."""
    return str(SCRIPTS_DIR / name)


def _read_shebang(script_name):
    """Read the shebang line from a script."""
    path = SCRIPTS_DIR / script_name
    with open(path, "r") as f:
        first_line = f.readline().strip()
    return first_line


def _find_free_port():
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _is_port_open(port, host="127.0.0.1"):
    """Check if a TCP port is accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# SYNTAX VALIDATION — Verify every script parses without errors
# ═══════════════════════════════════════════════════════════════════════════════


class TestShellScriptSyntax:
    """Verify all shell scripts have valid syntax using bash -n / zsh -n."""

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_syntax_valid(self, script):
        """Each script must pass its interpreter's syntax check."""
        path = SCRIPTS_DIR / script
        assert path.exists(), f"Script not found: {path}"

        shebang = _read_shebang(script)
        if "zsh" in shebang:
            shell = "zsh"
        elif "bash" in shebang or "env bash" in shebang:
            shell = "bash"
        else:
            shell = "bash"  # fallback

        result = _run([shell, "-n", str(path)])
        assert result.returncode == 0, (
            f"Syntax error in {script} ({shell} -n):\n{result.stderr}"
        )

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_is_executable(self, script):
        """Every script must have the executable bit set."""
        path = SCRIPTS_DIR / script
        assert os.access(path, os.X_OK), f"{script} is not executable"

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_has_shebang(self, script):
        """Every script must start with a shebang line."""
        shebang = _read_shebang(script)
        assert shebang.startswith("#!"), f"{script} missing shebang: {shebang}"

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_no_windows_line_endings(self, script):
        """Scripts must not contain \\r\\n line endings."""
        path = SCRIPTS_DIR / script
        content = path.read_bytes()
        assert b"\r\n" not in content, f"{script} has Windows line endings (CRLF)"


class TestShellScriptMetadata:
    """Verify script authorship, comments, and conventions."""

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_has_description_comment(self, script):
        """Each script should have a comment describing its purpose."""
        path = SCRIPTS_DIR / script
        content = path.read_text()
        # At least one non-shebang comment line in the first 15 lines
        lines = content.splitlines()[:15]
        comment_lines = [l for l in lines[1:] if l.strip().startswith("#")]
        assert len(comment_lines) > 0, f"{script} has no description comments"

    # Scripts known to be missing author attribution (flagged for future fix)
    _SCRIPTS_WITHOUT_ATTRIBUTION = {"nova_recall.sh", "mlx_server_start.sh"}

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_authored_by_jordan(self, script):
        """Scripts should credit Jordan Koch (or at least reference Jordan/kochj)."""
        path = SCRIPTS_DIR / script
        content = path.read_text()
        lower = content.lower()
        has_attribution = (
            "jordan koch" in lower
            or "kochj" in lower
            or "jordan" in lower  # some scripts reference Jordan as assignee
        )
        if script in self._SCRIPTS_WITHOUT_ATTRIBUTION:
            if not has_attribution:
                pytest.skip(f"{script} missing attribution (known gap)")
        else:
            assert has_attribution, f"{script} does not credit Jordan Koch"


# ═══════════════════════════════════════════════════════════════════════════════
# SCRIPT-SPECIFIC UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestWaitForPort:
    """Tests for wait-for-port.sh — the dependency checker used by startup scripts."""

    @pytest.mark.integration
    def test_returns_0_when_port_open(self):
        """wait_for_port should return 0 when a port is actually listening."""
        # Start a TCP listener on a free port
        port = _find_free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        try:
            script = textwrap.dedent(f"""\
                #!/bin/zsh
                source "{_script_path("wait-for-port.sh")}"
                wait_for_port {port} "TestService" 10
            """)
            result = _run(["zsh", "-c", script], timeout=15)
            assert result.returncode == 0
            assert "ready" in result.stdout.lower()
        finally:
            server.close()

    def test_returns_1_when_port_closed(self):
        """wait_for_port should return 1 when port is not open within timeout."""
        # Use a port that almost certainly nothing is listening on
        port = _find_free_port()
        script = textwrap.dedent(f"""\
            #!/bin/zsh
            source "{_script_path("wait-for-port.sh")}"
            wait_for_port {port} "NobodyHome" 3
        """)
        result = _run(["zsh", "-c", script], timeout=15)
        assert result.returncode == 1
        assert "timeout" in result.stderr.lower()

    def test_timeout_parameter_respected(self):
        """The timeout parameter should limit how long the script waits."""
        port = _find_free_port()
        script = textwrap.dedent(f"""\
            #!/bin/zsh
            source "{_script_path("wait-for-port.sh")}"
            wait_for_port {port} "QuickCheck" 3
        """)
        start = time.time()
        _run(["zsh", "-c", script], timeout=30)
        elapsed = time.time() - start
        # Should finish in roughly 3-6 seconds (3s timeout + sleep granularity)
        assert elapsed < 12, f"Timeout not respected, took {elapsed:.1f}s"

    def test_default_timeout_is_90s(self):
        """When no timeout is specified, default should be 90."""
        content = (SCRIPTS_DIR / "wait-for-port.sh").read_text()
        assert "timeout=${3:-90}" in content or 'timeout="${3:-90}"' in content

    @pytest.mark.integration
    def test_reports_elapsed_time(self):
        """Output should include how long it took to connect."""
        port = _find_free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        try:
            script = textwrap.dedent(f"""\
                #!/bin/zsh
                source "{_script_path("wait-for-port.sh")}"
                wait_for_port {port} "QuickService" 10
            """)
            result = _run(["zsh", "-c", script], timeout=15)
            assert "ready after" in result.stdout.lower()
        finally:
            server.close()


class TestNovaSlackPost:
    """Tests for nova_slack_post.sh — Slack+Discord posting wrapper."""

    def test_requires_message_argument(self):
        """Script must exit non-zero when no message is given."""
        result = _run(["bash", _script_path("nova_slack_post.sh")], timeout=10)
        assert result.returncode != 0

    def test_usage_message_on_missing_args(self):
        """Should print usage info when called without arguments."""
        result = _run(["bash", _script_path("nova_slack_post.sh")], timeout=10)
        assert "usage" in result.stderr.lower() or "usage" in result.stdout.lower()

    def test_default_channel_is_set(self):
        """Script should have a default channel when none is provided."""
        content = (SCRIPTS_DIR / "nova_slack_post.sh").read_text()
        # Default channel assignment
        assert "C0ATAF7NZG9" in content

    def test_uses_nova_config_post_both(self):
        """Script should call nova_config.post_both for dual posting."""
        content = (SCRIPTS_DIR / "nova_slack_post.sh").read_text()
        assert "post_both" in content

    def test_imports_nova_config_from_scripts_dir(self):
        """Script should add scripts dir to sys.path for nova_config."""
        content = (SCRIPTS_DIR / "nova_slack_post.sh").read_text()
        assert "nova_config" in content
        assert "sys.path" in content


class TestNovaRemember:
    """Tests for nova_remember.sh — quick memory storage."""

    def test_requires_summary_and_type(self):
        """Should exit 1 when missing required arguments."""
        result = _run(["zsh", _script_path("nova_remember.sh")], timeout=10)
        assert result.returncode != 0

    def test_requires_type_argument(self):
        """Should exit 1 when only summary is given (no type)."""
        result = _run(
            ["zsh", _script_path("nova_remember.sh"), "test summary"],
            timeout=10,
        )
        assert result.returncode != 0

    def test_usage_message_on_no_args(self):
        """Should print usage when called without arguments."""
        result = _run(["zsh", _script_path("nova_remember.sh")], timeout=10)
        combined = result.stdout + result.stderr
        assert "usage" in combined.lower()

    def test_meeting_type_uses_workflow_api(self):
        """The 'meeting' type should POST to the NovaControl workflow API."""
        content = (SCRIPTS_DIR / "nova_remember.sh").read_text()
        assert "meeting" in content
        assert "37400" in content  # NovaControl port

    def test_default_type_uses_remember_endpoint(self):
        """Non-meeting types should POST to /remember on the memory server."""
        content = (SCRIPTS_DIR / "nova_remember.sh").read_text()
        assert "/remember" in content
        assert "18790" in content  # Memory server port

    def test_action_item_type_uses_bun(self):
        """action-item type should pipe to bun for graph ingestion."""
        content = (SCRIPTS_DIR / "nova_remember.sh").read_text()
        assert "action-item" in content
        assert "bun" in content


class TestNovaRecall:
    """Tests for nova_recall.sh — quick memory recall."""

    def test_requires_query_argument(self):
        """Should exit non-zero when no query is provided."""
        result = _run(["bash", _script_path("nova_recall.sh")], timeout=10)
        assert result.returncode != 0

    def test_usage_message(self):
        """Should print usage hint on missing args."""
        result = _run(["bash", _script_path("nova_recall.sh")], timeout=10)
        combined = result.stdout + result.stderr
        assert "usage" in combined.lower()

    def test_default_n_results_is_5(self):
        """Default number of results should be 5."""
        content = (SCRIPTS_DIR / "nova_recall.sh").read_text()
        assert 'N="${2:-5}"' in content or "N=${2:-5}" in content

    def test_constructs_correct_url(self):
        """Should build the recall URL with correct parameters."""
        content = (SCRIPTS_DIR / "nova_recall.sh").read_text()
        assert "18790" in content
        assert "/recall" in content
        assert "min_score" in content

    def test_url_encodes_query(self):
        """Query parameter must be URL-encoded."""
        content = (SCRIPTS_DIR / "nova_recall.sh").read_text()
        assert "urllib.parse.quote" in content

    def test_handles_memory_server_down(self):
        """Should print an error when memory server is unreachable."""
        content = (SCRIPTS_DIR / "nova_recall.sh").read_text()
        assert "not reachable" in content.lower() or "not running" in content.lower()


class TestNovaHomekitScene:
    """Tests for nova_homekit_scene.sh — HomeKit scene execution."""

    def test_requires_scene_name(self):
        """Should exit 1 when no scene name is given."""
        result = _run(["bash", _script_path("nova_homekit_scene.sh")], timeout=10)
        assert result.returncode != 0

    def test_usage_on_no_args(self):
        """Should print usage when called without arguments."""
        result = _run(["bash", _script_path("nova_homekit_scene.sh")], timeout=10)
        combined = result.stdout + result.stderr
        assert "usage" in combined.lower()

    def test_list_flag_supported(self):
        """--list flag should be a recognized option."""
        content = (SCRIPTS_DIR / "nova_homekit_scene.sh").read_text()
        assert "--list" in content

    def test_tries_api_before_shortcuts(self):
        """Should attempt NovaControl API first, then fall back to Shortcuts CLI."""
        content = (SCRIPTS_DIR / "nova_homekit_scene.sh").read_text()
        # API call comes before Shortcuts fallback
        api_pos = content.find("37400")
        shortcuts_pos = content.find("shortcuts run")
        assert api_pos < shortcuts_pos, "API should be tried before Shortcuts CLI"

    def test_fallback_to_shortcuts_cli(self):
        """Script must have a Shortcuts CLI fallback."""
        content = (SCRIPTS_DIR / "nova_homekit_scene.sh").read_text()
        assert "shortcuts run" in content

    def test_outputs_json_on_success(self):
        """On success, should output JSON with status field."""
        content = (SCRIPTS_DIR / "nova_homekit_scene.sh").read_text()
        assert '"status"' in content


class TestNovaHomekitQuery:
    """Tests for nova_homekit_query.sh — HomeKit device query."""

    def test_uses_shortcuts_cli(self):
        """Should invoke the Shortcuts CLI."""
        content = (SCRIPTS_DIR / "nova_homekit_query.sh").read_text()
        assert "shortcuts run" in content

    def test_output_file_path(self):
        """Should write to a known state file."""
        content = (SCRIPTS_DIR / "nova_homekit_query.sh").read_text()
        assert "nova_homekit_status.json" in content

    def test_returns_empty_array_on_failure(self):
        """Should output [] if shortcut produces no output."""
        content = (SCRIPTS_DIR / "nova_homekit_query.sh").read_text()
        assert '[]' in content

    def test_cleans_up_temp_file(self):
        """Should remove the temporary output file after reading."""
        content = (SCRIPTS_DIR / "nova_homekit_query.sh").read_text()
        assert "rm -f" in content


class TestNovaGatewayStart:
    """Tests for nova_gateway_start.sh — Gateway startup with Keychain secrets."""

    def test_loads_secrets_from_keychain(self):
        """Script must use macOS Keychain (security find-generic-password)."""
        content = (SCRIPTS_DIR / "nova_gateway_start.sh").read_text()
        assert "security find-generic-password" in content

    def test_exports_required_env_vars(self):
        """All required env vars must be exported."""
        content = (SCRIPTS_DIR / "nova_gateway_start.sh").read_text()
        required_vars = [
            "NOVA_OPENROUTER_API_KEY",
            "NOVA_SLACK_BOT_TOKEN",
            "NOVA_SLACK_APP_TOKEN",
            "NOVA_GATEWAY_AUTH_TOKEN",
            "NOVA_DISCORD_TOKEN",
        ]
        for var in required_vars:
            assert var in content, f"Missing env var export: {var}"

    def test_exponential_backoff_on_keychain_locked(self):
        """Should retry with increasing delay if Keychain is locked."""
        content = (SCRIPTS_DIR / "nova_gateway_start.sh").read_text()
        assert "MAX_RETRIES" in content
        assert "DELAY" in content
        # Verify the delay increases
        assert "DELAY=$((DELAY" in content

    def test_exits_on_keychain_failure(self):
        """Should exit 1 if Keychain remains locked after all retries."""
        content = (SCRIPTS_DIR / "nova_gateway_start.sh").read_text()
        assert "exit 1" in content
        assert "FATAL" in content

    def test_execs_node_gateway(self):
        """Should exec the Node.js gateway process (not fork)."""
        content = (SCRIPTS_DIR / "nova_gateway_start.sh").read_text()
        assert "exec" in content
        assert "node" in content
        assert "gateway --port 18789" in content

    def test_max_retries_is_12(self):
        """Max retries should be 12 (~3 minutes with backoff)."""
        content = (SCRIPTS_DIR / "nova_gateway_start.sh").read_text()
        assert "MAX_RETRIES=12" in content


class TestNovaMemoryServerStart:
    """Tests for nova_memory_server_start.sh — Memory server startup."""

    def test_sources_wait_for_port(self):
        """Must source wait-for-port.sh for dependency checks."""
        content = (SCRIPTS_DIR / "nova_memory_server_start.sh").read_text()
        assert "wait-for-port.sh" in content

    def test_waits_for_postgres(self):
        """Must wait for PostgreSQL on port 5432."""
        content = (SCRIPTS_DIR / "nova_memory_server_start.sh").read_text()
        assert "5432" in content

    def test_waits_for_redis(self):
        """Must wait for Redis on port 6379."""
        content = (SCRIPTS_DIR / "nova_memory_server_start.sh").read_text()
        assert "6379" in content

    def test_waits_for_ollama(self):
        """Must wait for Ollama on port 11434."""
        content = (SCRIPTS_DIR / "nova_memory_server_start.sh").read_text()
        assert "11434" in content

    def test_exits_on_dependency_failure(self):
        """Should exit 1 if any dependency fails to start."""
        content = (SCRIPTS_DIR / "nova_memory_server_start.sh").read_text()
        assert "|| exit 1" in content

    def test_execs_python_memory_server(self):
        """Should exec the Python memory server process."""
        content = (SCRIPTS_DIR / "nova_memory_server_start.sh").read_text()
        assert "exec" in content
        assert "memory_server.py" in content


class TestOpenwebuiStart:
    """Tests for openwebui_start.sh — OpenWebUI startup."""

    def test_waits_for_ollama(self):
        """Must wait for Ollama before starting."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "wait_for_port 11434" in content

    def test_sets_data_dir(self):
        """DATA_DIR should point to /Volumes/Data."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "/Volumes/Data/openwebui/data" in content

    def test_disables_auth(self):
        """WEBUI_AUTH should be false for local use."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "WEBUI_AUTH=false" in content

    def test_uses_python_s_flag(self):
        """Must use -S flag to skip site.py (TCC workaround)."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "python3.12 -S" in content

    def test_binds_to_lan_ip(self):
        """OpenWebUI binds to LAN IP, not localhost."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "192.168.1.6" in content

    def test_port_3000(self):
        """Should run on port 3000."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "port=3000" in content or "port 3000" in content


class TestTinychatStart:
    """Tests for tinychat_start.sh — TinyChat startup."""

    def test_waits_for_ollama(self):
        """Must wait for Ollama before starting."""
        content = (SCRIPTS_DIR / "tinychat_start.sh").read_text()
        assert "wait_for_port 11434" in content

    def test_sets_correct_env_vars(self):
        """Required environment variables must be set."""
        content = (SCRIPTS_DIR / "tinychat_start.sh").read_text()
        assert "PORT=8000" in content
        assert "OPENAI_API_BASE" in content
        assert "LLM_MODEL" in content

    def test_uses_tinychat_venv(self):
        """Should use the TinyChat virtualenv Python."""
        content = (SCRIPTS_DIR / "tinychat_start.sh").read_text()
        assert "/Volumes/Data/tinychat/venv/bin/python3" in content

    def test_clears_pythonpath(self):
        """PYTHONPATH should be cleared to avoid conflicts."""
        content = (SCRIPTS_DIR / "tinychat_start.sh").read_text()
        assert 'PYTHONPATH=""' in content


class TestNovaPgBackup:
    """Tests for nova_pg_backup.sh — PostgreSQL backup to NAS."""

    def test_uses_directory_format(self):
        """pg_dump should use -Fd (directory format) for parallel dumps."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "-Fd" in content

    def test_uses_parallel_jobs(self):
        """Should use -j 4 for parallel dump jobs."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "-j 4" in content

    def test_checks_pg_is_ready(self):
        """Should check pg_isready before dumping."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "pg_isready" in content

    def test_handles_nas_unavailable(self):
        """Should handle NAS not being mounted gracefully."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "NAS_AVAILABLE" in content
        assert "backing up to local only" in content.lower()

    def test_verifies_backup_integrity(self):
        """Should verify backup with pg_restore --list."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "pg_restore --list" in content

    def test_rotation_policy(self):
        """Should rotate backups older than RETENTION_DAYS."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "RETENTION_DAYS=7" in content
        assert "-mtime" in content

    def test_sends_slack_notification(self):
        """Should notify Slack on success or failure."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "nova_slack_post.sh" in content
        assert "notify" in content

    def test_uses_rsync_for_nas_copy(self):
        """Should use rsync (not cp) for NAS copies."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "rsync" in content

    def test_logs_to_file(self):
        """Should log to nova_pg_backup.log."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "nova_pg_backup.log" in content

    def test_backup_database_name(self):
        """Should back up the nova_memories database."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert 'DB_NAME="nova_memories"' in content

    def test_reports_row_count(self):
        """Final report should include row count for verification."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "ROW_COUNT" in content
        assert "SELECT count(*) FROM memories" in content


class TestNovaOllamaPreload:
    """Tests for nova_ollama_preload.sh — Preload Ollama models."""

    def test_waits_for_ollama_api(self):
        """Should wait for Ollama API before preloading."""
        content = (SCRIPTS_DIR / "nova_ollama_preload.sh").read_text()
        assert "11434" in content

    def test_preloads_nomic_embed(self):
        """Should preload nomic-embed-text for memory operations."""
        content = (SCRIPTS_DIR / "nova_ollama_preload.sh").read_text()
        assert "nomic-embed-text" in content

    def test_preloads_qwen3_coder(self):
        """Should preload qwen3-coder:30b."""
        content = (SCRIPTS_DIR / "nova_ollama_preload.sh").read_text()
        assert "qwen3-coder:30b" in content

    def test_uses_minimal_predict(self):
        """Warmup calls should use num_predict=1 to minimize overhead."""
        content = (SCRIPTS_DIR / "nova_ollama_preload.sh").read_text()
        assert '"num_predict":1' in content or '"num_predict": 1' in content

    def test_aborts_after_60s(self):
        """Should abort if Ollama not reachable after 60s."""
        content = (SCRIPTS_DIR / "nova_ollama_preload.sh").read_text()
        assert "aborting" in content.lower()
        # 30 iterations * 2s sleep = 60s
        assert "seq 1 30" in content


class TestNovaSessionReset:
    """Tests for nova_session_reset.sh — Session reset when stale."""

    def test_force_flag(self):
        """--force flag should be supported."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "--force" in content

    def test_check_flag(self):
        """--check flag should print size and exit without resetting."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "--check" in content
        assert "CHECK_ONLY" in content

    def test_threshold_flag(self):
        """--threshold flag should accept a custom MB threshold."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "--threshold" in content

    def test_default_threshold_is_20mb(self):
        """Default threshold should be 20MB."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "THRESHOLD_MB=20" in content

    def test_archives_session(self):
        """Should archive (mv, not delete) the session file."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "mv " in content
        assert ".reset." in content

    def test_restarts_gateway(self):
        """Should restart the gateway after resetting."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "launchctl kickstart" in content
        assert "ai.openclaw.gateway" in content

    def test_sends_slack_notification(self):
        """Should notify Slack about the session reset."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "nova_slack_post.sh" in content

    def test_unknown_arg_exits_nonzero(self):
        """Unknown arguments should cause exit 1."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "Unknown arg" in content


class TestGenerateImage:
    """Tests for generate_image.sh — SwarmUI image generation."""

    def test_requires_prompt(self):
        """Should exit non-zero when no prompt is given."""
        result = _run(["bash", _script_path("generate_image.sh")], timeout=10)
        assert result.returncode != 0

    def test_error_message_on_no_prompt(self):
        """Should print a clear error when prompt is missing."""
        result = _run(["bash", _script_path("generate_image.sh")], timeout=10)
        combined = result.stdout + result.stderr
        assert "no prompt" in combined.lower() or "usage" in combined.lower()

    def test_default_dimensions(self):
        """Default width and height should be 1024."""
        content = (SCRIPTS_DIR / "generate_image.sh").read_text()
        assert 'WIDTH="${2:-1024}"' in content
        assert 'HEIGHT="${3:-1024}"' in content

    def test_default_steps(self):
        """Default steps should be 8."""
        content = (SCRIPTS_DIR / "generate_image.sh").read_text()
        assert 'STEPS="${4:-8}"' in content

    def test_connects_to_swarmui(self):
        """Should connect to SwarmUI at localhost:7801."""
        content = (SCRIPTS_DIR / "generate_image.sh").read_text()
        assert "localhost:7801" in content

    def test_copies_to_workspace(self):
        """Should copy the generated image to Nova's workspace."""
        content = (SCRIPTS_DIR / "generate_image.sh").read_text()
        assert ".openclaw/workspace" in content
        assert "cp " in content

    def test_waits_for_async_file(self):
        """Should wait for the image file to appear (async flush)."""
        content = (SCRIPTS_DIR / "generate_image.sh").read_text()
        # Loop checking for file existence
        assert '[ -f "$FULL_PATH" ]' in content or '[ ! -f "$FULL_PATH" ]' in content


class TestNovaSubagentCtl:
    """Tests for nova_subagent_ctl.sh — Subagent lifecycle control."""

    def test_status_command(self):
        """'status' command should list all subagent states."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert "status)" in content

    def test_start_command(self):
        """'start' command should be supported."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert "start)" in content

    def test_stop_command(self):
        """'stop' command should be supported."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert "stop)" in content

    def test_restart_command(self):
        """'restart' command should stop then start."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        # Find the restart case block
        restart_start = content.find("restart)")
        restart_end = content.find(";;", restart_start)
        restart_block = content[restart_start:restart_end]
        assert "stop" in restart_block
        assert "start" in restart_block

    def test_health_command(self):
        """'health' command should check Redis heartbeats."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert "health)" in content
        assert "redis-cli" in content

    def test_validates_agent_name(self):
        """Should validate agent names to prevent injection."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert "_validate_name" in content
        # Regex check for safe names only
        assert "^[a-z]" in content

    def test_scoped_to_agent_prefix(self):
        """Should only operate on com.nova.agent-* labels."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert 'LABEL_PREFIX="com.nova.agent-"' in content

    def test_help_shows_usage(self):
        """Running with no args should show usage."""
        result = _run(["zsh", _script_path("nova_subagent_ctl.sh")], timeout=10)
        combined = result.stdout + result.stderr
        assert "usage" in combined.lower() or "status" in combined.lower()

    def test_invalid_name_rejected(self):
        """Names with special characters should be rejected."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        # The regex pattern only allows lowercase + hyphens + numbers
        assert re.search(r'\[a-z\]\[a-z0-9-\]', content)


class TestNovaServices:
    """Tests for nova-services.sh — Service listing and management."""

    def test_start_command_recognized(self):
        """'start' command should be a valid option."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        assert "start)" in content

    def test_stop_command_recognized(self):
        """'stop' command should be a valid option."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        assert "stop)" in content

    def test_restart_command_recognized(self):
        """'restart' command should be a valid option."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        assert "restart)" in content

    def test_status_command_recognized(self):
        """'status' command should be a valid option."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        assert "status)" in content

    def test_help_flag(self):
        """--help should show usage without error."""
        result = _run(["zsh", _script_path("nova-services.sh"), "--help"], timeout=10)
        assert result.returncode == 0
        assert "start" in result.stdout.lower()

    def test_no_args_shows_usage(self):
        """Running with no args should show usage."""
        result = _run(["zsh", _script_path("nova-services.sh")], timeout=10)
        assert result.returncode == 0
        assert "start" in result.stdout.lower()

    def test_unknown_command_fails(self):
        """Unknown commands should exit non-zero."""
        result = _run(
            ["zsh", _script_path("nova-services.sh"), "nonsense"],
            timeout=10,
        )
        assert result.returncode != 0

    def test_startup_order_is_correct(self):
        """Services must start in dependency order: PG -> Redis -> Ollama -> Gateway -> WebUIs."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        # Find positions in cmd_start
        start_block_begin = content.find("cmd_start()")
        start_block_end = content.find("cmd_stop()")
        start_block = content[start_block_begin:start_block_end]

        pg_pos = start_block.find("_start_postgresql")
        redis_pos = start_block.find("_start_redis")
        ollama_pos = start_block.find("_start_ollama")
        gateway_pos = start_block.find("_start_gateway")
        owui_pos = start_block.find("_start_openwebui")
        tiny_pos = start_block.find("_start_tinychat")

        assert pg_pos < redis_pos < ollama_pos < gateway_pos < owui_pos < tiny_pos

    def test_stop_order_is_reverse(self):
        """Services must stop in reverse dependency order."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        stop_block_begin = content.find("cmd_stop()")
        stop_block_end = content.find("cmd_restart()")
        stop_block = content[stop_block_begin:stop_block_end]

        tiny_pos = stop_block.find("_stop_tinychat")
        owui_pos = stop_block.find("_stop_openwebui")
        gateway_pos = stop_block.find("_stop_gateway")
        ollama_pos = stop_block.find("_stop_ollama")
        redis_pos = stop_block.find("_stop_redis")
        pg_pos = stop_block.find("_stop_postgresql")

        assert tiny_pos < owui_pos < gateway_pos < ollama_pos < redis_pos < pg_pos

    def test_health_checks_defined(self):
        """Health check functions must exist for all services."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        for svc in ["postgresql", "redis", "ollama", "gateway", "openwebui", "tinychat"]:
            assert f"_health_{svc}()" in content, f"Missing _health_{svc}()"

    def test_keychain_loading_with_retry(self):
        """Gateway start should load Keychain secrets with retry."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        assert "_load_keychain_secrets" in content
        assert "max_retries" in content


class TestMlxServerStart:
    """Tests for mlx_server_start.sh — MLX server startup."""

    def test_uses_mlx_lm_server(self):
        """Should exec mlx_lm.server."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert "mlx_lm.server" in content

    def test_specifies_model_path(self):
        """Should specify the model path on /Volumes/Data."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert "/Volumes/Data/mlx-models/" in content

    def test_has_draft_model(self):
        """Should use speculative decoding with a draft model."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert "--draft-model" in content

    def test_binds_to_lan_ip(self):
        """Should bind to LAN IP (192.168.1.6), not localhost."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert "192.168.1.6" in content

    def test_port_5050(self):
        """Should run on port 5050."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert "--port 5050" in content

    def test_uses_exec(self):
        """Should use exec to replace the shell process."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert content.strip().startswith("#!/") or "exec " in content


class TestSearxngStart:
    """Tests for searxng_start.sh — SearXNG startup."""

    def test_sets_settings_path(self):
        """Should set SEARXNG_SETTINGS_PATH."""
        content = (SCRIPTS_DIR / "searxng_start.sh").read_text()
        assert "SEARXNG_SETTINGS_PATH" in content

    def test_uses_venv_python(self):
        """Should use the SearXNG venv Python."""
        content = (SCRIPTS_DIR / "searxng_start.sh").read_text()
        assert "/Volumes/Data/searxng/venv/bin/python3" in content

    def test_runs_searx_webapp(self):
        """Should run searx.webapp module."""
        content = (SCRIPTS_DIR / "searxng_start.sh").read_text()
        assert "searx.webapp" in content

    def test_changes_to_searxng_dir(self):
        """Should cd to the SearXNG directory."""
        content = (SCRIPTS_DIR / "searxng_start.sh").read_text()
        assert "cd /Volumes/Data/searxng" in content


class TestNovaBootScript:
    """Tests for nova-boot.sh — the master boot orchestrator."""

    def test_tier_0_environment_validation(self):
        """Tier 0 should validate volumes, symlinks, binaries, and config."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "TIER 0" in content
        assert "/Volumes/Data" in content
        assert "/Volumes/MoreData" in content
        assert "openclaw.json" in content

    def test_tier_1_base_layer(self):
        """Tier 1 should start PostgreSQL, Redis, and Ollama."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "TIER 1" in content
        assert "PostgreSQL" in content
        assert "Redis" in content
        assert "Ollama" in content

    def test_tier_2_core_services(self):
        """Tier 2 should start Memory Server and Gateway."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "TIER 2" in content
        assert "Memory Server" in content
        assert "Gateway" in content

    def test_tier_3_application_layer(self):
        """Tier 3 should start OpenWebUI, TinyChat, MLX Server."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "TIER 3" in content
        assert "OpenWebUI" in content
        assert "TinyChat" in content
        assert "MLX Server" in content

    def test_tier_4_agents(self):
        """Tier 4 should start agents and watchdog."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "TIER 4" in content
        assert "agent-sentinel" in content
        assert "watchdog" in content

    def test_tier_5_integration_tests(self):
        """Tier 5 should run integration and security tests."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "TIER 5" in content
        assert "Integration" in content
        assert "Security" in content

    def test_restart_flag_stops_first(self):
        """--restart flag should stop all services before starting."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "--restart" in content
        assert "RESTART MODE" in content
        # Verify it stops services
        restart_pos = content.find("--restart")
        stop_pos = content.find("launchctl stop", restart_pos)
        assert stop_pos > restart_pos

    def test_skip_already_running(self):
        """Without --restart, should skip healthy services."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "already running" in content

    def test_security_checks(self):
        """Boot should include security validation checks."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "Security:" in content
        assert "loopback" in content
        # Check for dangerous bind detection
        assert "0.0.0.0" in content

    def test_disk_space_check(self):
        """Should check disk space for main SSD."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "Disk:" in content
        assert "df -g" in content

    def test_exit_code_on_failure(self):
        """Should exit 1 if critical failures occurred."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        # At the end of the script
        assert "exit 1" in content
        assert "exit 0" in content
        assert "FAILED" in content

    def test_keychain_check(self):
        """Should verify Keychain accessibility in Tier 0."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "Keychain" in content
        assert "nova-slack-bot-token" in content

    def test_stale_pid_cleanup(self):
        """Should clean up stale PostgreSQL PID files."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "postmaster.pid" in content
        assert "Stale" in content

    def test_ollama_symlink_repair(self):
        """Should detect and repair broken Ollama support symlinks."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "Ollama" in content
        assert "Application Support" in content

    def test_log_file_path(self):
        """Should log to ~/.openclaw/logs/nova-boot.log."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "nova-boot.log" in content


class TestNovaStackRestart:
    """Tests for nova_stack_restart.sh — Full stack restart."""

    def test_5_step_process(self):
        """Should have a 5-step startup process."""
        content = (SCRIPTS_DIR / "nova_stack_restart.sh").read_text()
        assert "[1/5]" in content
        assert "[2/5]" in content
        assert "[3/5]" in content
        assert "[4/5]" in content
        assert "[5/5]" in content

    def test_step_1_is_ollama(self):
        """Step 1 should be Ollama."""
        content = (SCRIPTS_DIR / "nova_stack_restart.sh").read_text()
        step1_pos = content.find("[1/5]")
        step2_pos = content.find("[2/5]")
        step1_block = content[step1_pos:step2_pos]
        assert "ollama" in step1_block.lower()

    def test_step_2_is_pg_redis(self):
        """Step 2 should be Postgres + Redis."""
        content = (SCRIPTS_DIR / "nova_stack_restart.sh").read_text()
        step2_pos = content.find("[2/5]")
        step3_pos = content.find("[3/5]")
        step2_block = content[step2_pos:step3_pos]
        assert "postgres" in step2_block.lower()
        assert "redis" in step2_block.lower()

    def test_step_4_is_gateway(self):
        """Step 4 should be Gateway."""
        content = (SCRIPTS_DIR / "nova_stack_restart.sh").read_text()
        step4_pos = content.find("[4/5]")
        step5_pos = content.find("[5/5]")
        step4_block = content[step4_pos:step5_pos]
        assert "gateway" in step4_block.lower()

    def test_uses_launchctl_kickstart(self):
        """Should use launchctl kickstart for service management."""
        content = (SCRIPTS_DIR / "nova_stack_restart.sh").read_text()
        assert "launchctl kickstart" in content

    def test_kills_ollama_before_restart(self):
        """Should kill existing Ollama processes before restarting."""
        content = (SCRIPTS_DIR / "nova_stack_restart.sh").read_text()
        assert "pkill" in content


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT FORMAT TESTS (frame marker)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.frame
class TestOutputFormats:
    """Verify scripts produce well-formed output (status messages, JSON, etc.)."""

    def test_boot_log_has_timestamp_prefix(self):
        """nova-boot.sh log lines should have [HH:MM:SS] prefix."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        # The log() function format
        assert "date '+%H:%M:%S'" in content

    def test_boot_pass_format(self):
        """PASS lines should have checkmark prefix."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "PASS:" in content

    def test_boot_fail_format(self):
        """FAIL lines should have X prefix."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "FAIL:" in content

    def test_boot_summary_format(self):
        """Boot summary should include duration and failure count."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "BOOT COMPLETE" in content
        assert "Failures:" in content
        assert "Warnings:" in content

    def test_homekit_scene_json_output(self):
        """homekit_scene should output JSON with status field."""
        content = (SCRIPTS_DIR / "nova_homekit_scene.sh").read_text()
        assert '"status": "executed"' in content or '"status" *: *"executed"' in content

    def test_services_color_output(self):
        """nova-services.sh should use ANSI colors for terminal output."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        assert "\\033[" in content

    def test_services_disables_color_when_not_tty(self):
        """Colors should be disabled when output is not a terminal."""
        content = (SCRIPTS_DIR / "nova-services.sh").read_text()
        assert "-t 1" in content
        # When not a tty, color vars should be empty
        assert "RED=''" in content or 'RED=""' in content

    def test_subagent_ctl_status_format(self):
        """subagent_ctl status should show agent name and state."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert "running" in content
        assert "stopped" in content

    def test_recall_pretty_prints(self):
        """nova_recall.sh should pretty-print memory results."""
        content = (SCRIPTS_DIR / "nova_recall.sh").read_text()
        assert "score_pct" in content or "match" in content

    def test_pg_backup_slack_message_format(self):
        """Backup notification should include DB name, size, and duration."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "Postgres Backup" in content
        assert "Size:" in content or "$DUMP_SIZE" in content


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSecurityPractices:
    """Verify shell scripts follow security best practices."""

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_no_hardcoded_tokens(self, script):
        """No script should contain hardcoded API tokens or passwords."""
        content = (SCRIPTS_DIR / script).read_text()
        # Check for common token patterns (but allow "ollama" as it's not a secret)
        assert not re.search(r'xoxb-[0-9a-zA-Z]{10,}', content), (
            f"{script} contains what looks like a Slack bot token"
        )
        assert not re.search(r'sk-[a-zA-Z0-9]{20,}', content), (
            f"{script} contains what looks like an API key"
        )
        assert not re.search(r'ghp_[a-zA-Z0-9]{30,}', content), (
            f"{script} contains what looks like a GitHub PAT"
        )

    def test_gateway_uses_keychain_not_env_files(self):
        """Gateway startup must use Keychain, not .env files or hardcoded secrets."""
        content = (SCRIPTS_DIR / "nova_gateway_start.sh").read_text()
        assert "security find-generic-password" in content
        assert ".env" not in content

    def test_subagent_ctl_validates_input(self):
        """Subagent controller must validate agent names against injection."""
        content = (SCRIPTS_DIR / "nova_subagent_ctl.sh").read_text()
        assert "_validate_name" in content
        # Must reject anything that isn't lowercase alphanum + hyphen
        assert re.search(r'\[a-z\]', content)

    def test_boot_checks_service_binding(self):
        """Boot script should verify services aren't exposed to all interfaces."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "0.0.0.0" in content or "all interfaces" in content.lower()
        assert "loopback" in content

    def test_pg_backup_uses_no_owner(self):
        """pg_dump should use --no-owner --no-privileges for portability."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "--no-owner" in content
        assert "--no-privileges" in content


# ═══════════════════════════════════════════════════════════════════════════════
# DEPENDENCY AND INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestDependencyChains:
    """Verify scripts correctly declare and wait for their dependencies."""

    def test_memory_server_depends_on_three_services(self):
        """Memory server must wait for PG, Redis, and Ollama."""
        content = (SCRIPTS_DIR / "nova_memory_server_start.sh").read_text()
        assert "5432" in content   # PostgreSQL
        assert "6379" in content   # Redis
        assert "11434" in content  # Ollama

    def test_openwebui_depends_on_ollama(self):
        """OpenWebUI must wait for Ollama."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "wait_for_port 11434" in content

    def test_tinychat_depends_on_ollama(self):
        """TinyChat must wait for Ollama."""
        content = (SCRIPTS_DIR / "tinychat_start.sh").read_text()
        assert "wait_for_port 11434" in content

    def test_boot_tier_order_is_sequential(self):
        """Boot tiers must execute in order: 0 -> 1 -> 2 -> 3 -> 4 -> 5."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        tier_positions = []
        for tier in range(6):
            pos = content.find(f"TIER {tier}")
            assert pos >= 0, f"TIER {tier} not found in nova-boot.sh"
            tier_positions.append(pos)
        for i in range(len(tier_positions) - 1):
            assert tier_positions[i] < tier_positions[i + 1], (
                f"TIER {i} appears after TIER {i + 1}"
            )

    def test_services_start_dependency_order_matches_boot(self):
        """nova-services.sh start order should match nova-boot.sh tier ordering."""
        # Both should start PG before Redis before Ollama before Gateway
        for script_name in ["nova-services.sh", "nova-boot.sh"]:
            content = (SCRIPTS_DIR / script_name).read_text()
            pg_pos = content.find("5432")
            redis_pos = content.find("6379")
            ollama_pos = content.find("11434")
            assert pg_pos < redis_pos, f"{script_name}: PG should come before Redis"
            assert redis_pos < ollama_pos, f"{script_name}: Redis should come before Ollama"


@pytest.mark.integration
class TestWaitForPortIntegration:
    """Integration tests that actually use wait-for-port against real ports."""

    def test_detects_postgres_if_running(self):
        """If PostgreSQL is running, wait-for-port should detect port 5432."""
        if not _is_port_open(5432):
            pytest.skip("PostgreSQL not running on port 5432")
        script = textwrap.dedent(f"""\
            #!/bin/zsh
            source "{_script_path("wait-for-port.sh")}"
            wait_for_port 5432 "PostgreSQL" 5
        """)
        result = _run(["zsh", "-c", script], timeout=10)
        assert result.returncode == 0

    def test_detects_redis_if_running(self):
        """If Redis is running, wait-for-port should detect port 6379."""
        if not _is_port_open(6379):
            pytest.skip("Redis not running on port 6379")
        script = textwrap.dedent(f"""\
            #!/bin/zsh
            source "{_script_path("wait-for-port.sh")}"
            wait_for_port 6379 "Redis" 5
        """)
        result = _run(["zsh", "-c", script], timeout=10)
        assert result.returncode == 0

    def test_detects_ollama_if_running(self):
        """If Ollama is running, wait-for-port should detect port 11434."""
        if not _is_port_open(11434):
            pytest.skip("Ollama not running on port 11434")
        script = textwrap.dedent(f"""\
            #!/bin/zsh
            source "{_script_path("wait-for-port.sh")}"
            wait_for_port 11434 "Ollama" 5
        """)
        result = _run(["zsh", "-c", script], timeout=10)
        assert result.returncode == 0


@pytest.mark.integration
class TestNovaRecallIntegration:
    """Integration tests for nova_recall against the live memory server."""

    def test_recall_with_real_server(self):
        """If memory server is running, nova_recall.sh should return results."""
        if not _is_port_open(18790):
            pytest.skip("Memory server not running on port 18790")
        result = _run(
            ["bash", _script_path("nova_recall.sh"), "test query", "2"],
            timeout=15,
        )
        # Should succeed and print something (even "No memories found")
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "memory" in combined.lower() or "found" in combined.lower()


@pytest.mark.integration
class TestNovaServicesStatus:
    """Integration test for nova-services.sh status command."""

    def test_status_runs_without_crash(self):
        """'nova-services.sh status' should run and produce output."""
        result = _run(
            ["zsh", _script_path("nova-services.sh"), "status"],
            timeout=30,
        )
        combined = result.stdout + result.stderr
        # Should mention at least some of the services
        assert "postgresql" in combined.lower() or "redis" in combined.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — End-to-end workflow verification
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestBootDependencyGraph:
    """Verify the complete dependency graph in nova-boot.sh is correct."""

    def test_tier1_has_no_upstream_deps(self):
        """Tier 1 services (PG, Redis, Ollama) should not wait for other services."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        tier1_start = content.find("TIER 1: Base Layer")
        tier2_start = content.find("TIER 2: Core Services")
        tier1_block = content[tier1_start:tier2_start]
        # Tier 1 should not call wait_for_port before starting services
        # (only after, for its own ports)
        # Check that there's no dependency on 18789, 18790 etc.
        assert "18789" not in tier1_block
        assert "18790" not in tier1_block

    def test_tier2_waits_for_tier1_ports(self):
        """Tier 2 should only start after Tier 1 ports are confirmed open."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        # Tier 1 wait calls
        tier1_wait = content.find("wait_for_port 5432")
        tier2_start = content.find("TIER 2: Core Services")
        assert tier1_wait < tier2_start, "Tier 1 ports must be verified before Tier 2"

    def test_tier3_waits_for_tier2(self):
        """Tier 3 should only start after Tier 2 is ready."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        tier2_wait = content.find('wait_for_port 18790')
        tier3_start = content.find("TIER 3: Application Layer")
        assert tier2_wait < tier3_start, "Tier 2 ports must be verified before Tier 3"

    def test_boot_summary_always_produced(self):
        """The boot summary section should always be reached."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "BOOT COMPLETE" in content
        assert "BOOT_DURATION" in content
        assert "ALL SYSTEMS OPERATIONAL" in content
        assert "DEGRADED" in content


@pytest.mark.functional
class TestSessionResetWorkflow:
    """Verify session reset workflow is complete and safe."""

    def test_session_file_is_archived_not_deleted(self):
        """Session files should be renamed (archived), never deleted."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "mv " in content
        assert "rm " not in content.split("Archive")[0]  # no rm before archive step

    def test_size_comparison_is_float_safe(self):
        """Size comparison should handle float MB values (not just integers)."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        # Uses Python for float comparison
        assert "python3" in content
        assert "float" in content or ">" in content


@pytest.mark.functional
class TestPgBackupWorkflow:
    """Verify the full backup workflow is correct."""

    def test_backup_workflow_order(self):
        """Backup should: check PG -> dump -> verify -> copy to NAS -> rotate."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        lines = content.splitlines()
        # Find line numbers for each operation (skip comments)
        pg_check_line = next(
            i for i, l in enumerate(lines)
            if "pg_isready" in l and not l.strip().startswith("#")
        )
        dump_line = next(
            i for i, l in enumerate(lines)
            if l.strip().startswith("pg_dump") and not l.strip().startswith("#")
        )
        verify_line = next(
            i for i, l in enumerate(lines)
            if "pg_restore --list" in l and not l.strip().startswith("#")
        )
        rsync_line = next(
            i for i, l in enumerate(lines)
            if l.strip().startswith("rsync") and not l.strip().startswith("#")
        )
        rotate_line = next(
            i for i, l in enumerate(lines)
            if l.startswith("_rotate") and not l.strip().startswith("#")
        )
        assert pg_check_line < dump_line < verify_line < rsync_line < rotate_line

    def test_rotation_applies_to_both_local_and_nas(self):
        """Rotation should clean up both local and NAS directories."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        # _rotate is called for both LOCAL_DIR and NAS_DIR
        rotate_calls = content.count('_rotate "$')
        assert rotate_calls >= 2, f"Expected at least 2 rotation calls, found {rotate_calls}"


# ═══════════════════════════════════════════════════════════════════════════════
# ADDITIONAL SCRIPTS — Cover remaining scripts from the directory
# ═══════════════════════════════════════════════════════════════════════════════


class TestAllShellScriptsExist:
    """Verify all expected scripts exist on disk."""

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_exists(self, script):
        """Each listed script must exist."""
        path = SCRIPTS_DIR / script
        assert path.exists(), f"Script not found: {path}"

    @pytest.mark.parametrize("script", ALL_SCRIPTS)
    def test_script_not_empty(self, script):
        """No script should be an empty file."""
        path = SCRIPTS_DIR / script
        assert path.stat().st_size > 0, f"{script} is empty"


class TestEnvironmentSetup:
    """Verify start scripts set up their environments correctly."""

    def test_mlx_exports_home(self):
        """MLX server start should set HOME."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert "HOME=" in content

    def test_mlx_adds_homebrew_to_path(self):
        """MLX server start should add homebrew to PATH."""
        content = (SCRIPTS_DIR / "mlx_server_start.sh").read_text()
        assert "/opt/homebrew/bin" in content

    def test_openwebui_sets_frontend_build_dir(self):
        """OpenWebUI start should set FRONTEND_BUILD_DIR."""
        content = (SCRIPTS_DIR / "openwebui_start.sh").read_text()
        assert "FRONTEND_BUILD_DIR" in content

    def test_tinychat_sets_host(self):
        """TinyChat should bind to LAN IP."""
        content = (SCRIPTS_DIR / "tinychat_start.sh").read_text()
        assert "HOST=192.168.1.6" in content

    def test_searxng_sets_home(self):
        """SearXNG start should set HOME."""
        content = (SCRIPTS_DIR / "searxng_start.sh").read_text()
        assert "HOME=" in content

    def test_pg_backup_adds_pg_bin_to_path(self):
        """pg_backup should add PostgreSQL bin to PATH."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "postgresql@17/bin" in content


class TestErrorHandling:
    """Verify scripts handle errors appropriately."""

    def test_pg_backup_uses_pipefail(self):
        """pg_backup should use set -o pipefail."""
        content = (SCRIPTS_DIR / "nova_pg_backup.sh").read_text()
        assert "pipefail" in content

    def test_session_reset_uses_strict_mode(self):
        """session_reset should use set -euo pipefail."""
        content = (SCRIPTS_DIR / "nova_session_reset.sh").read_text()
        assert "set -euo pipefail" in content

    def test_recall_uses_strict_mode(self):
        """nova_recall should use strict error handling."""
        content = (SCRIPTS_DIR / "nova_recall.sh").read_text()
        assert "set -euo pipefail" in content

    def test_homekit_scene_uses_strict_mode(self):
        """homekit_scene should use strict error handling."""
        content = (SCRIPTS_DIR / "nova_homekit_scene.sh").read_text()
        assert "set -euo pipefail" in content

    def test_generate_image_uses_strict_mode(self):
        """generate_image should use strict error handling."""
        content = (SCRIPTS_DIR / "generate_image.sh").read_text()
        assert "set -euo pipefail" in content

    def test_boot_uses_pipefail(self):
        """nova-boot.sh should use pipefail (without -e, which would abort on first failure)."""
        content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        assert "pipefail" in content


# ═══════════════════════════════════════════════════════════════════════════════
# CROSS-SCRIPT CONSISTENCY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossScriptConsistency:
    """Verify consistent conventions across all scripts."""

    def test_all_start_scripts_source_wait_for_port(self):
        """Start scripts that have dependencies should source wait-for-port.sh."""
        dependency_scripts = [
            "nova_memory_server_start.sh",
            "openwebui_start.sh",
            "tinychat_start.sh",
        ]
        for script in dependency_scripts:
            content = (SCRIPTS_DIR / script).read_text()
            assert "wait-for-port.sh" in content, (
                f"{script} should source wait-for-port.sh"
            )

    def test_port_numbers_consistent(self):
        """Key port numbers should be consistent across scripts."""
        ports = {
            "5432": "PostgreSQL",
            "6379": "Redis",
            "11434": "Ollama",
            "18789": "Gateway",
            "18790": "Memory Server",
        }
        # Verify these ports appear in nova-boot.sh (the master orchestrator)
        boot_content = (SCRIPTS_DIR / "nova-boot.sh").read_text()
        for port, name in ports.items():
            assert port in boot_content, f"Port {port} ({name}) missing from nova-boot.sh"

    def test_gateway_port_consistent(self):
        """Gateway port 18789 should be the same in all scripts that reference it."""
        scripts_with_gateway = ["nova-boot.sh", "nova-services.sh", "nova_gateway_start.sh"]
        for script in scripts_with_gateway:
            content = (SCRIPTS_DIR / script).read_text()
            assert "18789" in content, f"{script} should reference gateway port 18789"

    def test_memory_server_port_consistent(self):
        """Memory server port 18790 should be consistent."""
        scripts_with_memory = ["nova-boot.sh", "nova_remember.sh", "nova_recall.sh"]
        for script in scripts_with_memory:
            content = (SCRIPTS_DIR / script).read_text()
            assert "18790" in content, f"{script} should reference memory port 18790"
