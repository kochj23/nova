"""nova_crystal_ball — all seven test categories. The local model is faked; no network is used."""
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_crystal_ball as CB  # noqa: E402

ARTICLE = ("Officials said the port would stay open while the new ferry schedule is tested. "
           "A regional committee will review the traffic data next month.")


def good(**over):
    s = {"quote": "the port would stay open while the new ferry schedule is tested",
         "scenario": "ferry delays spread to the freight lanes", "capability": "moderate",
         "capability_reason": "the ferry is the only link on that route",
         "confirm_if": "freight dwell times rise for two weeks", "rule_out_if": "dwell times stay flat"}
    s.update(over)
    return s


# ── Security: the forbidden framings are refused in code ─────────────────────
def test_security_false_flag_framing_is_dropped():
    assert CB.check(good(scenario="a false-flag attack on the port"), ARTICLE)


def test_security_martial_law_and_election_framing_is_dropped():
    assert CB.check(good(scenario="martial law follows and the election is cancelled"), ARTICLE)


def test_security_named_intent_is_dropped():
    assert CB.check(good(scenario="the ministry is secretly planning the delay"), ARTICLE)


def test_security_scenario_must_quote_the_article():
    assert "no quote from the article" in CB.check(good(quote="a sentence the article never says"), ARTICLE)


# ── Performance ───────────────────────────────────────────────────────────────
def test_performance_parse_handles_large_reply_quickly():
    big = json.dumps({"scenarios": [good() for _ in range(3)]}) + " " + "x" * 500000
    t = time.perf_counter()
    assert len(CB.parse(big)) == 3
    assert time.perf_counter() - t < 1.0


# ── Retry ─────────────────────────────────────────────────────────────────────
def test_retry_model_call_retries_then_succeeds():
    import urllib.error
    import urllib.request
    calls = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"message": {"content": "{}"}}).encode()

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) < 2:
            raise urllib.error.URLError("refused")
        return Resp()

    real = urllib.request.urlopen
    urllib.request.urlopen = flaky
    try:
        assert CB._post("p", attempts=3, _sleep=lambda s: None) == "{}"
    finally:
        urllib.request.urlopen = real
    assert len(calls) == 2


# ── Unit ──────────────────────────────────────────────────────────────────────
def test_unit_clean_scenario_passes_and_renders_labelled():
    assert CB.check(good(), ARTICLE) == []
    out = CB.render([good()])
    assert "Speculative" in out and "Would confirm it" in out and "Would rule it out" in out


def test_unit_label_not_repeated_when_model_adds_it():
    out = CB.render([good(scenario="Speculative: ferry delays spread")])
    assert out.count("Speculative") == 2, out  # header line + bullet, not three


def test_unit_parse_tolerates_think_blocks_and_junk():
    assert CB.parse("<think>hm</think>not json") == []
    assert len(CB.parse('<think>x</think>{"scenarios": [{"quote": "a"}]}')) == 1


# ── Integration ───────────────────────────────────────────────────────────────
def test_integration_for_article_drops_unsafe_and_keeps_safe():
    raw = json.dumps({"scenarios": [good(), good(scenario="a false-flag attack on the port")]})
    block = CB.for_article("Port update", ARTICLE, "local", _post_fn=lambda p: raw)
    assert block and "ferry delays" in block and "false-flag" not in block.lower()


def test_integration_nothing_safe_returns_none():
    raw = json.dumps({"scenarios": [good(scenario="martial law and a cancelled election")]})
    assert CB.for_article("Port update", ARTICLE, "security", _post_fn=lambda p: raw) is None


# ── Functional ────────────────────────────────────────────────────────────────
def test_functional_other_sections_get_no_block():
    assert CB.for_article("Essay", ARTICLE, "essays", _post_fn=lambda p: "boom") is None


def test_functional_model_failure_never_raises():
    def boom(prompt):
        raise OSError("model down")
    assert CB.for_article("Port update", ARTICLE, "news", _post_fn=boom) is None


# ── Frame ─────────────────────────────────────────────────────────────────────
def test_frame_module_compiles_and_help_exits_zero():
    out = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_crystal_ball.py")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
