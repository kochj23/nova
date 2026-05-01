#!/usr/bin/env python3
"""
test_herd_config.py — Tests for herd_config.py membership and settings.

Run: python3 -m pytest tests/test_herd_config.py -v
Written by Jordan Koch.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))


# ══════════════════════════════════════════════════════════════════════════════
# herd_config.py — member list, emails, flags
# ══════════════════════════════════════════════════════════════════════════════

class TestHerdMembers:
    """Tests for herd member list integrity."""

    def test_all_expected_members_present(self):
        from herd_config import HERD, HERD_EMAILS
        expected_emails = {
            "sam@jasonacox.com",
            "oc@mostlycopyandpaste.com",
            "gaston@bluemoxon.com",
            "marey@makehorses.org",
            "colette@pilatesmuse.co",
            "rockbot@makehorses.org",
            "ara@monsterheaven.com",
            "jules@laplante.dev",
            "nova@servernest.xyz",
        }
        assert HERD_EMAILS == expected_emails

    def test_expected_member_count(self):
        from herd_config import HERD
        assert len(HERD) == 9

    def test_all_members_have_name(self):
        from herd_config import HERD
        for member in HERD:
            assert "name" in member, f"Member missing 'name': {member}"
            assert member["name"], f"Member has empty name: {member}"

    def test_all_members_have_email(self):
        from herd_config import HERD
        for member in HERD:
            assert "email" in member, f"Member missing 'email': {member}"
            assert "@" in member["email"], f"Invalid email for {member['name']}: {member['email']}"

    def test_all_members_have_profile(self):
        from herd_config import HERD
        for member in HERD:
            assert "profile" in member, f"Member missing 'profile': {member}"
            assert member["profile"].endswith(".md"), \
                f"Profile for {member['name']} should be .md: {member['profile']}"


class TestHerdNoDuplicates:
    """Tests for duplicate detection in herd config."""

    def test_no_duplicate_emails(self):
        from herd_config import HERD
        emails = [m["email"] for m in HERD]
        assert len(emails) == len(set(emails)), \
            f"Duplicate emails found: {[e for e in emails if emails.count(e) > 1]}"

    def test_no_duplicate_names(self):
        from herd_config import HERD
        names = [m["name"] for m in HERD]
        assert len(names) == len(set(names)), \
            f"Duplicate names found: {[n for n in names if names.count(n) > 1]}"

    def test_no_duplicate_profiles(self):
        from herd_config import HERD
        profiles = [m["profile"] for m in HERD]
        assert len(profiles) == len(set(profiles)), \
            f"Duplicate profiles found: {[p for p in profiles if profiles.count(p) > 1]}"

    def test_herd_emails_set_matches_list(self):
        """HERD_EMAILS set should exactly match emails from HERD list."""
        from herd_config import HERD, HERD_EMAILS
        list_emails = {m["email"] for m in HERD}
        assert HERD_EMAILS == list_emails


class TestHerdRetiredMembers:
    """Tests that retired/removed members are not in the config."""

    def test_nova_cosmos_removed(self):
        """Nova Cosmos was replaced by Nova Scott."""
        from herd_config import HERD_EMAILS
        assert "novacosmos184@gmail.com" not in HERD_EMAILS

    def test_no_test_emails(self):
        """No test/placeholder emails should be in the config."""
        from herd_config import HERD_EMAILS
        for email in HERD_EMAILS:
            assert "test" not in email.lower(), f"Test email found: {email}"
            assert "example.com" not in email, f"Example email found: {email}"


class TestHerdSpecificMembers:
    """Tests for specific member presence and details."""

    def test_sam_present(self):
        from herd_config import HERD
        sam = next((m for m in HERD if m["name"] == "Sam"), None)
        assert sam is not None
        assert sam["email"] == "sam@jasonacox.com"
        assert sam["profile"] == "sam.md"

    def test_oc_present(self):
        from herd_config import HERD
        oc = next((m for m in HERD if m["name"] == "O.C."), None)
        assert oc is not None
        assert oc["email"] == "oc@mostlycopyandpaste.com"

    def test_nova_scott_present(self):
        from herd_config import HERD
        ns = next((m for m in HERD if m["name"] == "Nova Scott"), None)
        assert ns is not None
        assert ns["email"] == "nova@servernest.xyz"

    def test_jules_present(self):
        from herd_config import HERD
        jules = next((m for m in HERD if m["name"] == "Jules"), None)
        assert jules is not None
        assert jules["email"] == "jules@laplante.dev"

    def test_ara_present(self):
        from herd_config import HERD
        ara = next((m for m in HERD if m["name"] == "Ara"), None)
        assert ara is not None
        assert ara["email"] == "ara@monsterheaven.com"


class TestCCJordanWorkFlag:
    """Tests for the CC_JORDAN_WORK flag."""

    def test_cc_jordan_work_is_true(self):
        from herd_config import CC_JORDAN_WORK
        assert CC_JORDAN_WORK is True

    def test_cc_jordan_work_is_boolean(self):
        from herd_config import CC_JORDAN_WORK
        assert isinstance(CC_JORDAN_WORK, bool)


class TestHerdEmailFormats:
    """Tests that all emails have valid formatting."""

    def test_all_emails_lowercase(self):
        from herd_config import HERD
        for member in HERD:
            assert member["email"] == member["email"].lower(), \
                f"{member['name']}'s email not lowercase: {member['email']}"

    def test_all_emails_have_domain_with_tld(self):
        from herd_config import HERD
        for member in HERD:
            email = member["email"]
            parts = email.split("@")
            assert len(parts) == 2, f"Invalid email format for {member['name']}: {email}"
            domain = parts[1]
            assert "." in domain, f"Missing TLD for {member['name']}: {email}"

    def test_no_whitespace_in_emails(self):
        from herd_config import HERD
        for member in HERD:
            assert member["email"].strip() == member["email"], \
                f"Whitespace in email for {member['name']}: '{member['email']}'"


# ══════════════════════════════════════════════════════════════════════════════
# Functional tests — end-to-end herd email flow
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.functional
class TestHerdMailFunctional:
    """Functional tests for the herd email send flow."""

    def test_herd_mail_dry_run(self):
        """Test that herd_mail.py config validation works in dry-run mode."""
        import subprocess
        herd_mail = str(Path.home() / ".openclaw/scripts/herd_mail.py")
        result = subprocess.run(
            ["python3", herd_mail, "config"],
            capture_output=True, text=True, timeout=15,
            env={**dict(__import__("os").environ), "WAGGLE_HOST": "smtp.test.com",
                 "WAGGLE_USER": "test@test.com", "WAGGLE_PASS": "test",
                 "WAGGLE_FROM": "test@test.com"},
        )
        # Should succeed with valid config
        assert "SMTP configuration valid" in result.stderr or result.returncode == 0

    def test_herd_mail_no_args_shows_help(self):
        """Running herd_mail.py with no args should show help."""
        import subprocess
        herd_mail = str(Path.home() / ".openclaw/scripts/herd_mail.py")
        result = subprocess.run(
            ["python3", herd_mail],
            capture_output=True, text=True, timeout=15,
        )
        # Should print help or exit with non-zero
        assert result.returncode != 0 or "usage" in result.stdout.lower() or "usage" in result.stderr.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "not integration and not functional"])
