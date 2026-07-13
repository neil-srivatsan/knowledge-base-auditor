"""Storage factory for kb_audit.

The single public entry point for obtaining a storage backend.
Callers remain responsible for connection lifecycle (.connect() / .close()).
"""

from __future__ import annotations

import os

from kb_audit.storage.contracts import AuditStorage
from kb_audit.storage.sqlite import SqliteStorage

# PostgreSQL URL schemes: handled by PostgresStorage.
_POSTGRES_SCHEMES = ("postgres://", "postgresql://")

# Other explicitly unsupported schemes.
_UNSUPPORTED_SCHEMES = ("mysql://", "mongodb://")

# SQLite URL prefixes that are accepted.
_SQLITE_PREFIXES = ("sqlite:///", "sqlite://", "jdbc:sqlite:./", "jdbc:sqlite:")


def create_storage(database_url: str | os.PathLike[str]) -> AuditStorage:
    """Return a storage backend for *database_url* without connecting to it.

    Supported inputs
    ----------------
    - ``:memory:`` — in-process SQLite (tests / demo)
    - Bare filesystem paths (``str`` or ``pathlib.Path``)
    - ``sqlite:///path/to/file.db`` or ``sqlite:///:memory:``
    - ``sqlite://path`` (two-slash form)
    - ``postgres://host/db`` or ``postgresql://host/db`` — PostgresStorage
      (requires psycopg; call ``.connect()`` before use)

    Unsupported inputs
    ------------------
    ``mysql://``, ``mongodb://``, and any other non-SQLite / non-Postgres
    ``<scheme>://`` form raise ``ValueError``.
    """
    url = str(database_url)

    # PostgreSQL: lazy import keeps psycopg optional for SQLite-only users.
    for scheme in _POSTGRES_SCHEMES:
        if url.startswith(scheme):
            from kb_audit.storage.postgres import PostgresStorage  # noqa: PLC0415
            return PostgresStorage(url)

    # Reject other explicitly unsupported schemes.
    for scheme in _UNSUPPORTED_SCHEMES:
        if url.startswith(scheme):
            raise ValueError(
                f"Unsupported storage backend {url!r}. "
                "Only SQLite storage is currently supported."
            )

    # Catch any other unknown <scheme>:// that is not an SQLite prefix.
    if "://" in url and not any(url.startswith(p) for p in _SQLITE_PREFIXES):
        raise ValueError(
            f"Unsupported storage backend {url!r}. "
            "Only SQLite storage is currently supported."
        )

    return SqliteStorage(url)
