"""nova_build_map — all seven test categories. Reads the repo only; writes to a temporary directory."""
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_build_map as M  # noqa: E402


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_output_escapes_html(tmp_path):
    stats = {"scripts": 1, "tasks": 1, "launch": 0, "ingests": 0, "publishers": 0, "feeds": 0,
             "feed_hosts": 0, "test_files": 0, "seven_category_tests": 0}
    tasks = [{"name": "<script>x</script>", "script": "a.py", "schedule": "every 1m", "category": "Other"}]
    page = M.render_html(stats, [("t", "flowchart LR\n A-->B")], tasks, [], [], [], [])
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page


def test_security_builder_writes_nothing_but_its_output(tmp_path):
    M.build(tmp_path / "map", dry_run=True)
    assert not (tmp_path / "map").exists()


def test_security_no_credentials_or_private_sources_read():
    src = (SCRIPTS / "nova_build_map.py").read_text()
    for banned in ("psycopg2", "urlopen", "keychain", "chat.db", "Messages/"):
        assert banned not in src, banned


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_feed_group_counts_many_urls():
    urls = [f"https://host{i % 500}.example.com/feed/{i}" for i in range(50000)]
    t = time.perf_counter()
    counts = M.feed_groups(urls)
    assert len(counts) == 500 and time.perf_counter() - t < 2.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_not_applicable_reads_local_files_only():
    # The builder makes no network or database calls, so there is nothing to retry.
    assert "urlopen" not in (SCRIPTS / "nova_build_map.py").read_text()


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_classify_known_names():
    assert M.classify("live_docs", "nova_live_docs.py") == "Operations and reliability"
    assert M.classify("pendulum", "nova_pendulum.py") == "Organs: cognition and self-model"
    assert M.classify("zzz_unknown", "nova_zzz.py") == "Other"


def test_unit_feed_groups_strip_www():
    assert M.feed_groups(["https://www.a.com/x", "https://a.com/y"])["a.com"] == 2


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_every_task_in_the_config_is_classified():
    tasks = M.scheduler_tasks()
    assert tasks and all(t["category"] for t in tasks)


def test_integration_build_counts_match_the_repo():
    res = M.build(Path("/nonexistent"), dry_run=True)
    assert res["stats"]["scripts"] == len(list(SCRIPTS.glob("*.py")))
    assert res["stats"]["test_files"] == len(list((SCRIPTS / "tests").glob("test_*.py")))


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_writes_html_and_markdown(tmp_path):
    M.build(tmp_path, dry_run=False)
    assert (tmp_path / "Nova-Map.html").exists() and (tmp_path / "Nova-Map.md").exists()
    md = (tmp_path / "Nova-Map.md").read_text()
    assert md.count("```mermaid") == 7
    assert re.search(r"\| tasks \| \d+ \|", md)


def test_functional_cli_dry_run_exits_zero(tmp_path):
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_build_map.py"), "--dry-run", "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "'tasks'" in out.stdout


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_help_exits_zero():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_build_map.py"), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
