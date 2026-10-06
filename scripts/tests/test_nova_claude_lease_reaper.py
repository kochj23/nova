#!/usr/bin/env python3
"""Tests for nova_claude_lease_reaper.py. The lease/claim logic itself is SQL (claude_reap et al.)
and is self-tested in a rolled-back transaction; this covers the script's wiring.
Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "nova_claude_lease_reaper.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("reaper_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    stub = types.ModuleType("nova_notify")
    stub.notify = MagicMock()
    with patch.dict(sys.modules, {"nova_notify": stub}):
        spec.loader.exec_module(mod)
    return mod


def _conn(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__.return_value = cur
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    return conn, cur


class TestReaper(unittest.TestCase):
    def test_no_credentials_and_read_only_sql_entry(self):
        self.assertNotRegex(SRC, r"password\s*=|sk-[A-Za-z0-9]{10}")
        self.assertIn("SELECT id, description, was FROM claude_reap()", SRC)

    def test_each_reaped_task_notifies_claude_fleet_once(self):
        m = _load()
        conn, cur = _conn([(7, "fix the thing", "nova-core10/abc"), (9, "x" * 500, "Office-M4-2/def")])
        m.psycopg2 = types.SimpleNamespace(connect=MagicMock(return_value=conn))
        m.main()
        self.assertEqual(m.notify.call_count, 2)
        kw = m.notify.call_args_list[0].kwargs
        self.assertEqual(kw["category"], "claude_fleet")
        self.assertEqual(kw["dedup_key"], "claude-reap:7:nova-core10/abc")
        self.assertIn("claude_claim('<your claim id>', 7)", kw["body"])
        self.assertLess(len(m.notify.call_args_list[1].kwargs["body"]), 400)  # long descriptions trimmed

    def test_nothing_to_reap_is_silent(self):
        m = _load()
        conn, _ = _conn([])
        m.psycopg2 = types.SimpleNamespace(connect=MagicMock(return_value=conn))
        m.main()
        m.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
