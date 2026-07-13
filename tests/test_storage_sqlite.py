"""Tests for the SqliteStorage backend and Database compatibility facade."""

from __future__ import annotations

from kb_audit.db import (
    Database,
    LeaseLostError,
    ScanLeaseContext,
    _UNSET,
    _sanitize_error,
)
from kb_audit.storage.contracts import AuditStorage
from kb_audit.storage.sqlite import SqliteStorage


class TestSqliteStorageBasics:
    def test_connect_and_close(self):
        db = SqliteStorage(":memory:")
        db.connect()
        assert db.conn is not None
        db.close()

    def test_close_is_idempotent(self):
        db = SqliteStorage(":memory:")
        db.connect()
        db.close()
        db.close()  # should not raise

    def test_conn_raises_before_connect(self):
        db = SqliteStorage(":memory:")
        try:
            _ = db.conn
            assert False, "Expected RuntimeError"
        except RuntimeError:
            pass

    def test_creates_expected_schema(self):
        db = SqliteStorage(":memory:")
        db.connect()
        tables = {
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"scans", "documents", "audit_results", "finding_workflow", "scan_state"} <= tables
        db.close()

    def test_url_prefix_normalization(self):
        for prefix in ("sqlite:///", "sqlite://", "jdbc:sqlite:./", "jdbc:sqlite:"):
            db = SqliteStorage(f"{prefix}:memory:")
            # After normalization the path should be ":memory:" (stripped prefix)
            assert db._path == ":memory:", f"prefix {prefix!r} not stripped"


class TestDatabaseFacade:
    def test_database_is_subclass_of_sqlite_storage(self):
        assert issubclass(Database, SqliteStorage)

    def test_database_instance_is_sqlite_storage(self):
        db = Database(":memory:")
        assert isinstance(db, SqliteStorage)

    def test_database_instance_is_audit_storage(self):
        db = Database(":memory:")
        assert isinstance(db, AuditStorage)

    def test_database_connect_and_close(self):
        db = Database(":memory:")
        db.connect()
        db.close()

    def test_database_creates_schema(self):
        db = Database(":memory:")
        db.connect()
        tables = {
            r[0]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "scans" in tables
        assert "finding_workflow" in tables
        db.close()

    def test_database_scan_state_row_exists(self):
        db = Database(":memory:")
        db.connect()
        row = db.conn.execute("SELECT id, in_progress FROM scan_state WHERE id = 1").fetchone()
        assert row == (1, 0)
        db.close()


class TestCompatibilityImports:
    """Verify that all names that were previously importable from kb_audit.db still work."""

    def test_database_importable(self):
        from kb_audit.db import Database as D  # noqa: F401
        assert D is Database

    def test_lease_lost_error_importable(self):
        from kb_audit.db import LeaseLostError as E  # noqa: F401
        assert E is LeaseLostError

    def test_scan_lease_context_importable(self):
        from kb_audit.db import ScanLeaseContext as C  # noqa: F401
        assert C is ScanLeaseContext

    def test_unset_importable(self):
        from kb_audit.db import _UNSET as U  # noqa: F401
        assert U is _UNSET

    def test_sanitize_error_importable(self):
        from kb_audit.db import _sanitize_error as fn  # noqa: F401
        assert fn is _sanitize_error

    def test_unset_is_singleton(self):
        from kb_audit.db import _UNSET as U1
        from kb_audit.storage.sqlite import _UNSET as U2
        assert U1 is U2

    def test_lease_lost_error_is_same_class(self):
        from kb_audit.db import LeaseLostError as E1
        from kb_audit.storage.sqlite import LeaseLostError as E2
        assert E1 is E2


class TestSqliteStorageAuditStorageCompat:
    def test_sqlite_storage_isinstance_audit_storage(self):
        db = SqliteStorage(":memory:")
        assert isinstance(db, AuditStorage)

    def test_assignable_as_audit_storage(self):
        store: AuditStorage = SqliteStorage(":memory:")  # type: ignore[assignment]
        store.connect()
        store.close()
