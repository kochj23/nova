#!/usr/bin/env python3
"""
test_self_management.py — Combined tests for nova_self_audit.py and nova_self_improve.py.

Covers: script auditing, service port checking, process detection, documentation auditing,
writing critique generation, lesson saving, Slack reporting, state management, security.

Run: python3 -m pytest tests/test_self_management.py -v
Written by Jordan Koch.
"""

import json
import sys
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def audit_module(mock_nova_config):
    """Import nova_self_audit fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_self_audit" in mod:
            del sys.modules[mod]
    import nova_self_audit
    return nova_self_audit


@pytest.fixture
def improve_module(mock_nova_config):
    """Import nova_self_improve fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_self_improve" in mod:
            del sys.modules[mod]
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="")
        import nova_self_improve
    return nova_self_improve


# ═══════════════════════════════════════════════════════════════════════════════
# SELF-AUDIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditScriptsOnDisk:
    """Tests for script discovery."""

    def test_finds_scripts(self, audit_module, tmp_path):
        (tmp_path / "nova_test.py").touch()
        (tmp_path / "nova_other.sh").touch()
        (tmp_path / "readme.md").touch()  # Should be ignored
        with patch.object(audit_module, "SCRIPTS_DIR", tmp_path):
            scripts = audit_module._scripts_on_disk()
        assert "nova_test.py" in scripts
        assert "nova_other.sh" in scripts
        assert "readme.md" not in scripts


class TestAuditScriptsInFile:
    """Tests for script reference extraction from docs."""

    def test_extracts_script_references(self, audit_module, tmp_path):
        doc = tmp_path / "MEMORY.md"
        doc.write_text("Uses nova_scheduler.py and dream_deliver.py for scheduling.")
        result = audit_module._scripts_in_file(doc)
        assert "nova_scheduler.py" in result
        assert "dream_deliver.py" in result

    def test_handles_missing_file(self, audit_module):
        result = audit_module._scripts_in_file(Path("/nonexistent/file.md"))
        assert result == set()


class TestAuditPortListening:
    """Tests for port checking."""

    def test_port_not_listening(self, audit_module):
        # Port 1 should never be listening
        result = audit_module._port_listening(1)
        assert result is False

    @patch("socket.socket")
    def test_port_listening_mocked(self, mock_socket_class, audit_module):
        mock_sock = MagicMock()
        mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)
        mock_sock.connect.return_value = None
        result = audit_module._port_listening(18790)
        # The mock should make it succeed
        assert result is True


class TestAuditProcessRunning:
    """Tests for process detection."""

    @patch("subprocess.run")
    def test_process_found(self, mock_run, audit_module):
        mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
        result = audit_module._process_running("nova_scheduler.py")
        assert result is True

    @patch("subprocess.run")
    def test_process_not_found(self, mock_run, audit_module):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = audit_module._process_running("nonexistent_process")
        assert result is False


class TestAuditDocs:
    """Tests for documentation auditing."""

    def test_detects_missing_memory_md(self, audit_module, tmp_path):
        with patch.object(audit_module, "MEMORY_MD", tmp_path / "nope.md"):
            with patch.object(audit_module, "IDENTITY_MD", tmp_path / "id.md"):
                (tmp_path / "id.md").write_text("Name: Nova\nFull identity doc")
                issues = audit_module.audit_docs()
        assert any("MEMORY.md is missing" in i for i in issues)

    def test_detects_empty_memory_md(self, audit_module, tmp_path):
        mem = tmp_path / "MEMORY.md"
        mem.write_text("hi")
        with patch.object(audit_module, "MEMORY_MD", mem):
            with patch.object(audit_module, "IDENTITY_MD", tmp_path / "id.md"):
                (tmp_path / "id.md").write_text("Full identity document with content")
                issues = audit_module.audit_docs()
        assert any("empty" in i or "minimal" in i for i in issues)


class TestAuditStateManagement:
    """Tests for audit state persistence."""

    def test_load_empty_state(self, audit_module, tmp_path):
        with patch.object(audit_module, "AUDIT_STATE_FILE", tmp_path / "nope.json"):
            state = audit_module._load_last_audit_state()
        assert state == {}

    def test_save_and_load_state(self, audit_module, tmp_path):
        sf = tmp_path / "state" / "audit.json"
        with patch.object(audit_module, "AUDIT_STATE_FILE", sf):
            audit_module._save_audit_state({"last_issue_key": "[]", "last_run": "2026-01-01"})
            state = audit_module._load_last_audit_state()
        assert state["last_issue_key"] == "[]"


class TestAuditFullRun:
    """Tests for the full audit workflow."""

    @patch("nova_self_audit.slack_post")
    @patch("nova_self_audit._save_audit_state")
    @patch("nova_self_audit._load_last_audit_state")
    @patch("nova_self_audit.audit_docs")
    @patch("nova_self_audit.audit_processes")
    @patch("nova_self_audit.audit_services")
    @patch("nova_self_audit.audit_scripts")
    def test_run_audit_no_issues(self, mock_scripts, mock_services, mock_processes,
                                  mock_docs, mock_load, mock_save, mock_slack, audit_module):
        mock_scripts.return_value = ([], [], 50, 40, 30)
        mock_services.return_value = ([], ["Memory Server", "Ollama"])
        mock_processes.return_value = ([], ["Scheduler", "Gateway"])
        mock_docs.return_value = []
        mock_load.return_value = {"last_issue_key": "[]"}

        issue_count = audit_module.run_audit()
        assert issue_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# SELF-IMPROVE TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestImproveStateManagement:
    """Tests for self-improvement state."""

    def test_load_default_state(self, improve_module, tmp_path):
        with patch.object(improve_module, "STATE_FILE", tmp_path / "nope.json"):
            state = improve_module.load_state()
        assert state == {"runs": [], "run_count": 0}

    def test_save_state(self, improve_module, tmp_path):
        sf = tmp_path / "improve.json"
        with patch.object(improve_module, "STATE_FILE", sf):
            improve_module.save_state({"runs": [{"date": "2026-01-01"}], "run_count": 1})
        data = json.loads(sf.read_text())
        assert data["run_count"] == 1


class TestImproveContentCollection:
    """Tests for collecting writing samples."""

    def test_collect_dreams(self, improve_module, tmp_path):
        dreams_dir = tmp_path / "dreams"
        dreams_dir.mkdir()
        today = date.today().isoformat()
        (dreams_dir / f"{today}.md").write_text("A dream about flying over mountains.")
        with patch.object(improve_module, "DREAMS_DIR", dreams_dir):
            dreams = improve_module.collect_dreams([today])
        assert len(dreams) == 1
        assert dreams[0]["date"] == today

    def test_collect_essays(self, improve_module, tmp_path):
        essays_dir = tmp_path / "essays"
        essays_dir.mkdir()
        today = date.today().isoformat()
        (essays_dir / f"{today}-test-essay.md").write_text("An essay about security.")
        with patch.object(improve_module, "ESSAYS_DIR", essays_dir):
            essays = improve_module.collect_essays([today])
        assert len(essays) == 1

    def test_collect_empty_week(self, improve_module, tmp_path):
        with patch.object(improve_module, "DREAMS_DIR", tmp_path / "nope"):
            dreams = improve_module.collect_dreams(["2020-01-01"])
        assert dreams == []


class TestImproveCritiquePrompt:
    """Tests for critique prompt building."""

    def test_builds_prompt_with_all_content(self, improve_module):
        dreams = [{"date": "2026-01-01", "content": "Dream text here"}]
        essays = [{"date": "2026-01-02", "file": "test.md", "content": "Essay text"}]
        opinions = [{"date": "2026-01-03", "file": "op.md", "content": "Opinion text"}]
        sys_prompt, user_prompt = improve_module.build_critique_prompt(dreams, essays, opinions)
        assert "DREAMS" in user_prompt
        assert "ESSAYS" in user_prompt
        assert "OPINIONS" in user_prompt
        assert "PEEL" in user_prompt

    def test_handles_empty_sections(self, improve_module):
        sys_prompt, user_prompt = improve_module.build_critique_prompt([], [], [])
        assert "[No dreams this week]" in user_prompt
        assert "[No essays this week]" in user_prompt


class TestImproveLessonSaving:
    """Tests for saving writing lessons."""

    def test_saves_lessons_with_header(self, improve_module, tmp_path):
        lessons_file = tmp_path / "lessons.md"
        with patch.object(improve_module, "LESSONS_FILE", lessons_file):
            improve_module.save_lessons("## Dreams\n- Stop using passive voice")
        content = lessons_file.read_text()
        assert "Nova's Writing Lessons" in content
        assert "Stop using passive voice" in content

    def test_preserves_existing_header(self, improve_module, tmp_path):
        lessons_file = tmp_path / "lessons.md"
        critique = "# Nova's Writing Lessons (auto-updated weekly)\nLast updated: 2026-01-01\n\n## Dreams\n- Lesson"
        with patch.object(improve_module, "LESSONS_FILE", lessons_file):
            improve_module.save_lessons(critique)
        content = lessons_file.read_text()
        assert "Nova's Writing Lessons" in content
        # Date should be updated
        assert date.today().isoformat() in content


class TestImproveSlackSummary:
    """Tests for Slack report card building."""

    def test_builds_slack_summary(self, improve_module):
        critique = "## Dreams\n- Lesson 1\n- Lesson 2\n## Avoid\n- Stop saying 'however'\n- Drop 'in conclusion'"
        msg = improve_module.build_slack_summary(critique, 5, 7, 7)
        assert "Report Card" in msg
        assert "5 dreams" in msg
        assert "7 essays" in msg
        assert "however" in msg


class TestImproveGeneration:
    """Tests for critique generation with fallbacks."""

    @patch("nova_self_improve.generate_via_ollama")
    @patch("nova_self_improve.generate_via_openrouter")
    @patch("nova_self_improve.get_openrouter_key")
    def test_primary_openrouter(self, mock_key, mock_openrouter, mock_ollama, improve_module):
        mock_key.return_value = "test-key"
        mock_openrouter.return_value = "A" * 200
        result = improve_module.generate_critique("sys", "user")
        assert result is not None
        mock_ollama.assert_not_called()

    @patch("nova_self_improve.generate_via_ollama")
    @patch("nova_self_improve.generate_via_openrouter")
    @patch("nova_self_improve.get_openrouter_key")
    def test_fallback_to_ollama(self, mock_key, mock_openrouter, mock_ollama, improve_module):
        mock_key.return_value = "test-key"
        mock_openrouter.side_effect = Exception("API down")
        mock_ollama.return_value = "B" * 200
        result = improve_module.generate_critique("sys", "user")
        assert result is not None

    @patch("nova_self_improve.generate_via_ollama")
    @patch("nova_self_improve.generate_via_openrouter")
    @patch("nova_self_improve.get_openrouter_key")
    def test_returns_none_on_short(self, mock_key, mock_openrouter, mock_ollama, improve_module):
        mock_key.return_value = "test-key"
        mock_openrouter.return_value = "Short"
        mock_ollama.return_value = ""
        result = improve_module.generate_critique("sys", "user")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSelfManagementSecurity:
    """Security tests for self-management scripts."""

    def test_audit_no_hardcoded_keys(self, audit_module):
        import inspect
        source = inspect.getsource(audit_module)
        assert "sk-" not in source
        assert "xoxb-" not in source

    def test_improve_no_hardcoded_keys(self, improve_module):
        import inspect
        source = inspect.getsource(improve_module)
        assert "sk-" not in source
        assert "AKIA" not in source

    def test_audit_expected_services_are_local(self, audit_module):
        """All monitored services should be on localhost."""
        for port in audit_module.EXPECTED_SERVICES:
            assert isinstance(port, int)
            # Port check function uses 127.0.0.1
            import inspect
            source = inspect.getsource(audit_module._port_listening)
            assert "127.0.0.1" in source


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestImproveFullWorkflow:
    """Functional test for complete self-improvement cycle."""

    @patch("nova_self_improve.nova_config")
    @patch("nova_self_improve.save_state")
    @patch("nova_self_improve.save_lessons")
    @patch("nova_self_improve.generate_critique")
    @patch("nova_self_improve.collect_opinions")
    @patch("nova_self_improve.collect_essays")
    @patch("nova_self_improve.collect_dreams")
    @patch("nova_self_improve.load_state")
    def test_full_pipeline(self, mock_load, mock_dreams, mock_essays, mock_opinions,
                           mock_critique, mock_lessons, mock_save, mock_config, improve_module):
        mock_load.return_value = {"runs": [], "run_count": 0}
        mock_dreams.return_value = [{"date": "2026-01-01", "content": "Dream"}]
        mock_essays.return_value = [{"date": "2026-01-02", "file": "e.md", "content": "Essay"}]
        mock_opinions.return_value = [{"date": "2026-01-03", "file": "o.md", "content": "Opinion"}]
        mock_critique.return_value = "# Critique\n## Dreams\n- Lesson\n## Avoid\n- Stop it"
        mock_config.post_both = MagicMock()

        improve_module.main()

        mock_critique.assert_called_once()
        mock_lessons.assert_called_once()
        mock_save.assert_called_once()
        saved_state = mock_save.call_args[0][0]
        assert saved_state["run_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring live filesystem."""

    def test_scripts_directory_exists(self, audit_module):
        """Verify the scripts directory exists at the expected path."""
        assert audit_module.SCRIPTS_DIR.exists()

    def test_scripts_directory_has_scripts(self, audit_module):
        """Verify scripts are actually present."""
        scripts = audit_module._scripts_on_disk()
        assert len(scripts) > 10  # Should have many scripts
