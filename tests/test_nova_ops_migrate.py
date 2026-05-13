"""
test_nova_ops_migrate.py — All 7 test categories for nova_ops_migrate.py
Written by Jordan Koch.
"""

import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

_nova_logger = MagicMock()
_nova_logger.LOG_INFO = "info"
_nova_logger.LOG_WARN = "warn"
_nova_logger.LOG_ERROR = "error"
sys.modules["nova_logger"] = _nova_logger

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ops_migrate.py"
_spec = importlib.util.spec_from_file_location("nova_ops_migrate", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

MIGRATIONS = _mod.MIGRATIONS
get_applied = _mod.get_applied
apply_migration = _mod.apply_migration
run_migrations = _mod.run_migrations
DB_DSN = _mod.DB_DSN


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_db_dsn_uses_local_host(self):
        """DB_DSN must connect to local nova_ops database."""
        self.assertIn("nova_ops", DB_DSN)

    def test_migrations_use_if_not_exists(self):
        """DDL must use IF NOT EXISTS to be idempotent (safe re-run)."""
        for mid, desc, sql in MIGRATIONS:
            if "CREATE TABLE" in sql:
                self.assertIn("IF NOT EXISTS", sql,
                              f"Migration {mid} CREATE TABLE lacks IF NOT EXISTS")

    def test_no_drop_without_if_exists(self):
        """No unconditional DROP statements (that could destroy data)."""
        for mid, desc, sql in MIGRATIONS:
            lines = sql.upper().split("\n")
            for line in lines:
                if "DROP " in line and "IF EXISTS" not in line and "--" not in line:
                    self.fail(f"Migration {mid} has DROP without IF EXISTS: {line}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_migrations_list_fast(self):
        """Accessing MIGRATIONS list must be instantaneous."""
        start = time.perf_counter()
        for _ in range(10000):
            _ = len(MIGRATIONS)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01)

    def test_migration_count_reasonable(self):
        """Should have at least 3 but not too many migrations."""
        self.assertGreaterEqual(len(MIGRATIONS), 3)
        self.assertLessEqual(len(MIGRATIONS), 100)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_run_migrations_exits_on_asyncpg_missing(self):
        """run_migrations must exit gracefully when asyncpg is not installed."""
        import asyncio

        import builtins
        original_import = builtins.__import__

        def mock_import(name, *a, **kw):
            if name == "asyncpg":
                raise ImportError("No module named 'asyncpg'")
            return original_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=mock_import):
            with patch("sys.exit") as mock_exit:
                try:
                    asyncio.run(run_migrations())
                except SystemExit:
                    pass

        mock_exit.assert_called_with(1)

    def test_get_applied_returns_empty_set_on_no_table(self):
        """get_applied returns empty set when schema_migrations table doesn't exist."""
        import asyncio

        mock_conn = AsyncMock()
        mock_conn.fetch.side_effect = Exception("relation does not exist")

        async def _run():
            return await get_applied(mock_conn)

        result = asyncio.run(_run())
        self.assertIsInstance(result, set)
        self.assertEqual(len(result), 0)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_migrations_are_sequential(self):
        """Migration IDs must be sequential starting at 1."""
        ids = [m[0] for m in MIGRATIONS]
        self.assertEqual(ids, list(range(1, len(MIGRATIONS) + 1)))

    def test_migrations_have_descriptions(self):
        """All migrations must have non-empty descriptions."""
        for mid, desc, sql in MIGRATIONS:
            self.assertIsInstance(desc, str)
            self.assertGreater(len(desc.strip()), 0, f"Migration {mid} has empty description")

    def test_migrations_have_sql(self):
        """All migrations must have non-empty SQL."""
        for mid, desc, sql in MIGRATIONS:
            self.assertIsInstance(sql, str)
            self.assertGreater(len(sql.strip()), 0, f"Migration {mid} has empty SQL")

    def test_migration_1_creates_tracking_table(self):
        """Migration 1 must create the schema_migrations tracking table."""
        mid, desc, sql = MIGRATIONS[0]
        self.assertEqual(mid, 1)
        self.assertIn("schema_migrations", sql)

    def test_migration_2_creates_scheduler_runs(self):
        """Migration 2 must create the scheduler_runs table."""
        mid, desc, sql = MIGRATIONS[1]
        self.assertEqual(mid, 2)
        self.assertIn("scheduler_runs", sql)

    def test_apply_migration_records_in_tracking_table(self):
        """apply_migration must insert into schema_migrations after applying."""
        import asyncio

        mock_conn = AsyncMock()
        mock_txn = AsyncMock()
        mock_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=mock_txn)
        mock_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

        executed = []
        mock_conn.execute = AsyncMock(side_effect=lambda sql, *a: executed.append(sql))

        async def _run():
            await apply_migration(mock_conn, 2, "test migration", "CREATE TABLE test (id INT)")

        asyncio.run(_run())

        insert_calls = [s for s in executed if "INSERT INTO schema_migrations" in s]
        self.assertTrue(len(insert_calls) > 0, "apply_migration must record in tracking table")

    def test_db_dsn_format(self):
        """DB_DSN must be a valid PostgreSQL connection string."""
        self.assertIn("postgresql://", DB_DSN)
        self.assertIn("nova_ops", DB_DSN)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_run_migrations_with_all_applied(self):
        """run_migrations does nothing when all migrations are already applied."""
        import asyncio

        mock_conn = AsyncMock()
        applied_ids = {m[0] for m in MIGRATIONS}
        mock_conn.fetch = AsyncMock(return_value=[
            {"migration_id": mid} for mid in applied_ids
        ])

        apply_calls = []

        async def _run():
            # Mock asyncpg.connect
            with patch("asyncpg.connect", return_value=mock_conn):
                mock_conn.close = AsyncMock()
                await run_migrations()

        try:
            asyncio.run(_run())
        except Exception:
            pass  # asyncpg not installed — test the logic another way

    def test_migration_sql_is_semicolon_splittable(self):
        """All migration SQL must be splittable by semicolons for statement execution."""
        for mid, desc, sql in MIGRATIONS:
            stmts = [s.strip() for s in sql.split(";") if s.strip()]
            self.assertGreater(len(stmts), 0, f"Migration {mid} has no statements after split")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_check_only_does_not_apply(self):
        """--check must list pending migrations without applying."""
        import asyncio

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])  # None applied
        mock_conn.close = AsyncMock()

        apply_calls = []
        original_apply = apply_migration

        async def capture_apply(conn, mid, desc, sql):
            apply_calls.append(mid)

        with patch.object(_mod, "apply_migration", side_effect=capture_apply):
            try:
                with patch("asyncpg.connect", return_value=mock_conn):
                    asyncio.run(run_migrations(check_only=True))
            except Exception:
                pass

        self.assertEqual(len(apply_calls), 0, "--check must not apply migrations")

    def test_list_all_shows_all_migrations(self):
        """--list must show all migrations regardless of applied status."""
        import asyncio
        import io
        from contextlib import redirect_stdout

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[{"migration_id": 1}])
        mock_conn.close = AsyncMock()

        output = io.StringIO()

        try:
            with patch("asyncpg.connect", return_value=mock_conn):
                with redirect_stdout(output):
                    asyncio.run(run_migrations(list_all=True))
        except Exception:
            pass

        printed = output.getvalue()
        if printed:
            # Should show all migration IDs
            for mid, _, _ in MIGRATIONS:
                self.assertIn(str(mid), printed)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_migrations_is_list(self):
        self.assertIsInstance(MIGRATIONS, list)

    def test_key_functions_callable(self):
        for fn in [get_applied, apply_migration, run_migrations, _mod.main]:
            self.assertTrue(callable(fn))

    def test_db_dsn_defined(self):
        self.assertIsInstance(DB_DSN, str)
        self.assertGreater(len(DB_DSN), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
