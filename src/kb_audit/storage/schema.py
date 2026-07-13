"""SQLite schema constants and initialization for kb_audit.

All CREATE TABLE statements and additive migrations live here.
Nothing in this module interprets or transforms data — it only defines
and applies structure.
"""

from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    document_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT NOT NULL,
    scan_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_type TEXT NOT NULL,
    url TEXT,
    last_modified TEXT,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (scan_id) REFERENCES scans(id),
    PRIMARY KEY (id, scan_id)
);

CREATE TABLE IF NOT EXISTS audit_results (
    document_id TEXT NOT NULL,
    scan_id INTEGER NOT NULL,
    overall_status TEXT NOT NULL,
    signals TEXT NOT NULL DEFAULT '[]',
    suggested_replacement_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    confidence_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (document_id, scan_id) REFERENCES documents(id, scan_id),
    PRIMARY KEY (document_id, scan_id)
);
"""

_FINDING_WORKFLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_workflow (
    finding_key TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    workflow_state TEXT NOT NULL DEFAULT 'open',
    note TEXT DEFAULT '',
    assigned_owner TEXT DEFAULT '',
    due_date TEXT,
    snoozed_until TEXT,
    dismissal_reason TEXT DEFAULT '',
    evidence_hash TEXT NOT NULL,
    first_seen_scan_id INTEGER,
    last_seen_scan_id INTEGER,
    last_checked_scan_id INTEGER,
    updated_at TEXT NOT NULL
);
"""

_SCAN_STATE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS scan_state "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), "
    "in_progress INTEGER NOT NULL DEFAULT 0, "
    "last_scan_id INTEGER, "
    "scan_error TEXT)"
)

_MIGRATIONS = [
    "ALTER TABLE audit_results ADD COLUMN confidence REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE audit_results ADD COLUMN confidence_reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE audit_results ADD COLUMN trust_data TEXT NOT NULL DEFAULT '{}'",
    _FINDING_WORKFLOW_SCHEMA,
    "ALTER TABLE finding_workflow ADD COLUMN last_checked_scan_id INTEGER",
    _SCAN_STATE_SCHEMA,
    "INSERT OR IGNORE INTO scan_state (id, in_progress) VALUES (1, 0)",
    "ALTER TABLE scan_state ADD COLUMN owner_token TEXT",
    "ALTER TABLE scan_state ADD COLUMN lease_expires_at TEXT",
    # Scan lifecycle: status (running/completed/failed), owner token, stored error.
    # Default 'completed' preserves existing finished scans as completed.
    "ALTER TABLE scans ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
    "ALTER TABLE scans ADD COLUMN owner_token TEXT",
    "ALTER TABLE scans ADD COLUMN error TEXT",
    # Legacy rows without finished_at were interrupted — mark them failed.
    # Only touches rows with the DEFAULT 'completed' status (pre-migration rows).
    # New 'running' rows created after migration are handled by _abandon_running_scans.
    "UPDATE scans SET status = 'failed' WHERE finished_at IS NULL AND status = 'completed'",
]


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Apply the base schema and all additive migrations to *conn*.

    Safe to call on an existing database: base tables use IF NOT EXISTS and
    migrations skip silently when the column or table already exists.
    """
    conn.executescript(_SCHEMA)
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column / table already exists
