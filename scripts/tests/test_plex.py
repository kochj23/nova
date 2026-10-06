#!/usr/bin/env python3
"""
test_plex.py — Comprehensive tests for nova_plex.py (Plex Media Server integration).

Covers: token retrieval, API construction, library 23 exclusion, all 13 subcommands,
genre classification, state file serialization, --quiet flag, security (no token leaks).

Run: python3 -m pytest tests/test_plex.py -v
Written by Jordan Koch.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_nova_config_for_plex(monkeypatch):
    """Mock nova_config before nova_plex imports it."""
    mock_config = MagicMock()
    mock_config.VECTOR_URL = "http://127.0.0.1:18790/remember"
    mock_config.SLACK_API = "https://slack.com/api"
    mock_config.SLACK_CHAN = "C_TEST_CHAT"
    mock_config.SLACK_NOTIFY = "C_TEST_NOTIFY"
    mock_config.JORDAN_DM = "D_TEST_DM"
    mock_config.slack_bot_token.return_value = "xoxb-test-token"
    mock_config.post_both = MagicMock()
    mock_config.post_discord = MagicMock(return_value=True)
    monkeypatch.setitem(sys.modules, "nova_config", mock_config)
    # nova_plex resolves PLEX_URL at import via nova_resolve, which queries PG — keep it offline.
    import nova_resolve
    monkeypatch.setattr(nova_resolve, "resolve_url", lambda *a, **k: "http://192.168.1.2:32400")
    return mock_config


@pytest.fixture
def plex_module(mock_nova_config_for_plex, monkeypatch, request):
    """Import nova_plex with mocked dependencies (the original sys.modules entry is restored after)."""
    if "nova_plex" in sys.modules:
        monkeypatch.delitem(sys.modules, "nova_plex")
    else:
        request.addfinalizer(lambda: sys.modules.pop("nova_plex", None))
    import nova_plex
    return nova_plex


@pytest.fixture
def sample_history_xml():
    """Build a sample XML response for Plex watch history."""
    xml_str = """<?xml version="1.0" encoding="UTF-8"?>
    <MediaContainer size="3">
      <Video title="Breaking Bad" grandparentTitle="Breaking Bad" type="episode"
             librarySectionID="6" viewedAt="1714600000" duration="3600000" viewOffset="3600000"
             ratingKey="12345">
        <Genre tag="Drama"/>
        <Genre tag="Thriller"/>
      </Video>
      <Video title="Cosmos" type="movie"
             librarySectionID="7" viewedAt="1714603600" duration="7200000" viewOffset="7200000"
             ratingKey="12346">
        <Genre tag="Documentary"/>
        <Genre tag="Science"/>
      </Video>
      <Video title="Other Library Item" type="movie"
             librarySectionID="23" viewedAt="1714607200" duration="5400000" viewOffset="5400000"
             ratingKey="99999">
        <Genre tag="Unknown"/>
      </Video>
    </MediaContainer>"""
    return ET.fromstring(xml_str)


@pytest.fixture
def sample_sessions_xml():
    """Build a sample XML for active sessions."""
    xml_str = """<?xml version="1.0" encoding="UTF-8"?>
    <MediaContainer size="1">
      <Video title="Ozark" grandparentTitle="Ozark" type="episode"
             librarySectionID="6" duration="3600000" viewOffset="1800000">
        <Player title="Apple TV" device="Apple TV" address="192.168.1.50"
                state="playing" machineIdentifier="DEVICE_ABC"/>
        <User title="Jordan"/>
      </Video>
    </MediaContainer>"""
    return ET.fromstring(xml_str)


@pytest.fixture
def sample_ondeck_xml():
    """Build a sample XML for on-deck items."""
    old_ts = str(int((datetime.now(timezone.utc) - timedelta(days=45)).timestamp()))
    xml_str = f"""<?xml version="1.0" encoding="UTF-8"?>
    <MediaContainer size="2">
      <Video title="Abandoned Show" grandparentTitle="Abandoned Show" type="episode"
             librarySectionID="6" lastViewedAt="{old_ts}" duration="2700000" viewOffset="1350000">
      </Video>
      <Video title="Lib23 Item" type="movie"
             librarySectionID="23" lastViewedAt="{old_ts}" duration="5400000" viewOffset="2700000">
      </Video>
    </MediaContainer>"""
    return ET.fromstring(xml_str)


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestTokenRetrieval:
    """Tests for Plex token retrieval from macOS Keychain."""

    @patch("subprocess.run")
    def test_plex_token_returns_string(self, mock_run, plex_module):
        """Token retrieval returns a stripped string."""
        mock_run.return_value = MagicMock(stdout="my-secret-token\n", returncode=0)
        plex_module._TOKEN_CACHE = None
        result = plex_module._plex_token()
        assert result == "my-secret-token"
        assert isinstance(result, str)

    @patch("subprocess.run")
    def test_plex_token_calls_keychain(self, mock_run, plex_module):
        """Token retrieval uses the security CLI with correct service name."""
        mock_run.return_value = MagicMock(stdout="tok123\n", returncode=0)
        plex_module._TOKEN_CACHE = None
        plex_module._plex_token()
        args = mock_run.call_args[0][0]
        assert "security" in args
        assert "nova-plex-token" in args
        assert "nova" in args

    @patch("subprocess.run")
    def test_token_caching(self, mock_run, plex_module):
        """Token is cached after first retrieval."""
        mock_run.return_value = MagicMock(stdout="cached-token\n", returncode=0)
        plex_module._TOKEN_CACHE = None
        first = plex_module.token()
        second = plex_module.token()
        assert first == second
        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_empty_token_exits(self, mock_run, plex_module):
        """Missing token causes sys.exit."""
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        plex_module._TOKEN_CACHE = None
        with pytest.raises(SystemExit):
            plex_module._plex_token()


class TestSkipLibrary:
    """Tests for the library 23 exclusion logic."""

    def test_skip_library_23(self, plex_module):
        assert plex_module._skip_library(23) is True
        assert plex_module._skip_library("23") is True

    def test_allow_other_libraries(self, plex_module):
        for lib_id in [6, 7, 9, 10, 21, 24, 25, 26]:
            assert plex_module._skip_library(lib_id) is False

    def test_skip_library_handles_none(self, plex_module):
        assert plex_module._skip_library(None) is False

    def test_skip_library_handles_garbage(self, plex_module):
        assert plex_module._skip_library("abc") is False

    def test_skip_libraries_constant(self, plex_module):
        assert 23 in plex_module.SKIP_LIBRARIES


class TestHelpers:
    """Tests for utility/helper functions."""

    def test_format_duration_hours_and_minutes(self, plex_module):
        assert plex_module.format_duration(3660) == "1h 1m"

    def test_format_duration_minutes_only(self, plex_module):
        assert plex_module.format_duration(300) == "5m"

    def test_format_duration_zero(self, plex_module):
        assert plex_module.format_duration(0) == "0m"

    def test_ts_to_dt_returns_utc(self, plex_module):
        dt = plex_module.ts_to_dt(1714600000)
        assert dt.tzinfo == timezone.utc

    def test_load_json_missing_file(self, plex_module, tmp_path):
        result = plex_module.load_json(tmp_path / "nonexistent.json", {"default": True})
        assert result == {"default": True}

    def test_load_json_existing_file(self, plex_module, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        result = plex_module.load_json(f)
        assert result == {"key": "value"}

    def test_save_json_creates_parent_dirs(self, plex_module, tmp_path):
        target = tmp_path / "deep" / "nested" / "file.json"
        plex_module.save_json(target, {"test": 1})
        assert target.exists()
        assert json.loads(target.read_text()) == {"test": 1}

    def test_load_json_corrupt_file(self, plex_module, tmp_path):
        f = tmp_path / "corrupt.json"
        f.write_text("not valid json{{{")
        result = plex_module.load_json(f, {"fallback": True})
        assert result == {"fallback": True}


class TestPostHelper:
    """Tests for the post() wrapper."""

    def test_quiet_mode_prints_instead(self, plex_module, capsys):
        plex_module.QUIET = True
        plex_module.post("Test message")
        captured = capsys.readouterr()
        assert "Test message" in captured.out
        plex_module.QUIET = False

    def test_normal_mode_calls_post_both(self, plex_module, mock_nova_config_for_plex):
        plex_module.QUIET = False
        plex_module.post("Hello Slack")
        mock_nova_config_for_plex.post_both.assert_called()


class TestGetAllLibraries:
    """Tests for get_all_libraries() — filtering out library 23."""

    @patch("nova_plex.plex_get")
    def test_excludes_library_23(self, mock_get, plex_module):
        xml_str = """<MediaContainer>
          <Directory key="6" title="TV Shows" type="show"/>
          <Directory key="7" title="Movies" type="movie"/>
          <Directory key="23" title="Other" type="movie"/>
        </MediaContainer>"""
        mock_get.return_value = ET.fromstring(xml_str)
        libs = plex_module.get_all_libraries()
        keys = [l["key"] for l in libs]
        assert 23 not in keys
        assert 6 in keys
        assert 7 in keys


class TestLibraryNames:
    """Verify the LIBRARY_NAMES constant completeness."""

    def test_library_names_does_not_include_23(self, plex_module):
        assert 23 not in plex_module.LIBRARY_NAMES

    def test_library_names_has_known_entries(self, plex_module):
        assert plex_module.LIBRARY_NAMES[6] == "TV Shows"
        assert plex_module.LIBRARY_NAMES[7] == "Movies"
        assert plex_module.LIBRARY_NAMES[21] == "Documentary"


# ═══════════════════════════════════════════════════════════════════════════════
# SUBCOMMAND UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestCmdHistory:
    """Tests for the history subcommand."""

    @patch("nova_plex.store_vector")
    @patch("nova_plex.plex_get")
    def test_history_ingests_items(self, mock_get, mock_store, plex_module, sample_history_xml):
        mock_get.return_value = sample_history_xml
        args = MagicMock()
        plex_module.cmd_history(args)
        # Should store 2 items (library 23 excluded)
        assert mock_store.call_count == 2

    @patch("nova_plex.store_vector")
    @patch("nova_plex.plex_get")
    def test_history_skips_library_23(self, mock_get, mock_store, plex_module, sample_history_xml):
        mock_get.return_value = sample_history_xml
        args = MagicMock()
        plex_module.cmd_history(args)
        for call in mock_store.call_args_list:
            text = call[0][0]
            assert "Other Library Item" not in text

    @patch("nova_plex.plex_get")
    def test_history_handles_empty_response(self, mock_get, plex_module, capsys):
        mock_get.return_value = ET.fromstring("<MediaContainer size='0'/>")
        args = MagicMock()
        plex_module.cmd_history(args)
        captured = capsys.readouterr()
        assert "No watch history" in captured.out


class TestCmdPlaying:
    """Tests for the playing subcommand."""

    @patch("nova_plex.save_json")
    @patch("nova_plex.plex_get")
    def test_playing_writes_state_file(self, mock_get, mock_save, plex_module, sample_sessions_xml):
        mock_get.return_value = sample_sessions_xml
        args = MagicMock()
        plex_module.cmd_playing(args)
        mock_save.assert_called_once()
        data = mock_save.call_args[0][1]
        assert "sessions" in data
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["title"] == "Ozark"

    @patch("nova_plex.plex_get")
    def test_playing_nothing_removes_state_file(self, mock_get, plex_module, tmp_path, capsys):
        mock_get.return_value = ET.fromstring("<MediaContainer size='0'/>")
        playing_file = tmp_path / "playing.json"
        playing_file.write_text("{}")
        with patch.object(plex_module, "PLAYING_FILE", playing_file):
            args = MagicMock()
            plex_module.cmd_playing(args)
        assert not playing_file.exists()
        captured = capsys.readouterr()
        assert "Nothing playing" in captured.out


class TestCmdStats:
    """Tests for the stats subcommand."""

    @patch("nova_plex.alert")  # stats goes to the alert bus, not SLACK_NOTIFY
    @patch("nova_plex.plex_get")
    def test_stats_calculates_hours(self, mock_get, mock_post, plex_module, sample_history_xml):
        mock_get.return_value = sample_history_xml
        args = MagicMock()
        plex_module.cmd_stats(args)
        mock_post.assert_called_once()
        msg = mock_post.call_args[0][0]
        assert "Plex Weekly Digest" in msg
        assert "hours" in msg

    @patch("nova_plex.alert")
    @patch("nova_plex.plex_get")
    def test_stats_no_history(self, mock_get, mock_post, plex_module):
        mock_get.return_value = ET.fromstring("<MediaContainer size='0'/>")
        args = MagicMock()
        plex_module.cmd_stats(args)
        mock_post.assert_called_once()
        assert "No Plex viewing" in mock_post.call_args[0][0]


class TestCmdShame:
    """Tests for the shame subcommand."""

    @patch("nova_plex.post_dm")
    @patch("nova_plex.plex_get")
    def test_shame_produces_roast_format(self, mock_get, mock_dm, plex_module, sample_ondeck_xml):
        mock_get.return_value = sample_ondeck_xml
        args = MagicMock()
        plex_module.cmd_shame(args)
        mock_dm.assert_called_once()
        msg = mock_dm.call_args[0][0]
        assert "Abandoned Pile Shame Board" in msg
        assert "Little Mister" in msg

    @patch("nova_plex.post_dm")
    @patch("nova_plex.plex_get")
    def test_shame_excludes_library_23(self, mock_get, mock_dm, plex_module, sample_ondeck_xml):
        mock_get.return_value = sample_ondeck_xml
        args = MagicMock()
        plex_module.cmd_shame(args)
        if mock_dm.called:
            msg = mock_dm.call_args[0][0]
            assert "Lib23 Item" not in msg

    @patch("nova_plex.plex_get")
    def test_shame_clean_deck(self, mock_get, plex_module, capsys):
        mock_get.return_value = ET.fromstring("<MediaContainer size='0'/>")
        args = MagicMock()
        plex_module.cmd_shame(args)
        captured = capsys.readouterr()
        assert "No shame today" in captured.out


class TestCmdRewatch:
    """Tests for the rewatch subcommand."""

    @patch("nova_plex.save_json")
    @patch("nova_plex.plex_get")
    def test_rewatch_returns_valid_data(self, mock_get, mock_save, plex_module, tmp_path):
        # 4 views of "Breaking Bad" to trigger canon (3+)
        items = "\n".join(
            f'<Video title="Breaking Bad" grandparentTitle="Breaking Bad" type="episode" '
            f'librarySectionID="6" viewedAt="{1714600000 + i * 86400}" duration="3600000" ratingKey="{i}"/>'
            for i in range(4)
        )
        xml = ET.fromstring(f"<MediaContainer>{items}</MediaContainer>")
        mock_get.return_value = xml
        canon_file = tmp_path / "canon.json"
        with patch.object(plex_module, "CANON_FILE", canon_file):
            args = MagicMock()
            plex_module.cmd_rewatch(args)
        assert mock_save.called


class TestCmdMood:
    """Tests for the mood subcommand."""

    @patch("nova_plex.save_json")
    @patch("nova_plex.plex_get")
    def test_mood_updates_state(self, mock_get, mock_save, plex_module, sample_history_xml, tmp_path):
        mock_get.return_value = sample_history_xml
        mood_file = tmp_path / "mood.json"
        with patch.object(plex_module, "MOOD_FILE", mood_file):
            args = MagicMock()
            plex_module.cmd_mood(args)
        mock_save.assert_called()
        data = mock_save.call_args[0][1]
        assert "days" in data


class TestCmdGuest:
    """Tests for the guest subcommand."""

    @patch("nova_plex.save_json")
    @patch("nova_plex.plex_get")
    def test_guest_detects_new_device(self, mock_get, mock_save, plex_module, sample_sessions_xml, tmp_path):
        mock_get.return_value = sample_sessions_xml
        guest_file = tmp_path / "guests.json"
        with patch.object(plex_module, "GUEST_FILE", guest_file):
            args = MagicMock()
            plex_module.cmd_guest(args)
        mock_save.assert_called()
        data = mock_save.call_args[0][1]
        assert "known_devices" in data
        assert "DEVICE_ABC" in data["known_devices"]


class TestCmdSeasonal:
    """Tests for the seasonal subcommand."""

    @patch("nova_plex.save_json")
    @patch("nova_plex.plex_get")
    def test_seasonal_stores_genre_data(self, mock_get, mock_save, plex_module, sample_history_xml, tmp_path):
        mock_get.return_value = sample_history_xml
        seasonal_file = tmp_path / "seasonal.json"
        with patch.object(plex_module, "SEASONAL_FILE", seasonal_file):
            args = MagicMock()
            plex_module.cmd_seasonal(args)
        mock_save.assert_called()
        data = mock_save.call_args[0][1]
        assert "months" in data


class TestAllSubcommandsExist:
    """Verify each subcommand function exists and is callable."""

    def test_all_13_commands_registered(self, plex_module):
        expected = {
            "history", "playing", "stats", "sync", "ondeck",
            "recommend", "mood", "filmschool", "shame",
            "velocity", "guest", "rewatch", "seasonal",
        }
        assert set(plex_module.COMMANDS.keys()) == expected

    def test_all_command_functions_callable(self, plex_module):
        for name, (func, desc) in plex_module.COMMANDS.items():
            assert callable(func), f"Command '{name}' function is not callable"
            assert isinstance(desc, str), f"Command '{name}' missing description"


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityPlex:
    """Security tests: no token leaks, no hardcoded secrets."""

    def test_token_not_in_source_code(self):
        source = Path(__file__).parent.parent / "nova_plex.py"
        content = source.read_text()
        # Should not have hardcoded tokens
        assert "xoxb-" not in content
        assert "sk-" not in content
        assert "AKIA" not in content

    def test_token_retrieved_from_keychain_only(self):
        source = Path(__file__).parent.parent / "nova_plex.py"
        content = source.read_text()
        assert "nova-plex-token" in content
        assert "find-generic-password" in content

    @patch("subprocess.run")
    def test_token_never_in_log_output(self, mock_run, plex_module, caplog):
        """Verify the token value never appears in log messages."""
        mock_run.return_value = MagicMock(stdout="SUPER_SECRET_TOKEN\n", returncode=0)
        plex_module._TOKEN_CACHE = None
        tok = plex_module._plex_token()
        assert tok == "SUPER_SECRET_TOKEN"
        # Check no log message contains the actual token
        for record in caplog.records:
            assert "SUPER_SECRET_TOKEN" not in record.getMessage()

    def test_state_files_dont_contain_auth_tokens(self, plex_module, tmp_path):
        """State files written by save_json should not contain auth tokens."""
        test_data = {
            "sessions": [{"title": "Test", "state": "playing"}],
            "updated": datetime.now().isoformat(),
        }
        target = tmp_path / "state.json"
        plex_module.save_json(target, test_data)
        content = target.read_text()
        assert "xoxb-" not in content
        assert "X-Plex-Token" not in content

    def test_no_library_23_data_in_output(self, plex_module, sample_history_xml):
        """Library 23 items must never appear in any output."""
        items = []
        for video in sample_history_xml.findall(".//Video"):
            lib_id = video.get("librarySectionID", "0")
            if not plex_module._skip_library(lib_id):
                items.append(video.get("title"))
        assert "Other Library Item" not in items


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestHistoryFlow:
    """Full history flow with mocked API responses."""

    @patch("nova_plex.store_vector")
    @patch("nova_plex.plex_get")
    def test_full_history_ingestion(self, mock_get, mock_store, plex_module, sample_history_xml):
        mock_get.return_value = sample_history_xml
        args = MagicMock()
        plex_module.cmd_history(args)
        # Check vector storage was called with correct source
        for call in mock_store.call_args_list:
            _, kwargs_or_args = call[0], call[1] if len(call) > 1 else {}
            text, source = call[0][0], call[0][1]
            assert source == "plex_watch_history"
            assert "Jordan watched" in text


@pytest.mark.functional
class TestShameFlow:
    """Full shame flow produces correct message format."""

    @patch("nova_plex.post_dm")
    @patch("nova_plex.plex_get")
    def test_shame_message_format(self, mock_get, mock_dm, plex_module, sample_ondeck_xml):
        mock_get.return_value = sample_ondeck_xml
        args = MagicMock()
        plex_module.cmd_shame(args)
        if mock_dm.called:
            msg = mock_dm.call_args[0][0]
            assert "Shame Board" in msg
            assert "%" in msg  # progress percentage
            assert "days ago" in msg


@pytest.mark.functional
class TestStatsFlow:
    """Full stats flow calculates hours correctly."""

    @patch("nova_plex.alert")
    @patch("nova_plex.plex_get")
    def test_stats_hour_calculation(self, mock_get, mock_post, plex_module, sample_history_xml):
        mock_get.return_value = sample_history_xml
        args = MagicMock()
        plex_module.cmd_stats(args)
        msg = mock_post.call_args[0][0]
        # 60min + 120min = 180min = 3.0 hours (library 23 excluded)
        assert "3.0 hours" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# FRAME TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.frame
class TestArgParsing:
    """Subcommand argument parsing for all 13 commands."""

    def test_all_commands_parse(self, plex_module):
        import argparse
        for cmd_name in plex_module.COMMANDS:
            parser = argparse.ArgumentParser()
            parser.add_argument("command", choices=plex_module.COMMANDS.keys())
            parser.add_argument("--quiet", "-q", action="store_true")
            args = parser.parse_args([cmd_name])
            assert args.command == cmd_name

    def test_quiet_flag_parses(self, plex_module):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=plex_module.COMMANDS.keys())
        parser.add_argument("--quiet", "-q", action="store_true")
        args = parser.parse_args(["history", "--quiet"])
        assert args.quiet is True

    def test_unknown_subcommand_errors(self, plex_module):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=plex_module.COMMANDS.keys())
        with pytest.raises(SystemExit):
            parser.parse_args(["nonexistent"])

    def test_quiet_suppresses_slack(self, plex_module, capsys):
        plex_module.QUIET = True
        plex_module.post("suppressed message")
        captured = capsys.readouterr()
        assert "suppressed message" in captured.out
        plex_module.QUIET = False


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("NOVA_LIVE_TESTS") != "1",
                    reason="live Plex + Keychain test; opt in with NOVA_LIVE_TESTS=1 (suite is offline by default)")
class TestPlexIntegration:
    """Integration tests that hit the real Plex server. Opt-in only (NOVA_LIVE_TESTS=1); skipped if unavailable."""

    @pytest.fixture(autouse=True)
    def check_plex_available(self):
        import urllib.request
        try:
            urllib.request.urlopen("http://192.168.1.2:32400/identity", timeout=3)
        except Exception:
            pytest.skip("Plex server at 192.168.1.2:32400 not available")

    def test_can_connect_to_plex(self, plex_module):
        """Verify connection to Plex server."""
        import urllib.request
        resp = urllib.request.urlopen("http://192.168.1.2:32400/identity", timeout=5)
        assert resp.status == 200

    def test_rewatch_returns_data(self, plex_module):
        """Rewatch command runs without error against live server."""
        args = MagicMock()
        plex_module.QUIET = True
        try:
            plex_module.cmd_rewatch(args)
        except Exception as e:
            pytest.fail(f"Rewatch command failed: {e}")
        finally:
            plex_module.QUIET = False

    def test_playing_returns_valid_state(self, plex_module, tmp_path):
        """Playing command runs and writes valid state."""
        playing_file = tmp_path / "playing.json"
        with patch.object(plex_module, "PLAYING_FILE", playing_file):
            args = MagicMock()
            plex_module.QUIET = True
            try:
                plex_module.cmd_playing(args)
            except Exception as e:
                pytest.fail(f"Playing command failed: {e}")
            finally:
                plex_module.QUIET = False


# ═══════════════════════════════════════════════════════════════════════════════
# HOUSE CATEGORIES (added 2026-10-05) — the 7 unittest classes.
# Tests for nova_plex.py — the 7 house categories (Security, Performance, Retry, Unit,
# Integration, Functional, Frame). Written by Jordan Koch (via Claude).
# A private copy of the module is loaded (never touches sys.modules["nova_plex"]) with PLEX_URL
# resolution, Keychain, Slack/notify and the vector store stubbed at load; state files go to a tempdir.
# ═══════════════════════════════════════════════════════════════════════════════
import importlib.util as _ilu
import re as _re
import subprocess as _sp
import tempfile as _tf
import time as _time
import unittest
import urllib.error as _uerr

_SCRIPT = Path(__file__).resolve().parent.parent / "nova_plex.py"
_SRC = _SCRIPT.read_text()
_PTMP = _tf.TemporaryDirectory()


def _load_px():
    import nova_resolve
    with patch.object(nova_resolve, "resolve_url", return_value="http://plex.test:32400"):
        spec = _ilu.spec_from_file_location("nova_plex_house_copy", _SCRIPT)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


px = _load_px()
px.nova_config = MagicMock(SLACK_NOTIFY="C_N", SLACK_CHAN="C_C", JORDAN_DM="D_J")
px.nova_notify = MagicMock()
px._keychain_get = MagicMock(return_value="tok-test")
px._keychain_set = MagicMock()
px.VECTOR_URL = "http://vector.test/remember"
_PROOT = Path(_PTMP.name)
px.WORKSPACE = _PROOT
for _n in ("PLAYING_FILE", "MOOD_FILE", "GUEST_FILE", "CANON_FILE", "SEASONAL_FILE"):
    setattr(px, _n, _PROOT / f"{_n.lower()}.json")


def _hist(*titles, lib="6"):
    v = "".join(f'<Video grandparentTitle="{t}" title="ep" librarySectionID="{lib}" viewedAt="1" '
                f'duration="60000" type="episode"/>' for t in titles)
    return ET.fromstring(f"<MediaContainer>{v}</MediaContainer>")


class _PxBase(unittest.TestCase):
    def setUp(self):
        px._TOKEN_CACHE = None
        px.QUIET = False
        px.nova_config.reset_mock(); px.nova_notify.reset_mock()
        px._keychain_get.reset_mock(return_value=True, side_effect=True)
        px._keychain_get.return_value = "tok-test"
        px.CANON_FILE.unlink(missing_ok=True)


class TestSecurity(_PxBase):
    def test_no_hardcoded_credentials(self):
        pat = _re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", _re.I)
        self.assertIsNone(pat.search(_SRC))

    def test_missing_keychain_creds_exit_without_network(self):
        px._keychain_get.return_value = ""
        with patch.object(px.urllib.request, "urlopen") as u, self.assertRaises(SystemExit):
            px._plex_token()
        u.assert_not_called()

    def test_token_exchange_saves_to_keychain_never_logs(self):
        resp = MagicMock(); resp.__enter__.return_value.read.return_value = b'{"user": {"authToken": "NEWTOK123"}}'
        px._keychain_get.side_effect = lambda s: {"nova-plex-email": "e", "nova-plex-password": "p"}.get(s, "")
        with patch.object(px.urllib.request, "urlopen", return_value=resp), patch.object(px, "log") as log:
            self.assertEqual(px._plex_token(), "NEWTOK123")
        px._keychain_set.assert_called_once_with("nova-plex-token", "NEWTOK123")
        self.assertNotIn("NEWTOK123", str(log.mock_calls))


class TestPerformance(_PxBase):
    def test_rewatch_over_10k_views_fast(self):
        root = _hist(*[f"Show{i % 500}" for i in range(10_000)])
        with patch.object(px, "plex_get", return_value=root), patch("builtins.print"):
            t0 = _time.perf_counter()
            px.QUIET = True
            px.cmd_rewatch(MagicMock())
        self.assertLess(_time.perf_counter() - t0, 2.0)
        self.assertEqual(len(px.load_json(px.CANON_FILE)["items"]), 500)


class TestRetry(_PxBase):
    def test_unreachable_plex_exits_1_once(self):
        # RETRY GAP: plex_get()/urlopen — one attempt; main() maps URLError to exit 1 (no retry/backoff)
        with patch.object(px.urllib.request, "urlopen", side_effect=_uerr.URLError("refused")) as u, \
             patch.object(sys, "argv", ["nova_plex.py", "history", "-q"]), patch("builtins.print"):
            px.cmd_history(MagicMock())                         # history swallows its own fetch error
            self.assertEqual(u.call_count, 1)
            with patch.object(px, "cmd_history", side_effect=_uerr.URLError("refused")), \
                 patch.dict(px.COMMANDS, {"history": (px.cmd_history, "")}):
                with self.assertRaises(SystemExit) as cm:
                    px.main()
        self.assertEqual(cm.exception.code, 1)

    def test_post_and_vector_fail_open(self):
        px.nova_config.post_both.side_effect = RuntimeError("slack down")
        with patch("builtins.print") as pr:
            px.post("hello")
        pr.assert_called_with("hello")
        px.nova_config.post_both.side_effect = None
        with patch.object(px.urllib.request, "urlopen", side_effect=OSError("down")):
            px.store_vector("t", "plex")


class TestUnit(_PxBase):
    def test_alert_title_body_split(self):
        px.alert("*Stats Title*\nline two", level="warning")
        args, kw = px.nova_notify.call_args
        self.assertEqual((args[0], kw["body"], kw["level"]), ("Stats Title", "line two", "warning"))

    def test_quiet_alert_prints(self):
        px.QUIET = True
        with patch("builtins.print") as pr:
            px.alert("x")
        px.nova_notify.assert_not_called()
        pr.assert_called_once_with("x")

    def test_token_cached(self):
        px.token(); px.token()
        self.assertEqual(px._keychain_get.call_count, 1)


class TestIntegration(_PxBase):
    def test_plex_get_builds_tokenized_url_on_resolved_host(self):
        resp = MagicMock(); resp.__enter__.return_value.read.return_value = b"<MediaContainer/>"
        with patch.object(px.urllib.request, "urlopen", return_value=resp) as u:
            px.plex_get("/library/sections", {"a": 1})
        url = u.call_args[0][0].full_url
        self.assertTrue(url.startswith("http://plex.test:32400/library/sections?"))
        self.assertIn("X-Plex-Token=tok-test", url)

    def test_new_canon_entry_posts_to_chat(self):
        with patch.object(px, "plex_get", return_value=_hist("Columbo", "Columbo", "Columbo", "Other", lib="6")), \
             patch("builtins.print"):
            px.cmd_rewatch(MagicMock())
        msg, kw = px.nova_config.post_both.call_args
        self.assertIn("Columbo (3x)", msg[0])
        self.assertEqual(kw["slack_channel"], "C_C")


class TestFunctional(_PxBase):
    def test_main_rewatch_quiet_golden_path(self):
        with patch.object(px, "plex_get", return_value=_hist("A", "A", "B", "Hidden", lib="6")), \
             patch.object(sys, "argv", ["nova_plex.py", "rewatch", "--quiet"]), patch("builtins.print"):
            px.main()
        canon = px.load_json(px.CANON_FILE)
        self.assertEqual(canon["items"]["A"]["count"], 2)
        px.nova_config.post_both.assert_not_called()          # --quiet never posts

    def test_library_23_never_counted(self):
        with patch.object(px, "plex_get", return_value=_hist("Secret", "Secret", "Secret", lib="23")), \
             patch("builtins.print"):
            px.QUIET = True
            px.cmd_rewatch(MagicMock())
        self.assertNotIn("Secret", px.load_json(px.CANON_FILE)["items"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        code = ("import sys, runpy, nova_resolve; nova_resolve.resolve_url = lambda *a, **k: 'http://plex.test:1'; "
                f"sys.argv = ['nova_plex.py', '--help']; runpy.run_path({str(_SCRIPT)!r}, run_name='__main__')")
        r = _sp.run([sys.executable, "-c", code], cwd=str(_SCRIPT.parent), capture_output=True, text=True,
                    timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rewatch", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', _SRC)
        code = "import nova_resolve; nova_resolve.resolve_url = lambda *a, **k: 'http://plex.test:1'; import nova_plex"
        r = _sp.run([sys.executable, "-c", code], cwd=str(_SCRIPT.parent), capture_output=True, text=True,
                    timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
