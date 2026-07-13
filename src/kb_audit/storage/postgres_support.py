"""PostgreSQL driver availability probe.

This module centralises detection of the optional psycopg v3 driver.
It does not open database connections and is not imported by runtime
paths unless explicitly needed by the opt-in PostgresStorage backend.
"""

from __future__ import annotations

_INSTALL_HINT = (
    "PostgreSQL support requires the psycopg driver. "
    "Install the project with PostgreSQL extras: "
    "pip install 'kb-audit[postgres]'  "
    "or install the driver directly: pip install 'psycopg[binary]>=3.1'"
)


def psycopg_available() -> bool:
    """Return ``True`` if psycopg v3 can be imported, ``False`` otherwise."""
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def require_psycopg() -> None:
    """Raise ``RuntimeError`` if psycopg v3 is not importable.

    Does not open a database connection.
    """
    if not psycopg_available():
        raise RuntimeError(_INSTALL_HINT)
