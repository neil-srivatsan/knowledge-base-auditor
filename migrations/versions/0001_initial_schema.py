"""Initial PostgreSQL schema for kb_audit.

Creates all tables, indexes, and the scan_state singleton row for a fresh
kb_audit PostgreSQL database.  The SQL is sourced directly from
``kb_audit.storage.schema_postgres.iter_postgres_schema_statements()`` so
that the migration and the runtime schema initializer share a single source
of truth and cannot silently drift apart.

Revision ID: 0001
Revises:
Create Date: 2026-07-12
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from kb_audit.storage.schema_postgres import iter_postgres_schema_statements

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Apply the full initial kb_audit schema to a blank PostgreSQL database.

    Delegates to ``iter_postgres_schema_statements()`` so that the migration
    stays in sync with the runtime schema by construction.
    """
    for stmt in iter_postgres_schema_statements():
        op.execute(stmt)


def downgrade() -> None:
    """Remove all kb_audit tables from the database."""
    # Drop in reverse dependency order; CASCADE handles remaining FK references.
    op.execute("DROP TABLE IF EXISTS finding_workflow CASCADE")
    op.execute("DROP TABLE IF EXISTS scan_state CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_results CASCADE")
    op.execute("DROP TABLE IF EXISTS documents CASCADE")
    op.execute("DROP TABLE IF EXISTS scans CASCADE")
