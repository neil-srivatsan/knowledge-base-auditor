"""Tests for PostgresStorage SQL behavior and storage-contract methods.

No psycopg installation and no running PostgreSQL server are required.
All database interaction is intercepted with fakes/mocks.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_psycopg(fake_conn: MagicMock) -> types.ModuleType:
    """Return a minimal fake psycopg module whose connect() returns fake_conn."""
    mod = types.ModuleType("psycopg")
    mod.connect = MagicMock(return_value=fake_conn)  # type: ignore[attr-defined]
    return mod


def _make_fake_conn() -> MagicMock:
    """Return a fake psycopg connection with a context-manager cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    # Support `with conn.cursor() as cur:` pattern
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------

class TestModuleImportSafety:
    def test_imports_without_psycopg(self):
        """Module must import cleanly even when psycopg is not installed."""
        import importlib

        mod_name = "kb_audit.storage.postgres"
        sys.modules.pop(mod_name, None)

        original = sys.modules.get("psycopg")
        sys.modules["psycopg"] = None  # type: ignore[assignment]
        try:
            mod = importlib.import_module(mod_name)
            assert mod is not None
        finally:
            if original is None:
                sys.modules.pop("psycopg", None)
            else:
                sys.modules["psycopg"] = original

    def test_no_top_level_psycopg_import(self):
        """Confirm no top-level `import psycopg` in the module source."""
        import ast
        import pathlib

        src = pathlib.Path("src/kb_audit/storage/postgres.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # Only flag top-level imports (not nested inside functions/methods)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and isinstance(
                getattr(node, "col_offset", None), int
            ):
                if node.col_offset == 0:
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            assert alias.name != "psycopg", (
                                "psycopg must not be imported at module level"
                            )
                    elif isinstance(node, ast.ImportFrom):
                        assert node.module != "psycopg", (
                            "psycopg must not be imported at module level"
                        )


# ---------------------------------------------------------------------------
# Construction (no connection)
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_stores_url(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        assert store._database_url == "postgresql://localhost/kbaudit"

    def test_accepts_path_like(self):
        import pathlib

        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage(pathlib.PurePosixPath("postgresql://host/db"))
        assert isinstance(store._database_url, str)

    def test_not_connected_after_init(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        assert not store.is_connected

    def test_conn_raises_before_connect(self):
        import pytest

        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        with pytest.raises(RuntimeError, match="not connected"):
            _ = store.conn


# ---------------------------------------------------------------------------
# close() before connect
# ---------------------------------------------------------------------------

class TestCloseBeforeConnect:
    def test_close_before_connect_is_safe(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        store.close()  # must not raise
        assert not store.is_connected

    def test_close_is_idempotent(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        store.close()
        store.close()  # second call must also not raise


# ---------------------------------------------------------------------------
# connect() behaviour (mocked psycopg)
# ---------------------------------------------------------------------------

class TestConnect:
    def _patched_store(self, fake_conn: MagicMock):
        """Return a PostgresStorage with psycopg and require_psycopg patched."""
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)
        return store, fake_psycopg

    def test_connect_calls_require_psycopg(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg") as mock_require, \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        mock_require.assert_called_once()

    def test_connect_calls_psycopg_connect_with_url(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        fake_psycopg.connect.assert_called_once_with("postgresql://localhost/kbaudit")

    def test_connect_executes_all_schema_statements(self):
        fake_conn = _make_fake_conn()
        fake_cursor = fake_conn.cursor.return_value.__enter__.return_value
        from kb_audit.storage.postgres import PostgresStorage
        from kb_audit.storage.schema_postgres import iter_postgres_schema_statements

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        expected_stmts = iter_postgres_schema_statements()

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        executed = [c.args[0] for c in fake_cursor.execute.call_args_list]
        assert list(executed) == list(expected_stmts)

    def test_connect_executes_statements_in_order(self):
        fake_conn = _make_fake_conn()
        fake_cursor = fake_conn.cursor.return_value.__enter__.return_value
        from kb_audit.storage.postgres import PostgresStorage
        from kb_audit.storage.schema_postgres import iter_postgres_schema_statements

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        expected = list(iter_postgres_schema_statements())

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        executed = [c.args[0] for c in fake_cursor.execute.call_args_list]
        assert executed == expected

    def test_connect_commits_after_schema(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        fake_conn.commit.assert_called_once()

    def test_connect_sets_is_connected(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        assert store.is_connected

    def test_conn_accessible_after_connect(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        assert store.conn is fake_conn

    def test_connect_uses_iter_postgres_schema_statements(self):
        """connect() must call iter_postgres_schema_statements, not hardcode SQL."""
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)
        sentinel = ("-- sentinel statement",)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch("kb_audit.storage.postgres.iter_postgres_schema_statements",
                   return_value=sentinel) as mock_iter, \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        mock_iter.assert_called_once()


# ---------------------------------------------------------------------------
# connect() error handling
# ---------------------------------------------------------------------------

class TestConnectErrorHandling:
    def test_schema_error_closes_connection_and_reraises(self):
        import pytest

        fake_conn = _make_fake_conn()
        fake_cursor = fake_conn.cursor.return_value.__enter__.return_value
        fake_cursor.execute.side_effect = RuntimeError("schema boom")

        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            with pytest.raises(RuntimeError, match="schema boom"):
                store.connect()

        fake_conn.close.assert_called_once()
        assert not store.is_connected

    def test_schema_error_does_not_commit(self):
        fake_conn = _make_fake_conn()
        fake_cursor = fake_conn.cursor.return_value.__enter__.return_value
        fake_cursor.execute.side_effect = RuntimeError("schema boom")

        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            try:
                store.connect()
            except RuntimeError:
                pass

        fake_conn.commit.assert_not_called()

    def test_require_psycopg_error_propagates(self):
        import pytest

        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")

        with patch("kb_audit.storage.postgres.require_psycopg",
                   side_effect=RuntimeError("driver missing")):
            with pytest.raises(RuntimeError, match="driver missing"):
                store.connect()

        assert not store.is_connected


# ---------------------------------------------------------------------------
# close() after connect
# ---------------------------------------------------------------------------

class TestCloseAfterConnect:
    def test_close_calls_conn_close(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        store.close()
        fake_conn.close.assert_called_once()

    def test_close_clears_internal_connection(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        store.close()
        assert not store.is_connected

    def test_close_is_idempotent_after_connect(self):
        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        store.close()
        store.close()  # second call must not raise
        # conn.close() called exactly once — second close() is a no-op
        fake_conn.close.assert_called_once()

    def test_conn_raises_after_close(self):
        import pytest

        fake_conn = _make_fake_conn()
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        fake_psycopg = _make_fake_psycopg(fake_conn)

        with patch("kb_audit.storage.postgres.require_psycopg"), \
             patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            store.connect()

        store.close()
        with pytest.raises(RuntimeError, match="not connected"):
            _ = store.conn


# ---------------------------------------------------------------------------
# No behavioral methods implemented
# ---------------------------------------------------------------------------

class TestNotYetImplementedMethods:
    """After Step 10, all storage-contract methods are implemented and Postgres
    URLs are factory-wired.  These tests mock SQL behavior and do not require
    a live Postgres server.
    """

    def test_history_diff_maintenance_methods_now_present(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        for method in (
            "get_scan_history", "get_scan_diff",
            "clear_all", "clear_all_if_idle", "prune_scans",
        ):
            assert hasattr(store, method), f"Expected {method!r} to exist after Step 7"

    def test_scan_lease_methods_now_present(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        for method in (
            "try_start_scan", "renew_lease", "owns_live_lease",
            "end_scan", "reset_scan_state", "get_scan_state",
        ):
            assert hasattr(store, method), f"Expected {method!r} to exist"

    def test_scan_lifecycle_methods_now_present(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        for method in ("start_scan", "finish_scan", "fail_scan"):
            assert hasattr(store, method), f"Expected {method!r} to exist"

    def test_document_result_methods_now_present(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        for method in (
            "store_document", "store_result", "get_previous_hashes",
            "carry_forward_results", "load_audit_results", "get_scan_results",
        ):
            assert hasattr(store, method), f"Expected {method!r} to exist"

    def test_workflow_methods_now_present(self):
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        for method in (
            "complete_scan_with_findings", "sync_findings", "update_workflow",
            "get_findings", "get_finding", "get_workflow_summary",
        ):
            assert hasattr(store, method), f"Expected {method!r} to exist"

# ---------------------------------------------------------------------------
# Helpers for scan lease / lifecycle tests
# ---------------------------------------------------------------------------

def _make_cursor(
    fetchone_values: list | None = None,
    fetchall_value: list | None = None,
    rowcount: int = 1,
) -> MagicMock:
    """Build a fake psycopg cursor with configurable fetchone/fetchall/rowcount."""
    cursor = MagicMock()
    if fetchone_values is not None:
        cursor.fetchone.side_effect = list(fetchone_values)
    if fetchall_value is not None:
        cursor.fetchall.return_value = fetchall_value
    cursor.rowcount = rowcount
    return cursor


def _store_with_conn(fake_conn: MagicMock):
    """Return a PostgresStorage whose _conn is pre-set (skips connect())."""
    from kb_audit.storage.postgres import PostgresStorage

    store = PostgresStorage("postgresql://localhost/kbaudit")
    store._conn = fake_conn  # type: ignore[assignment]
    return store


def _conn_with_cursor(cursor: MagicMock) -> MagicMock:
    """Return a fake connection that yields *cursor* from its cursor() context manager."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


def _executed_sqls(cursor: MagicMock) -> list[str]:
    """Return all SQL strings passed to cursor.execute() calls."""
    return [c.args[0] for c in cursor.execute.call_args_list]


def _executed_params(cursor: MagicMock) -> list:
    """Return all parameter tuples passed to cursor.execute() calls."""
    return [c.args[1] if len(c.args) > 1 else () for c in cursor.execute.call_args_list]


# ---------------------------------------------------------------------------
# Scan guard
# ---------------------------------------------------------------------------

class TestScanMethodsRequireConnect:
    METHODS = [
        "try_start_scan", "renew_lease", "owns_live_lease",
        "end_scan", "get_scan_state", "reset_scan_state",
        "start_scan", "finish_scan", "fail_scan",
        "store_document", "store_result",
        "get_previous_hashes", "carry_forward_results",
        "load_audit_results", "get_scan_results",
        "complete_scan_with_findings", "sync_findings",
        "get_findings", "get_finding", "get_workflow_summary",
        "get_scan_history", "get_scan_diff",
        "clear_all", "clear_all_if_idle", "prune_scans",
    ]

    def test_methods_raise_before_connect(self):
        import pytest
        from kb_audit.models import AuditResult, Document
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        _doc = Document(id="d", title="T", content="c", source_type="test")
        _result = AuditResult(document=_doc)
        method_args = {
            "try_start_scan": [],
            "renew_lease": ["dummy-token"],
            "owns_live_lease": ["dummy-token"],
            "end_scan": ["dummy-token", None, None],
            "get_scan_state": [],
            "reset_scan_state": [],
            "start_scan": [],
            "finish_scan": [1, 0],
            "fail_scan": [1, None],
            "store_document": [1, _doc],
            "store_result": [1, _result],
            "get_previous_hashes": [],
            "carry_forward_results": [1, ["d1"]],
            "load_audit_results": [1, ["d1"]],
            "get_scan_results": [1],
            "complete_scan_with_findings": [1, 0, []],
            "sync_findings": [1, []],
            "get_findings": [],
            "get_finding": ["key"],
            "get_workflow_summary": [],
            "get_scan_history": [],
            "get_scan_diff": [1, 0],
            "clear_all": [],
            "clear_all_if_idle": [],
            "prune_scans": [],
        }
        for method in self.METHODS:
            args = method_args[method]
            with pytest.raises(RuntimeError, match="not connected"):
                getattr(store, method)(*args)

    def test_update_workflow_requires_connect_with_state(self):
        import pytest
        from kb_audit.storage.postgres import PostgresStorage

        store = PostgresStorage("postgresql://localhost/kbaudit")
        with pytest.raises(RuntimeError, match="not connected"):
            store.update_workflow("key", state="open")


# ---------------------------------------------------------------------------
# Scan lease
# ---------------------------------------------------------------------------

class TestTryStartScan:
    def test_uses_for_update_on_scan_state(self):
        # The first SQL must lock the singleton row
        cursor = _make_cursor(
            fetchone_values=[(False, None, None), None],  # idle, no unleased scans
            fetchall_value=[],
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.try_start_scan(now="2024-01-01T00:00:00+00:00")
        first_sql = _executed_sqls(cursor)[0]
        assert "FOR UPDATE" in first_sql
        assert "scan_state" in first_sql

    def test_returns_none_when_live_lease_exists(self):
        from datetime import datetime, timezone

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchone_values=[(True, future, "other_token")])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)

        result = store.try_start_scan(now="2024-01-01T00:00:00+00:00")

        assert result is None
        conn.rollback.assert_called()

    def test_returns_token_when_idle(self):
        cursor = _make_cursor(
            fetchone_values=[(False, None, None), None],
            fetchall_value=[],
        )
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)

        result = store.try_start_scan(now="2024-01-01T00:00:00+00:00")

        assert isinstance(result, str)
        assert len(result) == 36  # UUID format
        conn.commit.assert_called()

    def test_updates_scan_state_on_success(self):
        cursor = _make_cursor(
            fetchone_values=[(False, None, None), None],
            fetchall_value=[],
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.try_start_scan(now="2024-01-01T00:00:00+00:00")

        sqls = _executed_sqls(cursor)
        update_sqls = [s for s in sqls if "UPDATE scan_state" in s]
        assert update_sqls, "Expected an UPDATE scan_state statement"
        assert "in_progress = TRUE" in update_sqls[0] or "in_progress=TRUE" in update_sqls[0] or \
               "TRUE" in update_sqls[0]

    def test_abandons_expired_owner_running_scans(self):
        from datetime import datetime, timezone

        past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(
            fetchone_values=[
                (True, past, "expired_token"),  # scan_state: expired lease
                None,  # no unleased running scans
            ],
            fetchall_value=[(42,)],  # one running scan for expired owner
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.try_start_scan(now="2024-01-01T00:00:00+00:00")

        sqls = _executed_sqls(cursor)
        assert any("abandoned: lease expired" in s for s in sqls), (
            "Expected abandoned scan to be marked failed"
        )
        assert any("DELETE FROM documents" in s for s in sqls)
        assert any("DELETE FROM audit_results" in s for s in sqls)

    def test_returns_none_when_unleased_scan_running(self):
        cursor = _make_cursor(
            fetchone_values=[
                (False, None, None),  # scan_state: idle
                (1,),                 # unleased running scan found
            ],
        )
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)

        result = store.try_start_scan(now="2024-01-01T00:00:00+00:00")

        assert result is None
        conn.rollback.assert_called()


class TestRenewLease:
    def test_returns_true_when_renewed(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.renew_lease("some-token", now="2024-01-01T00:00:00+00:00")
        assert result is True

    def test_returns_false_when_token_mismatch_or_expired(self):
        cursor = _make_cursor(rowcount=0)
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.renew_lease("wrong-token", now="2024-01-01T00:00:00+00:00")
        assert result is False

    def test_commits_after_update(self):
        cursor = _make_cursor(rowcount=1)
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.renew_lease("some-token", now="2024-01-01T00:00:00+00:00")
        conn.commit.assert_called()

    def test_sql_updates_lease_expires_at(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.renew_lease("some-token", now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert any("lease_expires_at" in s and "UPDATE scan_state" in s for s in sqls)


class TestOwnsLiveLease:
    def test_returns_true_when_row_found(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        assert store.owns_live_lease("my-token", now="2024-01-01T00:00:00+00:00") is True

    def test_returns_false_when_no_row(self):
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        assert store.owns_live_lease("other-token", now="2024-01-01T00:00:00+00:00") is False

    def test_sql_checks_owner_token_and_expiry(self):
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.owns_live_lease("my-token", now="2024-01-01T00:00:00+00:00")
        sql = _executed_sqls(cursor)[0]
        assert "owner_token" in sql
        assert "lease_expires_at" in sql


class TestEndScan:
    def test_returns_true_when_lease_released(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.end_scan("my-token", last_scan_id=7, error=None,
                                now="2024-01-01T00:00:00+00:00")
        assert result is True

    def test_returns_false_when_token_mismatch(self):
        cursor = _make_cursor(rowcount=0)
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.end_scan("wrong-token", last_scan_id=None, error=None,
                                now="2024-01-01T00:00:00+00:00")
        assert result is False

    def test_sanitizes_error_before_storing(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        raw_error = "Failed: Bearer token=s3cr3t"
        store.end_scan("my-token", last_scan_id=1, error=raw_error,
                       now="2024-01-01T00:00:00+00:00")
        params = _executed_params(cursor)[0]
        # The stored error (second param) must not contain the raw secret
        stored_error = params[1]
        assert "s3cr3t" not in str(stored_error or "")

    def test_sets_in_progress_false_and_clears_token(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.end_scan("my-token", last_scan_id=5, error=None,
                       now="2024-01-01T00:00:00+00:00")
        sql = _executed_sqls(cursor)[0]
        assert "in_progress = FALSE" in sql or "in_progress=FALSE" in sql or "FALSE" in sql
        assert "owner_token = NULL" in sql or "owner_token=NULL" in sql


class TestGetScanState:
    def test_returns_not_in_progress_when_no_row(self):
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        state = store.get_scan_state(now="2024-01-01T00:00:00+00:00")
        assert state == {"in_progress": False, "last_scan_id": None, "scan_error": None}

    def test_returns_in_progress_when_live_lease(self):
        from datetime import datetime, timezone

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchone_values=[(True, 7, None, future)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        state = store.get_scan_state(now="2024-01-01T00:00:00+00:00")
        assert state["in_progress"] is True

    def test_treats_expired_lease_as_idle(self):
        from datetime import datetime, timezone

        past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchone_values=[(True, 7, None, past)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        state = store.get_scan_state(now="2024-01-01T00:00:00+00:00")
        assert state["in_progress"] is False
        assert state["last_scan_id"] == 7

    def test_returns_scan_error_from_row(self):
        cursor = _make_cursor(fetchone_values=[(False, 3, "some error", None)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        state = store.get_scan_state(now="2024-01-01T00:00:00+00:00")
        assert state["scan_error"] == "some error"


class TestResetScanState:
    def test_clears_all_lease_fields(self):
        cursor = _make_cursor()
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.reset_scan_state()

        sql = _executed_sqls(cursor)[0]
        assert "UPDATE scan_state" in sql
        assert "in_progress" in sql
        assert "owner_token" in sql
        assert "lease_expires_at" in sql
        conn.commit.assert_called()

    def test_targets_singleton_row(self):
        cursor = _make_cursor()
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.reset_scan_state()
        sql = _executed_sqls(cursor)[0]
        assert "id = 1" in sql


# ---------------------------------------------------------------------------
# Scan lifecycle
# ---------------------------------------------------------------------------

class TestStartScan:
    def test_returns_scan_id_from_returning(self):
        cursor = _make_cursor(fetchone_values=[None, (42,)])
        # fetchone[0] → lease check returns a row (lease valid)
        # fetchone[1] → RETURNING id returns 42
        cursor.fetchone.side_effect = [(1,), (42,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        scan_id = store.start_scan(owner_token="tok", now="2024-01-01T00:00:00+00:00")
        assert scan_id == 42

    def test_inserts_running_status(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (7,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.start_scan(owner_token="tok", now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        insert_sqls = [s for s in sqls if "INSERT INTO scans" in s]
        assert insert_sqls
        assert "'running'" in insert_sqls[0] or "running" in insert_sqls[0]

    def test_uses_returning_id_clause(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (7,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.start_scan(owner_token="tok", now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        insert_sqls = [s for s in sqls if "INSERT INTO scans" in s]
        assert any("RETURNING id" in s for s in insert_sqls)

    def test_unleased_path_inserts_null_owner(self):
        # Unleased: scan_state check returns no row (no live lease), then RETURNING id.
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [None, (55,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        scan_id = store.start_scan(owner_token=None, now="2024-01-01T00:00:00+00:00")
        assert scan_id == 55
        sqls = _executed_sqls(cursor)
        insert_sqls = [s for s in sqls if "INSERT INTO scans" in s]
        assert insert_sqls

    def test_leased_path_checks_lease_with_for_update(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (7,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.start_scan(owner_token="tok", now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        lease_check_sqls = [s for s in sqls if "scan_state" in s and "FOR UPDATE" in s]
        assert lease_check_sqls, "Expected a FOR UPDATE lock on scan_state"

    def test_raises_lease_lost_when_lease_check_fails(self):
        import pytest
        from kb_audit.storage.sqlite import LeaseLostError

        # Lease check returns no row → lease lost
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        with pytest.raises(LeaseLostError):
            store.start_scan(owner_token="tok", now="2024-01-01T00:00:00+00:00")


class TestFinishScan:
    def test_transitions_to_completed(self):
        cursor = _make_cursor()
        # _check_lease returns a row, _assert_scan_running returns a row
        cursor.fetchone.side_effect = [(1,), (1,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.finish_scan(scan_id=3, document_count=10, owner_token="tok",
                          now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        update_sqls = [s for s in sqls if "UPDATE scans" in s]
        assert any("completed" in s for s in update_sqls)

    def test_stores_document_count(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (1,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.finish_scan(scan_id=3, document_count=99, owner_token="tok",
                          now="2024-01-01T00:00:00+00:00")
        params = _executed_params(cursor)
        update_params = [p for p in params if 99 in p]
        assert update_params, "Expected document_count=99 in UPDATE params"

    def test_commits_on_success(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (1,)]
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.finish_scan(scan_id=3, document_count=0, owner_token="tok",
                          now="2024-01-01T00:00:00+00:00")
        conn.commit.assert_called()

    def test_raises_lease_lost_on_owner_mismatch(self):
        import pytest
        from kb_audit.storage.sqlite import LeaseLostError

        cursor = _make_cursor(fetchone_values=[None])  # lease check fails
        store = _store_with_conn(_conn_with_cursor(cursor))
        with pytest.raises(LeaseLostError):
            store.finish_scan(scan_id=3, document_count=0, owner_token="bad-tok",
                              now="2024-01-01T00:00:00+00:00")

    def test_raises_lease_lost_when_scan_not_running(self):
        import pytest
        from kb_audit.storage.sqlite import LeaseLostError

        # Lease OK, but scan check fails
        cursor = _make_cursor(fetchone_values=[(1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        with pytest.raises(LeaseLostError):
            store.finish_scan(scan_id=99, document_count=0, owner_token="tok",
                              now="2024-01-01T00:00:00+00:00")


class TestFailScan:
    def test_transitions_to_failed(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (1,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.fail_scan(scan_id=5, error=None, owner_token="tok",
                        now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        update_sqls = [s for s in sqls if "UPDATE scans" in s]
        assert any("failed" in s for s in update_sqls)

    def test_deletes_partial_documents_and_results(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (1,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.fail_scan(scan_id=5, error=None, owner_token="tok",
                        now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert any("DELETE FROM documents" in s for s in sqls)
        assert any("DELETE FROM audit_results" in s for s in sqls)

    def test_sanitizes_error(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (1,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.fail_scan(scan_id=5, error="token=s3cr3t boom",
                        owner_token="tok", now="2024-01-01T00:00:00+00:00")
        params = _executed_params(cursor)
        # Find the UPDATE scans params — error is the first param
        sqls = _executed_sqls(cursor)
        for sql, param in zip(sqls, params):
            if "UPDATE scans" in sql:
                assert "s3cr3t" not in str(param[0] or "")

    def test_returns_true_on_success(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (1,)]
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.fail_scan(scan_id=5, error=None, owner_token="tok",
                                 now="2024-01-01T00:00:00+00:00")
        assert result is True

    def test_raises_lease_lost_on_owner_mismatch(self):
        import pytest
        from kb_audit.storage.sqlite import LeaseLostError

        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        with pytest.raises(LeaseLostError):
            store.fail_scan(scan_id=5, error=None, owner_token="bad-tok",
                            now="2024-01-01T00:00:00+00:00")

    def test_commits_on_success(self):
        cursor = _make_cursor()
        cursor.fetchone.side_effect = [(1,), (1,)]
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.fail_scan(scan_id=5, error=None, owner_token="tok",
                        now="2024-01-01T00:00:00+00:00")
        conn.commit.assert_called()


# ---------------------------------------------------------------------------
# Document persistence
# ---------------------------------------------------------------------------

class TestStoreDocument:
    def _doc(self):
        from kb_audit.models import Document
        return Document(id="d1", title="Title", content="body", source_type="confluence")

    def test_upserts_into_documents(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.store_document(1, self._doc())
        sqls = _executed_sqls(cursor)
        assert any("INSERT INTO documents" in s and "ON CONFLICT" in s for s in sqls)

    def test_metadata_uses_jsonb_cast(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.store_document(1, self._doc())
        sqls = _executed_sqls(cursor)
        assert any("::jsonb" in s for s in sqls)

    def test_verifies_lease_when_owner_token_supplied(self):
        # _check_lease (fetchone → (1,)) then _assert_scan_running (fetchone → (1,))
        cursor = _make_cursor(fetchone_values=[(1,), (1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.store_document(1, self._doc(), owner_token="tok",
                             now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert any("FOR UPDATE" in s and "scan_state" in s for s in sqls)

    def test_commits_on_success(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.store_document(1, self._doc())
        conn.commit.assert_called()

    def test_rollback_on_failure(self):
        import pytest

        cursor = _make_cursor(fetchone_values=[(1,)])
        cursor.execute.side_effect = [None, RuntimeError("db error")]
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="db error"):
            store.store_document(1, self._doc())
        conn.rollback.assert_called()

    def test_doc_id_and_scan_id_in_params(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.store_document(42, self._doc())
        params = _executed_params(cursor)
        insert_params = [p for p in params if 42 in p]
        assert any("d1" in str(p) for p in insert_params)


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------

class TestStoreResult:
    def _result(self):
        from kb_audit.models import AuditResult, Document
        doc = Document(id="d1", title="Title", content="body", source_type="confluence")
        return AuditResult(document=doc, status="stale", confidence=0.9)

    def test_upserts_into_audit_results(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.store_result(1, self._result())
        sqls = _executed_sqls(cursor)
        assert any("INSERT INTO audit_results" in s and "ON CONFLICT" in s for s in sqls)

    def test_signals_and_trust_data_use_jsonb_cast(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.store_result(1, self._result())
        sqls = _executed_sqls(cursor)
        insert_sql = next(s for s in sqls if "INSERT INTO audit_results" in s)
        assert insert_sql.count("::jsonb") >= 2

    def test_verifies_lease_when_owner_token_supplied(self):
        cursor = _make_cursor(fetchone_values=[(1,), (1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.store_result(1, self._result(), owner_token="tok",
                           now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert any("FOR UPDATE" in s and "scan_state" in s for s in sqls)

    def test_commits_on_success(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.store_result(1, self._result())
        conn.commit.assert_called()

    def test_rollback_on_failure(self):
        import pytest

        cursor = _make_cursor(fetchone_values=[(1,)])
        cursor.execute.side_effect = [None, RuntimeError("db error")]
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="db error"):
            store.store_result(1, self._result())
        conn.rollback.assert_called()

    def test_jsonb_not_imported_at_module_level(self):
        import sys
        # psycopg must not be imported when kb_audit.storage.postgres is imported
        if "psycopg" in sys.modules:
            return  # psycopg already present from another test — skip
        import kb_audit.storage.postgres  # noqa: F401
        assert "psycopg" not in sys.modules


# ---------------------------------------------------------------------------
# Previous hashes
# ---------------------------------------------------------------------------

class TestGetPreviousHashes:
    def test_returns_empty_when_no_completed_scan(self):
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_previous_hashes()
        assert result == {}

    def test_returns_doc_id_to_hash_mapping(self):
        cursor = _make_cursor(
            fetchone_values=[(42,)],
            fetchall_value=[("d1", "hash1"), ("d2", "hash2")],
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_previous_hashes()
        assert result == {"d1": "hash1", "d2": "hash2"}

    def test_queries_latest_completed_scan_descending(self):
        cursor = _make_cursor(fetchone_values=[(42,)], fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_previous_hashes()
        sqls = _executed_sqls(cursor)
        assert any("completed" in s and "ORDER BY id DESC" in s for s in sqls)

    def test_uses_prev_scan_id_for_document_query(self):
        cursor = _make_cursor(fetchone_values=[(42,)], fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_previous_hashes()
        params = _executed_params(cursor)
        assert any(42 in p for p in params if p)


# ---------------------------------------------------------------------------
# Carry-forward results
# ---------------------------------------------------------------------------

class TestCarryForwardResults:
    def test_returns_zero_when_no_doc_ids(self):
        from unittest.mock import MagicMock
        store = _store_with_conn(MagicMock())
        result = store.carry_forward_results(1, [])
        assert result == 0

    def test_returns_zero_when_no_previous_scan(self):
        # _assert_scan_running OK, then no completed scan
        cursor = _make_cursor(fetchone_values=[(1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.carry_forward_results(1, ["d1"])
        assert result == 0

    def test_copies_results_and_returns_rowcount(self):
        cursor = _make_cursor(fetchone_values=[(1,), (7,)], rowcount=2)
        store = _store_with_conn(_conn_with_cursor(cursor))
        count = store.carry_forward_results(1, ["d1", "d2"])
        assert count == 2

    def test_uses_insert_select_with_on_conflict(self):
        cursor = _make_cursor(fetchone_values=[(1,), (7,)], rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.carry_forward_results(1, ["d1"])
        sqls = _executed_sqls(cursor)
        assert any(
            "INSERT INTO audit_results" in s and "SELECT" in s and "ON CONFLICT" in s
            for s in sqls
        )

    def test_uses_any_array_parameter_for_doc_ids(self):
        cursor = _make_cursor(fetchone_values=[(1,), (7,)], rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.carry_forward_results(1, ["d1"])
        sqls = _executed_sqls(cursor)
        assert any("= ANY(" in s for s in sqls)

    def test_verifies_lease_when_owner_token_supplied(self):
        # _check_lease, _assert_scan_running, then prev scan
        cursor = _make_cursor(fetchone_values=[(1,), (1,), (7,)], rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.carry_forward_results(1, ["d1"], owner_token="tok",
                                    now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert any("FOR UPDATE" in s and "scan_state" in s for s in sqls)

    def test_commits_on_success(self):
        cursor = _make_cursor(fetchone_values=[(1,), (7,)], rowcount=1)
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.carry_forward_results(1, ["d1"])
        conn.commit.assert_called()

    def test_rollback_on_failure(self):
        import pytest

        cursor = _make_cursor(fetchone_values=[(1,), (7,)])
        cursor.execute.side_effect = [None, None, RuntimeError("carry error")]
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="carry error"):
            store.carry_forward_results(1, ["d1"])
        conn.rollback.assert_called()


# ---------------------------------------------------------------------------
# Load audit results
# ---------------------------------------------------------------------------

class TestLoadAuditResults:
    def test_returns_empty_when_no_doc_ids(self):
        from unittest.mock import MagicMock
        store = _store_with_conn(MagicMock())
        result = store.load_audit_results(1, [])
        assert result == []

    def test_reconstructs_audit_result_objects(self):
        import json
        from kb_audit.models import AuditResult

        signals_json = json.dumps([{
            "signal_type": "outdated", "severity": "warning",
            "message": "old content", "details": {},
        }])
        trust_json = json.dumps({"metadata": {"flag": True}, "evidence": {"src": "x"}})

        cursor = _make_cursor(fetchall_value=[
            ("d1", "Doc Title", "confluence", "http://x.com",
             "stale", signals_json, 0.8, "high confidence", trust_json),
        ])
        store = _store_with_conn(_conn_with_cursor(cursor))
        results = store.load_audit_results(1, ["d1"])

        assert len(results) == 1
        r = results[0]
        assert isinstance(r, AuditResult)
        assert r.document.id == "d1"
        assert r.document.title == "Doc Title"
        assert r.status == "stale"
        assert len(r.signals) == 1
        assert r.signals[0].signal_type == "outdated"
        assert r.trust_metadata == {"flag": True}
        assert r.trust_evidence == {"src": "x"}

    def test_uses_any_array_parameter(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.load_audit_results(1, ["d1", "d2"])
        sqls = _executed_sqls(cursor)
        assert any("= ANY(" in s for s in sqls)

    def test_selects_signals_as_text(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.load_audit_results(1, ["d1"])
        sqls = _executed_sqls(cursor)
        assert any("signals::text" in s for s in sqls)

    def test_selects_trust_data_as_text(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.load_audit_results(1, ["d1"])
        sqls = _executed_sqls(cursor)
        assert any("trust_data::text" in s for s in sqls)

    def test_joins_documents_on_scan_id(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.load_audit_results(1, ["d1"])
        sqls = _executed_sqls(cursor)
        assert any("JOIN documents" in s and "d.scan_id = ar.scan_id" in s for s in sqls)


# ---------------------------------------------------------------------------
# Get scan results
# ---------------------------------------------------------------------------

class TestGetScanResults:
    def test_returns_empty_when_no_rows(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        assert store.get_scan_results(1) == []

    def test_returns_api_shape(self):
        import json

        signals_json = json.dumps([])
        trust_json = json.dumps({"metadata": {"k": "v"}, "evidence": {}})

        cursor = _make_cursor(fetchall_value=[
            ("d1", "Title", "http://x", "confluence", None,
             "stale", signals_json, None, 0.9, "reason", trust_json),
        ])
        store = _store_with_conn(_conn_with_cursor(cursor))
        results = store.get_scan_results(1)

        assert len(results) == 1
        r = results[0]
        assert r["id"] == "d1"
        assert r["title"] == "Title"
        assert r["url"] == "http://x"
        assert r["overall_status"] == "stale"
        assert r["confidence"] == 0.9
        assert r["trust_metadata"] == {"k": "v"}
        assert r["trust_evidence"] == {}
        assert isinstance(r["signals"], list)
        assert r["last_modified"] is None

    def test_converts_datetime_last_modified_to_iso_string(self):
        import json
        from datetime import datetime, timezone

        dt = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        signals_json = json.dumps([])
        trust_json = json.dumps({"metadata": {}, "evidence": {}})

        cursor = _make_cursor(fetchall_value=[
            ("d1", "T", "http://x", "confluence", dt,
             "current", signals_json, None, 1.0, "", trust_json),
        ])
        store = _store_with_conn(_conn_with_cursor(cursor))
        results = store.get_scan_results(1)
        assert isinstance(results[0]["last_modified"], str)

    def test_joins_scans_and_documents(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_results(1)
        sql = _executed_sqls(cursor)[0]
        assert "JOIN documents" in sql
        assert "JOIN scans" in sql
        assert "status = 'completed'" in sql

    def test_orders_stale_first_via_case(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_results(1)
        sql = _executed_sqls(cursor)[0]
        assert "CASE" in sql and "'stale'" in sql

    def test_selects_signals_and_trust_as_text(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_results(1)
        sql = _executed_sqls(cursor)[0]
        assert "signals::text" in sql
        assert "trust_data::text" in sql


# ---------------------------------------------------------------------------
# Workflow: complete_scan_with_findings
# ---------------------------------------------------------------------------

def _make_actionable_result(status: str = "stale"):
    """Return an AuditResult that passes _actionable_results() via explicit flag."""
    from kb_audit.models import AuditResult, Document
    doc = Document(id="d1", title="Title", content="body", source_type="confluence")
    return AuditResult(
        document=doc,
        status=status,
        trust_metadata={"requires_human_audit": True},
    )


class TestCompleteScansWithFindings:
    def test_verifies_lease_when_owner_token_supplied(self):
        # _check_lease → (1,), _assert_scan_running → (1,),
        # finding lookup → None (new), then scans UPDATE
        cursor = _make_cursor(fetchone_values=[(1,), (1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.complete_scan_with_findings(
            1, 0, [_make_actionable_result()],
            owner_token="tok", now="2024-01-01T00:00:00+00:00",
        )
        sqls = _executed_sqls(cursor)
        assert any("FOR UPDATE" in s and "scan_state" in s for s in sqls)

    def test_marks_scan_completed_after_sync(self):
        cursor = _make_cursor(fetchone_values=[(1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.complete_scan_with_findings(1, 5, [_make_actionable_result()])
        sqls = _executed_sqls(cursor)
        assert any("UPDATE scans" in s and "completed" in s for s in sqls)

    def test_inserts_new_finding_in_same_transaction(self):
        # _assert_scan_running → (1,), finding lookup → None (new)
        cursor = _make_cursor(fetchone_values=[(1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        stats = store.complete_scan_with_findings(1, 0, [_make_actionable_result()])
        sqls = _executed_sqls(cursor)
        assert any("INSERT INTO finding_workflow" in s for s in sqls)
        assert stats["new"] == 1

    def test_commits_on_success(self):
        cursor = _make_cursor(fetchone_values=[(1,), None])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.complete_scan_with_findings(1, 0, [])
        conn.commit.assert_called()

    def test_rollback_on_failure(self):
        import pytest

        cursor = _make_cursor(fetchone_values=[(1,)])
        cursor.execute.side_effect = [None, RuntimeError("db boom")]
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="db boom"):
            store.complete_scan_with_findings(1, 0, [])
        conn.rollback.assert_called()


# ---------------------------------------------------------------------------
# Workflow: sync_findings
# ---------------------------------------------------------------------------

class TestSyncFindings:
    def test_creates_finding_when_requires_human_audit_true(self):
        # _assert_scan_running → (1,), finding lookup → None (new)
        cursor = _make_cursor(fetchone_values=[(1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        stats = store.sync_findings(1, [_make_actionable_result()])
        sqls = _executed_sqls(cursor)
        insert_sqls = [s for s in sqls if "INSERT INTO finding_workflow" in s]
        assert insert_sqls, "Expected INSERT INTO finding_workflow"
        assert any("ON CONFLICT (finding_key)" in s for s in insert_sqls), (
            "New-finding INSERT must use ON CONFLICT (finding_key) for upsert safety"
        )
        assert stats["new"] == 1

    def test_skips_result_when_requires_human_audit_false(self):
        from kb_audit.models import AuditResult, Document
        doc = Document(id="d1", title="T", content="c", source_type="confluence")
        not_actionable = AuditResult(
            document=doc, status="stale",
            trust_metadata={"requires_human_audit": False},
        )
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        stats = store.sync_findings(1, [not_actionable])
        sqls = _executed_sqls(cursor)
        assert not any("INSERT INTO finding_workflow" in s for s in sqls)
        assert stats["new"] == 0

    def test_reopens_terminal_finding_on_evidence_change(self):
        # _assert_scan_running → (1,), finding lookup → ("dismissed", "old_hash", None)
        cursor = _make_cursor(fetchone_values=[(1,), ("dismissed", "DIFFERENT_HASH", None)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        stats = store.sync_findings(1, [_make_actionable_result()])
        sqls = _executed_sqls(cursor)
        reopen_sqls = [s for s in sqls if "UPDATE finding_workflow" in s and "'open'" in s]
        assert reopen_sqls, "Expected UPDATE that reopens finding to 'open'"
        assert stats["reopened"] == 1

    def test_auto_fixes_missing_finding_when_scanned_doc_ids_provided(self):
        from kb_audit.models import AuditResult, Document
        # Existing finding for doc "d1" in DB (open), but NOT in current results.
        # Scanned doc "d1" → should auto-fix.
        doc2 = Document(id="d2", title="Other", content="c", source_type="confluence")
        other_result = AuditResult(
            document=doc2, status="stale",
            trust_metadata={"requires_human_audit": True},
        )
        # _assert_scan_running → (1,)
        # finding lookup for d2 → None (new, will INSERT)
        # auto-fix fetchall → [("fk_d1", "open")]  — old finding for d1
        cursor = _make_cursor(
            fetchone_values=[(1,), None],
            fetchall_value=[("fk_d1", "open")],
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        stats = store.sync_findings(1, [other_result], scanned_doc_ids={"d1", "d2"})
        sqls = _executed_sqls(cursor)
        assert any("'fixed'" in s for s in sqls)
        assert stats["auto_fixed"] == 1

    def test_finding_lookup_uses_percent_s_placeholder(self):
        cursor = _make_cursor(fetchone_values=[(1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.sync_findings(1, [_make_actionable_result()])
        sqls = _executed_sqls(cursor)
        select_sqls = [s for s in sqls if "finding_workflow" in s and "SELECT" in s]
        assert any("%s" in s for s in select_sqls)

    def test_verifies_lease_when_owner_token_supplied(self):
        cursor = _make_cursor(fetchone_values=[(1,), (1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.sync_findings(1, [_make_actionable_result()], owner_token="tok",
                            now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert any("FOR UPDATE" in s and "scan_state" in s for s in sqls)

    def test_commits_on_success(self):
        cursor = _make_cursor(fetchone_values=[(1,), None])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.sync_findings(1, [_make_actionable_result()])
        conn.commit.assert_called()

    def test_rollback_on_failure(self):
        import pytest

        cursor = _make_cursor(fetchone_values=[(1,)])
        cursor.execute.side_effect = [None, RuntimeError("sync error")]
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="sync error"):
            store.sync_findings(1, [_make_actionable_result()])
        conn.rollback.assert_called()

    def test_returns_stats_with_expected_keys(self):
        cursor = _make_cursor(fetchone_values=[(1,)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        stats = store.sync_findings(1, [])
        assert set(stats) == {"new", "updated", "reopened", "auto_fixed"}


# ---------------------------------------------------------------------------
# Workflow: update_workflow
# ---------------------------------------------------------------------------

class TestUpdateWorkflow:
    def test_returns_false_when_no_fields_supplied(self):
        from kb_audit.storage.postgres import PostgresStorage
        store = PostgresStorage("postgresql://localhost/kbaudit")
        # No conn needed — returns False before accessing self.conn
        result = store.update_workflow("some-key")
        assert result is False

    def test_returns_false_when_finding_not_found(self):
        cursor = _make_cursor(rowcount=0)
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        result = store.update_workflow("nonexistent", state="open")
        assert result is False

    def test_returns_true_when_finding_updated(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.update_workflow("fk1", state="acknowledged")
        assert result is True

    def test_raises_value_error_for_snoozed_without_until(self):
        import pytest
        from kb_audit.storage.postgres import PostgresStorage
        store = PostgresStorage("postgresql://localhost/kbaudit")
        with pytest.raises(ValueError, match="snoozed_until"):
            store.update_workflow("fk1", state="snoozed")

    def test_updates_workflow_state_in_sql(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.update_workflow("fk1", state="acknowledged")
        sqls = _executed_sqls(cursor)
        assert any("UPDATE finding_workflow" in s for s in sqls)
        assert any("workflow_state" in s for s in sqls)

    def test_note_only_update_does_not_touch_state(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.update_workflow("fk1", note="my note")
        sqls = _executed_sqls(cursor)
        update_sql = next(s for s in sqls if "UPDATE finding_workflow" in s)
        assert "note" in update_sql
        assert "workflow_state" not in update_sql

    def test_commits_on_success(self):
        cursor = _make_cursor(rowcount=1)
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.update_workflow("fk1", state="open")
        conn.commit.assert_called()

    def test_snoozed_until_string_converted_for_timestamptz(self):
        cursor = _make_cursor(rowcount=1)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.update_workflow("fk1", snoozed_until="2099-01-01T00:00:00+00:00")
        params = _executed_params(cursor)
        update_params = [p for p in params if p]
        # The snoozed_until value should be a datetime, not a raw string
        from datetime import datetime
        assert any(
            isinstance(v, datetime) for p in update_params for v in p
        )

    def test_rollback_on_execute_failure(self):
        import pytest

        cursor = _make_cursor(rowcount=1)
        cursor.execute.side_effect = RuntimeError("db error")
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="db error"):
            store.update_workflow("fk1", state="open")
        conn.rollback.assert_called()


# ---------------------------------------------------------------------------
# Workflow: get_findings
# ---------------------------------------------------------------------------

class TestGetFindings:
    def test_returns_empty_when_no_findings(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_findings()
        assert result == []

    def test_default_excludes_terminal_states(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_findings()
        sql = _executed_sqls(cursor)[0]
        # Must filter out terminal states
        assert "ANY" in sql or "NOT" in sql
        assert "fixed" in sql or "dismissed" in sql

    def test_include_all_removes_state_filter(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_findings(include_all=True)
        sql = _executed_sqls(cursor)[0]
        # No WHERE clause when no scan_id and include_all=True
        assert "WHERE" not in sql

    def test_scan_id_filter_appends_clause(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_findings(scan_id=42)
        sql = _executed_sqls(cursor)[0]
        assert "last_checked_scan_id" in sql
        params = _executed_params(cursor)
        assert any(42 in p for p in params if p)

    def test_states_filter_uses_any(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_findings(states=["open", "acknowledged"])
        sql = _executed_sqls(cursor)[0]
        assert "= ANY(" in sql

    def test_orders_open_first_via_case(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_findings(include_all=True)
        sql = _executed_sqls(cursor)[0]
        assert "CASE" in sql and "'open'" in sql

    def test_returns_finding_with_enriched_audit_context(self):
        import json
        signals_json = json.dumps([])
        trust_json = json.dumps({"metadata": {}, "evidence": {}})
        finding_row = (
            "fk1", "d1", "confluence", "Doc Title", "open",
            "", "", None, None, "", "ev_hash",
            1, 1, None, 1,
        )
        enrichment_row = ("stale", 0.9, "reason", signals_json, trust_json, "http://x")
        cursor = _make_cursor(
            fetchone_values=[enrichment_row],
            fetchall_value=[finding_row],
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        findings = store.get_findings(include_all=True)
        assert len(findings) == 1
        f = findings[0]
        assert f["finding_key"] == "fk1"
        assert f["workflow_state"] == "open"
        assert "audit_context" in f
        assert f["audit_context"]["overall_status"] == "stale"


# ---------------------------------------------------------------------------
# Workflow: get_finding
# ---------------------------------------------------------------------------

class TestGetFinding:
    def test_returns_none_when_not_found(self):
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_finding("nonexistent-key")
        assert result is None

    def test_returns_finding_dict(self):
        import json
        signals_json = json.dumps([])
        trust_json = json.dumps({"metadata": {}, "evidence": {}})
        finding_row = (
            "fk1", "d1", "confluence", "Title", "open",
            "", "", None, None, "", "ev_hash",
            1, 1, None, 1,
        )
        enrichment_row = ("stale", 0.9, "reason", signals_json, trust_json, "http://x")
        cursor = _make_cursor(fetchone_values=[finding_row, enrichment_row])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_finding("fk1")
        assert result is not None
        assert result["finding_key"] == "fk1"
        assert result["workflow_state"] == "open"
        assert "audit_context" in result

    def test_returns_none_on_audit_context_miss(self):
        # Finding row exists but enrichment query returns None
        finding_row = (
            "fk1", "d1", "confluence", "Title", "open",
            "", "", None, None, "", "ev_hash",
            1, 1, None, 1,
        )
        cursor = _make_cursor(fetchone_values=[finding_row, None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_finding("fk1")
        assert result is not None
        assert result["audit_context"] is None

    def test_selects_signals_as_text_in_enrichment(self):
        finding_row = (
            "fk1", "d1", "confluence", "Title", "open",
            "", "", None, None, "", "ev_hash",
            1, 1, None, 1,
        )
        cursor = _make_cursor(fetchone_values=[finding_row, None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_finding("fk1")
        sqls = _executed_sqls(cursor)
        enrichment_sqls = [s for s in sqls if "audit_results" in s]
        assert any("signals::text" in s for s in enrichment_sqls)


# ---------------------------------------------------------------------------
# Workflow: get_workflow_summary
# ---------------------------------------------------------------------------

class TestGetWorkflowSummary:
    def test_returns_empty_dict_when_no_findings(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_workflow_summary()
        assert result == {}

    def test_returns_count_by_state(self):
        cursor = _make_cursor(fetchall_value=[("open", 3), ("acknowledged", 1)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_workflow_summary(include_all=True)
        assert result == {"open": 3, "acknowledged": 1}

    def test_default_filters_out_terminal_states(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_workflow_summary()
        sql = _executed_sqls(cursor)[0]
        # Terminal states are passed as a parameter (= ANY(%s)), not embedded in SQL
        assert "ANY" in sql
        assert "NOT" in sql
        params = _executed_params(cursor)
        terminal_states = {"fixed", "dismissed", "accepted_risk"}
        assert any(
            isinstance(p, (list, tuple)) and terminal_states.issubset(set(p))
            for param_tuple in params
            for p in param_tuple
            if isinstance(p, (list, tuple))
        )

    def test_scan_id_filter(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_workflow_summary(scan_id=7)
        sql = _executed_sqls(cursor)[0]
        assert "last_checked_scan_id" in sql
        params = _executed_params(cursor)
        assert any(7 in p for p in params if p)

    def test_include_all_removes_terminal_filter(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_workflow_summary(include_all=True)
        sql = _executed_sqls(cursor)[0]
        # No WHERE clause when include_all=True and no scan_id
        assert "WHERE" not in sql

# ---------------------------------------------------------------------------
# Scan history: get_scan_diff
# ---------------------------------------------------------------------------

class TestGetScanDiff:
    def test_returns_empty_when_no_changes(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_diff(2, 1)
        assert result == []

    def test_returns_dict_with_expected_keys(self):
        cursor = _make_cursor(fetchall_value=[("doc-1", "My Doc", "current", "stale")])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_diff(2, 1)
        assert len(result) == 1
        row = result[0]
        assert row["document_id"] == "doc-1"
        assert row["title"] == "My Doc"
        assert row["old_status"] == "current"
        assert row["new_status"] == "stale"

    def test_old_status_is_none_for_new_documents(self):
        cursor = _make_cursor(fetchall_value=[("doc-1", "New Doc", None, "stale")])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_diff(2, 1)
        assert result[0]["old_status"] is None

    def test_uses_left_join_and_percent_s_placeholders(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_diff(10, 9)
        sql = _executed_sqls(cursor)[0]
        assert "LEFT JOIN" in sql
        assert "%s" in sql

    def test_passes_prev_and_current_scan_ids_as_params(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_diff(10, 9)
        params = _executed_params(cursor)[0]
        assert 9 in params   # prev_scan_id
        assert 10 in params  # scan_id


# ---------------------------------------------------------------------------
# Scan history: get_scan_history
# ---------------------------------------------------------------------------

class TestGetScanHistory:
    def test_returns_empty_when_no_completed_scans(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_history()
        assert result == []

    def test_filters_completed_scans_only(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_history()
        sql = _executed_sqls(cursor)[0]
        assert "completed" in sql

    def test_orders_by_scan_id_descending(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_history()
        sql = _executed_sqls(cursor)[0]
        assert "ORDER BY" in sql
        assert "DESC" in sql

    def test_respects_limit_parameter(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_history(limit=5)
        sql = _executed_sqls(cursor)[0]
        params = _executed_params(cursor)[0]
        assert "LIMIT" in sql
        assert 5 in params

    def test_aggregates_status_counts(self):
        cursor = _make_cursor(fetchall_value=[])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.get_scan_history()
        sql = _executed_sqls(cursor)[0]
        assert "stale" in sql
        assert "needs_review" in sql
        assert "unknown" in sql

    def test_returns_expected_dict_keys(self):
        from datetime import datetime, timezone
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchall_value=[(1, ts, ts, 10, 3, 1, 0)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_history()
        assert len(result) == 1
        entry = result[0]
        for key in ("scan_id", "started_at", "finished_at", "document_count",
                    "stale_count", "needs_review_count", "unknown_count", "changes"):
            assert key in entry, f"Missing key {key!r}"
        assert entry["scan_id"] == 1
        assert entry["document_count"] == 10
        assert entry["stale_count"] == 3
        assert entry["needs_review_count"] == 1
        assert entry["unknown_count"] == 0

    def test_normalizes_datetime_timestamps_to_iso_strings(self):
        from datetime import datetime, timezone
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchall_value=[(1, ts, ts, 0, 0, 0, 0)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_history()
        entry = result[0]
        assert isinstance(entry["started_at"], str)
        assert isinstance(entry["finished_at"], str)
        assert "2024" in entry["started_at"]

    def test_passes_through_non_datetime_timestamps_unchanged(self):
        cursor = _make_cursor(
            fetchall_value=[(1, "2024-01-01T00:00:00", "2024-01-02T00:00:00", 0, 0, 0, 0)]
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_history()
        assert result[0]["started_at"] == "2024-01-01T00:00:00"

    def test_single_scan_has_no_changes(self):
        from datetime import datetime, timezone
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchall_value=[(1, ts, ts, 5, 0, 0, 0)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.get_scan_history()
        assert result[0]["changes"] is None

    def test_calls_get_scan_diff_for_consecutive_pairs(self):
        from unittest.mock import patch
        from datetime import datetime, timezone
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Two scans: newer (id=2) and older (id=1) — newest first
        cursor = _make_cursor(
            fetchall_value=[(2, ts, ts, 5, 1, 0, 0), (1, ts, ts, 3, 0, 0, 0)]
        )
        store = _store_with_conn(_conn_with_cursor(cursor))
        with patch.object(store, "get_scan_diff", return_value=[]) as mock_diff:
            result = store.get_scan_history()
        mock_diff.assert_called_once_with(2, 1)
        assert result[0]["changes"] == []  # _summarize_changes([]) → []
        assert result[1]["changes"] is None  # oldest has no prior


# ---------------------------------------------------------------------------
# Maintenance: clear_all
# ---------------------------------------------------------------------------

class TestClearAll:
    def test_deletes_audit_results(self):
        cursor = _make_cursor()
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.clear_all()
        sqls = _executed_sqls(cursor)
        assert any("audit_results" in s and "DELETE" in s for s in sqls)

    def test_deletes_documents(self):
        cursor = _make_cursor()
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.clear_all()
        sqls = _executed_sqls(cursor)
        assert any("documents" in s and "DELETE" in s for s in sqls)

    def test_deletes_scans(self):
        cursor = _make_cursor()
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.clear_all()
        sqls = _executed_sqls(cursor)
        assert any("scans" in s and "DELETE" in s for s in sqls)

    def test_deletes_finding_workflow(self):
        cursor = _make_cursor()
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.clear_all()
        sqls = _executed_sqls(cursor)
        assert any("finding_workflow" in s and "DELETE" in s for s in sqls)

    def test_resets_scan_state(self):
        cursor = _make_cursor()
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.clear_all()
        sqls = _executed_sqls(cursor)
        reset_sqls = [s for s in sqls if "scan_state" in s and "UPDATE" in s]
        assert reset_sqls
        assert any("in_progress" in s for s in reset_sqls)

    def test_commits_on_success(self):
        cursor = _make_cursor()
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.clear_all()
        conn.commit.assert_called()

    def test_rollback_on_failure(self):
        import pytest
        cursor = _make_cursor()
        cursor.execute.side_effect = RuntimeError("delete error")
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="delete error"):
            store.clear_all()
        conn.rollback.assert_called()


# ---------------------------------------------------------------------------
# Maintenance: clear_all_if_idle
# ---------------------------------------------------------------------------

class TestClearAllIfIdle:
    def test_uses_for_update_on_scan_state(self):
        cursor = _make_cursor(fetchone_values=[(False, None)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.clear_all_if_idle()
        first_sql = _executed_sqls(cursor)[0]
        assert "FOR UPDATE" in first_sql
        assert "scan_state" in first_sql

    def test_returns_false_when_live_lease_exists(self):
        from datetime import datetime, timezone
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchone_values=[(True, future)])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        result = store.clear_all_if_idle(now="2024-01-01T00:00:00+00:00")
        assert result is False
        conn.rollback.assert_called()

    def test_does_not_delete_when_live_lease_exists(self):
        from datetime import datetime, timezone
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchone_values=[(True, future)])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.clear_all_if_idle(now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert not any("DELETE" in s for s in sqls)

    def test_treats_expired_lease_as_idle_and_clears(self):
        from datetime import datetime, timezone
        past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        cursor = _make_cursor(fetchone_values=[(True, past)])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        result = store.clear_all_if_idle(now="2024-01-01T00:00:00+00:00")
        assert result is True
        conn.commit.assert_called()
        sqls = _executed_sqls(cursor)
        assert any("DELETE" in s for s in sqls)

    def test_returns_true_when_idle(self):
        cursor = _make_cursor(fetchone_values=[(False, None)])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        result = store.clear_all_if_idle()
        assert result is True
        conn.commit.assert_called()

    def test_returns_true_when_no_scan_state_row(self):
        cursor = _make_cursor(fetchone_values=[None])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        result = store.clear_all_if_idle()
        assert result is True


# ---------------------------------------------------------------------------
# Maintenance: prune_scans
# ---------------------------------------------------------------------------

class TestPruneScans:
    def test_returns_zero_when_fewer_scans_than_keep(self):
        # Cutoff query returns None — fewer than keep terminal scans exist
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.prune_scans(keep=10)
        assert result == 0

    def test_selects_terminal_scans_only_for_cutoff(self):
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.prune_scans(keep=5)
        cutoff_sql = _executed_sqls(cursor)[0]
        assert "completed" in cutoff_sql
        assert "failed" in cutoff_sql
        assert "running" not in cutoff_sql

    def test_deletes_only_terminal_scans_older_than_cutoff(self):
        cursor = _make_cursor(fetchone_values=[(3,)], rowcount=2)
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.prune_scans(keep=5)
        sqls = _executed_sqls(cursor)
        delete_sqls = [s for s in sqls if "DELETE" in s]
        assert delete_sqls, "Expected DELETE statements after cutoff found"
        for sql in delete_sqls:
            assert "running" not in sql, f"DELETE must not target running scans: {sql}"
            assert "completed" in sql or "failed" in sql

    def test_returns_scan_delete_rowcount(self):
        cursor = _make_cursor(fetchone_values=[(3,)], rowcount=4)
        store = _store_with_conn(_conn_with_cursor(cursor))
        result = store.prune_scans(keep=5)
        assert result == 4

    def test_commits_on_success(self):
        cursor = _make_cursor(fetchone_values=[None])
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        store.prune_scans()
        conn.commit.assert_called()

    def test_rollback_on_failure(self):
        import pytest
        cursor = _make_cursor(fetchone_values=[(3,)])
        cursor.execute.side_effect = RuntimeError("prune error")
        conn = _conn_with_cursor(cursor)
        store = _store_with_conn(conn)
        with pytest.raises(RuntimeError, match="prune error"):
            store.prune_scans()
        conn.rollback.assert_called()

    def test_verifies_lease_when_owner_token_supplied(self):
        # lease check: fetchone → (1,) to pass _check_lease, then cutoff → None
        cursor = _make_cursor(fetchone_values=[(1,), None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.prune_scans(owner_token="tok", now="2024-01-01T00:00:00+00:00")
        sqls = _executed_sqls(cursor)
        assert any("FOR UPDATE" in s and "scan_state" in s for s in sqls)

    def test_offset_is_keep_minus_one(self):
        cursor = _make_cursor(fetchone_values=[None])
        store = _store_with_conn(_conn_with_cursor(cursor))
        store.prune_scans(keep=4)
        params = _executed_params(cursor)
        # OFFSET = keep - 1 = 3 must appear in the first query's params
        assert any(3 in p for p in params if p)


# ---------------------------------------------------------------------------
# Factory: Postgres URLs wired to PostgresStorage (Step 10)
# ---------------------------------------------------------------------------

class TestFactoryWiredAfterStep10:
    def test_postgresql_url_returns_postgres_storage(self):
        from kb_audit.storage import create_storage
        from kb_audit.storage.postgres import PostgresStorage
        result = create_storage("postgresql://localhost/kbaudit")
        assert isinstance(result, PostgresStorage)

    def test_postgres_url_returns_postgres_storage(self):
        from kb_audit.storage import create_storage
        from kb_audit.storage.postgres import PostgresStorage
        result = create_storage("postgres://localhost/kbaudit")
        assert isinstance(result, PostgresStorage)

    def test_factory_does_not_connect(self):
        from kb_audit.storage import create_storage
        result = create_storage("postgresql://localhost/kbaudit")
        assert not result.is_connected
