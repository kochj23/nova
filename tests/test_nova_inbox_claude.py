"""
test_nova_inbox_claude.py — All 7 test categories for nova_inbox_claude.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["herd_config"] = MagicMock(HERD=[
    {"email": "sam@example.com", "name": "Sam"},
    {"email": "gaston@example.com", "name": "Gaston"},
])

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_inbox_claude.py"
_spec = importlib.util.spec_from_file_location("nova_inbox_claude", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

run_herd = _mod.run_herd
recall_context = _mod.recall_context
remember = _mod.remember
call_local_llm = _mod.call_local_llm
generate_reply = _mod.generate_reply
process_email = _mod.process_email


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "Bearer "]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential in source: {p!r}")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII in source: {p!r}")

    def test_local_llm_privacy_comment(self):
        """Source must document that email content stays local (privacy policy)."""
        src = _SCRIPT.read_text()
        self.assertIn("local", src.lower(), "Must mention local LLM for privacy")

    def test_call_local_llm_uses_ollama_not_openai(self):
        """LLM calls must go to local Ollama, not OpenAI API."""
        self.assertIn("11434", _mod.OLLAMA_URL, "Must use Ollama local port 11434")
        self.assertNotIn("openai.com", _mod.OLLAMA_URL)

    def test_body_truncated_in_prompt(self):
        """generate_reply must truncate body to 400 chars in prompt."""
        captured_prompts = []

        def fake_llm(prompt):
            captured_prompts.append(prompt)
            return "Test reply."

        with patch.object(_mod, "call_local_llm", side_effect=fake_llm):
            generate_reply("s@example.com", "Test", "X" * 1000)

        self.assertTrue(len(captured_prompts) > 0)
        prompt = captured_prompts[0]
        # The truncated body in the prompt must be at most 400 chars
        # Find where the body starts in the prompt
        body_idx = prompt.find("Body:")
        if body_idx >= 0:
            body_section = prompt[body_idx:body_idx + 500]
            self.assertNotIn("X" * 401, body_section, "Body not truncated to 400 chars")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_recall_context_fast_on_timeout(self):
        """recall_context must not block more than 6s on network timeout."""
        import urllib.request
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
            start = time.perf_counter()
            result = recall_context("test query")
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, "recall_context blocked too long on timeout")
        self.assertEqual(result, [])

    def test_remember_fast_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("conn")):
            start = time.perf_counter()
            result = remember("test text")
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
        self.assertIsNone(result)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_call_local_llm_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = call_local_llm("test prompt")
        self.assertIsNone(result)

    def test_process_email_returns_false_on_llm_failure(self):
        """process_email must return False when LLM returns None."""
        msg = {"uid": "1", "from_addr": "s@example.com", "subject": "Hi"}
        with patch.object(_mod, "run_herd", return_value=(0, json.dumps({"body": "Hello"}))):
            with patch.object(_mod, "generate_reply", return_value=None):
                result = process_email(msg)
        self.assertFalse(result)

    def test_recall_context_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("server error")):
            result = recall_context("some query")
        self.assertEqual(result, [])

    def test_remember_returns_none_on_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = remember("text")
        self.assertIsNone(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_run_herd_returns_code_and_output(self):
        """run_herd must return (returncode, stdout) tuple."""
        fake = MagicMock(returncode=0, stdout="output", stderr="")
        with patch("subprocess.run", return_value=fake):
            code, out = run_herd(["list"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "output")

    def test_run_herd_returns_1_on_exception(self):
        with patch("subprocess.run", side_effect=Exception("boom")):
            code, out = run_herd(["list"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")

    def test_call_local_llm_strips_think_tags(self):
        """call_local_llm must strip </think> reasoning artifacts."""
        response_json = json.dumps({
            "response": "<think>reasoning</think>The actual reply here."
        }).encode()
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = response_json

        with patch("urllib.request.urlopen", return_value=mock_r):
            result = call_local_llm("test prompt")

        self.assertNotIn("</think>", result)
        self.assertIn("The actual reply", result)

    def test_call_local_llm_returns_none_on_empty_response(self):
        response_json = json.dumps({"response": ""}).encode()
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = response_json

        with patch("urllib.request.urlopen", return_value=mock_r):
            result = call_local_llm("test prompt")

        self.assertIsNone(result)

    def test_generate_reply_includes_sender_in_prompt(self):
        """generate_reply prompt must reference the sender."""
        captured = []
        with patch.object(_mod, "call_local_llm", side_effect=lambda p: captured.append(p) or "reply"):
            generate_reply("sam@example.com", "Hello", "Test body")

        self.assertTrue(len(captured) > 0)
        # Sender or sender name should appear in prompt
        self.assertTrue(
            "sam" in captured[0].lower() or "sam@example.com" in captured[0].lower(),
            "Sender should be referenced in prompt"
        )

    def test_process_email_returns_false_on_herd_read_failure(self):
        msg = {"uid": "99", "from_addr": "x@example.com", "subject": "Test"}
        with patch.object(_mod, "run_herd", return_value=(1, "")):
            result = process_email(msg)
        self.assertFalse(result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_process_email_stores_memory_on_success(self):
        """process_email must store a memory entry on successful reply."""
        remembered = []
        msg = {"uid": "1", "from_addr": "sam@example.com", "subject": "Hello"}

        with patch.object(_mod, "run_herd", side_effect=[
            (0, json.dumps({"body": "Hi Nova!"})),  # read
            (0, "sent"),                              # send
        ]):
            with patch.object(_mod, "generate_reply", return_value="Hey Sam!"):
                with patch.object(_mod, "remember",
                                   side_effect=lambda t, **kw: remembered.append(t)):
                    result = process_email(msg)

        self.assertTrue(result)
        self.assertGreater(len(remembered), 0, "Memory should be stored on success")

    def test_main_processes_up_to_3_emails(self):
        """main() must limit processing to 3 emails per run."""
        process_calls = [0]

        def fake_process(msg):
            process_calls[0] += 1
            return True

        messages = [{"uid": str(i), "from_addr": f"u{i}@ex.com", "subject": f"S{i}"}
                    for i in range(10)]
        msg_output = json.dumps({"messages": messages})

        with patch.object(_mod, "run_herd", return_value=(0, msg_output)):
            with patch.object(_mod, "process_email", side_effect=fake_process):
                _mod.main()

        self.assertLessEqual(process_calls[0], 3, "main() should process at most 3 emails")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_returns_on_empty_inbox(self):
        """main() must return gracefully when no unread messages."""
        with patch.object(_mod, "run_herd", return_value=(0, json.dumps({"messages": []}))):
            try:
                _mod.main()
            except Exception as e:
                self.fail(f"main() raised on empty inbox: {e}")

    def test_main_handles_herd_list_failure(self):
        """main() must return gracefully when herd list fails."""
        with patch.object(_mod, "run_herd", return_value=(1, "")):
            try:
                _mod.main()
            except Exception as e:
                self.fail(f"main() raised on herd list failure: {e}")

    def test_generate_reply_returns_string_on_success(self):
        """generate_reply must return the LLM response string."""
        with patch.object(_mod, "call_local_llm", return_value="A generated reply."):
            result = generate_reply("sam@example.com", "Hello", "Test body")
        self.assertEqual(result, "A generated reply.")

    def test_ollama_model_constant(self):
        self.assertIsInstance(_mod.OLLAMA_MODEL, str)
        self.assertGreater(len(_mod.OLLAMA_MODEL), 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_inbox_claude.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIn("11434", _mod.OLLAMA_URL)
        self.assertIn("18790", _mod.MEMORY_URL)

    def test_all_functions_callable(self):
        for fn in [run_herd, recall_context, remember,
                    call_local_llm, generate_reply, process_email, _mod.main]:
            self.assertTrue(callable(fn), f"{fn.__name__} not callable")

    def test_script_readable(self):
        """Script must exist and be readable."""
        self.assertTrue(_SCRIPT.exists(), f"{_SCRIPT} does not exist")
        self.assertTrue(os.access(_SCRIPT, os.R_OK), f"{_SCRIPT} is not readable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
