"""PostgreSQL readiness / preflight helper.

Validates PostgreSQL runtime prerequisites without opening a connection unless
explicitly asked.  All checks are import-safe: psycopg, Alembic, and SQLAlchemy
are never imported at module level.

Public API
----------
check_readiness(url)  -> PgReadinessReport   # offline checks only
connect_check(url)    -> str                 # "" on success, error text on failure
"""

from __future__ import annotations

import importlib.util
import pathlib
from dataclasses import dataclass, field

_POSTGRES_SCHEMES = ("postgres://", "postgresql://")

_INSTALL_HINT = (
    "Install the postgres extra: pip install 'kb-audit[postgres]'\n"
    "  or directly: pip install 'psycopg[binary]>=3.1' alembic sqlalchemy"
)


def _migrations_dir() -> pathlib.Path:
    """Return the migrations/versions directory for this project.

    Resolved relative to this file:
      src/kb_audit/storage/pg_readiness.py
      parents[3] == project root
      project root / migrations / versions
    """
    return pathlib.Path(__file__).parents[3] / "migrations" / "versions"


def _module_available(name: str) -> bool:
    """Return True if *name* can be imported, without actually importing it."""
    return importlib.util.find_spec(name) is not None


@dataclass
class PgReadinessReport:
    """Structured result from :func:`check_readiness`.

    Attributes
    ----------
    url_supplied:
        True when a URL was passed to :func:`check_readiness`.
    is_postgres_url:
        True when the URL begins with ``postgres://`` or ``postgresql://``.
    psycopg_available:
        True when psycopg v3 can be imported.
    alembic_available:
        True when alembic can be imported.
    migrations_on_disk:
        True when at least one migration file exists under ``migrations/versions/``.
    migration_count:
        Number of non-``__init__`` ``.py`` files found under ``migrations/versions/``.
    messages:
        Human-readable diagnostic messages for any failing check.
    """

    url_supplied: bool
    is_postgres_url: bool
    psycopg_available: bool
    alembic_available: bool
    migrations_on_disk: bool
    migration_count: int
    messages: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when all offline prerequisites are satisfied for a Postgres URL."""
        return (
            self.is_postgres_url
            and self.psycopg_available
            and self.alembic_available
            and self.migrations_on_disk
        )


def check_readiness(url: str | None = None) -> PgReadinessReport:
    """Check PostgreSQL offline prerequisites for *url*.

    Does **not** open a database connection.  Pass a ``postgres://`` or
    ``postgresql://`` URL to get a full report; pass ``None`` or a
    non-Postgres URL for a short report explaining what is wrong.

    Returns
    -------
    PgReadinessReport
        Structured result with per-check flags and human-readable messages.
    """
    url_supplied = url is not None
    is_postgres = url_supplied and any(str(url).startswith(s) for s in _POSTGRES_SCHEMES)

    psycopg_ok = _module_available("psycopg")
    alembic_ok = _module_available("alembic")

    mdir = _migrations_dir()
    migrations_exist = mdir.is_dir()
    migration_files = (
        [f for f in mdir.glob("*.py") if not f.name.startswith("__")]
        if migrations_exist
        else []
    )
    migration_count = len(migration_files)
    migrations_ok = migrations_exist and migration_count > 0

    messages: list[str] = []

    if not url_supplied:
        messages.append(
            "No URL supplied. Provide a postgres:// or postgresql:// URL."
        )
    elif not is_postgres:
        messages.append(
            f"URL is not a PostgreSQL URL: {url!r}. "
            "SQLite remains the default backend for non-Postgres URLs."
        )

    if is_postgres:
        if not psycopg_ok:
            messages.append(f"psycopg is not installed. {_INSTALL_HINT}")
        if not alembic_ok:
            messages.append(f"alembic is not installed. {_INSTALL_HINT}")
        if not migrations_exist:
            messages.append(
                f"Migrations directory not found: {mdir}. "
                "Run from the project root or verify the repository structure."
            )
        elif migration_count == 0:
            messages.append(f"No migration files found in {mdir}.")

    return PgReadinessReport(
        url_supplied=url_supplied,
        is_postgres_url=is_postgres,
        psycopg_available=psycopg_ok,
        alembic_available=alembic_ok,
        migrations_on_disk=migrations_ok,
        migration_count=migration_count,
        messages=messages,
    )


def connect_check(url: str) -> str:
    """Attempt a lightweight read-only connection check.

    Requires psycopg.  Does **not** mutate data or run migrations.
    Closes the connection before returning.

    Parameters
    ----------
    url:
        A ``postgres://`` or ``postgresql://`` connection URL.

    Returns
    -------
    str
        Empty string on success; a human-readable error message on failure.

    Raises
    ------
    RuntimeError
        If psycopg is not installed.
    """
    from kb_audit.storage.postgres_support import require_psycopg  # noqa: PLC0415

    require_psycopg()

    import psycopg  # noqa: PLC0415 — deferred; psycopg confirmed available above

    try:
        with psycopg.connect(url) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return ""
