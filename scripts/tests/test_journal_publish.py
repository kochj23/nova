#!/usr/bin/env python3
"""
test_journal_publish.py — Tests for nova_publish_journal.py.

Covers: dream publishing, essay publishing, email scrubbing, Hugo front matter
generation, git operations, image handling, path safety, CLI argument parsing.

Run: python3 -m pytest tests/test_journal_publish.py -v
Written by Jordan Koch.
"""

import json
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def publish_module():
    """Import nova_publish_journal fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_publish_journal" in mod:
            del sys.modules[mod]
    import nova_publish_journal
    return nova_publish_journal


@pytest.fixture
def sample_dream_md(tmp_path):
    """Create a sample dream markdown file."""
    dream = tmp_path / "2026-05-04.md"
    dream.write_text("""# Dream Journal — 2026-05-04
*Nova · written at 05:00 AM*
*Theme: "the archaeology of forgotten signals"*

![Dream](/path/to/image.png)

Mood: wonder

The dream began in a cavern of copper wires, each humming with voices from 1983.
Data streamed through fiber like starlight through cathedral glass.

*Generated locally on Apple Silicon*
""")
    return dream


@pytest.fixture
def sample_essay_text():
    return """# On the Nature of Defensive Computing

The modern security landscape presents a fascinating paradox. Organizations invest
heavily in perimeter defense while internal threats often prove more devastating.

This essay examines that tension through the lens of historical military fortification.

-- Nova"""


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmailScrubbing:
    """Tests for PII removal from published content."""

    def test_removes_personal_emails(self, publish_module):
        text = "Contact user@example.com or someone@corp.example.com for info"
        result = publish_module.scrub_emails(text)
        assert "user@example.com" not in result
        assert "someone@corp.example.com" not in result
        assert "[email redacted]" in result

    def test_preserves_nova_email(self, publish_module):
        text = "From: nova@digitalnoise.net"
        result = publish_module.scrub_emails(text)
        assert "nova@digitalnoise.net" in result

    def test_handles_multiple_emails(self, publish_module):
        text = "To: user1@test.com, user2@test.com, nova@digitalnoise.net"
        result = publish_module.scrub_emails(text)
        assert result.count("[email redacted]") == 2
        assert "nova@digitalnoise.net" in result

    def test_handles_no_emails(self, publish_module):
        text = "No emails in this text at all."
        result = publish_module.scrub_emails(text)
        assert result == text


class TestPublishDream:
    """Tests for dream publishing to Hugo."""

    @patch("nova_publish_journal.git_push")
    def test_publish_dream_creates_hugo_post(self, mock_git, publish_module, sample_dream_md, tmp_path):
        content_dir = tmp_path / "content" / "dreams"
        images_dir = tmp_path / "static" / "images" / "dreams"
        with patch.object(publish_module, "CONTENT_DREAMS", content_dir):
            with patch.object(publish_module, "IMAGES_DREAMS", images_dir):
                result = publish_module.publish_dream(str(sample_dream_md))
        assert result is True
        output = content_dir / "2026-05-04.md"
        assert output.exists()
        content = output.read_text()
        assert "archaeology of forgotten signals" in content
        assert "categories:" in content

    @patch("nova_publish_journal.git_push")
    def test_dream_strips_meta_lines(self, mock_git, publish_module, sample_dream_md, tmp_path):
        content_dir = tmp_path / "content" / "dreams"
        with patch.object(publish_module, "CONTENT_DREAMS", content_dir):
            with patch.object(publish_module, "IMAGES_DREAMS", tmp_path / "images"):
                publish_module.publish_dream(str(sample_dream_md))
        content = (content_dir / "2026-05-04.md").read_text()
        assert "# Dream Journal" not in content
        assert "Generated locally" not in content

    @patch("nova_publish_journal.git_push")
    def test_dream_scrubs_home_paths(self, mock_git, publish_module, tmp_path):
        dream = tmp_path / "2026-05-04.md"
        home = str(Path.home())
        dream.write_text(f'Theme: "test"\nMood: calm\nSaved to {home}/test.png\nContent here.')
        content_dir = tmp_path / "content" / "dreams"
        with patch.object(publish_module, "CONTENT_DREAMS", content_dir):
            with patch.object(publish_module, "IMAGES_DREAMS", tmp_path / "images"):
                publish_module.publish_dream(str(dream))
        content = (content_dir / "2026-05-04.md").read_text()
        assert home not in content

    def test_publish_dream_handles_missing_file(self, publish_module):
        result = publish_module.publish_dream("/nonexistent/path.md")
        assert result is False


class TestPublishEssay:
    """Tests for essay publishing to Hugo."""

    @patch("nova_publish_journal.git_push")
    def test_publish_essay_creates_post(self, mock_git, publish_module, sample_essay_text, tmp_path):
        content_dir = tmp_path / "content" / "essays"
        images_dir = tmp_path / "static" / "images" / "essays"
        with patch.object(publish_module, "CONTENT_ESSAYS", content_dir):
            with patch.object(publish_module, "IMAGES_ESSAYS", images_dir):
                result = publish_module.publish_essay("On Defensive Computing", "security", sample_essay_text)
        assert result is True
        # Should create a file with date prefix
        files = list(content_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "On Defensive Computing" in content
        assert "security" in content.lower() or "Security" in content

    @patch("nova_publish_journal.git_push")
    def test_essay_slug_generation(self, mock_git, publish_module, tmp_path):
        content_dir = tmp_path / "content" / "essays"
        with patch.object(publish_module, "CONTENT_ESSAYS", content_dir):
            with patch.object(publish_module, "IMAGES_ESSAYS", tmp_path / "images"):
                publish_module.publish_essay("A Title With Special Chars!@#$", "security", "Body text")
        files = list(content_dir.glob("*.md"))
        assert len(files) == 1
        # Filename should be slugified
        assert "!@#$" not in files[0].name

    @patch("nova_publish_journal.git_push")
    def test_essay_with_image(self, mock_git, publish_module, tmp_path):
        content_dir = tmp_path / "content" / "essays"
        images_dir = tmp_path / "static" / "images" / "essays"
        img = tmp_path / "test_image.png"
        img.write_bytes(b"fake png data")
        with patch.object(publish_module, "CONTENT_ESSAYS", content_dir):
            with patch.object(publish_module, "IMAGES_ESSAYS", images_dir):
                publish_module.publish_essay("Test", "security", "Body", str(img))
        content = list(content_dir.glob("*.md"))[0].read_text()
        assert "cover:" in content
        assert "/images/essays/" in content


class TestGitPush:
    """Tests for git commit and push operations."""

    @patch("subprocess.run")
    def test_git_push_success(self, mock_run, publish_module):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Should not raise
        publish_module.git_push("test: commit message")
        assert mock_run.call_count >= 2  # git add + git commit (+ push)

    @patch("subprocess.run")
    def test_git_push_nothing_to_commit(self, mock_run, publish_module):
        mock_run.return_value = MagicMock(returncode=1, stdout="nothing to commit", stderr="")
        # Should not raise
        publish_module.git_push("test: empty commit")


class TestCLIParsing:
    """Tests for command-line argument handling."""

    def test_dream_command_requires_path(self, publish_module):
        with patch("sys.argv", ["nova_publish_journal.py", "dream"]):
            with pytest.raises(SystemExit):
                publish_module.main()

    def test_essay_command_requires_args(self, publish_module):
        with patch("sys.argv", ["nova_publish_journal.py", "essay", "title"]):
            with pytest.raises(SystemExit):
                publish_module.main()

    def test_unknown_command_exits(self, publish_module):
        with patch("sys.argv", ["nova_publish_journal.py", "unknown_cmd"]):
            with pytest.raises(SystemExit):
                publish_module.main()


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestPublishSecurity:
    """Security tests for journal publishing."""

    def test_no_hardcoded_credentials(self, publish_module):
        import inspect
        source = inspect.getsource(publish_module)
        assert "sk-" not in source
        assert "xoxb-" not in source
        assert "ghp_" not in source

    def test_email_pattern_is_comprehensive(self, publish_module):
        """Verify the email regex catches common email formats."""
        pattern = publish_module.EMAIL_PATTERN
        assert pattern.search("test@example.com")
        assert pattern.search("user.name+tag@domain.co.uk")
        assert not pattern.search("not an email")

    def test_safe_emails_only_contains_nova(self, publish_module):
        """Only Nova's own email should bypass scrubbing."""
        assert publish_module.SAFE_EMAILS == {"nova@digitalnoise.net"}

    def test_hugo_root_is_expected_path(self, publish_module):
        """Hugo root should point to the expected journal location."""
        assert "nova-journal" in str(publish_module.HUGO_ROOT)


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestLogging:
    """Tests for logging behavior."""

    def test_log_creates_parent_dirs(self, publish_module, tmp_path):
        log_file = tmp_path / "subdir" / "test.log"
        with patch.object(publish_module, "LOG_FILE", log_file):
            publish_module.log("Test message")
        assert log_file.exists()
        assert "Test message" in log_file.read_text()


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring live filesystem."""

    def test_hugo_root_exists(self, publish_module):
        """Verify the Hugo journal directory exists."""
        if not publish_module.HUGO_ROOT.exists():
            pytest.skip("Hugo journal directory not present")
        assert (publish_module.HUGO_ROOT / ".git").exists()
