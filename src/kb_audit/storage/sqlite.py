"""SQLite storage implementation for kb_audit.

This module contains the full persistence implementation.  The public API
surface is SqliteStorage — a self-contained class that owns the connection
lifecycle, lease management, scan lifecycle, document/result storage, and
finding workflow.

Callers should ordinarily import the backward-compatible alias
``from kb_audit.db import Database`` rather than importing SqliteStorage
directly.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb_audit.models import AuditResult, Document, WorkflowState
from kb_audit.storage.schema import initialize_schema
from kb_audit.storage.serialization import (
    deserialize_signal_records,
    deserialize_signals,
    deserialize_trust_data,
    sanitize_error,
    serialize_document_metadata,
    serialize_signals,
    serialize_trust_data,
)

logger = logging.getLogger(__name__)


class _UnsetType:
    """Sentinel for update_workflow() parameters not present in the request.

    ``None`` means "explicitly set this field to NULL/empty."
    ``_UNSET`` means "this field was not supplied; leave it unchanged."
    """

    _instance: _UnsetType | None = None

    def __new__(cls) -> _UnsetType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<UNSET>"


#: Singleton sentinel.  Import alongside SqliteStorage when you need it.
_UNSET: _UnsetType = _UnsetType()


#: Backward-compatible alias — callers within this module use _sanitize_error.
_sanitize_error = sanitize_error


LEASE_DURATION_SECONDS = 300
RENEW_INTERVAL_SECONDS = 60


class LeaseLostError(Exception):
    """Raised when a scan worker detects that its lease is no longer valid."""


class ScanLeaseContext:
    """Manages the full lifecycle of a leased scan: renewal, checking, and release.

    Owns the renewal-thread, the lease-check callable, and the safe
    ``end_scan`` release so that both the CLI and web scan execution use the
    same implementation.

    Usage::

        with ScanLeaseContext(db, owner_token) as ctx:
            try:
                auditor.run(lease_check=ctx.check, owner_token=owner_token)
                ctx.last_scan_id = ...   # set on success
            except LeaseLostError:
                pass  # end_scan is lease-fenced; a takeover won't clobber
            except Exception as exc:
                ctx.error = str(exc)
                raise
    """

    def __init__(self, db: SqliteStorage, owner_token: str) -> None:
        self._db = db
        self._owner_token = owner_token
        self._ownership_lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set by caller on success; passed to end_scan().
        self.last_scan_id: int | None = None
        #: Set by caller on failure; passed to end_scan() for storage.
        self.error: str | None = None

    def __enter__(self) -> ScanLeaseContext:
        """Start the renewal heartbeat thread."""
        self._thread = threading.Thread(
            target=self._renewal_loop,
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Stop the renewal thread and release the lease via end_scan()."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            released = self._db.end_scan(
                self._owner_token, self.last_scan_id, self.error
            )
            if not released:
                logger.warning(
                    "Lease already released or taken over; "
                    "scan state not updated for token %s",
                    self._owner_token,
                )
        except Exception:
            logger.exception(
                "Failed to record scan completion for token %s", self._owner_token
            )

    def check(self) -> None:
        """Raise LeaseLostError if this worker no longer holds the lease."""
        if self._ownership_lost.is_set():
            raise LeaseLostError(
                f"Lease ownership lost for token {self._owner_token}"
            )
        if not self._db.owns_live_lease(self._owner_token):
            self._ownership_lost.set()
            raise LeaseLostError(
                f"Lease ownership lost for token {self._owner_token}"
            )

    @property
    def ownership_lost(self) -> bool:
        """True if the lease has been taken over or expired."""
        return self._ownership_lost.is_set()

    def _renewal_loop(self) -> None:
        """Heartbeat: renew the lease every RENEW_INTERVAL_SECONDS."""
        while not self._stop.wait(RENEW_INTERVAL_SECONDS):
            rdb = SqliteStorage(self._db._path)
            try:
                rdb.connect()
                renewed = rdb.renew_lease(self._owner_token)
                if not renewed:
                    self._ownership_lost.set()
                    self._stop.set()
                    return
            except Exception:
                logger.exception(
                    "Lease renewal error for token %s", self._owner_token
                )
            finally:
                rdb.close()


def _assert_scan_running(
    conn: sqlite3.Connection, scan_id: int, owner_token: str | None
) -> None:
    """Verify scan_id is in 'running' state and owned by owner_token.

    Raises LeaseLostError when the check fails.  Called inside every
    mutation that must not write to a completed, failed, or foreign-owned
    scan.
    """
    if owner_token is not None:
        row = conn.execute(
            "SELECT 1 FROM scans WHERE id = ? AND owner_token = ? AND status = 'running'",
            (scan_id, owner_token),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM scans WHERE id = ? AND owner_token IS NULL AND status = 'running'",
            (scan_id,),
        ).fetchone()
    if row is None:
        raise LeaseLostError(
            f"Scan {scan_id} is not running or not owned by the current token"
        )


# SQL used by _leased_write to guard every protected mutation.
_LEASE_GUARD_SQL = (
    "SELECT 1 FROM scan_state WHERE id = 1 AND in_progress = 1 "
    "AND owner_token = ? AND lease_expires_at IS NOT NULL AND lease_expires_at > ?"
)


def _summarize_changes(changes: list[dict]) -> list[str]:
    """Turn a list of status-change dicts into human-readable summary lines."""
    if not changes:
        return []
    became_stale = 0
    became_current = 0
    became_unknown = 0
    became_needs_review = 0
    added = 0
    for c in changes:
        if c["old_status"] is None:
            added += 1
        elif c["new_status"] == "stale":
            became_stale += 1
        elif c["new_status"] == "current":
            became_current += 1
        elif c["new_status"] == "needs_review":
            became_needs_review += 1
        elif c["new_status"] == "unknown":
            became_unknown += 1
    lines: list[str] = []
    if added:
        lines.append(f"{added} document{'s' if added != 1 else ''} added")
    if became_stale:
        lines.append(f"{became_stale} document{'s' if became_stale != 1 else ''} became stale")
    if became_needs_review:
        lines.append(f"{became_needs_review} document{'s' if became_needs_review != 1 else ''} needs review")
    if became_current:
        lines.append(f"{became_current} document{'s' if became_current != 1 else ''} became current")
    if became_unknown:
        lines.append(f"{became_unknown} document{'s' if became_unknown != 1 else ''} became unknown")
    return lines


class SqliteStorage:
    def __init__(self, db_path: str | Path = "kbaudit.db") -> None:
        path_str = str(db_path)
        # Normalize various URL forms to a plain path
        for prefix in ("sqlite:///", "sqlite://", "jdbc:sqlite:./", "jdbc:sqlite:"):
            if path_str.startswith(prefix):
                path_str = path_str[len(prefix):]
                break
        self._path = path_str
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        initialize_schema(self._conn)

    def try_start_scan(self, now: str | None = None) -> str | None:
        """Atomically acquire the scan lease.

        Returns a UUID owner token on success, or None if a live lease exists.
        A lease is considered live when in_progress=1 AND the lease has not
        expired (lease_expires_at is NULL or in the future).

        Uses a dedicated connection with BEGIN IMMEDIATE for file-based DBs so
        that the check-then-set is atomic across OS processes.  Falls back to
        the shared connection for :memory: DBs (test-only; no cross-process
        concern).
        """
        if now is None:
            now = datetime.now(timezone.utc).isoformat()
        expires_at = (
            datetime.fromisoformat(now) + timedelta(seconds=LEASE_DURATION_SECONDS)
        ).isoformat()
        token = str(uuid.uuid4())

        if self._path == ":memory:":
            row = self.conn.execute(
                "SELECT in_progress, lease_expires_at, owner_token FROM scan_state WHERE id = 1"
            ).fetchone()
            if row and row[0] and (row[1] is None or row[1] > now):
                return None
            # Block if an unleased scan is currently running.
            if self.conn.execute(
                "SELECT 1 FROM scans WHERE status = 'running' AND owner_token IS NULL"
            ).fetchone():
                return None
            # Clean up any running scans from the expired owner atomically.
            if row and row[2]:
                self._abandon_running_scans(self.conn, row[2], now)
            self.conn.execute(
                "UPDATE scan_state SET in_progress = 1, scan_error = NULL, "
                "owner_token = ?, lease_expires_at = ? WHERE id = 1",
                (token, expires_at),
            )
            self.conn.commit()
            return token

        # File-based DB: dedicated connection with explicit transaction control.
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT in_progress, lease_expires_at, owner_token FROM scan_state WHERE id = 1"
            ).fetchone()
            if row and row[0] and (row[1] is None or row[1] > now):
                conn.execute("ROLLBACK")
                return None
            # Block if an unleased scan is currently running.
            if conn.execute(
                "SELECT 1 FROM scans WHERE status = 'running' AND owner_token IS NULL"
            ).fetchone():
                conn.execute("ROLLBACK")
                return None
            # Clean up any running scans from the expired owner atomically.
            if row and row[2]:
                self._abandon_running_scans(conn, row[2], now)
            conn.execute(
                "UPDATE scan_state SET in_progress = 1, scan_error = NULL, "
                "owner_token = ?, lease_expires_at = ? WHERE id = 1",
                (token, expires_at),
            )
            conn.execute("COMMIT")
            return token
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _abandon_running_scans(
        conn: sqlite3.Connection, expired_token: str, finished_at: str
    ) -> None:
        """Mark running scans from an expired owner as failed and delete their data.

        Called within the caller's transaction — does NOT commit.
        """
        rows = conn.execute(
            "SELECT id FROM scans WHERE status = 'running' AND owner_token = ?",
            (expired_token,),
        ).fetchall()
        for (scan_id,) in rows:
            conn.execute("DELETE FROM audit_results WHERE scan_id = ?", (scan_id,))
            conn.execute("DELETE FROM documents WHERE scan_id = ?", (scan_id,))
            conn.execute(
                "UPDATE scans SET status = 'failed', "
                "error = 'abandoned: lease expired', finished_at = ? WHERE id = ?",
                (finished_at, scan_id),
            )

    def renew_lease(self, owner_token: str, now: str | None = None) -> bool:
        """Extend the lease expiry by LEASE_DURATION_SECONDS for the given owner.

        Returns True if the lease was renewed.  Returns False when:
        - the token doesn't match the current owner, or
        - the scan is no longer in progress, or
        - the lease has already expired (an expired lease is never resurrected).

        A live lease satisfies: in_progress=1, owner_token matches, and
        lease_expires_at is strictly later than *now*.
        """
        if now is None:
            now = datetime.now(timezone.utc).isoformat()
        expires_at = (
            datetime.fromisoformat(now) + timedelta(seconds=LEASE_DURATION_SECONDS)
        ).isoformat()
        cursor = self.conn.execute(
            "UPDATE scan_state SET lease_expires_at = ? "
            "WHERE id = 1 AND in_progress = 1 AND owner_token = ? "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (expires_at, owner_token, now),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def owns_live_lease(self, owner_token: str, now: str | None = None) -> bool:
        """Return True only when this worker holds a live, unexpired lease.

        A lease is live when in_progress=1, owner_token matches, and
        lease_expires_at is not null and strictly later than *now*.
        Time is injectable so tests can use deterministic timestamps.
        """
        if now is None:
            now = datetime.now(timezone.utc).isoformat()
        row = self.conn.execute(
            "SELECT 1 FROM scan_state WHERE id = 1 AND in_progress = 1 "
            "AND owner_token = ? AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (owner_token, now),
        ).fetchone()
        return row is not None

    @contextmanager
    def _leased_write(
        self, owner_token: str, now: str | None = None
    ) -> Generator[sqlite3.Connection, None, None]:
        """Atomic lease-verified write transaction.

        Yields the connection to use for mutations.  Commits on clean exit;
        rolls back and raises LeaseLostError if the lease has been lost.

        For :memory: databases (tests): uses the shared connection directly —
        single-connection, so BEGIN IMMEDIATE is unnecessary.

        For file-based databases: opens a dedicated connection with
        BEGIN IMMEDIATE so the lease check and mutation are atomic across
        OS processes.
        """
        if now is None:
            now = datetime.now(timezone.utc).isoformat()

        if self._path == ":memory:":
            row = self.conn.execute(_LEASE_GUARD_SQL, (owner_token, now)).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Lease not held or expired for token {owner_token!r}"
                )
            try:
                yield self.conn
                self.conn.commit()
            except LeaseLostError:
                raise
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            return

        # File-based: dedicated connection with explicit transaction control.
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(_LEASE_GUARD_SQL, (owner_token, now)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise LeaseLostError(
                    f"Lease not held or expired for token {owner_token!r}"
                )
            try:
                yield conn
                conn.execute("COMMIT")
            except LeaseLostError:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()

    @contextmanager
    def _unleased_write(self) -> Generator[sqlite3.Connection, None, None]:
        """Atomic write transaction for unleased (CLI) mutations.

        Guarantees that the scan-ownership check and the subsequent mutation
        execute in the same transaction so no status change can slip between
        them.

        For :memory: databases: yields self.conn directly and commits on
        clean exit; rolls back on any exception.

        For file-backed databases: opens a dedicated connection with
        BEGIN IMMEDIATE so the check and mutation are atomic even if another
        OS process is present (unlikely for CLI, but defence-in-depth).
        """
        if self._path == ":memory:":
            try:
                yield self.conn
                self.conn.commit()
            except LeaseLostError:
                raise
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            return

        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except LeaseLostError:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()

    def end_scan(
        self,
        owner_token: str,
        last_scan_id: int | None,
        error: str | None,
        now: str | None = None,
    ) -> bool:
        """Release the lease and record scan completion.

        Returns True on success.  Returns False when:
        - the owner_token doesn't match the current owner, or
        - the lease has expired (an expired worker must not clobber newer state).

        The conditional UPDATE is expiry-fenced: an expired owner cannot update
        in_progress, last_scan_id, scan_error, owner_token, or lease_expires_at
        even if its token still happens to match.
        """
        if now is None:
            now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "UPDATE scan_state SET in_progress = 0, owner_token = NULL, "
            "lease_expires_at = NULL, last_scan_id = ?, scan_error = ? "
            "WHERE id = 1 AND owner_token = ? "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
            (last_scan_id, error, owner_token, now),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_scan_state(self, now: str | None = None) -> dict:
        """Return {in_progress, last_scan_id, scan_error} from persistent storage.

        An expired lease (lease_expires_at <= now) is reported as in_progress=False
        without writing to the DB.
        """
        if now is None:
            now = datetime.now(timezone.utc).isoformat()
        row = self.conn.execute(
            "SELECT in_progress, last_scan_id, scan_error, lease_expires_at "
            "FROM scan_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return {"in_progress": False, "last_scan_id": None, "scan_error": None}
        in_progress = bool(row[0])
        lease_expires_at = row[3]
        if in_progress and lease_expires_at is not None and lease_expires_at <= now:
            in_progress = False
        return {
            "in_progress": in_progress,
            "last_scan_id": row[1],
            "scan_error": row[2],
        }

    def reset_scan_state(self) -> None:
        """Unconditionally clear scan state — for admin use (clear_scans endpoint)."""
        self.conn.execute(
            "UPDATE scan_state SET in_progress = 0, owner_token = NULL, "
            "lease_expires_at = NULL, last_scan_id = NULL, scan_error = NULL WHERE id = 1"
        )
        self.conn.commit()

    def clear_all(self) -> None:
        """Delete all scans, documents, results, and workflow findings."""
        self.conn.execute("DELETE FROM audit_results")
        self.conn.execute("DELETE FROM documents")
        self.conn.execute("DELETE FROM scans")
        self.conn.execute("DELETE FROM sqlite_sequence WHERE name='scans'")
        # finding_workflow may not exist on very old databases that haven't
        # run migrations yet, so handle gracefully.
        try:
            self.conn.execute("DELETE FROM finding_workflow")
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet
        self.conn.commit()

    def _execute_clear(self, conn: sqlite3.Connection) -> None:
        """Execute all deletion and scan-state reset SQL on *conn*. Does NOT commit."""
        conn.execute("DELETE FROM audit_results")
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='scans'")
        try:
            conn.execute("DELETE FROM finding_workflow")
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet
        conn.execute(
            "UPDATE scan_state SET in_progress = 0, owner_token = NULL, "
            "lease_expires_at = NULL, last_scan_id = NULL, scan_error = NULL WHERE id = 1"
        )

    def clear_all_if_idle(self, now: str | None = None) -> bool:
        """Atomically check for a live lease and, if absent, clear all scan data.

        Returns True when clearing succeeds (idle or expired lease).
        Returns False when a live, unexpired lease is present (scan in progress).
        An expired lease is treated as idle and does not block clearing.
        """
        if now is None:
            now = datetime.now(timezone.utc).isoformat()

        if self._path == ":memory:":
            row = self.conn.execute(
                "SELECT in_progress, lease_expires_at FROM scan_state WHERE id = 1"
            ).fetchone()
            if row and row[0] and (row[1] is None or row[1] > now):
                return False
            try:
                self._execute_clear(self.conn)
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            return True

        # File-based: dedicated connection with explicit transaction control.
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT in_progress, lease_expires_at FROM scan_state WHERE id = 1"
            ).fetchone()
            if row and row[0] and (row[1] is None or row[1] > now):
                conn.execute("ROLLBACK")
                return False
            self._execute_clear(conn)
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def start_scan(
        self, owner_token: str | None = None, now: str | None = None
    ) -> int:
        started_at = datetime.now(timezone.utc).isoformat()
        _sql = (
            "INSERT INTO scans (started_at, status, owner_token) VALUES (?, 'running', ?)"
        )
        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                cursor = conn.execute(_sql, (started_at, owner_token))
                return cursor.lastrowid  # type: ignore[return-value]
        # Unleased path: reject atomically when a live lease is held.
        check_now = now or datetime.now(timezone.utc).isoformat()
        with self._unleased_write() as conn:
            row = conn.execute(
                "SELECT 1 FROM scan_state WHERE id = 1 AND in_progress = 1 "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (check_now,),
            ).fetchone()
            if row is not None:
                raise LeaseLostError(
                    "Cannot start an unleased scan while a live lease is held by another worker"
                )
            cursor = conn.execute(_sql, (started_at, None))
            return cursor.lastrowid  # type: ignore[return-value]

    def finish_scan(
        self,
        scan_id: int,
        document_count: int,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None:
        finished_at = datetime.now(timezone.utc).isoformat()
        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                _assert_scan_running(conn, scan_id, owner_token)
                conn.execute(
                    "UPDATE scans SET status = 'completed', finished_at = ?, "
                    "document_count = ? WHERE id = ?",
                    (finished_at, document_count, scan_id),
                )
            return
        with self._unleased_write() as conn:
            _assert_scan_running(conn, scan_id, None)
            conn.execute(
                "UPDATE scans SET status = 'completed', finished_at = ?, "
                "document_count = ? WHERE id = ?",
                (finished_at, document_count, scan_id),
            )

    def fail_scan(
        self,
        scan_id: int,
        error: str | None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Mark a scan as failed and remove its partial documents and results.

        For leased scans: verifies both the global lease and that the scan
        belongs to *owner_token* and is still in running state.  Raises
        LeaseLostError if ownership has been lost (caller should not retry;
        the replacement worker cleans up via takeover).

        For unleased (CLI) scans: unconditionally cleans up and marks failed.

        Returns True on success.
        """
        finished_at = datetime.now(timezone.utc).isoformat()
        sanitized = _sanitize_error(error)

        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                _assert_scan_running(conn, scan_id, owner_token)
                conn.execute("DELETE FROM audit_results WHERE scan_id = ?", (scan_id,))
                conn.execute("DELETE FROM documents WHERE scan_id = ?", (scan_id,))
                conn.execute(
                    "UPDATE scans SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                    (sanitized, finished_at, scan_id),
                )
            return True

        with self._unleased_write() as conn:
            _assert_scan_running(conn, scan_id, None)
            conn.execute("DELETE FROM audit_results WHERE scan_id = ?", (scan_id,))
            conn.execute("DELETE FROM documents WHERE scan_id = ?", (scan_id,))
            conn.execute(
                "UPDATE scans SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (sanitized, finished_at, scan_id),
            )
        return True

    def complete_scan_with_findings(
        self,
        scan_id: int,
        document_count: int,
        results: list[AuditResult],
        scanned_doc_ids: set[str] | None = None,
        reanalyzed_doc_ids: set[str] | None = None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> dict:
        """Atomically sync workflow findings and mark the scan completed.

        Combines sync_findings and finish_scan in a single transaction so
        that workflow entries are never visible for an incomplete scan.

        Returns the sync stats dict: {new, updated, reopened, auto_fixed}.
        """
        finished_at = datetime.now(timezone.utc).isoformat()

        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                _assert_scan_running(conn, scan_id, owner_token)
                stats = self._do_sync_findings(
                    conn, scan_id, results, scanned_doc_ids, reanalyzed_doc_ids
                )
                conn.execute(
                    "UPDATE scans SET status = 'completed', finished_at = ?, "
                    "document_count = ? WHERE id = ?",
                    (finished_at, document_count, scan_id),
                )
            return stats

        with self._unleased_write() as conn:
            _assert_scan_running(conn, scan_id, None)
            stats = self._do_sync_findings(
                conn, scan_id, results, scanned_doc_ids, reanalyzed_doc_ids
            )
            conn.execute(
                "UPDATE scans SET status = 'completed', finished_at = ?, "
                "document_count = ? WHERE id = ?",
                (finished_at, document_count, scan_id),
            )
        return stats

    def store_document(
        self,
        scan_id: int,
        doc: Document,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None:
        _sql = """INSERT OR REPLACE INTO documents
               (id, scan_id, title, content_hash, source_type, url, last_modified, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        _params = (
            doc.id,
            scan_id,
            doc.title,
            doc.content_hash,
            doc.source_type,
            doc.url,
            doc.last_modified.isoformat() if doc.last_modified else None,
            serialize_document_metadata(doc.metadata),
        )
        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                _assert_scan_running(conn, scan_id, owner_token)
                conn.execute(_sql, _params)
            return
        with self._unleased_write() as conn:
            _assert_scan_running(conn, scan_id, None)
            conn.execute(_sql, _params)

    def store_result(
        self,
        scan_id: int,
        result: AuditResult,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None:
        signals_json = serialize_signals(result.signals)
        trust_data_json = serialize_trust_data(result.trust_metadata, result.trust_evidence)
        _sql = """INSERT OR REPLACE INTO audit_results
               (document_id, scan_id, overall_status, signals, suggested_replacement_id,
                confidence, confidence_reason, trust_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        _params = (
            result.document.id,
            scan_id,
            result.overall_status,
            signals_json,
            result.suggested_replacement.id if result.suggested_replacement else None,
            result.confidence,
            result.confidence_reason,
            trust_data_json,
        )
        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                _assert_scan_running(conn, scan_id, owner_token)
                conn.execute(_sql, _params)
            return
        with self._unleased_write() as conn:
            _assert_scan_running(conn, scan_id, None)
            conn.execute(_sql, _params)

    def get_previous_hashes(self) -> dict[str, str]:
        """Return doc_id -> content_hash from the most recent completed scan."""
        row = self.conn.execute(
            "SELECT id FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {}
        scan_id = row[0]
        rows = self.conn.execute(
            "SELECT id, content_hash FROM documents WHERE scan_id = ?",
            (scan_id,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def carry_forward_results(
        self,
        scan_id: int,
        doc_ids: list[str],
        owner_token: str | None = None,
        now: str | None = None,
    ) -> int:
        """Copy audit results from the previous scan for unchanged documents."""
        if not doc_ids:
            return 0
        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                _assert_scan_running(conn, scan_id, owner_token)
                return self._do_carry_forward(conn, scan_id, doc_ids)
        with self._unleased_write() as conn:
            _assert_scan_running(conn, scan_id, None)
            return self._do_carry_forward(conn, scan_id, doc_ids)

    def _do_carry_forward(
        self, conn: sqlite3.Connection, scan_id: int, doc_ids: list[str]
    ) -> int:
        row = conn.execute(
            "SELECT id FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return 0
        prev_scan_id = row[0]
        placeholders = ",".join("?" for _ in doc_ids)
        cursor = conn.execute(
            f"""INSERT OR REPLACE INTO audit_results
                (document_id, scan_id, overall_status, signals,
                 suggested_replacement_id, confidence, confidence_reason,
                 trust_data)
                SELECT document_id, ?, overall_status, signals,
                       suggested_replacement_id, confidence, confidence_reason,
                       trust_data
                FROM audit_results
                WHERE scan_id = ? AND document_id IN ({placeholders})""",
            (scan_id, prev_scan_id, *doc_ids),
        )
        return cursor.rowcount

    def load_audit_results(self, scan_id: int, doc_ids: list[str]) -> list[AuditResult]:
        """Reconstruct AuditResult objects from stored audit_results for workflow sync.

        Returns lightweight AuditResult objects with enough fidelity for
        finding_key and evidence_hash computation.  Used to include
        carried-forward results in workflow sync without re-analyzing them.
        """
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        rows = self.conn.execute(
            f"""SELECT d.id, d.title, d.source_type, d.url,
                       ar.overall_status, ar.signals, ar.confidence,
                       ar.confidence_reason, ar.trust_data
                FROM audit_results ar
                JOIN documents d ON d.id = ar.document_id AND d.scan_id = ar.scan_id
                WHERE ar.scan_id = ? AND ar.document_id IN ({placeholders})""",
            (scan_id, *doc_ids),
        ).fetchall()
        results: list[AuditResult] = []
        for r in rows:
            doc = Document(
                id=r[0], title=r[1], content="", source_type=r[2], url=r[3],
            )
            signals = deserialize_signals(r[5])
            trust_metadata, trust_evidence = deserialize_trust_data(r[8])
            results.append(AuditResult(
                document=doc,
                signals=signals,
                status=r[4],
                confidence=r[6],
                confidence_reason=r[7],
                trust_evidence=trust_evidence,
                trust_metadata=trust_metadata,
            ))
        return results

    def get_scan_results(self, scan_id: int) -> list[dict]:
        """Return full audit results for a specific scan."""
        rows = self.conn.execute(
            """SELECT d.id, d.title, d.url, d.source_type, d.last_modified,
                      ar.overall_status, ar.signals, ar.suggested_replacement_id,
                      ar.confidence, ar.confidence_reason, ar.trust_data
               FROM audit_results ar
               JOIN documents d ON d.id = ar.document_id AND d.scan_id = ar.scan_id
               JOIN scans s ON s.id = ar.scan_id AND s.status = 'completed'
               WHERE ar.scan_id = ?
               ORDER BY
                   CASE ar.overall_status
                       WHEN 'stale' THEN 0
                       WHEN 'needs_review' THEN 1
                       WHEN 'unknown' THEN 2
                       ELSE 3
                   END,
                   ar.confidence ASC""",
            (scan_id,),
        ).fetchall()
        results = []
        for r in rows:
            trust_metadata, trust_evidence = deserialize_trust_data(r[10])
            result = {
                "id": r[0],
                "title": r[1],
                "url": r[2],
                "source_type": r[3],
                "last_modified": r[4],
                "overall_status": r[5],
                "signals": deserialize_signal_records(r[6]),
                "suggested_replacement_id": r[7],
                "confidence": r[8],
                "confidence_reason": r[9],
                "trust_metadata": trust_metadata,
                "trust_evidence": trust_evidence,
            }
            results.append(result)
        return results

    def prune_scans(
        self, keep: int = 10, owner_token: str | None = None, now: str | None = None
    ) -> int:
        """Delete terminal scans beyond the most recent `keep`, returning count removed.

        Running scans are never pruned; only completed and failed scans count
        toward the retention window and are eligible for deletion.
        """
        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                return self._do_prune_scans(conn, keep)
        with self._unleased_write() as conn:
            return self._do_prune_scans(conn, keep)

    def _do_prune_scans(self, conn: sqlite3.Connection, keep: int) -> int:
        # Cutoff is calculated from terminal scans only — running scans are excluded.
        row = conn.execute(
            "SELECT id FROM scans WHERE status IN ('completed', 'failed') "
            "ORDER BY id DESC LIMIT 1 OFFSET ?",
            (keep - 1,),
        ).fetchone()
        if not row:
            return 0
        cutoff_id = row[0]
        # Only delete terminal scans; never touch running scans.
        conn.execute(
            "DELETE FROM audit_results WHERE scan_id IN "
            "(SELECT id FROM scans WHERE id < ? AND status IN ('completed', 'failed'))",
            (cutoff_id,),
        )
        conn.execute(
            "DELETE FROM documents WHERE scan_id IN "
            "(SELECT id FROM scans WHERE id < ? AND status IN ('completed', 'failed'))",
            (cutoff_id,),
        )
        cursor = conn.execute(
            "DELETE FROM scans WHERE id < ? AND status IN ('completed', 'failed')",
            (cutoff_id,),
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Finding workflow
    # ------------------------------------------------------------------

    def sync_findings(
        self,
        scan_id: int,
        results: list[AuditResult],
        scanned_doc_ids: set[str] | None = None,
        reanalyzed_doc_ids: set[str] | None = None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> dict:
        """Sync audit results into the finding_workflow table.

        For each actionable finding (stale / needs_review):
        - New finding → insert as 'open'.
        - Existing finding with same evidence_hash → update last_seen_scan_id.
        - Existing finding with *changed* evidence_hash and a terminal state
          (dismissed/fixed/accepted_risk) → reopen to 'open'.
        - Snoozed findings past their snoozed_until date → reopen to 'open'.

        When *scanned_doc_ids* is provided, prior findings for those documents
        that are no longer actionable are auto-resolved to 'fixed'.

        *reanalyzed_doc_ids* — IDs of documents that were actually fetched and
        analyzed in this scan (as opposed to carried forward unchanged).  Only
        findings for re-analyzed docs advance last_seen_scan_id; carried-forward
        findings only advance last_checked_scan_id.  When None, all docs are
        treated as re-analyzed (backwards-compatible default).

        **Exception — expired snooze:** A carried-forward finding whose snooze
        has expired is unconditionally reopened and its last_seen_scan_id IS
        advanced.  This is intentional: a snooze expiry is an operator-driven
        event and the finding re-enters the actionable queue; advancing
        last_seen_scan_id signals that the issue was confirmed still present in
        this scan (the carry-forward data is the evidence).

        *owner_token* — when provided, the entire sync runs inside a single
        lease-guarded transaction; LeaseLostError is raised if ownership has
        been taken over between the pre-check and this call.

        Returns a summary dict: {new, updated, reopened, auto_fixed}.
        """
        if owner_token is not None:
            with self._leased_write(owner_token, now) as conn:
                _assert_scan_running(conn, scan_id, owner_token)
                return self._do_sync_findings(
                    conn, scan_id, results, scanned_doc_ids, reanalyzed_doc_ids
                )
        with self._unleased_write() as conn:
            _assert_scan_running(conn, scan_id, None)
            return self._do_sync_findings(
                conn, scan_id, results, scanned_doc_ids, reanalyzed_doc_ids
            )

    def _do_sync_findings(
        self,
        conn: sqlite3.Connection,
        scan_id: int,
        results: list[AuditResult],
        scanned_doc_ids: set[str] | None,
        reanalyzed_doc_ids: set[str] | None,
    ) -> dict:
        """Inner sync logic — operates on *conn*, does NOT commit."""
        ts = datetime.now(timezone.utc).isoformat()
        stats = {"new": 0, "updated": 0, "reopened": 0, "auto_fixed": 0}

        actionable = self._actionable_results(results)

        seen_keys: set[str] = set()
        for result in actionable:
            key = result.finding_key
            if key in seen_keys:
                continue
            seen_keys.add(key)

            ev_hash = result.evidence_hash
            existing = conn.execute(
                "SELECT workflow_state, evidence_hash, snoozed_until "
                "FROM finding_workflow WHERE finding_key = ?",
                (key,),
            ).fetchone()

            if existing is None:
                # New finding
                conn.execute(
                    """INSERT INTO finding_workflow
                       (finding_key, document_id, source_type, title,
                        workflow_state, evidence_hash,
                        first_seen_scan_id, last_seen_scan_id,
                        last_checked_scan_id, updated_at)
                       VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
                    (key, result.document.id, result.document.source_type,
                     result.document.title, ev_hash, scan_id, scan_id,
                     scan_id, ts),
                )
                stats["new"] += 1
            else:
                old_state, old_hash, snoozed_until = existing

                should_reopen_snooze = (
                    old_state == "snoozed"
                    and snoozed_until
                    and snoozed_until <= ts
                )

                terminal = {"dismissed", "fixed", "accepted_risk"}
                should_reopen_evidence = (
                    old_state in terminal
                    and old_hash != ev_hash
                )

                is_reanalyzed = (
                    reanalyzed_doc_ids is None
                    or result.document.id in reanalyzed_doc_ids
                )

                if should_reopen_snooze or should_reopen_evidence:
                    conn.execute(
                        """UPDATE finding_workflow
                           SET workflow_state = 'open',
                               evidence_hash = ?,
                               title = ?,
                               last_seen_scan_id = ?,
                               last_checked_scan_id = ?,
                               snoozed_until = NULL,
                               updated_at = ?
                           WHERE finding_key = ?""",
                        (ev_hash, result.document.title, scan_id,
                         scan_id, ts, key),
                    )
                    stats["reopened"] += 1
                elif is_reanalyzed:
                    conn.execute(
                        """UPDATE finding_workflow
                           SET last_seen_scan_id = ?,
                               last_checked_scan_id = ?,
                               title = ?,
                               evidence_hash = ?,
                               updated_at = ?
                           WHERE finding_key = ?""",
                        (scan_id, scan_id, result.document.title, ev_hash,
                         ts, key),
                    )
                    stats["updated"] += 1
                else:
                    conn.execute(
                        """UPDATE finding_workflow
                           SET last_checked_scan_id = ?,
                               updated_at = ?
                           WHERE finding_key = ?""",
                        (scan_id, ts, key),
                    )
                    stats["updated"] += 1

        if scanned_doc_ids:
            auto_fixable = {"open", "acknowledged", "snoozed", "accepted_risk", "dismissed"}
            placeholders = ",".join("?" for _ in scanned_doc_ids)
            rows = conn.execute(
                f"""SELECT finding_key, workflow_state
                    FROM finding_workflow
                    WHERE document_id IN ({placeholders})
                      AND workflow_state IN ({",".join("?" for _ in auto_fixable)})""",
                (*scanned_doc_ids, *auto_fixable),
            ).fetchall()
            for fk, _state in rows:
                if fk not in seen_keys:
                    conn.execute(
                        """UPDATE finding_workflow
                           SET workflow_state = 'fixed',
                               last_checked_scan_id = ?,
                               note = ?,
                               updated_at = ?
                           WHERE finding_key = ?""",
                        (scan_id, f"No longer detected in scan #{scan_id}", ts, fk),
                    )
                    stats["auto_fixed"] += 1

            conn.execute(
                f"""UPDATE finding_workflow
                    SET last_checked_scan_id = ?
                    WHERE document_id IN ({placeholders})""",
                (scan_id, *scanned_doc_ids),
            )

        return stats

    # Fields to auto-clear when transitioning to a given state (caller-supplied
    # values for the same field always take precedence over cleanup).
    _STATE_CLEANUP: dict[str, dict[str, object]] = {
        "open":          {"snoozed_until": None, "dismissal_reason": None},
        "acknowledged":  {"snoozed_until": None, "dismissal_reason": None},
        "fixed":         {"snoozed_until": None},
        "dismissed":     {"snoozed_until": None},
        "accepted_risk": {"snoozed_until": None},
        # "snoozed" requires snoozed_until — validated below; nothing to clear.
    }

    def update_workflow(
        self,
        finding_key: str,
        *,
        state: WorkflowState | None | _UnsetType = _UNSET,
        note: str | None | _UnsetType = _UNSET,
        assigned_owner: str | None | _UnsetType = _UNSET,
        due_date: str | None | _UnsetType = _UNSET,
        snoozed_until: str | None | _UnsetType = _UNSET,
        dismissal_reason: str | None | _UnsetType = _UNSET,
    ) -> bool:
        """Update the workflow state of a finding.  Returns True if found.

        Two sentinel values govern how parameters are interpreted:

        - ``_UNSET`` (the default): the field was not supplied; leave it
          unchanged in storage.  Existing callers that omit keyword arguments
          get this behaviour automatically.
        - ``None``: the field was explicitly provided as null; clear it in
          storage.

        State-transition cleanup:
        - open / acknowledged: clears snoozed_until and dismissal_reason for
          any field the caller left as _UNSET.
        - fixed / dismissed / accepted_risk: clears snoozed_until if _UNSET.
        - snoozed: requires snoozed_until to be a non-null value; raises
          ValueError if it is _UNSET or None.

        Caller-supplied non-UNSET values always override transition cleanup.
        """
        if state == "snoozed" and snoozed_until in (_UNSET, None):
            raise ValueError(
                "Transitioning to 'snoozed' requires snoozed_until to be specified."
            )

        sets: list[str] = []
        params: list[object] = []

        if state is not _UNSET:
            sets.append("workflow_state = ?")
            params.append(state)
            # Auto-clear stale metadata for this target state, but only for
            # fields the caller left as _UNSET (i.e. did not explicitly supply).
            for field, null_val in self._STATE_CLEANUP.get(state, {}).items():  # type: ignore[arg-type]
                caller_val = locals()[field]
                if caller_val is _UNSET:
                    sets.append(f"{field} = ?")
                    params.append(null_val)

        # For each metadata field: add to SET only when the caller supplied a
        # value (including None to explicitly clear the column).
        if note is not _UNSET:
            sets.append("note = ?")
            params.append(note)
        if assigned_owner is not _UNSET:
            sets.append("assigned_owner = ?")
            params.append(assigned_owner)
        if due_date is not _UNSET:
            sets.append("due_date = ?")
            params.append(due_date)
        if snoozed_until is not _UNSET:
            sets.append("snoozed_until = ?")
            params.append(snoozed_until)
        if dismissal_reason is not _UNSET:
            sets.append("dismissal_reason = ?")
            params.append(dismissal_reason)

        if not sets:
            return False

        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(finding_key)

        cursor = self.conn.execute(
            f"UPDATE finding_workflow SET {', '.join(sets)} WHERE finding_key = ?",
            params,
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # States that represent completed/non-actionable work.
    _TERMINAL_STATES = {"fixed", "dismissed", "accepted_risk"}

    def get_findings(
        self,
        *,
        scan_id: int | None = None,
        states: list[str] | None = None,
        include_all: bool = False,
    ) -> list[dict]:
        """Return findings, optionally filtered by scan and/or workflow state.

        Default (actionable-only) behaviour:
        - Excludes terminal states (fixed, dismissed, accepted_risk).
        - Excludes snoozed findings whose snoozed_until is in the future.
        - Includes open, acknowledged, and expired-snoozed findings.

        When *states* is provided the state filter is applied exactly,
        but future-snoozed findings are still hidden unless include_all
        is True.

        When *include_all* is True no state or snooze filtering is applied
        (useful for audit/history views and result enrichment).
        """
        clauses: list[str] = []
        params: list[object] = []

        if scan_id is not None:
            clauses.append("last_checked_scan_id = ?")
            params.append(scan_id)

        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"workflow_state IN ({placeholders})")
            params.extend(states)
        elif not include_all:
            # Default: exclude terminal states
            terminal_placeholders = ",".join("?" for _ in self._TERMINAL_STATES)
            clauses.append(f"workflow_state NOT IN ({terminal_placeholders})")
            params.extend(sorted(self._TERMINAL_STATES))

        if not include_all:
            now = datetime.now(timezone.utc).isoformat()
            clauses.append(
                "(workflow_state != 'snoozed' OR snoozed_until IS NULL OR snoozed_until <= ?)"
            )
            params.append(now)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = self.conn.execute(
            f"""SELECT finding_key, document_id, source_type, title,
                       workflow_state, note, assigned_owner, due_date,
                       snoozed_until, dismissal_reason, evidence_hash,
                       first_seen_scan_id, last_seen_scan_id, updated_at,
                       last_checked_scan_id
                FROM finding_workflow {where}
                ORDER BY
                    CASE workflow_state
                        WHEN 'open' THEN 0
                        WHEN 'acknowledged' THEN 1
                        WHEN 'snoozed' THEN 2
                        WHEN 'accepted_risk' THEN 3
                        WHEN 'dismissed' THEN 4
                        WHEN 'fixed' THEN 5
                    END,
                    updated_at DESC""",
            params,
        ).fetchall()

        findings = [
            {
                "finding_key": r[0],
                "document_id": r[1],
                "source_type": r[2],
                "title": r[3],
                "workflow_state": r[4],
                "note": r[5],
                "assigned_owner": r[6],
                "due_date": r[7],
                "snoozed_until": r[8],
                "dismissal_reason": r[9],
                "evidence_hash": r[10],
                "first_seen_scan_id": r[11],
                "last_seen_scan_id": r[12],
                "updated_at": r[13],
                "last_checked_scan_id": r[14],
            }
            for r in rows
        ]
        return [self._enrich_finding_with_audit_context(f) for f in findings]

    def get_finding(self, finding_key: str) -> dict | None:
        """Return a single finding by key, or None.

        Always returns the finding regardless of snooze state — callers
        asking for a specific key should get it.
        """
        row = self.conn.execute(
            """SELECT finding_key, document_id, source_type, title,
                      workflow_state, note, assigned_owner, due_date,
                      snoozed_until, dismissal_reason, evidence_hash,
                      first_seen_scan_id, last_seen_scan_id, updated_at,
                      last_checked_scan_id
               FROM finding_workflow WHERE finding_key = ?""",
            (finding_key,),
        ).fetchone()
        if row is None:
            return None
        finding = {
            "finding_key": row[0],
            "document_id": row[1],
            "source_type": row[2],
            "title": row[3],
            "workflow_state": row[4],
            "note": row[5],
            "assigned_owner": row[6],
            "due_date": row[7],
            "snoozed_until": row[8],
            "dismissal_reason": row[9],
            "evidence_hash": row[10],
            "first_seen_scan_id": row[11],
            "last_seen_scan_id": row[12],
            "updated_at": row[13],
            "last_checked_scan_id": row[14],
        }
        return self._enrich_finding_with_audit_context(finding)

    def _enrich_finding_with_audit_context(self, finding: dict) -> dict:
        """Join audit_results + documents data for a finding's last_seen scan."""
        scan_id = finding.get("last_seen_scan_id")
        doc_id = finding.get("document_id")
        if scan_id is None or doc_id is None:
            finding["audit_context"] = None
            return finding

        row = self.conn.execute(
            """SELECT ar.overall_status, ar.confidence, ar.confidence_reason,
                      ar.signals, ar.trust_data, d.url
               FROM audit_results ar
               JOIN documents d ON d.id = ar.document_id AND d.scan_id = ar.scan_id
               WHERE ar.scan_id = ? AND ar.document_id = ?""",
            (scan_id, doc_id),
        ).fetchone()
        if row is None:
            finding["audit_context"] = None
            return finding

        trust_metadata, trust_evidence = deserialize_trust_data(row[4])
        finding["audit_context"] = {
            "overall_status": row[0],
            "confidence": row[1],
            "confidence_reason": row[2],
            "signals": deserialize_signal_records(row[3]),
            "url": row[5],
            "trust_metadata": trust_metadata,
            "trust_evidence": trust_evidence,
        }
        return finding

    def get_workflow_summary(
        self,
        scan_id: int | None = None,
        include_all: bool = False,
    ) -> dict:
        """Return counts by workflow state.

        Default (actionable-only): excludes terminal states and
        future-snoozed findings.  Set *include_all=True* for full counts.
        """
        clauses: list[str] = []
        params: list[object] = []
        if scan_id is not None:
            clauses.append("last_checked_scan_id = ?")
            params.append(scan_id)

        if not include_all:
            terminal_placeholders = ",".join("?" for _ in self._TERMINAL_STATES)
            clauses.append(f"workflow_state NOT IN ({terminal_placeholders})")
            params.extend(sorted(self._TERMINAL_STATES))
            now = datetime.now(timezone.utc).isoformat()
            clauses.append(
                "(workflow_state != 'snoozed' OR snoozed_until IS NULL OR snoozed_until <= ?)"
            )
            params.append(now)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT workflow_state, COUNT(*) FROM finding_workflow {where} GROUP BY workflow_state",
            params,
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    @staticmethod
    def _actionable_results(results: list[AuditResult]) -> list[AuditResult]:
        """Filter results to those that need workflow tracking.

        When a result carries an explicit ``requires_human_audit`` flag in
        ``trust_metadata`` (set by the actionability layer), that flag governs
        whether the result gets a workflow finding.

        Backward compatibility: results without the flag fall back to the
        original behaviour — stale, needs_review, and unknown all get findings.
        """
        actionable = []
        for r in results:
            if r.status == "current":
                continue
            flag = r.trust_metadata.get("requires_human_audit")
            if flag is None:
                # Legacy: no explicit flag → old status-based rule
                if r.status in ("stale", "needs_review", "unknown"):
                    actionable.append(r)
            elif flag:
                actionable.append(r)
            # else: explicitly not actionable → skip
        return actionable

    def get_scan_diff(self, scan_id: int, prev_scan_id: int) -> list[dict]:
        """Compare two scans and return status changes."""
        rows = self.conn.execute(
            """SELECT cur.document_id,
                      d.title,
                      prev.overall_status AS old_status,
                      cur.overall_status AS new_status
               FROM audit_results cur
               LEFT JOIN audit_results prev
                   ON prev.document_id = cur.document_id AND prev.scan_id = ?
               JOIN documents d
                   ON d.id = cur.document_id AND d.scan_id = cur.scan_id
               WHERE cur.scan_id = ?
                 AND (prev.overall_status IS NULL
                      OR prev.overall_status != cur.overall_status)""",
            (prev_scan_id, scan_id),
        ).fetchall()
        return [
            {
                "document_id": r[0],
                "title": r[1],
                "old_status": r[2],
                "new_status": r[3],
            }
            for r in rows
        ]

    def get_scan_history(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.id, s.started_at, s.finished_at, s.document_count,
                      COUNT(CASE WHEN ar.overall_status = 'stale' THEN 1 END) as stale,
                      COUNT(CASE WHEN ar.overall_status = 'needs_review' THEN 1 END) as needs_review,
                      COUNT(CASE WHEN ar.overall_status = 'unknown' THEN 1 END) as unknown
               FROM scans s
               LEFT JOIN audit_results ar ON s.id = ar.scan_id
               WHERE s.status = 'completed'
               GROUP BY s.id
               ORDER BY s.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        scans = [
            {
                "scan_id": r[0],
                "started_at": r[1],
                "finished_at": r[2],
                "document_count": r[3],
                "stale_count": r[4],
                "needs_review_count": r[5],
                "unknown_count": r[6],
            }
            for r in rows
        ]

        # Compute change summaries for consecutive scan pairs
        for i, scan in enumerate(scans):
            if i + 1 < len(scans):
                prev = scans[i + 1]
                changes = self.get_scan_diff(scan["scan_id"], prev["scan_id"])
                scan["changes"] = _summarize_changes(changes)
            else:
                scan["changes"] = None  # first scan, no previous

        return scans
