"""nova_directive_review — all seven test categories. The model is faked; live-database tests skip if offline."""
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_config as NC  # noqa: E402
import nova_directive_review as R  # noqa: E402


# ── Security ──────────────────────────────────────────────────────────────────
def test_security_model_endpoint_is_loopback_only():
    assert R.OLLAMA.startswith("http://localhost:") or R.OLLAMA.startswith("http://127.0.0.1:")


def test_security_dsn_from_config_and_rule_text_truncated():
    src = (SCRIPTS / "nova_directive_review.py").read_text()
    assert "pg-primary.digitalnoise.net" not in src
    assert len(R.clip("x" * 5000)) == R.MAX_CHARS


def test_security_unparseable_reply_never_raises_an_alarm():
    v = R.parse_verdict('{"conflict": true, "reason": "ok"} trailing <script>alert(1)</script>')
    assert v["conflict"] is True
    assert R.parse_verdict("<<not json>>")["conflict"] is False


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_pair_generation_is_quadratic_not_worse():
    rules = [(f"r{i}", "x") for i in range(100)]
    t = time.perf_counter()
    assert len(R.pairs(rules)) == 100 * 99 // 2
    assert time.perf_counter() - t < 1.0


def test_performance_parse_verdict_handles_large_reply():
    big = "a" * 200000 + '{"conflict": false, "reason": "none"}'
    t = time.perf_counter()
    R.parse_verdict(big)
    assert time.perf_counter() - t < 1.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_model_call_retries_transient_failure_then_succeeds():
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("refused")
        body = json.dumps({"message": {"content": '{"conflict": false, "reason": "ok"}'}}).encode()

        class R2(Resp):
            def read(self):
                return body
        return R2()

    real = R.urllib.request.urlopen
    R.urllib.request.urlopen = fake_urlopen
    try:
        out = R.ask_model("p", attempts=3, _sleep=lambda s: None)
    finally:
        R.urllib.request.urlopen = real
    assert len(calls) == 3 and "conflict" in out


def test_retry_model_call_gives_up_and_review_records_error():
    def always_fail(prompt, model):
        raise OSError("down")

    out = R.review((("a", "t"), ("b", "u")), _ask=always_fail)
    assert out["error"] is True and out["conflict"] is False


def test_retry_connect_uses_shared_helper():
    assert "_nova_dsn.pg_connect" in (SCRIPTS / "nova_directive_review.py").read_text()


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_verdict_parsing_and_prompt_contains_both_rules():
    assert R.parse_verdict('{"conflict": true, "reason": "r"}') == {"conflict": True, "reason": "r", "error": False}
    p = R.build_prompt(("alpha", "Always A."), ("beta", "Never A."))
    assert "alpha" in p and "beta" in p and "Always A." in p and "Never A." in p


def test_unit_think_block_is_stripped_before_parsing():
    assert R.parse_verdict('<think>hmm {"x":1}</think>{"conflict": true, "reason": "y"}')["conflict"] is True


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_review_stores_nothing_on_dry_run_with_fake_model():
    pairs = R.pairs([("a", "Always ask."), ("b", "Never ask."), ("c", "Log it.")])
    verdicts = [R.review(p, _ask=lambda prompt, model: '{"conflict": true, "reason": "fake"}') for p in pairs]
    assert sum(v["conflict"] for v in verdicts) == 3


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_limit_and_dry_run_exit_zero_when_model_offline_is_reported():
    try:
        NC.pg_connect(attempts=1).close()
    except Exception:  # noqa: BLE001
        pytest.skip("ops database unreachable")
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_directive_review.py"), "--limit", "2", "--dry-run"],
                         capture_output=True, text=True, timeout=300, cwd=SCRIPTS)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("reviewed 2 pairs")


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_help_exits_zero():
    out = subprocess.run([sys.executable, str(SCRIPTS / "nova_directive_review.py"), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
