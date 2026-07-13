"""Tests for the storage factory."""

from __future__ import annotations

import pytest

from kb_audit.storage import AuditStorage, create_storage
from kb_audit.storage.postgres import PostgresStorage
from kb_audit.storage.sqlite import SqliteStorage


class TestCreateStorage:
    def test_memory_returns_sqlite_storage(self):
        store = create_storage(":memory:")
        assert isinstance(store, SqliteStorage)

    def test_sqlite_url_memory(self):
        store = create_storage("sqlite:///:memory:")
        assert isinstance(store, SqliteStorage)

    def test_sqlite_two_slash_url(self):
        store = create_storage("sqlite://:memory:")
        assert isinstance(store, SqliteStorage)

    def test_path_object(self, tmp_path):
        store = create_storage(tmp_path / "audit.db")
        assert isinstance(store, SqliteStorage)

    def test_bare_filename(self):
        store = create_storage("kbaudit-demo.db")
        assert isinstance(store, SqliteStorage)

    def test_bare_path_string(self, tmp_path):
        store = create_storage(str(tmp_path / "audit.db"))
        assert isinstance(store, SqliteStorage)

    def test_returned_object_satisfies_audit_storage(self):
        store = create_storage(":memory:")
        assert isinstance(store, AuditStorage)

    def test_does_not_connect(self):
        # create_storage must not call .connect(); conn access should raise.
        store = create_storage(":memory:")
        with pytest.raises(RuntimeError):
            _ = store.conn  # type: ignore[attr-defined]

    def test_postgresql_returns_postgres_storage(self):
        store = create_storage("postgresql://localhost/kbaudit")
        assert isinstance(store, PostgresStorage)

    def test_postgres_returns_postgres_storage(self):
        store = create_storage("postgres://localhost/kbaudit")
        assert isinstance(store, PostgresStorage)

    def test_postgres_does_not_connect(self):
        # create_storage must not call .connect() for PostgresStorage either.
        store = create_storage("postgresql://localhost/kbaudit")
        assert not store.is_connected

    def test_mysql_raises(self):
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            create_storage("mysql://localhost/kbaudit")

    def test_mongodb_raises(self):
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            create_storage("mongodb://localhost/kbaudit")

    def test_unknown_scheme_raises(self):
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            create_storage("redis://localhost/0")

class TestAppConstructionPaths:
    """Smoke-test that CLI and web construction paths no longer reference Database."""

    def test_cli_does_not_import_database_directly(self):
        import ast
        import pathlib

        src = pathlib.Path("src/kb_audit/cli.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module == "kb_audit.db":
                    names = [alias.name for alias in node.names]
                    assert "Database" not in names, (
                        "cli.py should not import Database from kb_audit.db; "
                        "use create_storage instead"
                    )

    def test_web_does_not_import_database_directly(self):
        import ast
        import pathlib

        src = pathlib.Path("src/kb_audit/web/app.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module == "kb_audit.db":
                    names = [alias.name for alias in node.names]
                    assert "Database" not in names, (
                        "web/app.py should not import Database from kb_audit.db; "
                        "use create_storage instead"
                    )
