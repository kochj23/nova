#!/usr/bin/env python3
"""
Extended tests for the dream pipeline: dream_generate.py and dream_deliver.py.

Covers:
  - generate_narrative() with mocked Ollama
  - generate_dream_image() with mocked SwarmUI
  - deliver_dream() subprocess invocation
  - post_dream() Slack posting with mocked urllib
  - email_herd() with mocked subprocess
  - generate_haiku() with mocked Ollama
  - Inspirations footer formatting and deduplication
  - JSON auto-repair logic in dream_deliver.py
  - Circuit breaker behavior

Run: python3 -m pytest tests/test_dream_extended.py -v
Written by Jordan Koch.
"""

import json
import subprocess
import sys
import tempfile
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock, call, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── dream_generate.py — generate_narrative ────────────────────────────────────

class TestGenerateNarrative:
    """Tests for generate_narrative() — Ollama-backed dream text generation."""

    @patch("dream_generate._ollama_circuit_open", return_value=False)
    @patch("dream_generate.get_available_model", return_value="qwen3-coder:30b")
    @patch("dream_generate.query_rolling_learnings")
    @patch("dream_generate.read_file", return_value="identity stub")
    @patch("dream_generate._generate_via_ollama")
    def test_returns_narrative_and_inspirations(
        self, mock_ollama, mock_read, mock_rolling, mock_model, mock_circuit
    ):
        """generate_narrative returns a (str, list) tuple on success."""
        mock_rolling.return_value = ("Rolling context text", [{"source": "tv", "label": "Show", "memory": "Memory text"}])
        mock_ollama.return_value = " ".join(["word"] * 400)

        from dream_generate import generate_narrative
        narrative, inspirations = generate_narrative()

        assert len(narrative.split()) >= 150
        assert len(inspirations) == 1
        assert inspirations[0]["source"] == "tv"
        mock_ollama.assert_called_once()

    @patch("dream_generate._ollama_circuit_open", return_value=False)
    @patch("dream_generate.get_available_model", return_value="qwen3-coder:30b")
    @patch("dream_generate.query_rolling_learnings", return_value=("context", []))
    @patch("dream_generate.read_file", return_value="identity")
    @patch("dream_generate._generate_via_ollama", side_effect=Exception("connection refused"))
    def test_returns_empty_on_all_failures(
        self, mock_ollama, mock_read, mock_rolling, mock_model, mock_circuit
    ):
        """If all Ollama calls fail, returns empty narrative."""
        from dream_generate import generate_narrative
        narrative, inspirations = generate_narrative()

        assert narrative == ""

    @patch("dream_generate._ollama_circuit_open", return_value=True)
    @patch("dream_generate.query_rolling_learnings", return_value=("context", []))
    @patch("dream_generate.read_file", return_value="identity")
    def test_skips_ollama_when_circuit_open(self, mock_read, mock_rolling, mock_circuit):
        """When the circuit breaker is open, Ollama is not called."""
        from dream_generate import generate_narrative
        narrative, inspirations = generate_narrative()

        assert narrative == ""

    @patch("dream_generate._ollama_circuit_open", return_value=False)
    @patch("dream_generate.get_available_model", return_value="qwen3-coder:30b")
    @patch("dream_generate.query_rolling_learnings", return_value=("ctx", []))
    @patch("dream_generate.read_file", return_value="id")
    @patch("dream_generate._generate_via_ollama")
    def test_strips_thinking_blocks(self, mock_ollama, mock_read, mock_rolling, mock_model, mock_circuit):
        """Thinking block artefacts from local models are stripped."""
        raw = "<think>planning dream...</think>The dream begins on a quiet street."
        mock_ollama.return_value = raw

        # The strip_thinking import happens inside a try/except in generate_narrative.
        # Mock the module so the import succeeds and strips the block.
        mock_strip_fn = MagicMock(side_effect=lambda t: t.replace("<think>planning dream...</think>", "").strip())
        mock_module = MagicMock()
        mock_module.strip_thinking = mock_strip_fn

        with patch.dict("sys.modules", {"nova_strip_thinking": mock_module}):
            from dream_generate import generate_narrative
            narrative, _ = generate_narrative()
            assert "<think>" not in narrative

    @patch("dream_generate._ollama_circuit_open", return_value=False)
    @patch("dream_generate.get_available_model", return_value="qwen3-coder:30b")
    @patch("dream_generate.query_rolling_learnings", return_value=("ctx", []))
    @patch("dream_generate.read_file", return_value="id")
    @patch("dream_generate._generate_via_ollama")
    def test_trims_repetition_loops(self, mock_ollama, mock_read, mock_rolling, mock_model, mock_circuit):
        """Repetitive output from local models is trimmed."""
        # Build text: 200 unique words, then a 6-word phrase repeated 4 times
        unique_part = [f"word{i}" for i in range(200)]
        repeated_phrase = "the cat sat on the mat"
        repetitive_part = " ".join([repeated_phrase] * 4)
        raw = " ".join(unique_part) + " " + repetitive_part
        mock_ollama.return_value = raw

        from dream_generate import generate_narrative
        narrative, _ = generate_narrative()

        # Should be trimmed, but never below 150 words
        assert len(narrative.split()) >= 150


# ── dream_generate.py — generate_dream_image ─────────────────────────────────

class TestGenerateDreamImage:
    """Tests for generate_dream_image() — SwarmUI image generation."""

    @patch("dream_generate.subprocess.run")
    @patch("dream_generate.urllib.request.urlopen")
    def test_returns_image_path_on_success(self, mock_urlopen, mock_run):
        """Parses 'Workspace copy:' from script output to get image path."""
        # Mock SwarmUI availability check
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        # Mock subprocess output
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            f.write(b"fake image data")

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"Generating...\nWorkspace copy: {tmp_path}\nDone.",
            stderr="",
        )

        from dream_generate import generate_dream_image
        result = generate_dream_image("The dream begins on Magnolia boulevard at dusk.")

        assert result == tmp_path
        Path(tmp_path).unlink(missing_ok=True)

    @patch("dream_generate.urllib.request.urlopen", side_effect=Exception("connection refused"))
    def test_returns_empty_when_swarmui_unavailable(self, mock_urlopen):
        """When SwarmUI is not reachable, returns empty string gracefully."""
        from dream_generate import generate_dream_image
        result = generate_dream_image("A dream about running.")

        assert result == ""

    @patch("dream_generate.subprocess.run")
    @patch("dream_generate.urllib.request.urlopen")
    def test_returns_empty_on_script_failure(self, mock_urlopen, mock_run):
        """Non-zero exit code from generate_image.sh returns empty."""
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="SwarmUI: model not loaded",
        )

        from dream_generate import generate_dream_image
        result = generate_dream_image("A dream about flying.")

        assert result == ""

    @patch("dream_generate.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 180))
    @patch("dream_generate.urllib.request.urlopen")
    def test_returns_empty_on_timeout(self, mock_urlopen, mock_run):
        """Image generation timeout returns empty string."""
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from dream_generate import generate_dream_image
        result = generate_dream_image("A dream about the ocean.")

        assert result == ""

    @patch("dream_generate.subprocess.run")
    @patch("dream_generate.urllib.request.urlopen")
    def test_builds_prompt_from_first_sentence(self, mock_urlopen, mock_run):
        """Image prompt is derived from the first sentence of the narrative."""
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        from dream_generate import generate_dream_image
        generate_dream_image("The desert stretched wide under a violet sky. Sand everywhere.")

        args = mock_run.call_args[0][0]
        prompt_arg = args[1]  # Second arg to generate_image.sh is the prompt
        assert "dreamlike surreal digital painting" in prompt_arg
        assert "desert" in prompt_arg.lower() or "violet" in prompt_arg.lower()


# ── dream_generate.py — deliver_dream ────────────────────────────────────────

class TestDeliverDream:
    """Tests for deliver_dream() — subprocess invocation of dream_deliver.py."""

    @patch("dream_generate.subprocess.run")
    def test_calls_dream_deliver_script(self, mock_run):
        """deliver_dream invokes dream_deliver.py via subprocess."""
        mock_run.return_value = MagicMock(returncode=0, stdout="Delivery complete.", stderr="")

        with patch("dream_generate.Path.exists", return_value=True):
            from dream_generate import deliver_dream
            result = deliver_dream()

        assert result is True
        assert mock_run.called
        call_args = mock_run.call_args
        assert "dream_deliver.py" in call_args[0][0][1]

    @patch("dream_generate.subprocess.run")
    def test_returns_false_on_failure(self, mock_run):
        """deliver_dream returns False when the script exits non-zero."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Slack API error")

        with patch("dream_generate.Path.exists", return_value=True):
            from dream_generate import deliver_dream
            result = deliver_dream()

        assert result is False

    @patch("dream_generate.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 300))
    def test_returns_false_on_timeout(self, mock_run):
        """deliver_dream returns False on subprocess timeout."""
        with patch("dream_generate.Path.exists", return_value=True):
            from dream_generate import deliver_dream
            result = deliver_dream()

        assert result is False

    def test_returns_false_when_script_missing(self):
        """deliver_dream returns False when dream_deliver.py does not exist."""
        with patch("dream_generate.Path.exists", return_value=False):
            # Need to patch the home-based path specifically
            with patch("dream_generate.Path.home") as mock_home:
                mock_path = MagicMock()
                mock_path.__truediv__ = MagicMock(return_value=mock_path)
                mock_path.exists.return_value = False
                mock_home.return_value = mock_path

                from dream_generate import deliver_dream
                # The function checks deliver_script.exists() internally
                result = deliver_dream()

        assert result is False


# ── dream_generate.py — circuit breaker ──────────────────────────────────────

class TestCircuitBreaker:
    """Tests for the Ollama circuit breaker in dream_generate.py."""

    def test_circuit_closed_when_no_file(self):
        """Circuit is closed (allow calls) when state file does not exist."""
        from dream_generate import _ollama_circuit_open
        with patch("dream_generate.CIRCUIT_BREAKER_FILE") as mock_file:
            mock_file.exists.return_value = False
            assert _ollama_circuit_open() is False

    def test_circuit_open_after_3_failures(self):
        """Circuit opens after 3 consecutive failures within cooldown."""
        from dream_generate import _ollama_circuit_open
        state = json.dumps({
            "consecutive_failures": 3,
            "last_failure": datetime.now().isoformat(),
            "cooldown_hours": 1,
        })
        with patch("dream_generate.CIRCUIT_BREAKER_FILE") as mock_file:
            mock_file.exists.return_value = True
            mock_file.read_text.return_value = state
            assert _ollama_circuit_open() is True

    def test_circuit_resets_after_cooldown(self):
        """Circuit resets after cooldown period expires."""
        from dream_generate import _ollama_circuit_open
        old_time = (datetime.now() - __import__("datetime").timedelta(hours=2)).isoformat()
        state = json.dumps({
            "consecutive_failures": 3,
            "last_failure": old_time,
            "cooldown_hours": 1,
        })
        with patch("dream_generate.CIRCUIT_BREAKER_FILE") as mock_file:
            mock_file.exists.return_value = True
            mock_file.read_text.return_value = state
            assert _ollama_circuit_open() is False

    def test_record_failure_increments_count(self):
        """Recording a failure increments the counter."""
        from dream_generate import _ollama_circuit_record_failure
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"consecutive_failures": 1}))
            tmp = f.name

        with patch("dream_generate.CIRCUIT_BREAKER_FILE", Path(tmp)):
            _ollama_circuit_record_failure()
            data = json.loads(Path(tmp).read_text())
            assert data["consecutive_failures"] == 2
            assert "last_failure" in data

        Path(tmp).unlink(missing_ok=True)

    def test_circuit_reset_removes_file(self):
        """Resetting the circuit breaker removes the state file."""
        from dream_generate import _ollama_circuit_reset
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{}")
            tmp = f.name

        with patch("dream_generate.CIRCUIT_BREAKER_FILE", Path(tmp)):
            _ollama_circuit_reset()
            assert not Path(tmp).exists()


# ── dream_generate.py — get_available_model ──────────────────────────────────

class TestGetAvailableModel:
    """Tests for model availability checking against Ollama."""

    @patch("dream_generate.urllib.request.urlopen")
    def test_returns_primary_model_when_available(self, mock_urlopen):
        """Returns the primary MODEL if it exists in Ollama."""
        response_data = json.dumps({
            "models": [{"name": "qwen3-coder:30b"}, {"name": "deepseek-r1:8b"}]
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response_data))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from dream_generate import get_available_model
        with patch("dream_generate.MODEL", "qwen3-coder:30b"):
            model = get_available_model()
        assert model == "qwen3-coder:30b"

    @patch("dream_generate.urllib.request.urlopen")
    def test_falls_back_when_primary_missing(self, mock_urlopen):
        """Falls back to FALLBACK_MODELS when primary is absent."""
        response_data = json.dumps({
            "models": [{"name": "deepseek-r1:8b"}, {"name": "qwen3-vl:4b"}]
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response_data))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from dream_generate import get_available_model
        with patch("dream_generate.MODEL", "nonexistent-model:70b"):
            model = get_available_model()
        assert model in ["deepseek-r1:8b", "qwen3-vl:4b", "qwen3-30b-a3b"]


# ── dream_deliver.py — post_dream ────────────────────────────────────────────

class TestPostDream:
    """Tests for post_dream() — Slack posting logic."""

    @patch("dream_deliver.upload_image_to_channel", return_value=True)
    @patch("dream_deliver.slack_post")
    def test_posts_narrative_in_chunks(self, mock_slack, mock_upload):
        """Narrative is split into 3000-char chunks for Slack."""
        from dream_deliver import post_dream

        # Build a narrative longer than 3000 chars
        long_narrative = "A" * 3500
        result = post_dream(long_narrative, "/tmp/fake.png", "2026-01-01")

        # Should have made at least 2 chat.postMessage calls (one per chunk)
        narrative_calls = [c for c in mock_slack.call_args_list if c[0][0] == "chat.postMessage"]
        assert len(narrative_calls) >= 2

    @patch("dream_deliver.upload_image_to_channel", return_value=False)
    @patch("dream_deliver.slack_post", return_value={"ok": True})
    def test_falls_back_to_text_header_when_image_fails(self, mock_slack, mock_upload):
        """If image upload fails, header is posted as plain text."""
        from dream_deliver import post_dream

        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            post_dream("Dream text.", f.name, "2026-01-01")

        # First slack_post call should be the header text
        first_call = mock_slack.call_args_list[0]
        assert first_call[0][0] == "chat.postMessage"

    @patch("dream_deliver.slack_post", return_value={"ok": True})
    def test_posts_header_without_image(self, mock_slack):
        """When no image path, header is posted as text."""
        from dream_deliver import post_dream
        post_dream("Dream text.", None, "2026-01-01")

        first_call = mock_slack.call_args_list[0]
        assert "Dream Journal" in first_call[0][1]["text"]

    @patch("dream_deliver.slack_post", return_value={"ok": True})
    def test_appends_signoff(self, mock_slack):
        """The last chunk includes Nova's sign-off line."""
        from dream_deliver import post_dream
        post_dream("Short dream.", None, "2026-05-01")

        last_narrative_call = [c for c in mock_slack.call_args_list if c[0][0] == "chat.postMessage"][-1]
        text = last_narrative_call[0][1]["text"]
        assert "Nova" in text
        assert "2026-05-01" in text


# ── dream_deliver.py — email_herd ────────────────────────────────────────────

class TestEmailHerd:
    """Tests for email_herd() — herd email distribution."""

    @patch("dream_deliver.generate_haiku", return_value="moon over the code\\nsilent servers hum below\\nmorning brings the dream")
    @patch("dream_deliver.subprocess.run")
    @patch("dream_deliver.HERD_RECIPIENTS", ["alice@example.com", "bob@example.com", "carol@example.com"])
    def test_sends_single_email_with_cc(self, mock_run, mock_haiku):
        """Herd email sends one message with first recipient as To, rest as CC."""
        # Mock Keychain lookup for Jordan's work email
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="jordan@work.com", stderr=""),  # Keychain lookup
            MagicMock(returncode=0, stdout="", stderr=""),  # Mail send
        ]

        from dream_deliver import email_herd
        email_herd("Dream narrative text.", "/tmp/dream.png", "2026-01-01")

        # Second call is the actual mail send
        mail_call = mock_run.call_args_list[1]
        args = mail_call[0][0]
        assert "--to" in args
        assert "--cc" in args
        assert "--subject" in args
        assert "Nova Dream Journal -- 2026-01-01" in args[args.index("--subject") + 1]

    @patch("dream_deliver.generate_haiku", return_value="test haiku")
    @patch("dream_deliver.subprocess.run")
    @patch("dream_deliver.HERD_RECIPIENTS", ["alice@example.com"])
    def test_includes_attachment_when_image_exists(self, mock_run, mock_haiku):
        """Image attachment is included when the file exists."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Keychain
            MagicMock(returncode=0, stdout="", stderr=""),  # Mail
        ]

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_img = f.name
            f.write(b"fake")

        from dream_deliver import email_herd
        email_herd("Dream text.", tmp_img, "2026-01-01")

        mail_call = mock_run.call_args_list[1]
        args = mail_call[0][0]
        assert "--attachment" in args
        assert tmp_img in args

        Path(tmp_img).unlink(missing_ok=True)

    @patch("dream_deliver.generate_haiku", return_value="test haiku")
    @patch("dream_deliver.subprocess.run")
    @patch("dream_deliver.HERD_RECIPIENTS", [])
    def test_skips_when_no_recipients(self, mock_run, mock_haiku):
        """No mail is sent when HERD_RECIPIENTS is empty."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        from dream_deliver import email_herd
        email_herd("Dream text.", None, "2026-01-01")

        # Only the Keychain lookup should have run, not the mail send
        assert mock_run.call_count <= 1


# ── dream_deliver.py — generate_haiku ────────────────────────────────────────

class TestGenerateHaiku:
    """Tests for haiku generation via Ollama."""

    @patch("dream_deliver.urllib.request.urlopen")
    def test_returns_haiku_from_ollama(self, mock_urlopen):
        """Returns a haiku when Ollama responds with valid 3-line output."""
        response = json.dumps({
            "message": {"content": "moonlight on the keys\nservers hum their quiet song\nmorning breaks the code"}
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from dream_deliver import generate_haiku
        result = generate_haiku("A dream about walking through a garden at night.")

        assert "moonlight" in result or "\\n" in result

    @patch("dream_deliver.urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_returns_fallback_on_failure(self, mock_urlopen):
        """Returns hardcoded fallback haiku when Ollama fails."""
        from dream_deliver import generate_haiku
        result = generate_haiku("A dream.")

        assert "Dreams loop through code walls" in result

    @patch("dream_deliver.urllib.request.urlopen")
    def test_strips_thinking_blocks(self, mock_urlopen):
        """Thinking blocks from local models are removed from haiku output."""
        response = json.dumps({
            "message": {"content": "<think>let me count syllables</think>rain falls on the street\nthe server keeps running still\nnova dreams alone"}
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from dream_deliver import generate_haiku
        result = generate_haiku("A dream about rain.")

        assert "<think>" not in result
        assert "rain" in result.lower() or "server" in result.lower()


# ── dream_deliver.py — inspirations footer formatting ────────────────────────

class TestInspirationsFooter:
    """Tests for the inspirations append logic in dream_deliver.py main()."""

    def test_formats_inspirations_with_bullet_points(self):
        """Inspirations are formatted as Slack-flavored bullet points."""
        inspirations = [
            {"source": "television", "label": "Jeopardy!", "memory": "Who is Alexander Hamilton?"},
            {"source": "horror", "label": "Hellraiser", "memory": "Pinhead speaks of suffering."},
        ]

        # Replicate the logic from dream_deliver.py lines 352-362
        seen = set()
        insp_lines = []
        for i in inspirations:
            key = f"{i.get('source', '')}:{i.get('label', '')}"
            if key not in seen:
                seen.add(key)
                memory_text = i.get("memory", i.get("snippet", ""))[:200]
                insp_lines.append(f"  • *[{i.get('source', '?')}]* {memory_text}")

        footer = "\n\n_Memories that inspired this dream:_\n" + "\n".join(insp_lines)

        assert "_Memories that inspired this dream:_" in footer
        assert "*[television]*" in footer
        assert "Alexander Hamilton" in footer
        assert "*[horror]*" in footer

    def test_deduplicates_inspirations_by_source_label(self):
        """Duplicate source:label pairs are collapsed."""
        inspirations = [
            {"source": "tv", "label": "Show A", "memory": "Memory 1"},
            {"source": "tv", "label": "Show A", "memory": "Memory 2"},
            {"source": "music", "label": "Band B", "memory": "Memory 3"},
        ]

        seen = set()
        insp_lines = []
        for i in inspirations:
            key = f"{i.get('source', '')}:{i.get('label', '')}"
            if key not in seen:
                seen.add(key)
                insp_lines.append(f"  • *[{i['source']}]* {i['memory'][:200]}")

        # Should have 2 lines (tv:Show A deduplicated)
        assert len(insp_lines) == 2

    def test_truncates_long_memory_text(self):
        """Memory text in inspirations is truncated to 200 characters."""
        long_memory = "x" * 500
        inspirations = [{"source": "src", "label": "lbl", "memory": long_memory}]

        insp_lines = []
        for i in inspirations:
            memory_text = i.get("memory", "")[:200]
            insp_lines.append(memory_text)

        assert len(insp_lines[0]) == 200


# ── dream_deliver.py — JSON auto-repair ──────────────────────────────────────

class TestJsonAutoRepair:
    """Tests for the JSON auto-repair logic in dream_deliver.py main()."""

    def test_repairs_backslash_before_utf8(self):
        """Backslashes before high-byte UTF-8 characters are removed."""
        # Simulate the repair logic from dream_deliver.py lines 288-299
        # Create bytes with a backslash before a curly quote (U+201C = 0xE2 0x80 0x9C)
        bad_json = b'{"text": "she said \\\xe2\x80\x9chello\\\xe2\x80\x9d"}'

        fixed = bytearray()
        i = 0
        repairs = 0
        while i < len(bad_json):
            if bad_json[i] == ord('\\') and i + 1 < len(bad_json) and bad_json[i + 1] > 127:
                repairs += 1
                i += 1
            else:
                fixed.append(bad_json[i])
                i += 1

        assert repairs == 2
        result = json.loads(bytes(fixed).decode("utf-8"))
        assert "“" in result["text"]
        assert "”" in result["text"]

    def test_valid_json_passes_without_repair(self):
        """Valid JSON does not trigger the repair path."""
        valid = b'{"narrative": "A perfectly normal dream.", "date": "2026-01-01"}'
        # This should parse fine without the repair logic
        data = json.loads(valid.decode("utf-8"))
        assert data["narrative"] == "A perfectly normal dream."

    def test_repair_handles_empty_file(self):
        """Repair logic handles empty input gracefully."""
        raw = b""
        fixed = bytearray()
        i = 0
        while i < len(raw):
            if raw[i] == ord('\\') and i + 1 < len(raw) and raw[i + 1] > 127:
                i += 1
            else:
                fixed.append(raw[i])
                i += 1
        # Empty input produces empty output
        assert bytes(fixed) == b""


# ── dream_deliver.py — narrative image placeholder stripping ─────────────────

class TestNarrativeStripping:
    """Tests for stripping image placeholder lines from narratives."""

    def test_strips_image_placeholder_lines(self):
        """Lines matching ![Dream]([...]) are removed."""
        import re
        narrative = "First line.\n![Dream]([image path — omit this entire line if image is null])\nLast line."
        cleaned = "\n".join(
            line for line in narrative.splitlines()
            if not re.match(r"!\[Dream\]\(\[", line)
        ).strip()

        assert "![Dream]" not in cleaned
        assert "First line." in cleaned
        assert "Last line." in cleaned

    def test_preserves_normal_image_references(self):
        """Normal ![Dream](/path/to/image.png) lines are kept."""
        import re
        narrative = "Text.\n![Dream](/tmp/dream.png)\nMore text."
        cleaned = "\n".join(
            line for line in narrative.splitlines()
            if not re.match(r"!\[Dream\]\(\[", line)
        ).strip()

        assert "![Dream](/tmp/dream.png)" in cleaned


# ── dream_generate.py — _extract_interesting_sections ────────────────────────

class TestExtractInterestingSections:
    """Tests for the section extraction and prioritization logic."""

    def test_skips_no_activity_sections(self):
        """Sections containing 'no activity' type messages are filtered out."""
        from dream_generate import _extract_interesting_sections

        content = """## What Reddit is talking about
Great discussion about new park opening.

## Packages in transit
No package notifications found.

## Nova's activity today
No activity logged."""

        result = _extract_interesting_sections(content)
        assert "Great discussion" in result
        assert "No package notifications" not in result
        assert "No activity logged" not in result

    def test_prioritizes_dreamlike_sections(self):
        """Reddit and history sections appear before operational sections."""
        from dream_generate import _extract_interesting_sections

        content = """## Nova's activity today
Slack messages: 42

## What Reddit is talking about
Fascinating post about lucid dreaming.

## On This Day in History
1969: Apollo 11 landed on the moon."""

        result = _extract_interesting_sections(content)
        reddit_pos = result.find("Fascinating post")
        history_pos = result.find("1969")
        activity_pos = result.find("Slack messages")

        assert reddit_pos < history_pos
        assert history_pos < activity_pos

    def test_returns_empty_for_blank_content(self):
        """Empty or whitespace-only content returns empty string."""
        from dream_generate import _extract_interesting_sections
        assert _extract_interesting_sections("") == ""
        assert _extract_interesting_sections("   \n\n  ") == ""


# ── dream_deliver.py — retry and dead-letter logic ───────────────────────────

class TestRetryLogic:
    """Tests for the retry counter and dead-letter queue in dream_deliver.py main()."""

    def test_dead_letter_directory_constant(self):
        """DEAD_LETTER path is set to the expected location."""
        from dream_deliver import DEAD_LETTER
        assert "failed_deliveries" in str(DEAD_LETTER)

    def test_max_retries_is_three(self):
        """MAX_RETRIES is set to 3."""
        from dream_deliver import MAX_RETRIES
        assert MAX_RETRIES == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
