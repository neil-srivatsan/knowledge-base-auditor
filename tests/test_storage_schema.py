"""Tests for storage schema initialization."""

from __future__ import annotations

import sqlite3

from kb_audit.db import Database
from kb_audit.storage.schema import initialize_schema


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {r[0] for r in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


class TestInitializeSchema:
    def test_creates_all_expected_tables(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        tables = _table_names(conn)
        assert {"scans", "documents", "audit_results", "finding_workflow", "scan_state"} <= tables

    def test_idempotent_on_second_call(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        # Should not raise — IF NOT EXISTS + silent OperationalError swallowing
        initialize_schema(conn)
        tables = _table_names(conn)
        assert {"scans", "documents", "audit_results", "finding_workflow", "scan_state"} <= tables

    def test_audit_results_has_migration_columns(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        cols = _column_names(conn, "audit_results")
        assert "confidence" in cols
        assert "confidence_reason" in cols
        assert "trust_data" in cols

    def test_finding_workflow_has_last_checked_scan_id(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        cols = _column_names(conn, "finding_workflow")
        assert "last_checked_scan_id" in cols

    def test_scan_state_has_lease_columns(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        cols = _column_names(conn, "scan_state")
        assert "owner_token" in cols
        assert "lease_expires_at" in cols

    def test_scan_state_singleton_row_inserted(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        row = conn.execute("SELECT id, in_progress FROM scan_state WHERE id = 1").fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == 0

    def test_scans_has_status_and_error_columns(self):
        conn = sqlite3.connect(":memory:")
        initialize_schema(conn)
        cols = _column_names(conn, "scans")
        assert "status" in cols
        assert "owner_token" in cols
        assert "error" in cols

    def test_applies_migrations_to_fresh_base_schema(self):
        # Simulate a database that only has the base tables (pre-migration state).
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                document_count INTEGER DEFAULT 0
            );
            CREATE TABLE documents (
                id TEXT NOT NULL,
                scan_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_type TEXT NOT NULL,
                url TEXT,
                last_modified TEXT,
                metadata TEXT DEFAULT '{}',
                PRIMARY KEY (id, scan_id)
            );
            CREATE TABLE audit_results (
                document_id TEXT NOT NULL,
                scan_id INTEGER NOT NULL,
                overall_status TEXT NOT NULL,
                signals TEXT NOT NULL DEFAULT '[]',
                suggested_replacement_id TEXT,
                PRIMARY KEY (document_id, scan_id)
            );
        """)
        # Running initialize_schema on this should add all migration columns
        # without raising.
        initialize_schema(conn)
        assert "confidence" in _column_names(conn, "audit_results")
        assert "trust_data" in _column_names(conn, "audit_results")
        assert "finding_workflow" in _table_names(conn)
        assert "scan_state" in _table_names(conn)


class TestDatabaseConnectUsesSchema:
    def test_connect_creates_usable_database(self):
        db = Database(":memory:")
        db.connect()
        tables = _table_names(db.conn)
        assert {"scans", "documents", "audit_results", "finding_workflow", "scan_state"} <= tables
        db.close()

    def test_connect_inserts_scan_state_row(self):
        db = Database(":memory:")
        db.connect()
        row = db.conn.execute("SELECT id, in_progress FROM scan_state WHERE id = 1").fetchone()
        assert row == (1, 0)
        db.close()

    def test_connect_twice_is_safe(self):
        # Reconnecting (close then connect again) should not raise.
        db = Database(":memory:")
        db.connect()
        db.close()
        db.connect()
        tables = _table_names(db.conn)
        assert "scans" in tables
        db.close()
