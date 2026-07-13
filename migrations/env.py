"""Alembic environment script for kb_audit PostgreSQL migrations.

Reads the target database URL from, in priority order:
  1. KB_AUDIT_POSTGRES_URL environment variable
  2. KB_AUDIT_POSTGRES_TEST_URL environment variable (test fallback)
  3. sqlalchemy.url in alembic.ini (if set)

PostgreSQL is not yet wired into create_storage() and these migrations are
applied manually, not at application startup.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic auto-generate uses target_metadata to compare database state against
# SQLAlchemy models.  We use raw DDL (not ORM models), so metadata is None.
target_metadata = None


def _get_url() -> str:
    """Return a SQLAlchemy-compatible Postgres URL from the environment."""
    url = (
        os.environ.get("KB_AUDIT_POSTGRES_URL")
        or os.environ.get("KB_AUDIT_POSTGRES_TEST_URL")
        or config.get_main_option("sqlalchemy.url") or ""
    )
    if not url:
        raise RuntimeError(
            "No Postgres URL found.  Set KB_AUDIT_POSTGRES_URL or "
            "KB_AUDIT_POSTGRES_TEST_URL, or set sqlalchemy.url in alembic.ini.\n"
            "Example: KB_AUDIT_POSTGRES_URL=postgresql://localhost/kbaudit "
            "alembic upgrade head"
        )
    # Normalize to the psycopg3 SQLAlchemy dialect (postgresql+psycopg://)
    # so that Alembic's internal version-table management uses the right driver.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout (offline / dry-run mode).

    Run with: alembic upgrade head --sql
    """
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database connection."""
    from sqlalchemy import create_engine

    engine = create_engine(_get_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
