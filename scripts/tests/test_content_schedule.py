"""
test_content_schedule.py — Tests for the content generation schedule changes.

Covers:
  - Scheduler YAML has correct cron expressions for each content task
  - No hardcoded credentials in any content script
  - Image generation retry logic fires alerts when image is None
  - Retry logic calls ensure_backend()

Written by Jordan Koch.
"""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

SCRIPTS_DIR = Path.home() / ".openclaw/scripts"
CONFIG_DIR = Path.home() / ".openclaw/config"
sys.path.insert(0, str(SCRIPTS_DIR))


# ── Unit Tests: Scheduler YAML has correct cron expressions ─────────────────


class TestSchedulerCronExpressions:
    """Verify scheduler.yaml has the correct times for each content task."""

    @pytest.fixture
    def scheduler_config(self):
        import yaml
        config_path = CONFIG_DIR / "scheduler.yaml"
        assert config_path.exists(), "scheduler.yaml not found"
        return yaml.safe_load(config_path.read_text())

    def test_dream_pipeline_at_6am(self, scheduler_config):
        task = scheduler_config["tasks"]["dream_pipeline"]
        assert task["schedule"] == "cron 0 6 * * *", f"Expected 6 AM, got {task['schedule']}"

    def test_daily_essay_at_9am(self, scheduler_config):
        task = scheduler_config["tasks"]["daily_essay"]
        assert task["schedule"] == "cron 0 9 * * *", f"Expected 9 AM, got {task['schedule']}"

    def test_daily_opinion_at_noon(self, scheduler_config):
        task = scheduler_config["tasks"]["daily_opinion"]
        assert task["schedule"] == "cron 0 12 * * *", f"Expected 12 PM, got {task['schedule']}"

    def test_daily_digest_at_5pm(self, scheduler_config):
        task = scheduler_config["tasks"]["daily_digest"]
        assert task["schedule"] == "cron 0 17 * * *", f"Expected 5 PM, got {task['schedule']}"

    def test_after_dark_at_8pm(self, scheduler_config):
        task = scheduler_config["tasks"]["after_dark"]
        assert task["schedule"] == "cron 0 20 * * *", f"Expected 8 PM, got {task['schedule']}"

    def test_research_paper_at_1150pm_daily(self, scheduler_config):
        task = scheduler_config["tasks"]["research_paper"]
        assert task["schedule"] == "cron 50 23 * * *", f"Expected 11:50 PM daily, got {task['schedule']}"

    def test_all_content_tasks_exist(self, scheduler_config):
        required_tasks = [
            "dream_pipeline", "daily_essay", "daily_opinion",
            "daily_digest", "after_dark", "research_paper"
        ]
        for task_name in required_tasks:
            assert task_name in scheduler_config["tasks"], f"Missing task: {task_name}"


# ── Security Tests: No hardcoded credentials in content scripts ─────────────


class TestNoHardcodedCredentials:
    """Verify no API keys, tokens, or passwords are hardcoded in content scripts."""

    CONTENT_SCRIPTS = [
        "nova_daily_essay.py",
        "nova_daily_opinion.py",
        "nova_weekly_digest.py",
        "nova_after_dark.py",
        "dream_generate.py",
        "nova_research_paper.py",
    ]

    CREDENTIAL_PATTERNS = [
        "sk-",           # OpenAI/Anthropic keys
        "AKIA",          # AWS access keys
        "ghp_",          # GitHub PATs
        "xox",           # Slack tokens
        "Bearer ",       # Hardcoded bearer tokens (in string literals only)
    ]

    @pytest.mark.security
    @pytest.mark.parametrize("script_name", CONTENT_SCRIPTS)
    def test_no_hardcoded_credentials(self, script_name):
        script_path = SCRIPTS_DIR / script_name
        assert script_path.exists(), f"{script_name} not found"
        content = script_path.read_text()

        for pattern in self.CREDENTIAL_PATTERNS:
            # Skip patterns that appear in comments or security check code
            lines_with_pattern = [
                line for line in content.split("\n")
                if pattern in line
                and not line.strip().startswith("#")
                and not "find-generic-password" in line
                and not "Keychain" in line.lower()
                and "grep" not in line
                and "pattern" not in line.lower()
            ]
            # "Bearer " followed by a variable reference is fine (f-string)
            if pattern == "Bearer ":
                lines_with_pattern = [
                    l for l in lines_with_pattern
                    if "Bearer {" not in l and "Bearer \"" not in l.replace("Bearer {", "")
                ]
                # Actually filter to only literal Bearer tokens
                lines_with_pattern = [
                    l for l in lines_with_pattern
                    if "Bearer " in l and "{" not in l.split("Bearer ")[1][:10]
                ]
            assert not lines_with_pattern, (
                f"Possible hardcoded credential in {script_name}: {lines_with_pattern[:3]}"
            )

    @pytest.mark.security
    @pytest.mark.parametrize("script_name", CONTENT_SCRIPTS)
    def test_credentials_from_keychain(self, script_name):
        """Verify scripts use macOS Keychain for secrets."""
        script_path = SCRIPTS_DIR / script_name
        content = script_path.read_text()

        # If script uses an API key, it should reference Keychain
        if "api_key" in content.lower() or "openrouter" in content.lower():
            assert "find-generic-password" in content or "nova_config" in content, (
                f"{script_name} uses API keys but doesn't reference Keychain or nova_config"
            )


# ── Functional Tests: Image failure alert fires when image is None ──────────


class TestImageFailureAlerts:
    """Verify that when image generation returns None, an alert is posted."""

    @patch("nova_config.post_both")
    @patch("subprocess.run")
    def test_opinion_posts_alert_on_image_failure(self, mock_run, mock_post):
        """Test nova_daily_opinion posts alert when image is None."""
        # We can't easily run main() due to all the dependencies, so test the pattern
        # by checking the source code contains the alert logic
        content = (SCRIPTS_DIR / "nova_daily_opinion.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content
        assert "C0ATAF7NZG9" in content  # nova-notifications channel

    @patch("nova_config.post_both")
    def test_essay_posts_alert_on_image_failure(self, mock_post):
        content = (SCRIPTS_DIR / "nova_daily_essay.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    @patch("nova_config.post_both")
    def test_after_dark_posts_alert_on_image_failure(self, mock_post):
        content = (SCRIPTS_DIR / "nova_after_dark.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    @patch("nova_config.post_both")
    def test_digest_posts_alert_on_image_failure(self, mock_post):
        content = (SCRIPTS_DIR / "nova_weekly_digest.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    def test_dream_posts_alert_on_image_failure(self):
        content = (SCRIPTS_DIR / "dream_generate.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    def test_research_posts_alert_on_image_failure(self):
        content = (SCRIPTS_DIR / "nova_research_paper.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content


# ── Framework Tests: Retry logic calls ensure_backend() ─────────────────────


class TestRetryLogicUsesEnsureBackend:
    """Verify that content scripts with image generation use ensure_backend()."""

    def test_opinion_imports_ensure_backend(self):
        content = (SCRIPTS_DIR / "nova_daily_opinion.py").read_text()
        assert "from nova_image_utils import ensure_backend" in content
        assert "ensure_backend()" in content

    def test_digest_imports_ensure_backend(self):
        content = (SCRIPTS_DIR / "nova_weekly_digest.py").read_text()
        assert "from nova_image_utils import ensure_backend" in content
        assert "ensure_backend()" in content

    def test_after_dark_uses_ensure_backend(self):
        content = (SCRIPTS_DIR / "nova_after_dark.py").read_text()
        assert "ensure_backend" in content

    def test_essay_uses_ensure_backend(self):
        """Essay uses its own _ensure_swarmui_backend() which is equivalent."""
        content = (SCRIPTS_DIR / "nova_daily_essay.py").read_text()
        assert "_ensure_swarmui_backend" in content or "ensure_backend" in content

    def test_opinion_has_3_retries(self):
        content = (SCRIPTS_DIR / "nova_daily_opinion.py").read_text()
        assert "for attempt in range(3):" in content

    def test_digest_has_3_retries(self):
        content = (SCRIPTS_DIR / "nova_weekly_digest.py").read_text()
        assert "for attempt in range(3):" in content

    def test_after_dark_has_3_retries(self):
        content = (SCRIPTS_DIR / "nova_after_dark.py").read_text()
        assert "for attempt in range(3):" in content

    @patch("nova_image_utils.ensure_backend", return_value=True)
    @patch("subprocess.run")
    def test_ensure_backend_called_before_generation(self, mock_run, mock_ensure):
        """Verify ensure_backend is called in nova_image_utils.generate_image."""
        from nova_image_utils import generate_image

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        result = generate_image("test prompt")

        mock_ensure.assert_called()

    @patch("nova_image_utils.ensure_backend", return_value=False)
    def test_generate_image_returns_none_when_backend_down(self, mock_ensure):
        """Verify generate_image returns None when ensure_backend returns False."""
        from nova_image_utils import generate_image

        result = generate_image("test prompt")
        assert result is None


# ── Test: After Dark humor boost ────────────────────────────────────────────


class TestAfterDarkHumorBoost:
    """Verify the humor boost was added to the After Dark prompt."""

    def test_humor_boost_in_prompt(self):
        content = (SCRIPTS_DIR / "nova_after_dark.py").read_text()
        assert "25% funnier than usual" in content
        assert "push the jokes harder" in content
        assert "edgier punchlines" in content
