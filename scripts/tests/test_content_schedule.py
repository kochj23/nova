"""
test_content_schedule.py — Tests for the content generation schedule changes.

Covers:
  - Content generation is unified in nova_journal.py (commit 01b4241) and the
    journal_* schedule migrated off this node (prune 2754348): this node's
    scheduler.yaml must not resurrect the retired per-script generators
  - No hardcoded credentials in any content script
  - Image generation failure is non-fatal and alerts/logs when image is None
  - Image retry + SwarmUI ensure_backend() live in nova_image_utils.generate_image
    (local ComfyUI first, OpenRouter fallback) and the content scripts delegate to it

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


# ── Unit Tests: content scheduling contract ─────────────────────────────────

# Per-script generators that commit 01b4241 folded into nova_journal.py. None of them
# may be scheduled on this node again (the journal_* tasks run on .2; see prune 2754348).
RETIRED_CONTENT_SCRIPTS = [
    "nova_daily_opinion.py", "nova_daily_essay.py", "nova_after_dark.py",
    "nova_research_paper.py", "nova_weekly_digest.py", "dream_generate.py",
]

# nova_journal.py <profile> subcommands that replaced the old content tasks.
JOURNAL_PROFILES = ["essay", "opinion", "after-dark", "research", "digest", "dream", "art"]


def _journal_profile_keys() -> list[str]:
    """Top-level keys of the PROFILES dict in nova_journal.py, read via ast so the
    test never imports the script (module import resolves services from PG)."""
    import ast
    tree = ast.parse((SCRIPTS_DIR / "nova_journal.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PROFILES" for t in node.targets
        ) and isinstance(node.value, ast.Dict):
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError("PROFILES dict not found in nova_journal.py")


class TestContentScheduling:
    """Content generation runs through nova_journal.py profiles, not the old scripts."""

    @pytest.fixture
    def scheduler_config(self):
        import yaml
        config_path = CONFIG_DIR / "scheduler.yaml"
        assert config_path.exists(), "scheduler.yaml not found"
        return yaml.safe_load(config_path.read_text())

    def test_retired_generators_not_scheduled_here(self, scheduler_config):
        scheduled = {t.get("script") for t in scheduler_config["tasks"].values()}
        resurrected = scheduled & set(RETIRED_CONTENT_SCRIPTS)
        assert not resurrected, f"retired content scripts back in scheduler.yaml: {resurrected}"

    def test_no_journal_stubs_left_on_this_node(self, scheduler_config):
        """Prune 2754348 removed every disabled journal_* stub; a journal_* task on this
        node must therefore be a real, enabled task -- never a dormant stub."""
        for name, task in scheduler_config["tasks"].items():
            if task.get("script") == "nova_journal.py":
                assert task.get("enabled", True) is True, f"{name} is a disabled stub"
                assert task.get("args"), f"{name} has no profile arg"

    def test_journal_profiles_exist(self):
        keys = _journal_profile_keys()
        missing = [p for p in JOURNAL_PROFILES if p not in keys]
        assert not missing, f"nova_journal.py PROFILES missing: {missing}"

    def test_journal_takes_profile_subcommand(self):
        content = (SCRIPTS_DIR / "nova_journal.py").read_text()
        assert "profile_name = sys.argv[1].lower().strip()" in content

    def test_all_content_profiles_documented(self):
        """The module docstring is the operator-facing usage line; keep it in sync."""
        content = (SCRIPTS_DIR / "nova_journal.py").read_text()
        head = content[:800]
        for profile in JOURNAL_PROFILES:
            assert profile in head, f"profile {profile!r} missing from nova_journal.py usage docstring"


# ── Security Tests: No hardcoded credentials in content scripts ─────────────


class TestNoHardcodedCredentials:
    """Verify no API keys, tokens, or passwords are hardcoded in content scripts."""

    CONTENT_SCRIPTS = [
        "nova_journal.py",          # unified generator (essay/opinion/after-dark/...)
        "nova_daily_essay.py",
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


# ── Functional Tests: image failure is non-fatal and surfaces an alert ─────────


class TestImageFailureAlerts:
    """Verify that when image generation returns None, the article still ships and
    an alert/log line points at SwarmUI."""

    def test_journal_image_failure_is_non_fatal(self):
        """nova_journal.py (essay/opinion/after-dark/...) publishes without a cover
        and logs a WARNING rather than aborting the article."""
        content = (SCRIPTS_DIR / "nova_journal.py").read_text()
        assert "Image generation error (non-fatal)" in content
        assert "No cover image" in content
        assert "publishing without one" in content

    def test_essay_posts_alert_on_image_failure(self):
        content = (SCRIPTS_DIR / "nova_daily_essay.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    def test_after_dark_posts_alert_on_image_failure(self):
        content = (SCRIPTS_DIR / "nova_after_dark.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    def test_digest_posts_alert_on_image_failure(self):
        content = (SCRIPTS_DIR / "nova_weekly_digest.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    def test_dream_posts_alert_on_image_failure(self):
        content = (SCRIPTS_DIR / "dream_generate.py").read_text()
        assert ":warning: *Image generation failed*" in content
        assert "SwarmUI may need attention" in content

    def test_research_posts_alert_on_image_failure(self):
        """Research paper alerts through nova_notify.notify(...) (level=warning)."""
        content = (SCRIPTS_DIR / "nova_research_paper.py").read_text()
        assert 'notify(\n            "Image generation failed"' in content
        assert "SwarmUI may need attention" in content
        assert 'level="warning"' in content


# ── Framework Tests: retry + ensure_backend() live in nova_image_utils ─────────


def _image_utils():
    """Import nova_image_utils once; the module has no side effects at import."""
    import nova_image_utils
    return nova_image_utils


class TestRetryLogicUsesEnsureBackend:
    """Content scripts delegate to nova_image_utils.generate_image, which checks the
    SwarmUI backend, retries the local ComfyUI path MAX_RETRIES times and only then
    falls back to OpenRouter."""

    def test_journal_delegates_to_image_utils(self):
        content = (SCRIPTS_DIR / "nova_journal.py").read_text()
        assert "from nova_image_utils import generate_image" in content
        assert "generate_image(img_prompt, section=" in content

    def test_digest_imports_ensure_backend(self):
        content = (SCRIPTS_DIR / "nova_weekly_digest.py").read_text()
        assert "from nova_image_utils import ensure_backend" in content
        assert "ensure_backend()" in content

    def test_after_dark_delegates_to_image_utils(self):
        """After Dark dropped its own subprocess loop in 4aead36; the health check
        now happens inside nova_image_utils."""
        content = (SCRIPTS_DIR / "nova_after_dark.py").read_text()
        assert "from nova_image_utils import generate_image as _gen_image" in content
        assert 'section="after-dark"' in content

    def test_essay_uses_ensure_backend(self):
        """Essay uses its own _ensure_swarmui_backend() which is equivalent."""
        content = (SCRIPTS_DIR / "nova_daily_essay.py").read_text()
        assert "_ensure_swarmui_backend" in content or "ensure_backend" in content

    def test_digest_has_3_retries(self):
        content = (SCRIPTS_DIR / "nova_weekly_digest.py").read_text()
        assert "for attempt in range(3):" in content

    def test_image_utils_retries_local_backend(self):
        iu = _image_utils()
        content = (SCRIPTS_DIR / "nova_image_utils.py").read_text()
        assert "for attempt in range(MAX_RETRIES):" in content
        assert iu.MAX_RETRIES >= 2, "local ComfyUI must get at least one retry"
        assert iu.RETRY_DELAY > 0

    @patch("nova_image_utils.time.sleep")
    @patch("nova_image_utils._openrouter_generate", return_value=None)
    @patch("nova_image_utils._model_available_via_api", return_value=True)
    @patch("nova_image_utils.ensure_backend", return_value=True)
    @patch("nova_image_utils.subprocess.run")
    def test_ensure_backend_called_before_generation(
        self, mock_run, mock_ensure, mock_avail, mock_openrouter, mock_sleep
    ):
        """ensure_backend gates the local path; every local attempt is used before
        falling back to OpenRouter exactly once."""
        iu = _image_utils()
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        result = iu.generate_image("test prompt")

        assert result is None
        mock_ensure.assert_called_once()
        assert mock_run.call_count == iu.MAX_RETRIES
        assert mock_sleep.call_count == iu.MAX_RETRIES - 1
        mock_openrouter.assert_called_once()

    @patch("nova_image_utils._openrouter_generate", return_value=None)
    @patch("nova_image_utils.ensure_backend", return_value=False)
    @patch("nova_image_utils.subprocess.run")
    def test_generate_image_returns_none_when_backend_down(
        self, mock_run, mock_ensure, mock_openrouter
    ):
        """SwarmUI down: no local attempts, OpenRouter fallback tried once, None if
        that fails too."""
        iu = _image_utils()
        result = iu.generate_image("test prompt")
        assert result is None
        mock_run.assert_not_called()
        mock_openrouter.assert_called_once()

    @patch("nova_image_utils._openrouter_generate", return_value="/tmp/or.png")
    @patch("nova_image_utils.ensure_backend", return_value=False)
    def test_openrouter_fallback_when_backend_down(self, mock_ensure, mock_openrouter):
        iu = _image_utils()
        assert iu.generate_image("test prompt", section="after-dark") == "/tmp/or.png"
        assert mock_openrouter.call_args.args[1] == "after-dark"

    @patch("nova_image_utils._openrouter_generate")
    @patch("nova_image_utils._local_comfyui_generate", return_value="/tmp/local.png")
    def test_local_success_skips_openrouter(self, mock_local, mock_openrouter):
        iu = _image_utils()
        assert iu.generate_image("test prompt") == "/tmp/local.png"
        mock_openrouter.assert_not_called()


# ── Test: After Dark humor boost ────────────────────────────────────────────


class TestAfterDarkHumorBoost:
    """Verify the humor boost was added to the After Dark prompt."""

    def test_humor_boost_in_prompt(self):
        content = (SCRIPTS_DIR / "nova_after_dark.py").read_text()
        assert "25% funnier than usual" in content
        assert "push the jokes harder" in content
        assert "edgier punchlines" in content
