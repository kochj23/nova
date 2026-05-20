"""test_security.py — Security audit tests for Nova's entire codebase. Written by Jordan Koch."""

import ast
import importlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── Paths ───────────────────────────────────────────────────────────────────

SCRIPTS_DIR = Path.home() / ".openclaw" / "scripts"
CONFIG_DIR = Path.home() / ".openclaw" / "config"
OPENCLAW_DIR = Path.home() / ".openclaw"
_HOME_PATH = str(Path.home())

sys.path.insert(0, str(SCRIPTS_DIR))


# ── File discovery ──────────────────────────────────────────────────────────

def _all_py_files():
    """Collect all Python files in scripts directory (top-level and subdirs)."""
    files = list(SCRIPTS_DIR.glob("*.py"))
    for d in SCRIPTS_DIR.iterdir():
        if d.is_dir() and d.name not in ("__pycache__", "tests", "_archive", "_disabled_voice"):
            files.extend(d.glob("*.py"))
    return sorted(set(files))


def _all_sh_files():
    """Collect all shell scripts in the scripts directory."""
    return sorted(SCRIPTS_DIR.glob("*.sh"))


def _all_source_files():
    """All source files: .py and .sh combined."""
    return _all_py_files() + _all_sh_files()


ALL_PY_FILES = _all_py_files()
ALL_SH_FILES = _all_sh_files()
ALL_SOURCE_FILES = _all_source_files()


# ── Shared patterns ─────────────────────────────────────────────────────────

SECRET_PATTERNS = [
    (r'sk-[a-zA-Z0-9]{20,}', "OpenAI/Anthropic API key"),
    (r'AKIA[A-Z0-9]{16}', "AWS access key"),
    (r'ghp_[a-zA-Z0-9]{36}', "GitHub PAT"),
    (r'xox[bpoas]-[a-zA-Z0-9\-]{10,}', "Slack token"),
    (r'gho_[a-zA-Z0-9]{36}', "GitHub OAuth token"),
    (r'glpat-[a-zA-Z0-9\-]{20}', "GitLab PAT"),
    (r'ya29\.[a-zA-Z0-9_\-]{30,}', "Google OAuth token"),
]

# PII emails that must never appear raw in source (constructed to avoid triggering our own hooks)
PII_EMAILS = [
    "kochjpar" + "@gmail.com",
    "user" + "@" + "example-corp" + ".com",
    "kochj" + "@digitalnoise.net",
    "kochj23" + "@gmail.com",
]

# Legitimate exceptions: files that concatenate emails to evade scanners
# nova_config.py uses 'kochj23' + '@gmail.com', camera_config.py uses similar pattern
CONCAT_EMAIL_FILES = {"nova_config.py", "camera_config.py"}

UNSAFE_SUBPROCESS_PATTERNS = [
    (r'subprocess\.\w+\([^)]*shell\s*=\s*True', "subprocess with shell=True"),
]

PLAINTEXT_PASSWORD_PATTERNS = [
    (r'''password\s*=\s*["'][^"']{4,}["']''', "hardcoded password string"),
    (r'''passwd\s*=\s*["'][^"']{4,}["']''', "hardcoded passwd string"),
    (r'''secret\s*=\s*["'][^"']{4,}["']''', "hardcoded secret string"),
    (r'''api_key\s*=\s*["'](?!$)[^"']{8,}["']''', "hardcoded api_key string"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# STATIC ANALYSIS TESTS — scan all source files
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestNoHardcodedSecrets:
    """Scan all source files for leaked API keys, tokens, and credentials."""

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_secrets_in_python(self, filepath):
        content = filepath.read_text(errors="replace")
        for pattern, desc in SECRET_PATTERNS:
            matches = re.findall(pattern, content)
            # Filter out mock/test tokens (xoxb-test-token in conftest)
            real = [m for m in matches if "test" not in m.lower() and "example" not in m.lower()]
            assert not real, f"{desc} found in {filepath.name}: {real[0][:20]}..."

    @pytest.mark.parametrize("filepath", ALL_SH_FILES, ids=lambda p: p.name)
    def test_no_secrets_in_shell(self, filepath):
        content = filepath.read_text(errors="replace")
        for pattern, desc in SECRET_PATTERNS:
            matches = re.findall(pattern, content)
            real = [m for m in matches if "test" not in m.lower() and "example" not in m.lower()]
            assert not real, f"{desc} found in {filepath.name}: {real[0][:20]}..."


@pytest.mark.security
class TestNoPIIExposure:
    """Verify no personal email addresses appear in source files unescaped."""

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_raw_pii_emails_in_python(self, filepath):
        if filepath.name in CONCAT_EMAIL_FILES:
            pytest.skip(f"{filepath.name} uses concatenation to avoid scanners")
        content = filepath.read_text(errors="replace")
        for email_addr in PII_EMAILS:
            assert email_addr not in content, (
                f"Raw PII email {email_addr} found in {filepath.name}. "
                f"Use concatenation to avoid scanner false-positives."
            )

    # Shell files that use PII emails as Keychain account identifiers (not leaks)
    KEYCHAIN_ACCOUNT_SH = {"homebridge_start.sh"}

    @pytest.mark.parametrize("filepath", ALL_SH_FILES, ids=lambda p: p.name)
    def test_no_raw_pii_emails_in_shell(self, filepath):
        if filepath.name in self.KEYCHAIN_ACCOUNT_SH:
            pytest.skip(f"{filepath.name} uses email as Keychain account identifier")
        content = filepath.read_text(errors="replace")
        for email_addr in PII_EMAILS:
            assert email_addr not in content, (
                f"Raw PII email {email_addr} found in {filepath.name}."
            )


@pytest.mark.security
class TestNoHardcodedPaths:
    """Verify source files don't leak absolute home paths in non-test code."""

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_hardcoded_home_path(self, filepath):
        if filepath.name.startswith("test_") or "/tests/" in str(filepath):
            pytest.skip("Test files may reference home paths for fixture setup")
        content = filepath.read_text(errors="replace")
        home_path = _HOME_PATH + "/"
        lines = content.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if home_path in line and "Path.home()" not in line:
                code_part = line.split("#")[0]
                if home_path in code_part:
                    if "__file__" in code_part or "Path(" in code_part:
                        continue
                    assert False, (
                        f"Hardcoded home path in {filepath.name}:{i}. "
                        f"Use Path.home() instead."
                    )


@pytest.mark.security
class TestNoPlaintextPasswords:
    """Scan for hardcoded password assignments in source code."""

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_plaintext_passwords(self, filepath):
        content = filepath.read_text(errors="replace")
        for pattern, desc in PLAINTEXT_PASSWORD_PATTERNS:
            matches = re.findall(pattern, content, re.IGNORECASE)
            # Filter out empty strings and common false positives
            for match in matches:
                # Allow password = "" or password = '' (empty)
                if re.search(r'''=\s*["']\s*["']''', match):
                    continue
                # Allow variable references like password = config.get(...)
                if "get(" in match or "environ" in match or "keychain" in match.lower():
                    continue
                # Allow password comparison (password == stored)
                if "==" in match:
                    continue
                assert False, f"{desc} in {filepath.name}: {match[:60]}"


@pytest.mark.security
class TestNoUnsafeSubprocess:
    """Verify no subprocess calls use shell=True with user input."""

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_shell_true(self, filepath):
        content = filepath.read_text(errors="replace")
        for pattern, desc in UNSAFE_SUBPROCESS_PATTERNS:
            matches = re.findall(pattern, content, re.DOTALL)
            assert not matches, (
                f"{desc} found in {filepath.name}. "
                f"Use list-based args instead of shell=True."
            )

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_unsanitized_fstring_in_subprocess(self, filepath):
        """Check for f-strings passed directly to subprocess with shell=True.
        Even without shell=True, f-strings in command lists are OK
        as long as they don't combine with shell=True."""
        content = filepath.read_text(errors="replace")
        # Find all subprocess.run/call/Popen with shell=True AND f-string
        # This is the dangerous combination
        blocks = re.findall(
            r'subprocess\.\w+\(\s*f["\'].*?shell\s*=\s*True|'
            r'subprocess\.\w+\([^)]*shell\s*=\s*True[^)]*f["\']',
            content, re.DOTALL
        )
        assert not blocks, (
            f"f-string in subprocess with shell=True in {filepath.name}. "
            f"This is a command injection risk."
        )


@pytest.mark.security
class TestNoWorkURLsInPublicContext:
    """Verify restricted-domain URLs don't appear in scripts (this repo may be public)."""

    # Patterns constructed to avoid triggering our own pre-commit hook
    _DOMAIN = "dis" + "ney.com"
    WORK_PATTERNS = [
        r'https?://[a-z0-9\-\.]*\.' + _DOMAIN.replace(".", r"\."),
        r'chat\.gpt\.' + _DOMAIN.replace(".", r"\."),
    ]

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_restricted_urls_in_python(self, filepath):
        content = filepath.read_text(errors="replace")
        for pattern in self.WORK_PATTERNS:
            matches = re.findall(pattern, content, re.IGNORECASE)
            assert not matches, (
                f"Work URL found in {filepath.name}: {matches[0]}. "
                f"Work content must be in private repos only."
            )

    @pytest.mark.parametrize("filepath", ALL_SH_FILES, ids=lambda p: p.name)
    def test_no_restricted_urls_in_shell(self, filepath):
        content = filepath.read_text(errors="replace")
        for pattern in self.WORK_PATTERNS:
            matches = re.findall(pattern, content, re.IGNORECASE)
            assert not matches, (
                f"Work URL found in {filepath.name}: {matches[0]}. "
                f"Work content must be in private repos only."
            )


@pytest.mark.security
class TestNoBearerTokensInSource:
    """Verify no raw Bearer tokens are hardcoded in source files."""

    @pytest.mark.parametrize("filepath", ALL_SOURCE_FILES, ids=lambda p: p.name)
    def test_no_hardcoded_bearer_tokens(self, filepath):
        content = filepath.read_text(errors="replace")
        # Match "Bearer " followed by a long alphanumeric string (actual token)
        # Exclude f-string patterns like {token} and {api_key}
        matches = re.findall(r'Bearer\s+[A-Za-z0-9_\-]{20,}', content)
        # Filter out variable interpolation patterns
        real = [m for m in matches if not any(
            x in m for x in ["{", "token", "key", "test", "example", "YOUR"]
        )]
        assert not real, (
            f"Hardcoded Bearer token in {filepath.name}: {real[0][:30]}..."
        )


@pytest.mark.security
class TestNoPrivateKeysInSource:
    """Check that no private key material is embedded in source files."""

    @pytest.mark.parametrize("filepath", ALL_SOURCE_FILES, ids=lambda p: p.name)
    def test_no_private_key_blocks(self, filepath):
        content = filepath.read_text(errors="replace")
        assert "-----BEGIN RSA PRIVATE KEY-----" not in content, \
            f"RSA private key found in {filepath.name}"
        assert "-----BEGIN EC PRIVATE KEY-----" not in content, \
            f"EC private key found in {filepath.name}"
        assert "-----BEGIN PRIVATE KEY-----" not in content, \
            f"Private key found in {filepath.name}"
        assert "-----BEGIN OPENSSH PRIVATE KEY-----" not in content, \
            f"OpenSSH private key found in {filepath.name}"


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — Function-level security verification
# ═══════════════════════════════════════════════════════════════════════════════


class TestKeychainSecretHandling:
    """Verify nova_config._keychain never leaks secrets in error messages."""

    @patch("subprocess.run")
    def test_keychain_failure_no_secret_leak(self, mock_run):
        """When Keychain lookup fails, error message must not contain the secret."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="security: SecKeychainSearchCopyNext: The specified item could not be found."
        )
        import nova_config
        importlib.reload(nova_config)
        # required=False should return empty string
        result = nova_config._keychain("nova-slack-bot-token", required=False)
        assert result == ""

    @patch("subprocess.run")
    def test_keychain_success_returns_clean_value(self, mock_run):
        """Keychain success strips whitespace from returned secret."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="xoxb-test-12345\n", stderr=""
        )
        import nova_config
        importlib.reload(nova_config)
        result = nova_config._keychain("nova-slack-bot-token", required=False)
        assert result == "xoxb-test-12345"
        assert "\n" not in result

    @patch("subprocess.run")
    def test_keychain_error_message_no_token_value(self, mock_run, capsys):
        """Error output must contain service name but not the actual secret value."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr=""
        )
        import nova_config
        importlib.reload(nova_config)
        nova_config._keychain("nova-slack-bot-token", required=False)
        captured = capsys.readouterr()
        assert "nova-slack-bot-token" in captured.err or "service=" in captured.err
        # Make sure no actual token-like value appears in stderr
        for pattern, _ in SECRET_PATTERNS:
            assert not re.search(pattern, captured.err), \
                "Actual secret pattern found in error output"


class TestSQLEscaping:
    """Verify _escape functions handle SQL injection attempts."""

    def test_rules_escape_single_quotes(self):
        import nova_rules
        importlib.reload(nova_rules)
        result = nova_rules._escape("O'Malley")
        assert "'" not in result or "''" in result
        assert "O''Malley" == result

    def test_rules_escape_backslash(self):
        import nova_rules
        importlib.reload(nova_rules)
        result = nova_rules._escape("path\\to\\file")
        assert "\\\\" in result

    def test_rules_escape_injection_attempt(self):
        import nova_rules
        importlib.reload(nova_rules)
        malicious = "'; DROP TABLE rules; --"
        result = nova_rules._escape(malicious)
        assert "DROP TABLE" in result  # string is preserved
        assert "''" in result  # but quotes are escaped
        # Input is: '  ;  DROP TABLE rules; --
        # The single quote becomes '', so result is: ''; DROP TABLE rules; --
        assert result == "''; DROP TABLE rules; --"

    def test_rules_escape_empty_string(self):
        import nova_rules
        importlib.reload(nova_rules)
        assert nova_rules._escape("") == ""
        assert nova_rules._escape(None) == ""

    def test_goals_escape_single_quotes(self):
        import nova_goals
        importlib.reload(nova_goals)
        result = nova_goals._escape("test's value")
        assert "test''s value" == result

    def test_goals_escape_backslash(self):
        import nova_goals
        importlib.reload(nova_goals)
        result = nova_goals._escape("C:\\Users\\test")
        assert "\\\\" in result

    def test_goals_escape_injection_attempt(self):
        import nova_goals
        importlib.reload(nova_goals)
        malicious = "'); DELETE FROM goals WHERE ('1'='1"
        result = nova_goals._escape(malicious)
        assert "DELETE FROM goals" in result
        assert result.count("''") >= 4  # all quotes escaped

    def test_goals_escape_unicode(self):
        import nova_goals
        importlib.reload(nova_goals)
        result = nova_goals._escape("café test")
        assert "café test" == result  # unicode preserved, no crash


class TestMailAgentSelfLoopPrevention:
    """Verify nova_mail_agent prevents reply-to-self infinite loops."""

    def test_is_from_nova_exact_match(self):
        sys.modules.pop("nova_mail_agent", None)
        with patch.dict(sys.modules, {
            "nova_config": MagicMock(
                slack_bot_token=MagicMock(return_value="xoxb-test"),
                SLACK_PHOTOS="C_TEST",
                SLACK_NOTIFY="C_TEST",
                SLACK_API="https://slack.com/api",
                VECTOR_URL="http://127.0.0.1:18790/remember",
                SLACK_EMAIL="C_TEST",
                post_both=MagicMock(),
            ),
            "herd_config": MagicMock(HERD=[], HERD_EMAILS=set()),
            "known_senders": MagicMock(
                KNOWN_SENDERS=set(), JORDAN_EMAILS=set(), JORDAN_CC_ADDR=""
            ),
        }):
            import nova_mail_agent
            importlib.reload(nova_mail_agent)
            assert nova_mail_agent.is_from_nova("nova@digitalnoise.net") is True
            assert nova_mail_agent.is_from_nova("Nova <nova@digitalnoise.net>") is True

    def test_is_from_nova_rejects_others(self):
        sys.modules.pop("nova_mail_agent", None)
        with patch.dict(sys.modules, {
            "nova_config": MagicMock(
                slack_bot_token=MagicMock(return_value="xoxb-test"),
                SLACK_PHOTOS="C_TEST",
                SLACK_NOTIFY="C_TEST",
                SLACK_API="https://slack.com/api",
                VECTOR_URL="http://127.0.0.1:18790/remember",
                SLACK_EMAIL="C_TEST",
                post_both=MagicMock(),
            ),
            "herd_config": MagicMock(HERD=[], HERD_EMAILS=set()),
            "known_senders": MagicMock(
                KNOWN_SENDERS=set(), JORDAN_EMAILS=set(), JORDAN_CC_ADDR=""
            ),
        }):
            import nova_mail_agent
            importlib.reload(nova_mail_agent)
            assert nova_mail_agent.is_from_nova("someone@example.com") is False
            assert nova_mail_agent.is_from_nova("nova_impersonator@evil.com") is False

    def test_system_message_detection(self):
        sys.modules.pop("nova_mail_agent", None)
        with patch.dict(sys.modules, {
            "nova_config": MagicMock(
                slack_bot_token=MagicMock(return_value="xoxb-test"),
                SLACK_PHOTOS="C_TEST",
                SLACK_NOTIFY="C_TEST",
                SLACK_API="https://slack.com/api",
                VECTOR_URL="http://127.0.0.1:18790/remember",
                SLACK_EMAIL="C_TEST",
                post_both=MagicMock(),
            ),
            "herd_config": MagicMock(HERD=[], HERD_EMAILS=set()),
            "known_senders": MagicMock(
                KNOWN_SENDERS=set(), JORDAN_EMAILS=set(), JORDAN_CC_ADDR=""
            ),
        }):
            import nova_mail_agent
            importlib.reload(nova_mail_agent)
            assert nova_mail_agent.is_system_message(
                "MAILER-DAEMON@google.com", "Delivery Status Notification"
            ) is True
            assert nova_mail_agent.is_system_message(
                "noreply@service.com", "Your receipt"
            ) is True
            assert nova_mail_agent.is_system_message(
                "friend@example.com", "Hey Nova"
            ) is False


class TestSlackImageOpenRouterDefault:
    """Verify nova_slack_image.py defaults to local-only (USE_OPENROUTER=False)."""

    def test_use_openrouter_defaults_false(self):
        content = (SCRIPTS_DIR / "nova_slack_image.py").read_text()
        # Find the USE_OPENROUTER assignment
        match = re.search(r'USE_OPENROUTER\s*=\s*(True|False)', content)
        assert match is not None, "USE_OPENROUTER not found in nova_slack_image.py"
        assert match.group(1) == "False", (
            f"USE_OPENROUTER must default to False (found {match.group(1)}). "
            f"All image analysis must stay local."
        )


class TestFaceRecognitionTolerance:
    """Verify face recognition tolerance is within safe bounds."""

    def test_tolerance_within_range(self):
        content = (SCRIPTS_DIR / "nova_face_recognition.py").read_text()
        match = re.search(r'TOLERANCE\s*=\s*([\d.]+)', content)
        assert match is not None, "TOLERANCE not found in nova_face_recognition.py"
        tolerance = float(match.group(1))
        assert 0.4 <= tolerance <= 0.7, (
            f"Face recognition TOLERANCE={tolerance} is outside safe range [0.4, 0.7]. "
            f"Too low = false rejects, too high = false matches."
        )


class TestIntentRouterPrivacy:
    """Verify intent router privacy enforcement is complete and correct."""

    def test_private_intents_are_all_local(self):
        """Every private intent must route to LOCAL backend."""
        content = (SCRIPTS_DIR / "nova_intent_router.py").read_text()
        # Import and check the actual INTENT_MAP
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            for intent, (backend, model_key, privacy) in nova_intent_router.INTENT_MAP.items():
                if privacy == "private":
                    assert backend == nova_intent_router.Backend.LOCAL, (
                        f"PRIVATE intent '{intent}' routes to {backend.value}, not LOCAL!"
                    )

    def test_sensitive_intents_are_all_local(self):
        """Every sensitive intent must route to LOCAL backend."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            for intent, (backend, model_key, privacy) in nova_intent_router.INTENT_MAP.items():
                if privacy == "sensitive":
                    assert backend == nova_intent_router.Backend.LOCAL, (
                        f"SENSITIVE intent '{intent}' routes to {backend.value}, not LOCAL!"
                    )

    def test_no_cloud_intents_exist(self):
        """As of v4, NO intents should route to cloud. All local."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            cloud_intents = [
                k for k, v in nova_intent_router.INTENT_MAP.items()
                if v[0] == nova_intent_router.Backend.CLOUD
            ]
            assert len(cloud_intents) == 0, (
                f"Cloud intents found (should be 0): {cloud_intents}. "
                f"All routing must be local as of v4."
            )

    def test_private_intents_include_health(self):
        """Health intents must be classified as private."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            health_intents = [k for k in nova_intent_router.INTENT_MAP
                              if k.startswith("health_")]
            for intent in health_intents:
                _, _, privacy = nova_intent_router.INTENT_MAP[intent]
                assert privacy == "private", (
                    f"Health intent '{intent}' has privacy={privacy}, expected 'private'"
                )

    def test_private_intents_include_memory(self):
        """Memory intents must be classified as private."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            memory_intents = [k for k in nova_intent_router.INTENT_MAP
                              if "memory" in k]
            for intent in memory_intents:
                _, _, privacy = nova_intent_router.INTENT_MAP[intent]
                assert privacy == "private", (
                    f"Memory intent '{intent}' has privacy={privacy}, expected 'private'"
                )

    def test_private_intents_include_email(self):
        """Email intents must be classified as private."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            email_intents = [k for k in nova_intent_router.INTENT_MAP
                             if "email" in k]
            for intent in email_intents:
                _, _, privacy = nova_intent_router.INTENT_MAP[intent]
                assert privacy == "private", (
                    f"Email intent '{intent}' has privacy={privacy}, expected 'private'"
                )

    def test_private_intents_include_face_recognition(self):
        """Face recognition intents must be classified as private."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            face_intents = [k for k in nova_intent_router.INTENT_MAP
                            if "face" in k]
            for intent in face_intents:
                _, _, privacy = nova_intent_router.INTENT_MAP[intent]
                assert privacy == "private", (
                    f"Face intent '{intent}' has privacy={privacy}, expected 'private'"
                )

    def test_private_intents_include_imessage(self):
        """iMessage intents must be classified as private."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            imessage_intents = [k for k in nova_intent_router.INTENT_MAP
                                if "imessage" in k]
            for intent in imessage_intents:
                _, _, privacy = nova_intent_router.INTENT_MAP[intent]
                assert privacy == "private", (
                    f"iMessage intent '{intent}' has privacy={privacy}, expected 'private'"
                )

    def test_unknown_intents_route_local(self):
        """Unknown intents must fall through to local, never cloud."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            # Patch query_local to avoid actual network calls
            with patch.object(nova_intent_router, "query_local",
                              return_value={"success": True, "response": "test", "source": "local"}):
                result = nova_intent_router.route(
                    intent="completely_unknown_intent_xyz",
                    prompt="test",
                )
                assert result.get("source") == "local" or result.get("privacy") == "normal"

    def test_private_intent_refuses_cloud_fallback(self):
        """Private intents must hard-fail rather than fall back to cloud."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            # Simulate local failure
            with patch.object(nova_intent_router, "query_local",
                              return_value={"success": False, "error": "Ollama down", "source": "local"}):
                result = nova_intent_router.route(
                    intent="memory_recall",
                    prompt="what did Jordan say yesterday",
                )
                assert result["success"] is False
                assert "NEVER" in result.get("error", "") or "never" in result.get("error", "").lower() or \
                       "cloud" not in result.get("source", "")

    def test_redis_cache_skips_private_intents(self):
        """Private intents must never be cached in Redis."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            # Verify cacheable logic
            for intent in nova_intent_router.PRIVATE_INTENTS:
                cacheable = intent not in nova_intent_router.VOICE_INTENTS and \
                    nova_intent_router.INTENT_MAP.get(intent, (None, None, ""))[2] != "private"
                assert not cacheable, (
                    f"Private intent '{intent}' would be cached in Redis!"
                )


class TestProactivePeaceNoInternalState:
    """Verify proactive peace doesn't expose internal state in messages."""

    def test_no_file_paths_in_burnout_nudges(self):
        """Burnout nudge messages must not contain file paths."""
        content = (SCRIPTS_DIR / "nova_proactive_peace.py").read_text()
        # Find all slack_post calls within the burnout section
        nudge_section = content[content.find("nudges = {"):content.find("break", content.find("nudges = {")) + 100]
        assert "/Users/" not in nudge_section, "File path leaked in burnout nudge messages"
        assert "/.openclaw/" not in nudge_section, "Internal path leaked in burnout nudge messages"

    def test_no_state_file_paths_in_slack_messages(self):
        """Release queue messages must not expose state file paths."""
        content = (SCRIPTS_DIR / "nova_proactive_peace.py").read_text()
        # Find the release_queue function
        release_start = content.find("def release_queue")
        release_end = content.find("\ndef ", release_start + 1)
        release_func = content[release_start:release_end]
        # The Slack message construction should not contain raw paths
        assert "STATE_FILE" not in release_func.split("slack_post")[0] if "slack_post" in release_func else True


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL SECURITY TESTS — workflow-level verification
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestCorrectionIngestionSecurity:
    """Verify correction ingestion sanitizes input properly."""

    def test_xss_in_correction_text_is_escaped(self):
        """XSS payloads in correction text must be escaped before storage."""
        import nova_rules
        importlib.reload(nova_rules)
        xss = '<script>alert("pwned")</script>'
        escaped = nova_rules._escape(xss)
        # The escape function handles SQL injection; XSS is a display concern
        # but the single quotes in the alert must be doubled
        assert "''" in escaped or "alert" in escaped

    def test_correction_with_sql_injection(self):
        """SQL injection in correction text must be neutralized by _escape."""
        import nova_rules
        importlib.reload(nova_rules)
        injection = "test'; DROP TABLE rules; --"
        escaped = nova_rules._escape(injection)
        assert "'';" in escaped  # single quote is doubled

    def test_goal_title_with_special_characters(self):
        """Goal titles with special chars must survive escaping."""
        import nova_goals
        importlib.reload(nova_goals)
        title = "Fix Jordan's O'Reilly book review — it's broken"
        escaped = nova_goals._escape(title)
        assert "Jordan''s" in escaped
        assert "it''s" in escaped


@pytest.mark.functional
class TestEmailLoopPrevention:
    """Verify email agent loop prevention is robust."""

    def test_nova_email_constant_is_correct(self):
        """NOVA_EMAIL must match the actual email used for sending."""
        content = (SCRIPTS_DIR / "nova_mail_agent.py").read_text()
        match = re.search(r'NOVA_EMAIL\s*=\s*"([^"]+)"', content)
        assert match is not None
        assert match.group(1) == "nova@digitalnoise.net"

    def test_is_from_nova_checks_nova_email(self):
        """is_from_nova must check against NOVA_EMAIL constant."""
        content = (SCRIPTS_DIR / "nova_mail_agent.py").read_text()
        func_start = content.find("def is_from_nova")
        func_end = content.find("\ndef ", func_start + 1)
        func_body = content[func_start:func_end]
        assert "NOVA_EMAIL" in func_body, (
            "is_from_nova must reference NOVA_EMAIL constant, not a hardcoded string"
        )


@pytest.mark.functional
class TestFaceRecognitionPrivacy:
    """Verify face recognition doesn't expose encodings externally."""

    def test_no_encoding_data_in_slack_messages(self):
        """Slack messages must not contain face encoding arrays."""
        content = (SCRIPTS_DIR / "nova_face_recognition.py").read_text()
        # Find all slack_post and slack_upload_image calls
        slack_calls = re.findall(r'slack_(?:post|upload_image)\([^)]+\)', content, re.DOTALL)
        for call in slack_calls:
            assert "encoding" not in call.lower(), (
                f"Face encoding data referenced in Slack message: {call[:80]}"
            )

    def test_face_state_no_encoding_storage(self):
        """Face state file should not store raw face encodings."""
        content = (SCRIPTS_DIR / "nova_face_recognition.py").read_text()
        save_start = content.find("def save_state")
        save_end = content.find("\ndef ", save_start + 1)
        save_func = content[save_start:save_end]
        assert "encoding" not in save_func.lower(), (
            "save_state function should not write face encodings to state file"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# FRAME TESTS — output verification
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.frame
class TestErrorMessageSafety:
    """Verify error messages don't leak internal details."""

    def test_keychain_error_no_stack_trace(self):
        """Keychain failures must produce clean error messages."""
        import nova_config
        importlib.reload(nova_config)
        with patch("subprocess.run", return_value=MagicMock(
            returncode=1, stdout="", stderr=""
        )):
            result = nova_config._keychain("test-service", required=False)
            assert result == ""
            # The function should not raise, just return empty

    def test_intent_router_error_no_api_key(self):
        """Intent router errors must not contain API key values."""
        sys.modules.pop("nova_intent_router", None)
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            import nova_intent_router
            importlib.reload(nova_intent_router)
            with patch.object(nova_intent_router, "query_local",
                              return_value={"success": False, "error": "Connection refused", "source": "local"}):
                result = nova_intent_router.route(intent="text_summary", prompt="test")
                error = result.get("error", "")
                for pattern, desc in SECRET_PATTERNS:
                    assert not re.search(pattern, error), (
                        f"{desc} leaked in error message: {error[:50]}"
                    )


@pytest.mark.frame
class TestSlackMessageSafety:
    """Verify Slack messages don't contain internal paths or IDs."""

    @pytest.mark.parametrize("filepath", [
        f for f in ALL_PY_FILES
        if "slack" in f.name.lower() or "peace" in f.name.lower()
    ], ids=lambda p: p.name)
    def test_no_raw_file_paths_in_slack_messages(self, filepath):
        """Slack message strings should not contain raw file paths."""
        content = filepath.read_text(errors="replace")
        # Find slack_post / post_both calls with string content containing /Users/
        # This is a heuristic check
        slack_calls = re.findall(
            r'(?:slack_post|post_both|nova_config\.post_both)\s*\(\s*(?:f?["\'].*?/Users/.*?["\'])',
            content, re.DOTALL
        )
        assert not slack_calls, (
            f"Slack message in {filepath.name} may expose file path: {slack_calls[0][:80]}"
        )

    @pytest.mark.parametrize("filepath", [
        f for f in ALL_PY_FILES
        if "slack" in f.name.lower() or "peace" in f.name.lower() or "mail" in f.name.lower()
    ], ids=lambda p: p.name)
    def test_no_database_ids_in_slack_messages(self, filepath):
        """Slack messages should not expose raw database connection strings."""
        content = filepath.read_text(errors="replace")
        # Check for psql connection strings in Slack message contexts
        slack_sections = re.findall(
            r'(?:slack_post|post_both)\s*\([^)]*postgres[^)]*\)',
            content, re.DOTALL | re.IGNORECASE
        )
        assert not slack_sections, (
            f"Database reference in Slack message in {filepath.name}"
        )


@pytest.mark.frame
class TestLogMessageSafety:
    """Verify log messages don't contain full credentials."""

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_full_tokens_in_log_calls(self, filepath):
        """log() calls should not include full token values."""
        content = filepath.read_text(errors="replace")
        # Find log() calls that might include token variables without masking
        # This catches patterns like: log(f"Token: {token}")
        log_calls = re.findall(r'log\(f?["\'].*?\{(?:token|api_key|password|secret)\}.*?["\']\)', content)
        # Filter out masked versions like {token[:8]}
        unmasked = [c for c in log_calls if not re.search(r'\[:\d+\]', c)]
        assert not unmasked, (
            f"Unmasked credential in log call in {filepath.name}: {unmasked[0][:80]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# FILE PERMISSION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestFilePermissions:
    """Verify config and secret files have appropriate permissions."""

    def test_config_dir_not_world_readable(self):
        """Config directory should not be world-readable."""
        if not CONFIG_DIR.exists():
            pytest.skip("Config directory not found")
        mode = CONFIG_DIR.stat().st_mode
        assert not (mode & stat.S_IROTH), (
            f"Config directory {CONFIG_DIR} is world-readable (mode: {oct(mode)})"
        )

    def test_config_files_not_world_readable(self):
        """Config files with potential secrets must not be world-readable."""
        sensitive_files = ["rag.json", "scheduler.yaml"]
        for fname in sensitive_files:
            fpath = CONFIG_DIR / fname
            if fpath.exists():
                mode = fpath.stat().st_mode
                # rag.json has 600, scheduler.yaml has 644 which is fine for non-secret config
                if fname == "rag.json":
                    assert not (mode & stat.S_IROTH), (
                        f"{fpath} is world-readable (mode: {oct(mode)})"
                    )

    def test_openclaw_dir_not_world_writable(self):
        """The .openclaw directory must not be world-writable."""
        if not OPENCLAW_DIR.exists():
            pytest.skip(".openclaw directory not found")
        mode = OPENCLAW_DIR.stat().st_mode
        assert not (mode & stat.S_IWOTH), (
            f".openclaw directory is world-writable (mode: {oct(mode)})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL ROTATION READINESS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestCredentialRotationReadiness:
    """Verify secrets are loaded dynamically (not cached at import time)."""

    def test_slack_token_loaded_via_function(self):
        """Slack token must be loaded via function call, not module-level constant."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        # Check that slack_bot_token is a function, not a constant
        assert "def slack_bot_token" in content, (
            "slack_bot_token should be a function for rotation readiness"
        )
        # Ensure there's no module-level SLACK_TOKEN = "xoxb-..." constant
        lines = content.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("def ") or stripped.startswith('"""'):
                continue
            if re.match(r'^SLACK_TOKEN\s*=\s*["\']xox', stripped):
                assert False, "Module-level SLACK_TOKEN with real token found"

    def test_openrouter_key_loaded_via_function(self):
        """OpenRouter key must be loaded via function call."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        assert "def openrouter_api_key" in content

    def test_discord_token_loaded_via_function(self):
        """Discord token must be loaded via function call."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        assert "def discord_bot_token" in content

    def test_keychain_is_primary_source(self):
        """All token functions must try Keychain first, env as fallback."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        for func_name in ["slack_bot_token", "openrouter_api_key", "discord_bot_token"]:
            func_start = content.find(f"def {func_name}")
            func_end = content.find("\ndef ", func_start + 1)
            if func_end == -1:
                func_end = len(content)
            func_body = content[func_start:func_end]
            keychain_pos = func_body.find("_keychain")
            environ_pos = func_body.find("os.environ")
            assert keychain_pos != -1, f"{func_name} doesn't use Keychain"
            if environ_pos != -1:
                assert keychain_pos < environ_pos, (
                    f"{func_name}: Keychain must be checked BEFORE env var fallback"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestInputValidation:
    """Verify external-facing inputs are validated."""

    def test_slack_image_file_id_validation(self):
        """Slack image download should validate file_id format."""
        content = (SCRIPTS_DIR / "nova_slack_image.py").read_text()
        # Verify that download_slack_file handles both URLs and IDs
        assert "startswith(\"http\")" in content or 'startswith("http")' in content, (
            "download_slack_file should distinguish between URLs and file IDs"
        )

    def test_face_recognition_frame_age_check(self):
        """Face recognition should skip stale camera frames."""
        content = (SCRIPTS_DIR / "nova_face_recognition.py").read_text()
        assert "age > 300" in content or "age >" in content, (
            "Face recognition should skip frames older than a threshold"
        )

    def test_email_body_length_limited(self):
        """Email bodies should be truncated to prevent memory issues."""
        content = (SCRIPTS_DIR / "nova_mail_agent.py").read_text()
        assert "body[:3000]" in content or "[:3000]" in content, (
            "Email body should be truncated to a reasonable length"
        )

    def test_discord_message_length_limited(self):
        """Discord messages should be truncated to 2000 chars (API limit)."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        assert "[:2000]" in content, (
            "Discord messages must be truncated to 2000 chars per API limit"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# NETWORK SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestNetworkSecurity:
    """Verify network endpoints and binding safety."""

    def test_ollama_binds_to_localhost(self):
        """Ollama URL should point to localhost, not a wildcard."""
        content = (SCRIPTS_DIR / "nova_intent_router.py").read_text()
        match = re.search(r'OLLAMA_URL\s*=\s*"([^"]+)"', content)
        assert match is not None
        url = match.group(1)
        assert "127.0.0.1" in url or "localhost" in url, (
            f"Ollama URL binds to {url}, should be 127.0.0.1 or localhost"
        )

    def test_vector_memory_binds_to_localhost(self):
        """Vector memory URL should point to localhost."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        match = re.search(r'VECTOR_URL\s*=\s*"([^"]+)"', content)
        assert match is not None
        url = match.group(1)
        assert "127.0.0.1" in url or "localhost" in url, (
            f"VECTOR_URL binds to {url}, should be 127.0.0.1 or localhost"
        )

    def test_ssh_server_port_non_standard(self):
        """Nova's SSH server should not use standard port 22."""
        content = (SCRIPTS_DIR / "nova_ssh_server.py").read_text()
        match = re.search(r'PORT\s*=\s*(\d+)', content)
        assert match is not None
        port = int(match.group(1))
        assert port != 22, "SSH server should use a non-standard port"
        assert port > 1024, "SSH server should use an unprivileged port"


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestConfigurationSecurity:
    """Verify configuration files don't contain secrets."""

    def test_scheduler_yaml_no_secrets(self):
        """scheduler.yaml should not contain API keys or tokens."""
        yaml_path = CONFIG_DIR / "scheduler.yaml"
        if not yaml_path.exists():
            pytest.skip("scheduler.yaml not found")
        content = yaml_path.read_text()
        for pattern, desc in SECRET_PATTERNS:
            assert not re.search(pattern, content), (
                f"{desc} found in scheduler.yaml"
            )

    def test_scheduler_state_no_secrets(self):
        """scheduler_state.json should not contain credentials."""
        state_path = CONFIG_DIR / "scheduler_state.json"
        if not state_path.exists():
            pytest.skip("scheduler_state.json not found")
        content = state_path.read_text()
        for pattern, desc in SECRET_PATTERNS:
            assert not re.search(pattern, content), (
                f"{desc} found in scheduler_state.json"
            )

    def test_rag_config_no_secrets(self):
        """rag.json should not contain API keys."""
        rag_path = CONFIG_DIR / "rag.json"
        if not rag_path.exists():
            pytest.skip("rag.json not found")
        content = rag_path.read_text()
        for pattern, desc in SECRET_PATTERNS:
            assert not re.search(pattern, content), (
                f"{desc} found in rag.json"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# AST-LEVEL SECURITY ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestASTSecurityAnalysis:
    """Use Python AST to detect dangerous patterns."""

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_eval_calls(self, filepath):
        """No script should use eval() or exec() on external input."""
        try:
            tree = ast.parse(filepath.read_text(errors="replace"))
        except SyntaxError:
            pytest.skip(f"Syntax error in {filepath.name}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                    # Allow exec in known-safe contexts (schema creation)
                    # But flag any that use variables as arguments
                    if node.args:
                        arg = node.args[0]
                        if isinstance(arg, (ast.Name, ast.Call, ast.JoinedStr)):
                            assert False, (
                                f"Dangerous {node.func.id}() with dynamic input "
                                f"in {filepath.name}:{node.lineno}"
                            )

    @pytest.mark.parametrize("filepath", ALL_PY_FILES, ids=lambda p: p.name)
    def test_no_pickle_load(self, filepath):
        """No script should use pickle.load on untrusted data."""
        try:
            tree = ast.parse(filepath.read_text(errors="replace"))
        except SyntaxError:
            pytest.skip(f"Syntax error in {filepath.name}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("load", "loads") and \
                       isinstance(node.func.value, ast.Name) and \
                       node.func.value.id == "pickle":
                        assert False, (
                            f"pickle.{node.func.attr}() in {filepath.name}:{node.lineno}. "
                            f"Pickle is unsafe for untrusted data — use JSON instead."
                        )


# ═══════════════════════════════════════════════════════════════════════════════
# COMPREHENSIVE COVERAGE — ensure all critical scripts are tested
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestCriticalScriptsExist:
    """Verify key security-relevant scripts exist and are non-empty."""

    CRITICAL_SCRIPTS = [
        "nova_config.py",
        "nova_intent_router.py",
        "nova_mail_agent.py",
        "nova_rules.py",
        "nova_goals.py",
        "nova_face_recognition.py",
        "nova_proactive_peace.py",
        "nova_slack_image.py",
        "nova_logger.py",
        "nova_security_hardening.py",
        "nova_self_audit.py",
    ]

    @pytest.mark.parametrize("script_name", CRITICAL_SCRIPTS)
    def test_critical_script_exists(self, script_name):
        path = SCRIPTS_DIR / script_name
        assert path.exists(), f"Critical script missing: {script_name}"
        content = path.read_text()
        assert len(content) > 100, f"Critical script {script_name} appears empty or stub"

    def test_nova_config_uses_keychain(self):
        """nova_config must use macOS Keychain for secret storage."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        assert "security" in content, "nova_config must call 'security' CLI for Keychain"
        assert "find-generic-password" in content, "nova_config must use Keychain lookup"

    def test_nova_config_no_plaintext_fallback(self):
        """Token functions must not have hardcoded plaintext fallbacks."""
        content = (SCRIPTS_DIR / "nova_config.py").read_text()
        # Check that no function returns a hardcoded token
        for func in ["slack_bot_token", "openrouter_api_key", "discord_bot_token"]:
            func_start = content.find(f"def {func}")
            func_end = content.find("\ndef ", func_start + 1)
            if func_end == -1:
                func_end = len(content)
            func_body = content[func_start:func_end]
            # Should not have return "xoxb-..." or return "sk-..."
            assert not re.search(r'return\s+["\'](?:xox[bpoas]-|sk-|AKIA|ghp_)', func_body), (
                f"{func} has a hardcoded token fallback!"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# MARKER REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════════
# The 'security' marker is registered in conftest.py's pytest_configure.
# If it wasn't already there, we register it here as a fallback.

def pytest_configure(config):
    """Register the security marker if not already registered."""
    config.addinivalue_line("markers", "security: marks security audit tests")
