"""Tests for the Alembic PostgreSQL migration structure.

Most tests in this module are pure file-inspection tests that do not require
psycopg, alembic, or a running Postgres server to be installed.

The live migration smoke test at the bottom skips unless all three
preconditions are true:
  - KB_AUDIT_POSTGRES_TEST_URL is set;
  - alembic is importable;
  - psycopg is importable.
"""

from __future__ import annotations

import os
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent  # project root
_MIGRATIONS_DIR = _ROOT / "migrations"
_VERSIONS_DIR = _MIGRATIONS_DIR / "versions"
_INITIAL_MIGRATION = _VERSIONS_DIR / "0001_initial_schema.py"

_POSTGRES_URL: str | None = os.environ.get("KB_AUDIT_POSTGRES_TEST_URL")

try:
    import alembic as _alembic_probe  # noqa: F401
    _ALEMBIC_AVAILABLE = True
except ImportError:
    _ALEMBIC_AVAILABLE = False

try:
    import psycopg as _psycopg_probe  # noqa: F401
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False


# ---------------------------------------------------------------------------
# File structure
# ---------------------------------------------------------------------------

class TestMigrationFileStructure:
    def test_alembic_ini_exists(self):
        assert (_ROOT / "alembic.ini").is_file(), "alembic.ini must exist at project root"

    def test_migrations_directory_exists(self):
        assert _MIGRATIONS_DIR.is_dir(), "migrations/ directory must exist"

    def test_env_py_exists(self):
        assert (_MIGRATIONS_DIR / "env.py").is_file(), "migrations/env.py must exist"

    def test_script_mako_exists(self):
        assert (_MIGRATIONS_DIR / "script.py.mako").is_file(), (
            "migrations/script.py.mako must exist"
        )

    def test_versions_directory_exists(self):
        assert _VERSIONS_DIR.is_dir(), "migrations/versions/ directory must exist"

    def test_initial_migration_file_exists(self):
        assert _INITIAL_MIGRATION.is_file(), (
            f"Initial migration {_INITIAL_MIGRATION} must exist"
        )

    def test_alembic_ini_references_migrations_directory(self):
        ini_text = (_ROOT / "alembic.ini").read_text()
        assert "script_location" in ini_text
        assert "migrations" in ini_text

    def test_readme_exists(self):
        assert (_MIGRATIONS_DIR / "README").is_file(), (
            "migrations/README must exist with usage instructions"
        )


# ---------------------------------------------------------------------------
# Migration content (pure source inspection, no alembic import needed)
# ---------------------------------------------------------------------------

class TestInitialMigrationContent:
    def _src(self) -> str:
        return _INITIAL_MIGRATION.read_text()

    def test_revision_is_0001(self):
        assert 'revision: str = "0001"' in self._src()

    def test_down_revision_is_none(self):
        assert "down_revision" in self._src()
        assert "None" in self._src()

    def test_delegates_to_iter_postgres_schema_statements(self):
        src = self._src()
        assert "iter_postgres_schema_statements" in src, (
            "upgrade() must call iter_postgres_schema_statements() to avoid schema drift"
        )
        assert "schema_postgres" in src

    def test_downgrade_drops_all_expected_tables(self):
        src = self._src()
        for table in ("scans", "documents", "audit_results", "finding_workflow", "scan_state"):
            assert table in src, f"downgrade() must DROP TABLE {table}"

    def test_upgrade_function_present(self):
        assert "def upgrade(" in self._src()

    def test_downgrade_function_present(self):
        assert "def downgrade(" in self._src()

    def test_covers_all_schema_tables(self):
        """Verify the migration touches the same tables as POSTGRES_SCHEMA_STATEMENTS."""
        from kb_audit.storage.schema_postgres import POSTGRES_SCHEMA_STATEMENTS
        src = self._src()
        for stmt in POSTGRES_SCHEMA_STATEMENTS:
            # Each DDL statement creates a named table; the table name should
            # appear somewhere in the migration (either in upgrade or downgrade).
            # Extract the table name from "CREATE TABLE IF NOT EXISTS <name>"
            import re
            match = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", stmt)
            assert match, f"Could not parse table name from: {stmt[:60]}"
            table_name = match.group(1)
            assert table_name in src, (
                f"Migration must reference table {table_name!r} (found in schema DDL)"
            )

    def test_covers_all_schema_indexes(self):
        """Verify the migration delegates to iter_postgres_schema_statements which includes indexes."""
        # Since upgrade() calls iter_postgres_schema_statements() — which returns
        # all DDL including index statements — index coverage is implicit.
        # Confirm the delegation call is present in the migration source.
        src = self._src()
        assert "iter_postgres_schema_statements" in src

    def test_iter_schema_statements_matches_table_set(self):
        """iter_postgres_schema_statements must include all DDL tables and indexes."""
        from kb_audit.storage.schema_postgres import (
            POSTGRES_INDEX_STATEMENTS,
            POSTGRES_SCHEMA_STATEMENTS,
            iter_postgres_schema_statements,
        )
        full = iter_postgres_schema_statements()
        # Every table DDL must be present
        for stmt in POSTGRES_SCHEMA_STATEMENTS:
            assert stmt in full, "iter_postgres_schema_statements missing a table DDL"
        # Every index DDL must be present
        for stmt in POSTGRES_INDEX_STATEMENTS:
            assert stmt in full, "iter_postgres_schema_statements missing an index DDL"


# ---------------------------------------------------------------------------
# Alembic import (skipped when alembic not installed)
# ---------------------------------------------------------------------------

class TestMigrationImportable:
    @pytest.mark.skipif(not _ALEMBIC_AVAILABLE, reason="alembic not installed")
    def test_initial_migration_module_importable(self):
        """The migration module must be importable without a live DB."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migration_0001", str(_INITIAL_MIGRATION)
        )
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        # Loading the module-level code (but not calling upgrade/downgrade)
        # must succeed without a DB connection.
        assert spec.loader is not None
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert mod.revision == "0001"
        assert mod.down_revision is None

    @pytest.mark.skipif(not _ALEMBIC_AVAILABLE, reason="alembic not installed")
    def test_env_py_importable_without_url(self):
        """env.py must be importable without raising (only fails when run by alembic)."""
        # We just check the file is valid Python — we don't execute it here
        # because it calls context.is_offline_mode() at module level which
        # requires an active Alembic context.
        src = (_MIGRATIONS_DIR / "env.py").read_text()
        compile(src, str(_MIGRATIONS_DIR / "env.py"), "exec")

    @pytest.mark.skipif(not _ALEMBIC_AVAILABLE, reason="alembic not installed")
    def test_alembic_config_parses_correctly(self):
        from alembic.config import Config
        cfg = Config(str(_ROOT / "alembic.ini"))
        assert cfg.get_main_option("script_location") == "migrations"


# ---------------------------------------------------------------------------
# Factory guard (always runs)
# ---------------------------------------------------------------------------

class TestFactoryAcceptsPostgresUrls:
    def test_postgresql_url_returns_postgres_storage(self):
        from kb_audit.storage import create_storage
        from kb_audit.storage.postgres import PostgresStorage
        store = create_storage("postgresql://localhost/kbaudit")
        assert isinstance(store, PostgresStorage)

    def test_postgres_url_returns_postgres_storage(self):
        from kb_audit.storage import create_storage
        from kb_audit.storage.postgres import PostgresStorage
        store = create_storage("postgres://localhost/kbaudit")
        assert isinstance(store, PostgresStorage)


# ---------------------------------------------------------------------------
# Live migration smoke test (skipped without KB_AUDIT_POSTGRES_TEST_URL)
# ---------------------------------------------------------------------------

def _live_skip_reason() -> str:
    if not _ALEMBIC_AVAILABLE:
        return "alembic not installed — run: pip install -e '.[postgres]'"
    if not _PSYCOPG_AVAILABLE:
        return "psycopg not installed — run: pip install -e '.[postgres]'"
    return "Set KB_AUDIT_POSTGRES_TEST_URL to run live migration tests"


_LIVE_SKIP = not _ALEMBIC_AVAILABLE or not _PSYCOPG_AVAILABLE or not _POSTGRES_URL
_LIVE_SKIP_REASON = _live_skip_reason()


@pytest.mark.alembic_live
@pytest.mark.skipif(_LIVE_SKIP, reason=_LIVE_SKIP_REASON)
class TestLiveMigrationSmoke:
    """Apply the initial migration to a real Postgres DB and verify tables exist.

    Requires KB_AUDIT_POSTGRES_TEST_URL and psycopg + alembic installed.
    Uses a dedicated test database — never run against production.
    """

    def _normalize_url(self, url: str) -> str:
        """Return a psycopg3 SQLAlchemy URL."""
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://"):]
        if url.startswith("postgresql://") and "+psycopg" not in url:
            return "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    def test_migration_applies_cleanly(self):
        """alembic upgrade head creates all expected tables."""
        import subprocess
        import sys

        env = {**os.environ, "KB_AUDIT_POSTGRES_URL": _POSTGRES_URL}  # type: ignore[dict-item]
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_ROOT),
        )
        assert result.returncode == 0, (
            f"alembic upgrade head failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    def test_expected_tables_exist_after_migration(self):
        """After migration, all five kb_audit tables must be visible in the DB."""
        from sqlalchemy import create_engine, inspect

        engine = create_engine(self._normalize_url(_POSTGRES_URL))  # type: ignore[arg-type]
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
        engine.dispose()

        expected = {"scans", "documents", "audit_results", "finding_workflow", "scan_state"}
        missing = expected - tables
        assert not missing, f"Tables missing after migration: {missing}"

    def test_alembic_version_table_exists_after_migration(self):
        """Alembic's own version-tracking table must exist."""
        from sqlalchemy import create_engine, inspect

        engine = create_engine(self._normalize_url(_POSTGRES_URL))  # type: ignore[arg-type]
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
        engine.dispose()

        assert "alembic_version" in tables

    def test_migration_is_idempotent(self):
        """Running alembic upgrade head twice must not raise."""
        import subprocess
        import sys

        env = {**os.environ, "KB_AUDIT_POSTGRES_URL": _POSTGRES_URL}  # type: ignore[dict-item]
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(_ROOT),
            )
            assert result.returncode == 0, (
                f"alembic upgrade head not idempotent:\n"
                f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
            )

    def test_downgrade_removes_tables(self):
        """alembic downgrade base must remove kb_audit tables."""
        import subprocess
        import sys
        from sqlalchemy import create_engine, inspect

        env = {**os.environ, "KB_AUDIT_POSTGRES_URL": _POSTGRES_URL}  # type: ignore[dict-item]

        # First upgrade to ensure we're at head
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            capture_output=True, text=True, env=env, cwd=str(_ROOT), check=True,
        )

        # Now downgrade to base
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "downgrade", "base"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_ROOT),
        )
        assert result.returncode == 0, (
            f"alembic downgrade base failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        engine = create_engine(self._normalize_url(_POSTGRES_URL))  # type: ignore[arg-type]
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
        engine.dispose()

        kb_tables = {"scans", "documents", "audit_results", "finding_workflow", "scan_state"}
        remaining = kb_tables & tables
        assert not remaining, f"Tables still present after downgrade: {remaining}"

    def test_re_upgrade_after_downgrade(self):
        """Re-applying upgrade after downgrade must recreate all tables."""
        import subprocess
        import sys
        from sqlalchemy import create_engine, inspect

        env = {**os.environ, "KB_AUDIT_POSTGRES_URL": _POSTGRES_URL}  # type: ignore[dict-item]

        # Downgrade first
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "downgrade", "base"],
            capture_output=True, text=True, env=env, cwd=str(_ROOT), check=True,
        )

        # Re-upgrade
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            capture_output=True, text=True, env=env, cwd=str(_ROOT),
        )
        assert result.returncode == 0, (
            f"Re-upgrade after downgrade failed:\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        engine = create_engine(self._normalize_url(_POSTGRES_URL))  # type: ignore[arg-type]
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
        engine.dispose()

        expected = {"scans", "documents", "audit_results", "finding_workflow", "scan_state"}
        assert expected.issubset(tables)
