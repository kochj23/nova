"""nova_wargame — all seven test categories. Nothing here changes a real system; the database is skipped if offline."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_config as NC  # noqa: E402
import nova_wargame as G  # noqa: E402


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_drills_never_call_a_system_command_or_http():
    src = (SCRIPTS / "nova_wargame.py").read_text()
    for banned in ("subprocess", "urlopen", "shell=True", "os.system"):
        assert banned not in src, banned


def test_security_no_gate_or_action_log_writes():
    src = (SCRIPTS / "nova_wargame.py").read_text()
    assert "claude_actions" not in src and "action_audit" not in src and "escalation" not in src


def test_security_database_host_from_config():
    assert "pg-primary.digitalnoise.net" not in (SCRIPTS / "nova_wargame.py").read_text()


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_simulate_many_rooms_is_fast():
    depends = {"hub": {"rooms": [f"room{i}" for i in range(20000)], "status": "assumed"}}
    scen = {"description": "d", "failed": "hub",
            "responses": {"fix": {"effect": "restores", "rooms_back": [f"room{i}" for i in range(0, 20000, 2)],
                                  "note": ""}}}
    t = time.perf_counter()
    r = G.simulate(scen, "fix", depends)
    assert r["recommendation"] == "APPLY" and time.perf_counter() - t < 1.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_connection_uses_shared_helper():
    assert "NC.pg_connect()" in (SCRIPTS / "nova_wargame.py").read_text()


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_restoring_response_is_applied():
    r = G.simulate(G.SCENARIOS["garage_ap_down"], "power_cycle_ap")
    assert r["recommendation"] == "APPLY" and r["exercise"] is True


def test_unit_no_effect_and_worsening_responses_are_held():
    assert G.simulate(G.SCENARIOS["garage_ap_down"], "restart_hub")["recommendation"] == "HOLD"
    assert G.simulate(G.SCENARIOS["homekit_hub_down"], "power_cycle_bridge")["recommendation"] == "HOLD"


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_every_scenario_response_yields_a_serialisable_result():
    import json
    for name, s in G.SCENARIOS.items():
        for resp in s["responses"]:
            json.dumps(G.simulate(s, resp))


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_list_prints_all_scenarios():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_wargame.py"), "--list"],
                         capture_output=True, text=True, timeout=30, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr
    for name in G.SCENARIOS:
        assert name in out.stdout


def test_functional_dry_run_prints_every_recommendation():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_wargame.py"), "--dry-run"],
                         capture_output=True, text=True, timeout=30, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("APPLY") + out.stdout.count("HOLD") == 4


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_help_exits_zero():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_wargame.py"), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
