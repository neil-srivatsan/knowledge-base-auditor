"""Backward-compatible facade for the SQLite storage implementation.

Existing imports continue to work without change:

    from kb_audit.db import Database
    from kb_audit.db import LeaseLostError
    from kb_audit.db import ScanLeaseContext
    from kb_audit.db import _UNSET
    from kb_audit.db import _sanitize_error

The implementation now lives in kb_audit.storage.sqlite.SqliteStorage.
"""

from __future__ import annotations

from kb_audit.storage.sqlite import (
    LEASE_DURATION_SECONDS,
    RENEW_INTERVAL_SECONDS,
    LeaseLostError,
    ScanLeaseContext,
    SqliteStorage,
    _UNSET,
    _UnsetType,
    _sanitize_error,
)

__all__ = [
    "Database",
    "LEASE_DURATION_SECONDS",
    "LeaseLostError",
    "RENEW_INTERVAL_SECONDS",
    "ScanLeaseContext",
    "SqliteStorage",
    "_UNSET",
    "_UnsetType",
    "_sanitize_error",
]


class Database(SqliteStorage):
    """Backward-compatible alias for the SQLite storage implementation.

    All behavior is inherited from SqliteStorage.  New code should prefer
    importing SqliteStorage directly from kb_audit.storage.sqlite, or depend
    on the AuditStorage protocol from kb_audit.storage.contracts.
    """
