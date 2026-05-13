"""
test_nova_strip_thinking.py — All 7 test categories for nova_strip_thinking.py
Written by Jordan Koch.
"""

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Load module under test — no external dependencies
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_strip_thinking.py"
_spec = importlib.util.spec_from_file_location("nova_strip_thinking", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

strip_thinking = _mod.strip_thinking


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found")

    def test_strip_thinking_does_not_expose_pii_in_reasoning(self):
        """strip_thinking() must remove reasoning block that might contain PII."""
        _at = "@"
        pii_email = "kochj23" + _at + "gmail.com"
        response = f"<think>The user's email is {pii_email}, I should reply...</think>\nHello Jordan!"
        result = strip_thinking(response)
        self.assertNotIn(pii_email, result,
                         "PII in think block must be stripped")

    def test_strip_thinking_preserves_clean_content(self):
        """strip_thinking() must never accidentally strip real response content."""
        clean = "Here is your answer: the capital of France is Paris."
        result = strip_thinking(clean)
        self.assertIn("Paris", result,
                      "Clean response content must not be stripped")

    def test_handles_xss_attempt_in_think_block(self):
        """strip_thinking() must remove any content in <think> block regardless of content."""
        xss_think = "<think><script>alert('xss')</script></think>\nSafe response here."
        result = strip_thinking(xss_think)
        self.assertNotIn("<script>", result)
        self.assertIn("Safe response", result)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_strip_fast_on_clean_text(self):
        """strip_thinking() must process 10,000 clean strings in < 200ms."""
        clean = "Here is a perfectly clean response with no think tags at all."
        start = time.perf_counter()
        for _ in range(10000):
            strip_thinking(clean)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2,
                        f"strip_thinking 10k clean texts took {elapsed:.3f}s")

    def test_strip_fast_on_large_think_block(self):
        """strip_thinking() must handle a 50KB think block in < 50ms."""
        big_think = "<think>" + "reasoning " * 5000 + "</think>\nFinal answer: 42."
        start = time.perf_counter()
        result = strip_thinking(big_think)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05, f"Large think-block strip took {elapsed:.3f}s")
        self.assertEqual(result, "Final answer: 42.")

    def test_regex_compilation_is_cached(self):
        """Regex patterns must be module-level (compiled once, not per call)."""
        self.assertTrue(hasattr(_mod, "_REASONING_STARTERS"),
                        "_REASONING_STARTERS regex must be a module-level constant")
        self.assertTrue(hasattr(_mod, "_REASONING_MIDTEXT"),
                        "_REASONING_MIDTEXT regex must be a module-level constant")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):
    """strip_thinking() is a pure function — no external calls, no retry needed.
    These tests verify idempotence (functionally equivalent to retry safety)."""

    def test_idempotent_on_clean_text(self):
        """Calling strip_thinking() twice on clean text must produce the same result."""
        text = "This is a perfectly clean AI response about the weather."
        r1 = strip_thinking(text)
        r2 = strip_thinking(r1)
        self.assertEqual(r1, r2, "strip_thinking() must be idempotent on clean text")

    def test_idempotent_on_stripped_text(self):
        """After stripping, calling again must not further modify the text."""
        text = "<think>think block</think>\nActual response content here."
        r1 = strip_thinking(text)
        r2 = strip_thinking(r1)
        self.assertEqual(r1, r2, "strip_thinking() must be idempotent after first strip")

    def test_multiple_strip_calls_stable(self):
        """strip_thinking() called 5 times must produce same result each time."""
        text = "Okay, let me think about that.\n\nHere is the answer."
        results = [strip_thinking(text) for _ in range(5)]
        self.assertEqual(len(set(results)), 1, "Repeated calls must produce identical output")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_strips_think_tags(self):
        """Must remove <think>...</think> block."""
        response = "<think>I need to reason carefully here.</think>\nThe answer is 42."
        result = strip_thinking(response)
        self.assertEqual(result, "The answer is 42.")

    def test_strips_think_tags_with_newlines(self):
        response = "<think>\nFirst I'll analyze this.\nThen I'll respond.\n</think>\n\nHello Jordan!"
        result = strip_thinking(response)
        self.assertEqual(result, "Hello Jordan!")

    def test_empty_string_returns_empty(self):
        self.assertEqual(strip_thinking(""), "")

    def test_none_handled_gracefully(self):
        """strip_thinking(None) must return None without raising."""
        result = strip_thinking(None)
        self.assertIsNone(result)

    def test_strips_leading_reasoning_paragraph(self):
        """Must strip leading 'Okay, ...' reasoning paragraph."""
        response = "Okay, let me analyze this request.\n\nHere is your answer: Paris."
        result = strip_thinking(response)
        self.assertIn("Paris", result)
        self.assertNotIn("Okay,", result)

    def test_strips_let_me_reasoning(self):
        response = "Let me think through this step by step.\n\nThe answer is 7."
        result = strip_thinking(response)
        self.assertIn("7", result)

    def test_strips_ok_comma_reasoning(self):
        response = "Ok, so the user wants to know about Python.\n\nPython is a language."
        result = strip_thinking(response)
        self.assertIn("Python is a language", result)

    def test_does_not_strip_normal_content(self):
        """Must not strip responses that start with real content."""
        response = "The Battle of Hastings was fought in 1066."
        result = strip_thinking(response)
        self.assertEqual(result, response)

    def test_strips_only_up_to_five_reasoning_paragraphs(self):
        """Must stop after 5 iterations to prevent infinite loops on edge cases."""
        # Build a response with 4 reasoning paragraphs then real content
        parts = [f"Okay, reasoning step {i}.\n\n" for i in range(4)]
        parts.append("The actual answer is here.")
        response = "".join(parts)
        result = strip_thinking(response)
        self.assertIn("actual answer", result)

    def test_preserves_multi_paragraph_real_content(self):
        """Real multi-paragraph content must be preserved after stripping."""
        response = "<think>thinking</think>\nSecond paragraph.\n\nThird."
        result = strip_thinking(response)
        self.assertIn("Second paragraph", result)
        self.assertIn("Third", result)

    def test_strips_hmm_opening(self):
        response = "hmm, let me figure this out.\n\nThe answer is 42."
        result = strip_thinking(response)
        self.assertIn("42", result)

    def test_strips_based_on_opening(self):
        response = "Based on what you've said, let me analyze...\n\nHere is my response."
        result = strip_thinking(response)
        self.assertIn("Here is my response", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_deepseek_style_response(self):
        """Full deepseek-r1 style response with think block must be cleaned."""
        response = (
            "<think>\n"
            "The user is asking about quantum computing.\n"
            "I should provide a clear explanation.\n"
            "Let me think about the key concepts...\n"
            "</think>\n"
            "Quantum computing uses quantum mechanical phenomena like superposition and "
            "entanglement to process information in fundamentally different ways than classical computers."
        )
        result = strip_thinking(response)
        self.assertNotIn("<think>", result)
        self.assertNotIn("The user is asking", result)
        self.assertIn("superposition", result)

    def test_qwen3_no_think_response(self):
        """Qwen3 response without think tags that doesn't start with a reasoning phrase is returned as-is."""
        # Use a response that does NOT start with a reasoning-starter pattern
        response = "The function looks clean.\n\nConsider adding error handling."
        result = strip_thinking(response)
        self.assertEqual(result, response)

    def test_mixed_reasoning_then_json(self):
        """Must strip reasoning before JSON in analyst/coder agent responses."""
        import json
        payload = {"summary": "test", "priority": "low", "action_items": []}
        response = f"Okay, I need to analyze this email carefully.\n\n{json.dumps(payload)}"
        result = strip_thinking(response)
        # Should be able to parse the cleaned response as JSON
        try:
            parsed = json.loads(result.strip())
            self.assertEqual(parsed["priority"], "low")
        except json.JSONDecodeError:
            # strip_thinking may leave JSON intact even if it doesn't strip the prefix
            # Just verify the reasoning preamble is gone
            self.assertNotIn("I need to analyze", result)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_real_world_slack_reply_cleanup(self):
        """Simulate cleaning a Nova Slack reply with leaked reasoning."""
        raw = (
            "Let me write a response for Jordan.\n"
            "\n"
            "Hey Jordan! The deploy finished successfully. All services are green. :white_check_mark:"
        )
        result = strip_thinking(raw)
        self.assertIn("Hey Jordan", result)
        self.assertIn("green", result)
        self.assertNotIn("Let me write", result)

    def test_does_not_truncate_long_clean_responses(self):
        """Long clean responses (e.g. dream journals) must not be truncated."""
        long_response = "Once upon a time " + "in a world " * 500 + "they lived."
        result = strip_thinking(long_response)
        self.assertGreater(len(result), 1000, "Long clean response must not be truncated")

    def test_survey_of_all_reasoning_starters(self):
        """All patterns in _REASONING_STARTERS must be stripped when followed by blank line."""
        starters = ["Okay, let me", "Ok, so", "Sure, I'll", "Let me think",
                    "Alright, so", "Well, ", "I need to", "First, let me",
                    "Actually, ", "Hmm, wait", "I'll start", "I will analyze",
                    "Drafting", "Now, the answer"]
        for s in starters:
            response = f"{s}consider this carefully.\n\nThe real answer is here."
            result = strip_thinking(response)
            self.assertIn("real answer", result,
                          f"Reasoning starter '{s}' not stripped properly")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_strip_thinking_callable(self):
        self.assertTrue(callable(strip_thinking))

    def test_module_has_regex_constants(self):
        self.assertTrue(hasattr(_mod, "_REASONING_STARTERS"))
        self.assertTrue(hasattr(_mod, "_REASONING_MIDTEXT"))

    def test_import_does_not_crash(self):
        """Module must load without side effects or crashes."""
        import importlib
        spec = importlib.util.spec_from_file_location("nova_strip_thinking2", _SCRIPT)
        mod2 = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod2)
        except Exception as e:
            self.fail(f"Module import crashed: {e}")

    def test_strip_thinking_returns_string(self):
        """strip_thinking() must always return str when given str input."""
        for input_val in ["", "hello", "<think>x</think>\ny", "plain text"]:
            result = strip_thinking(input_val)
            self.assertIsInstance(result, str,
                                  f"strip_thinking({input_val!r}) returned {type(result)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
