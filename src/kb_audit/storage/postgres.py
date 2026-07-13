"""PostgreSQL storage backend for kb_audit.

Status
------
- ``create_storage()`` returns ``PostgresStorage`` for ``postgres://`` and
  ``postgresql://`` URLs (Step 10).
- ``PostgresStorage`` has opt-in live conformance tests (Step 8) via
  ``KB_AUDIT_POSTGRES_TEST_URL``; it has not been validated in CI.
- An Alembic migration layer was added in Step 9
  (``migrations/versions/0001_initial_schema.py``).  Migrations are
  applied manually with ``alembic upgrade head``; they are **not** run
  automatically by ``connect()``.

Implemented (Steps 3–7):
- Connection lifecycle (``connect`` / ``close`` / ``conn`` / ``is_connected``)
- Scan lease: ``try_start_scan``, ``renew_lease``, ``owns_live_lease``,
  ``end_scan``, ``reset_scan_state``, ``get_scan_state``
- Scan lifecycle: ``start_scan``, ``finish_scan``, ``fail_scan``
- Document/result persistence: ``store_document``, ``store_result``,
  ``get_previous_hashes``, ``carry_forward_results``,
  ``load_audit_results``, ``get_scan_results``
- Workflow persistence: ``complete_scan_with_findings``, ``sync_findings``,
  ``update_workflow``, ``get_findings``, ``get_finding``,
  ``get_workflow_summary``
- Scan history/diff: ``get_scan_history``, ``get_scan_diff``
- Maintenance: ``clear_all``, ``clear_all_if_idle``, ``prune_scans``

``connect()`` applies schema DDL directly via
``iter_postgres_schema_statements()``, independent of Alembic.

Import safety
-------------
``import kb_audit.storage.postgres`` is safe even when psycopg is not
installed.  The ``psycopg`` driver is imported only inside ``connect()``,
after ``require_psycopg()`` has confirmed availability.

JSONB serialization
-------------------
``store_document`` and ``store_result`` serialize Python objects to JSON
strings using the helpers in ``storage.serialization``, then pass them to
PostgreSQL with ``%s::jsonb`` casts.  This avoids psycopg3 JSON adapter
imports and keeps the serialization path identical to the SQLite backend.
``load_audit_results`` and ``get_scan_results`` cast JSONB columns back to
``text`` (``signals::text``, ``trust_data::text``) so the same string-based
deserializers can be reused unchanged.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from kb_audit.models import AuditResult, Document, WorkflowState
from kb_audit.storage.postgres_support import require_psycopg
from kb_audit.storage.schema_postgres import iter_postgres_schema_statements
from kb_audit.storage.serialization import (
    deserialize_signal_records,
    deserialize_signals,
    deserialize_trust_data,
    sanitize_error,
    serialize_document_metadata,
    serialize_signals,
    serialize_trust_data,
)
from kb_audit.storage.sqlite import (
    LEASE_DURATION_SECONDS,
    LeaseLostError,
    _UNSET,
    _UnsetType,
    _summarize_changes,
)


class PostgresStorage:
    """PostgreSQL storage backend — connection lifecycle and scan lease/lifecycle.

    Instantiating this class does not open a connection or require psycopg
    to be installed.  Call :meth:`connect` to open the connection and
    initialize the schema.

    Parameters
    ----------
    database_url:
        A ``postgresql://`` or ``postgres://`` connection URL accepted by
        ``psycopg.connect()``.
    """

    def __init__(self, database_url: str | os.PathLike[str]) -> None:
        self._database_url = str(database_url)
        self._conn: object | None = None  # psycopg.Connection typed as object to avoid import

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open a psycopg connection and initialize the database schema.

        Steps:
        1. Call ``require_psycopg()`` — raises ``RuntimeError`` if the driver
           is absent (with a clear install hint).
        2. Import psycopg and open a connection to ``self._database_url``.
        3. Execute every statement from ``iter_postgres_schema_statements()``
           in order inside a transaction, then commit.
        4. On any error during schema initialization, close the partial
           connection and re-raise.
        """
        require_psycopg()

        import psycopg  # noqa: PLC0415 — intentional deferred import

        conn = psycopg.connect(self._database_url)
        try:
            with conn.cursor() as cur:
                for statement in iter_postgres_schema_statements():
                    cur.execute(statement)
            conn.commit()
        except Exception:
            conn.close()
            raise

        self._conn = conn

    def close(self) -> None:
        """Close the connection if open.  Safe to call more than once."""
        if self._conn is not None:
            self._conn.close()  # type: ignore[union-attr]
            self._conn = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def conn(self) -> object:
        """Return the active psycopg connection.

        Raises
        ------
        RuntimeError
            If called before :meth:`connect`.
        """
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the connection is currently open."""
        return self._conn is not None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _now_dt(self, now: str | None) -> datetime:
        """Return a timezone-aware datetime from *now* (ISO string) or current UTC."""
        if now is None:
            return datetime.now(timezone.utc)
        dt = datetime.fromisoformat(now)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _expires_at(self, now_dt: datetime) -> datetime:
        """Return the lease-expiry datetime for a lease acquired at *now_dt*."""
        return now_dt + timedelta(seconds=LEASE_DURATION_SECONDS)

    @staticmethod
    def _abandon_running_scans(cur: object, expired_token: str, finished_at: datetime) -> None:
        """Mark running scans from *expired_token* as failed and delete their data.

        Called within a caller-owned transaction — does NOT commit.
        """
        cur.execute(  # type: ignore[union-attr]
            "SELECT id FROM scans WHERE status = 'running' AND owner_token = %s",
            (expired_token,),
        )
        rows = cur.fetchall()  # type: ignore[union-attr]
        for (scan_id,) in rows:
            cur.execute(  # type: ignore[union-attr]
                "DELETE FROM audit_results WHERE scan_id = %s",
                (scan_id,),
            )
            cur.execute(  # type: ignore[union-attr]
                "DELETE FROM documents WHERE scan_id = %s",
                (scan_id,),
            )
            cur.execute(  # type: ignore[union-attr]
                "UPDATE scans SET status = 'failed', "
                "error = 'abandoned: lease expired', finished_at = %s WHERE id = %s",
                (finished_at, scan_id),
            )

    @staticmethod
    def _assert_scan_running(cur: object, scan_id: int, owner_token: str | None) -> None:
        """Raise ``LeaseLostError`` if the scan is not running or owner mismatch."""
        if owner_token is not None:
            cur.execute(  # type: ignore[union-attr]
                "SELECT 1 FROM scans "
                "WHERE id = %s AND owner_token = %s AND status = 'running'",
                (scan_id, owner_token),
            )
        else:
            cur.execute(  # type: ignore[union-attr]
                "SELECT 1 FROM scans "
                "WHERE id = %s AND owner_token IS NULL AND status = 'running'",
                (scan_id,),
            )
        if cur.fetchone() is None:  # type: ignore[union-attr]
            raise LeaseLostError(
                f"Scan {scan_id} is not running or not owned by the current token"
            )

    def _check_lease(self, cur: object, owner_token: str, now_dt: datetime) -> None:
        """Raise ``LeaseLostError`` if the lease is no longer valid.

        Issues ``SELECT … FOR UPDATE`` to lock the singleton scan_state row
        within the caller's transaction.
        """
        cur.execute(  # type: ignore[union-attr]
            "SELECT 1 FROM scan_state "
            "WHERE id = 1 AND in_progress = TRUE AND owner_token = %s "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > %s FOR UPDATE",
            (owner_token, now_dt),
        )
        if cur.fetchone() is None:  # type: ignore[union-attr]
            raise LeaseLostError(
                f"Lease not held or expired for token {owner_token!r}"
            )

    # ------------------------------------------------------------------
    # Scan lease methods
    # ------------------------------------------------------------------

    def try_start_scan(self, now: str | None = None) -> str | None:
        """Atomically acquire the scan lease.

        Returns a UUID owner token on success, or ``None`` if a live lease exists.
        Uses ``SELECT … FOR UPDATE`` on the singleton scan_state row to prevent
        concurrent acquisition.

        Expired leases are taken over: running scans from the old owner are
        marked failed and their partial data is removed.
        """
        now_dt = self._now_dt(now)
        expires_at = self._expires_at(now_dt)
        token = str(uuid.uuid4())
        conn = self.conn

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(  # type: ignore[union-attr]
                    "SELECT in_progress, lease_expires_at, owner_token "
                    "FROM scan_state WHERE id = 1 FOR UPDATE"
                )
                row = cur.fetchone()  # type: ignore[union-attr]

                # Live lease: in_progress and expiry is in the future.
                if row and row[0] and (row[1] is None or row[1] > now_dt):
                    conn.rollback()  # type: ignore[union-attr]
                    return None

                # Block if an unleased scan is running (no concurrent owner).
                cur.execute(  # type: ignore[union-attr]
                    "SELECT 1 FROM scans WHERE status = 'running' AND owner_token IS NULL"
                )
                if cur.fetchone() is not None:  # type: ignore[union-attr]
                    conn.rollback()  # type: ignore[union-attr]
                    return None

                # Abandon running scans from the now-expired previous owner.
                if row and row[2]:
                    self._abandon_running_scans(cur, row[2], now_dt)

                cur.execute(  # type: ignore[union-attr]
                    "UPDATE scan_state "
                    "SET in_progress = TRUE, scan_error = NULL, "
                    "owner_token = %s, lease_expires_at = %s WHERE id = 1",
                    (token, expires_at),
                )
            conn.commit()  # type: ignore[union-attr]
            return token
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def renew_lease(self, owner_token: str, now: str | None = None) -> bool:
        """Extend the lease expiry by ``LEASE_DURATION_SECONDS`` for *owner_token*.

        Returns ``True`` if renewed, ``False`` if the token doesn't match, the
        scan is no longer in progress, or the lease has already expired.
        """
        now_dt = self._now_dt(now)
        expires_at = self._expires_at(now_dt)
        conn = self.conn

        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "UPDATE scan_state SET lease_expires_at = %s "
                "WHERE id = 1 AND in_progress = TRUE AND owner_token = %s "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > %s",
                (expires_at, owner_token, now_dt),
            )
            updated = cur.rowcount  # type: ignore[union-attr]
        conn.commit()  # type: ignore[union-attr]
        return updated > 0

    def owns_live_lease(self, owner_token: str, now: str | None = None) -> bool:
        """Return ``True`` only when this worker holds a live, unexpired lease."""
        now_dt = self._now_dt(now)
        conn = self.conn

        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT 1 FROM scan_state "
                "WHERE id = 1 AND in_progress = TRUE AND owner_token = %s "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > %s",
                (owner_token, now_dt),
            )
            row = cur.fetchone()  # type: ignore[union-attr]
        return row is not None

    def end_scan(
        self,
        owner_token: str,
        last_scan_id: int | None,
        error: str | None,
        now: str | None = None,
    ) -> bool:
        """Release the lease and record scan completion.

        Returns ``True`` on success.  Returns ``False`` when the token doesn't
        match the current owner or the lease has expired (expiry-fenced).
        """
        now_dt = self._now_dt(now)
        sanitized = sanitize_error(error)
        conn = self.conn

        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "UPDATE scan_state "
                "SET in_progress = FALSE, owner_token = NULL, lease_expires_at = NULL, "
                "last_scan_id = %s, scan_error = %s "
                "WHERE id = 1 AND owner_token = %s "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > %s",
                (last_scan_id, sanitized, owner_token, now_dt),
            )
            updated = cur.rowcount  # type: ignore[union-attr]
        conn.commit()  # type: ignore[union-attr]
        return updated > 0

    def get_scan_state(self, now: str | None = None) -> dict:
        """Return ``{in_progress, last_scan_id, scan_error}`` from the singleton row.

        An expired lease (``lease_expires_at <= now``) is reported as
        ``in_progress=False`` without writing to the database.
        """
        now_dt = self._now_dt(now)
        conn = self.conn

        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT in_progress, last_scan_id, scan_error, lease_expires_at "
                "FROM scan_state WHERE id = 1"
            )
            row = cur.fetchone()  # type: ignore[union-attr]

        if row is None:
            return {"in_progress": False, "last_scan_id": None, "scan_error": None}

        in_progress = bool(row[0])
        lease_expires_at = row[3]
        # Psycopg3 returns datetime objects for TIMESTAMPTZ columns.
        if in_progress and lease_expires_at is not None and lease_expires_at <= now_dt:
            in_progress = False

        return {
            "in_progress": in_progress,
            "last_scan_id": row[1],
            "scan_error": row[2],
        }

    def reset_scan_state(self) -> None:
        """Unconditionally clear scan state — for admin use."""
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "UPDATE scan_state "
                "SET in_progress = FALSE, owner_token = NULL, "
                "lease_expires_at = NULL, last_scan_id = NULL, scan_error = NULL "
                "WHERE id = 1"
            )
        conn.commit()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Scan lifecycle methods
    # ------------------------------------------------------------------

    def start_scan(
        self, owner_token: str | None = None, now: str | None = None
    ) -> int:
        """Insert a running scan and return its generated id.

        When *owner_token* is supplied the active lease is verified first.
        When used without an owner_token (unleased CLI path), rejects if a live
        lease is already held.
        """
        started_at = datetime.now(timezone.utc)
        now_dt = self._now_dt(now)
        conn = self.conn

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                    cur.execute(  # type: ignore[union-attr]
                        "INSERT INTO scans (started_at, status, owner_token) "
                        "VALUES (%s, 'running', %s) RETURNING id",
                        (started_at, owner_token),
                    )
                else:
                    # Unleased path: reject if a live lease is held.
                    cur.execute(  # type: ignore[union-attr]
                        "SELECT 1 FROM scan_state "
                        "WHERE id = 1 AND in_progress = TRUE "
                        "AND lease_expires_at IS NOT NULL AND lease_expires_at > %s "
                        "FOR UPDATE",
                        (now_dt,),
                    )
                    if cur.fetchone() is not None:  # type: ignore[union-attr]
                        raise LeaseLostError(
                            "Cannot start an unleased scan while a live lease is held"
                        )
                    cur.execute(  # type: ignore[union-attr]
                        "INSERT INTO scans (started_at, status, owner_token) "
                        "VALUES (%s, 'running', NULL) RETURNING id",
                        (started_at,),
                    )
                row = cur.fetchone()  # type: ignore[union-attr]
                scan_id: int = row[0]
            conn.commit()  # type: ignore[union-attr]
            return scan_id
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def finish_scan(
        self,
        scan_id: int,
        document_count: int,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None:
        """Transition a running scan to ``completed``."""
        finished_at = datetime.now(timezone.utc)
        now_dt = self._now_dt(now)
        conn = self.conn

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                self._assert_scan_running(cur, scan_id, owner_token)
                cur.execute(  # type: ignore[union-attr]
                    "UPDATE scans SET status = 'completed', finished_at = %s, "
                    "document_count = %s WHERE id = %s",
                    (finished_at, document_count, scan_id),
                )
            conn.commit()  # type: ignore[union-attr]
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def fail_scan(
        self,
        scan_id: int,
        error: str | None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Transition a running scan to ``failed``, storing a sanitized error.

        Removes partial documents and audit results for the scan.

        Returns ``True`` on success.  Raises ``LeaseLostError`` if the lease
        has been lost (caller should not retry).
        """
        finished_at = datetime.now(timezone.utc)
        now_dt = self._now_dt(now)
        sanitized = sanitize_error(error)
        conn = self.conn

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                self._assert_scan_running(cur, scan_id, owner_token)
                cur.execute(  # type: ignore[union-attr]
                    "DELETE FROM audit_results WHERE scan_id = %s", (scan_id,)
                )
                cur.execute(  # type: ignore[union-attr]
                    "DELETE FROM documents WHERE scan_id = %s", (scan_id,)
                )
                cur.execute(  # type: ignore[union-attr]
                    "UPDATE scans SET status = 'failed', error = %s, finished_at = %s "
                    "WHERE id = %s",
                    (sanitized, finished_at, scan_id),
                )
            conn.commit()  # type: ignore[union-attr]
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

        return True

    # ------------------------------------------------------------------
    # Document / result persistence
    # ------------------------------------------------------------------

    def store_document(
        self,
        scan_id: int,
        doc: Document,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None:
        """Upsert a document into the ``documents`` table.

        When *owner_token* is supplied the active lease is verified first.
        Uses ``INSERT … ON CONFLICT (id, scan_id) DO UPDATE`` for upsert.
        ``metadata`` is cast to JSONB at the SQL level via ``%s::jsonb``.
        """
        now_dt = self._now_dt(now)
        metadata_json = serialize_document_metadata(doc.metadata)
        conn = self.conn

        _sql = (
            "INSERT INTO documents "
            "(id, scan_id, title, content_hash, source_type, url, last_modified, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (id, scan_id) DO UPDATE SET "
            "title = EXCLUDED.title, "
            "content_hash = EXCLUDED.content_hash, "
            "source_type = EXCLUDED.source_type, "
            "url = EXCLUDED.url, "
            "last_modified = EXCLUDED.last_modified, "
            "metadata = EXCLUDED.metadata"
        )
        _params = (
            doc.id, scan_id, doc.title, doc.content_hash,
            doc.source_type, doc.url, doc.last_modified, metadata_json,
        )

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                self._assert_scan_running(cur, scan_id, owner_token)
                cur.execute(_sql, _params)  # type: ignore[union-attr]
            conn.commit()  # type: ignore[union-attr]
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def store_result(
        self,
        scan_id: int,
        result: AuditResult,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None:
        """Upsert an audit result into the ``audit_results`` table.

        When *owner_token* is supplied the active lease is verified first.
        Uses ``INSERT … ON CONFLICT (document_id, scan_id) DO UPDATE``.
        ``signals`` and ``trust_data`` are cast to JSONB at the SQL level.
        """
        now_dt = self._now_dt(now)
        signals_json = serialize_signals(result.signals)
        trust_data_json = serialize_trust_data(result.trust_metadata, result.trust_evidence)
        conn = self.conn

        _sql = (
            "INSERT INTO audit_results "
            "(document_id, scan_id, overall_status, signals, "
            "suggested_replacement_id, confidence, confidence_reason, trust_data) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (document_id, scan_id) DO UPDATE SET "
            "overall_status = EXCLUDED.overall_status, "
            "signals = EXCLUDED.signals, "
            "suggested_replacement_id = EXCLUDED.suggested_replacement_id, "
            "confidence = EXCLUDED.confidence, "
            "confidence_reason = EXCLUDED.confidence_reason, "
            "trust_data = EXCLUDED.trust_data"
        )
        _params = (
            result.document.id, scan_id, result.overall_status,
            signals_json,
            result.suggested_replacement.id if result.suggested_replacement else None,
            result.confidence, result.confidence_reason,
            trust_data_json,
        )

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                self._assert_scan_running(cur, scan_id, owner_token)
                cur.execute(_sql, _params)  # type: ignore[union-attr]
            conn.commit()  # type: ignore[union-attr]
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def get_previous_hashes(self) -> dict[str, str]:
        """Return ``{doc_id: content_hash}`` from the most recent completed scan."""
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT id FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()  # type: ignore[union-attr]
            if not row:
                return {}
            prev_scan_id = row[0]
            cur.execute(  # type: ignore[union-attr]
                "SELECT id, content_hash FROM documents WHERE scan_id = %s",
                (prev_scan_id,),
            )
            rows = cur.fetchall()  # type: ignore[union-attr]
        return {r[0]: r[1] for r in rows}

    def carry_forward_results(
        self,
        scan_id: int,
        doc_ids: list[str],
        owner_token: str | None = None,
        now: str | None = None,
    ) -> int:
        """Copy audit results from the most recent completed scan for *doc_ids*.

        Uses ``INSERT … SELECT … ON CONFLICT DO UPDATE`` with ``= ANY(%s)``
        to pass the document-id list as a Postgres array parameter.
        Returns the number of results carried forward.
        """
        if not doc_ids:
            return 0
        now_dt = self._now_dt(now)
        conn = self.conn

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                self._assert_scan_running(cur, scan_id, owner_token)
                cur.execute(  # type: ignore[union-attr]
                    "SELECT id FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()  # type: ignore[union-attr]
                if not row:
                    count = 0
                else:
                    prev_scan_id = row[0]
                    cur.execute(  # type: ignore[union-attr]
                        "INSERT INTO audit_results "
                        "(document_id, scan_id, overall_status, signals, "
                        "suggested_replacement_id, confidence, confidence_reason, trust_data) "
                        "SELECT document_id, %s, overall_status, signals, "
                        "suggested_replacement_id, confidence, confidence_reason, trust_data "
                        "FROM audit_results "
                        "WHERE scan_id = %s AND document_id = ANY(%s) "
                        "ON CONFLICT (document_id, scan_id) DO UPDATE SET "
                        "overall_status = EXCLUDED.overall_status, "
                        "signals = EXCLUDED.signals, "
                        "suggested_replacement_id = EXCLUDED.suggested_replacement_id, "
                        "confidence = EXCLUDED.confidence, "
                        "confidence_reason = EXCLUDED.confidence_reason, "
                        "trust_data = EXCLUDED.trust_data",
                        (scan_id, prev_scan_id, doc_ids),
                    )
                    count = cur.rowcount  # type: ignore[union-attr]
            conn.commit()  # type: ignore[union-attr]
            return count
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def load_audit_results(self, scan_id: int, doc_ids: list[str]) -> list[AuditResult]:
        """Reconstruct ``AuditResult`` objects from stored rows.

        Selects ``signals::text`` and ``trust_data::text`` so that the existing
        string-based deserialization helpers work without modification.
        Uses ``= ANY(%s)`` to pass *doc_ids* as a Postgres array parameter.
        """
        if not doc_ids:
            return []
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT d.id, d.title, d.source_type, d.url, "
                "ar.overall_status, ar.signals::text, ar.confidence, "
                "ar.confidence_reason, ar.trust_data::text "
                "FROM audit_results ar "
                "JOIN documents d ON d.id = ar.document_id AND d.scan_id = ar.scan_id "
                "WHERE ar.scan_id = %s AND ar.document_id = ANY(%s)",
                (scan_id, doc_ids),
            )
            rows = cur.fetchall()  # type: ignore[union-attr]
        results: list[AuditResult] = []
        for r in rows:
            doc = Document(id=r[0], title=r[1], content="", source_type=r[2], url=r[3])
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
        """Return full audit results for *scan_id* in the API/reporting shape.

        Selects ``signals::text`` and ``trust_data::text`` for compatibility with
        existing string-based deserialization helpers.  ``last_modified``
        (TIMESTAMPTZ) is converted to an ISO 8601 string if psycopg3 returns a
        datetime object, preserving the same shape as the SQLite implementation.
        """
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT d.id, d.title, d.url, d.source_type, d.last_modified, "
                "ar.overall_status, ar.signals::text, ar.suggested_replacement_id, "
                "ar.confidence, ar.confidence_reason, ar.trust_data::text "
                "FROM audit_results ar "
                "JOIN documents d ON d.id = ar.document_id AND d.scan_id = ar.scan_id "
                "JOIN scans s ON s.id = ar.scan_id AND s.status = 'completed' "
                "WHERE ar.scan_id = %s "
                "ORDER BY "
                "CASE ar.overall_status "
                "WHEN 'stale' THEN 0 "
                "WHEN 'needs_review' THEN 1 "
                "WHEN 'unknown' THEN 2 "
                "ELSE 3 "
                "END, "
                "ar.confidence ASC",
                (scan_id,),
            )
            rows = cur.fetchall()  # type: ignore[union-attr]
        results = []
        for r in rows:
            last_modified = r[4]
            if last_modified is not None and hasattr(last_modified, "isoformat"):
                last_modified = last_modified.isoformat()
            trust_metadata, trust_evidence = deserialize_trust_data(r[10])
            results.append({
                "id": r[0],
                "title": r[1],
                "url": r[2],
                "source_type": r[3],
                "last_modified": last_modified,
                "overall_status": r[5],
                "signals": deserialize_signal_records(r[6]),
                "suggested_replacement_id": r[7],
                "confidence": r[8],
                "confidence_reason": r[9],
                "trust_metadata": trust_metadata,
                "trust_evidence": trust_evidence,
            })
        return results

    # ------------------------------------------------------------------
    # Workflow persistence
    # ------------------------------------------------------------------

    # States that represent completed/non-actionable work — mirrors SQLite.
    _TERMINAL_STATES: set[str] = {"fixed", "dismissed", "accepted_risk"}

    # Fields to auto-clear when transitioning to a given state — mirrors SQLite.
    _STATE_CLEANUP: dict[str, dict[str, object]] = {
        "open":          {"snoozed_until": None, "dismissal_reason": None},
        "acknowledged":  {"snoozed_until": None, "dismissal_reason": None},
        "fixed":         {"snoozed_until": None},
        "dismissed":     {"snoozed_until": None},
        "accepted_risk": {"snoozed_until": None},
    }

    @staticmethod
    def _parse_ts_str(s: str | None) -> datetime | None:
        """Parse an ISO timestamp string to a timezone-aware datetime, or None.

        Used to convert caller-supplied ISO strings to Python datetime objects
        for PostgreSQL TIMESTAMPTZ columns (e.g. ``snoozed_until``).
        """
        if s is None:
            return None
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _actionable_results(results: list[AuditResult]) -> list[AuditResult]:
        """Return results that require workflow tracking — mirrors SQLite."""
        actionable = []
        for r in results:
            if r.status == "current":
                continue
            flag = r.trust_metadata.get("requires_human_audit")
            if flag is None:
                if r.status in ("stale", "needs_review", "unknown"):
                    actionable.append(r)
            elif flag:
                actionable.append(r)
        return actionable

    @staticmethod
    def _row_to_finding(row: tuple) -> dict:
        """Convert a ``finding_workflow`` SELECT row to the standard finding dict."""
        return {
            "finding_key":          row[0],
            "document_id":          row[1],
            "source_type":          row[2],
            "title":                row[3],
            "workflow_state":       row[4],
            "note":                 row[5],
            "assigned_owner":       row[6],
            "due_date":             row[7],
            "snoozed_until":        row[8],
            "dismissal_reason":     row[9],
            "evidence_hash":        row[10],
            "first_seen_scan_id":   row[11],
            "last_seen_scan_id":    row[12],
            "updated_at":           row[13],
            "last_checked_scan_id": row[14],
        }

    def _enrich_finding_with_audit_context_pg(
        self, finding: dict, cur: object
    ) -> dict:
        """Join audit_results + documents for a finding's last_seen scan.

        Reuses the same open cursor so all enrichment queries share the
        implicit read transaction.  Casts JSONB columns to text so the
        string-based deserializers in ``serialization.py`` work unchanged.
        """
        scan_id = finding.get("last_seen_scan_id")
        doc_id = finding.get("document_id")
        if scan_id is None or doc_id is None:
            finding["audit_context"] = None
            return finding

        cur.execute(  # type: ignore[union-attr]
            "SELECT ar.overall_status, ar.confidence, ar.confidence_reason, "
            "ar.signals::text, ar.trust_data::text, d.url "
            "FROM audit_results ar "
            "JOIN documents d ON d.id = ar.document_id AND d.scan_id = ar.scan_id "
            "WHERE ar.scan_id = %s AND ar.document_id = %s",
            (scan_id, doc_id),
        )
        row = cur.fetchone()  # type: ignore[union-attr]
        if row is None:
            finding["audit_context"] = None
            return finding

        trust_metadata, trust_evidence = deserialize_trust_data(row[4])
        finding["audit_context"] = {
            "overall_status":    row[0],
            "confidence":        row[1],
            "confidence_reason": row[2],
            "signals":           deserialize_signal_records(row[3]),
            "url":               row[5],
            "trust_metadata":    trust_metadata,
            "trust_evidence":    trust_evidence,
        }
        return finding

    def _do_sync_findings_pg(
        self,
        cur: object,
        scan_id: int,
        results: list[AuditResult],
        scanned_doc_ids: set[str] | None,
        reanalyzed_doc_ids: set[str] | None,
    ) -> dict:
        """Inner workflow-sync logic — operates on *cur*, does NOT commit.

        Mirrors ``SqliteStorage._do_sync_findings`` but uses psycopg3 cursor
        conventions: ``%s`` placeholders, ``= ANY(%s)`` for list parameters,
        and ``datetime`` objects for TIMESTAMPTZ columns.
        """
        ts_dt = datetime.now(timezone.utc)
        stats = {"new": 0, "updated": 0, "reopened": 0, "auto_fixed": 0}

        actionable = self._actionable_results(results)
        seen_keys: set[str] = set()

        for result in actionable:
            key = result.finding_key
            if key in seen_keys:
                continue
            seen_keys.add(key)

            ev_hash = result.evidence_hash
            cur.execute(  # type: ignore[union-attr]
                "SELECT workflow_state, evidence_hash, snoozed_until "
                "FROM finding_workflow WHERE finding_key = %s",
                (key,),
            )
            existing = cur.fetchone()  # type: ignore[union-attr]

            if existing is None:
                cur.execute(  # type: ignore[union-attr]
                    "INSERT INTO finding_workflow "
                    "(finding_key, document_id, source_type, title, "
                    "workflow_state, evidence_hash, "
                    "first_seen_scan_id, last_seen_scan_id, "
                    "last_checked_scan_id, updated_at) "
                    "VALUES (%s, %s, %s, %s, 'open', %s, %s, %s, %s, %s) "
                    "ON CONFLICT (finding_key) DO UPDATE SET "
                    "last_checked_scan_id = EXCLUDED.last_checked_scan_id, "
                    "updated_at = EXCLUDED.updated_at",
                    (key, result.document.id, result.document.source_type,
                     result.document.title, ev_hash, scan_id, scan_id,
                     scan_id, ts_dt),
                )
                stats["new"] += 1
            else:
                old_state, old_hash, snoozed_until = existing

                # psycopg3 returns datetime for TIMESTAMPTZ; compare directly.
                should_reopen_snooze = (
                    old_state == "snoozed"
                    and snoozed_until is not None
                    and snoozed_until <= ts_dt
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
                    cur.execute(  # type: ignore[union-attr]
                        "UPDATE finding_workflow "
                        "SET workflow_state = 'open', evidence_hash = %s, title = %s, "
                        "last_seen_scan_id = %s, last_checked_scan_id = %s, "
                        "snoozed_until = NULL, updated_at = %s "
                        "WHERE finding_key = %s",
                        (ev_hash, result.document.title, scan_id, scan_id, ts_dt, key),
                    )
                    stats["reopened"] += 1
                elif is_reanalyzed:
                    cur.execute(  # type: ignore[union-attr]
                        "UPDATE finding_workflow "
                        "SET last_seen_scan_id = %s, last_checked_scan_id = %s, "
                        "title = %s, evidence_hash = %s, updated_at = %s "
                        "WHERE finding_key = %s",
                        (scan_id, scan_id, result.document.title, ev_hash, ts_dt, key),
                    )
                    stats["updated"] += 1
                else:
                    cur.execute(  # type: ignore[union-attr]
                        "UPDATE finding_workflow "
                        "SET last_checked_scan_id = %s, updated_at = %s "
                        "WHERE finding_key = %s",
                        (scan_id, ts_dt, key),
                    )
                    stats["updated"] += 1

        if scanned_doc_ids:
            auto_fixable = {"open", "acknowledged", "snoozed", "accepted_risk", "dismissed"}
            cur.execute(  # type: ignore[union-attr]
                "SELECT finding_key, workflow_state FROM finding_workflow "
                "WHERE document_id = ANY(%s) AND workflow_state = ANY(%s)",
                (list(scanned_doc_ids), list(auto_fixable)),
            )
            rows = cur.fetchall()  # type: ignore[union-attr]
            for fk, _state in rows:
                if fk not in seen_keys:
                    cur.execute(  # type: ignore[union-attr]
                        "UPDATE finding_workflow "
                        "SET workflow_state = 'fixed', last_checked_scan_id = %s, "
                        "note = %s, updated_at = %s "
                        "WHERE finding_key = %s",
                        (scan_id, f"No longer detected in scan #{scan_id}", ts_dt, fk),
                    )
                    stats["auto_fixed"] += 1

            cur.execute(  # type: ignore[union-attr]
                "UPDATE finding_workflow SET last_checked_scan_id = %s "
                "WHERE document_id = ANY(%s)",
                (scan_id, list(scanned_doc_ids)),
            )

        return stats

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

        Runs ``_do_sync_findings_pg`` and the scan-completion UPDATE in one
        transaction — workflow entries are never visible for an incomplete scan.
        Mirrors ``SqliteStorage.complete_scan_with_findings``.

        Returns the sync stats dict: ``{new, updated, reopened, auto_fixed}``.
        """
        finished_at = datetime.now(timezone.utc)
        now_dt = self._now_dt(now)
        conn = self.conn

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                self._assert_scan_running(cur, scan_id, owner_token)
                stats = self._do_sync_findings_pg(
                    cur, scan_id, results, scanned_doc_ids, reanalyzed_doc_ids
                )
                cur.execute(  # type: ignore[union-attr]
                    "UPDATE scans SET status = 'completed', finished_at = %s, "
                    "document_count = %s WHERE id = %s",
                    (finished_at, document_count, scan_id),
                )
            conn.commit()  # type: ignore[union-attr]
            return stats
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def sync_findings(
        self,
        scan_id: int,
        results: list[AuditResult],
        scanned_doc_ids: set[str] | None = None,
        reanalyzed_doc_ids: set[str] | None = None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> dict:
        """Sync audit results into the ``finding_workflow`` table.

        Mirrors ``SqliteStorage.sync_findings``.  Uses ``= ANY(%s)`` for list
        parameters and datetime objects for TIMESTAMPTZ columns.

        Returns a summary dict: ``{new, updated, reopened, auto_fixed}``.
        """
        now_dt = self._now_dt(now)
        conn = self.conn

        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                self._assert_scan_running(cur, scan_id, owner_token)
                stats = self._do_sync_findings_pg(
                    cur, scan_id, results, scanned_doc_ids, reanalyzed_doc_ids
                )
            conn.commit()  # type: ignore[union-attr]
            return stats
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

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

        Mirrors ``SqliteStorage.update_workflow``.  ``snoozed_until`` ISO strings
        are parsed to timezone-aware datetimes for the TIMESTAMPTZ column.
        ``updated_at`` is stored as a datetime object.

        The ``_UNSET`` sentinel (leave unchanged) vs ``None`` (explicitly clear)
        distinction is identical to the SQLite implementation.
        """
        if state == "snoozed" and snoozed_until in (_UNSET, None):
            raise ValueError(
                "Transitioning to 'snoozed' requires snoozed_until to be specified."
            )

        sets: list[str] = []
        params: list[object] = []

        if state is not _UNSET:
            sets.append("workflow_state = %s")
            params.append(state)
            for field, null_val in self._STATE_CLEANUP.get(state, {}).items():  # type: ignore[arg-type]
                caller_val = locals()[field]
                if caller_val is _UNSET:
                    sets.append(f"{field} = %s")
                    params.append(null_val)

        if note is not _UNSET:
            sets.append("note = %s")
            params.append(note)
        if assigned_owner is not _UNSET:
            sets.append("assigned_owner = %s")
            params.append(assigned_owner)
        if due_date is not _UNSET:
            sets.append("due_date = %s")
            params.append(due_date)
        if snoozed_until is not _UNSET:
            sets.append("snoozed_until = %s")
            val = snoozed_until
            params.append(self._parse_ts_str(val) if isinstance(val, str) else val)
        if dismissal_reason is not _UNSET:
            sets.append("dismissal_reason = %s")
            params.append(dismissal_reason)

        if not sets:
            return False

        sets.append("updated_at = %s")
        params.append(datetime.now(timezone.utc))
        params.append(finding_key)

        conn = self.conn
        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(  # type: ignore[union-attr]
                    f"UPDATE finding_workflow SET {', '.join(sets)} WHERE finding_key = %s",
                    params,
                )
                updated = cur.rowcount  # type: ignore[union-attr]
            conn.commit()  # type: ignore[union-attr]
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise
        return updated > 0

    def get_findings(
        self,
        *,
        scan_id: int | None = None,
        states: list[str] | None = None,
        include_all: bool = False,
    ) -> list[dict]:
        """Return findings, optionally filtered by scan and/or workflow state.

        Mirrors ``SqliteStorage.get_findings``.  Uses ``= ANY(%s)`` for list
        parameters and a datetime object for the snoozed_until comparison.
        """
        clauses: list[str] = []
        params: list[object] = []

        if scan_id is not None:
            clauses.append("last_checked_scan_id = %s")
            params.append(scan_id)

        if states:
            clauses.append("workflow_state = ANY(%s)")
            params.append(states)
        elif not include_all:
            clauses.append("NOT (workflow_state = ANY(%s))")
            params.append(sorted(self._TERMINAL_STATES))

        if not include_all:
            clauses.append(
                "(workflow_state != 'snoozed' OR snoozed_until IS NULL OR snoozed_until <= %s)"
            )
            params.append(datetime.now(timezone.utc))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                f"SELECT finding_key, document_id, source_type, title, "
                f"workflow_state, note, assigned_owner, due_date, "
                f"snoozed_until, dismissal_reason, evidence_hash, "
                f"first_seen_scan_id, last_seen_scan_id, updated_at, "
                f"last_checked_scan_id "
                f"FROM finding_workflow {where} "
                f"ORDER BY "
                f"CASE workflow_state "
                f"WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 "
                f"WHEN 'snoozed' THEN 2 WHEN 'accepted_risk' THEN 3 "
                f"WHEN 'dismissed' THEN 4 WHEN 'fixed' THEN 5 "
                f"END, updated_at DESC",
                params,
            )
            rows = cur.fetchall()  # type: ignore[union-attr]
            findings = [self._row_to_finding(r) for r in rows]
            return [self._enrich_finding_with_audit_context_pg(f, cur) for f in findings]

    def get_finding(self, finding_key: str) -> dict | None:
        """Return a single finding by key, or ``None``."""
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT finding_key, document_id, source_type, title, "
                "workflow_state, note, assigned_owner, due_date, "
                "snoozed_until, dismissal_reason, evidence_hash, "
                "first_seen_scan_id, last_seen_scan_id, updated_at, "
                "last_checked_scan_id "
                "FROM finding_workflow WHERE finding_key = %s",
                (finding_key,),
            )
            row = cur.fetchone()  # type: ignore[union-attr]
            if row is None:
                return None
            finding = self._row_to_finding(row)
            return self._enrich_finding_with_audit_context_pg(finding, cur)

    def get_workflow_summary(
        self,
        scan_id: int | None = None,
        include_all: bool = False,
    ) -> dict:
        """Return counts by workflow state — mirrors ``SqliteStorage.get_workflow_summary``."""
        clauses: list[str] = []
        params: list[object] = []

        if scan_id is not None:
            clauses.append("last_checked_scan_id = %s")
            params.append(scan_id)

        if not include_all:
            clauses.append("NOT (workflow_state = ANY(%s))")
            params.append(sorted(self._TERMINAL_STATES))
            clauses.append(
                "(workflow_state != 'snoozed' OR snoozed_until IS NULL OR snoozed_until <= %s)"
            )
            params.append(datetime.now(timezone.utc))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                f"SELECT workflow_state, COUNT(*) "
                f"FROM finding_workflow {where} GROUP BY workflow_state",
                params,
            )
            rows = cur.fetchall()  # type: ignore[union-attr]
        return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------
    # Scan history / diff
    # ------------------------------------------------------------------

    def get_scan_diff(self, scan_id: int, prev_scan_id: int) -> list[dict]:
        """Compare two scans and return rows whose status changed or is new.

        Mirrors ``SqliteStorage.get_scan_diff``.  Returns one dict per
        document with keys ``document_id``, ``title``, ``old_status``,
        ``new_status``.  ``old_status`` is ``None`` for documents that did
        not appear in *prev_scan_id*.
        """
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT cur.document_id, d.title, "
                "prev.overall_status AS old_status, cur.overall_status AS new_status "
                "FROM audit_results cur "
                "LEFT JOIN audit_results prev "
                "ON prev.document_id = cur.document_id AND prev.scan_id = %s "
                "JOIN documents d "
                "ON d.id = cur.document_id AND d.scan_id = cur.scan_id "
                "WHERE cur.scan_id = %s "
                "AND (prev.overall_status IS NULL "
                "OR prev.overall_status != cur.overall_status)",
                (prev_scan_id, scan_id),
            )
            rows = cur.fetchall()  # type: ignore[union-attr]
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
        """Return the most recent *limit* completed scans with status counts.

        Mirrors ``SqliteStorage.get_scan_history``.  Each entry includes
        ``scan_id``, ``started_at``, ``finished_at``, ``document_count``,
        ``stale_count``, ``needs_review_count``, ``unknown_count``, and
        ``changes`` (a list of human-readable summary strings comparing the
        scan to its predecessor, or ``None`` for the oldest in the window).

        ``started_at`` and ``finished_at`` are normalized to ISO 8601 strings
        so the shape matches ``SqliteStorage.get_scan_history``.
        """
        conn = self.conn
        with conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(  # type: ignore[union-attr]
                "SELECT s.id, s.started_at, s.finished_at, s.document_count, "
                "COUNT(CASE WHEN ar.overall_status = 'stale' THEN 1 END) AS stale, "
                "COUNT(CASE WHEN ar.overall_status = 'needs_review' THEN 1 END) AS needs_review, "
                "COUNT(CASE WHEN ar.overall_status = 'unknown' THEN 1 END) AS unknown "
                "FROM scans s "
                "LEFT JOIN audit_results ar ON s.id = ar.scan_id "
                "WHERE s.status = 'completed' "
                "GROUP BY s.id "
                "ORDER BY s.id DESC "
                "LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()  # type: ignore[union-attr]

        def _ts(v: object) -> object:
            return v.isoformat() if hasattr(v, "isoformat") else v  # type: ignore[union-attr]

        scans = [
            {
                "scan_id": r[0],
                "started_at": _ts(r[1]),
                "finished_at": _ts(r[2]),
                "document_count": r[3],
                "stale_count": r[4],
                "needs_review_count": r[5],
                "unknown_count": r[6],
            }
            for r in rows
        ]

        # Compute change summaries for consecutive scan pairs.
        for i, scan in enumerate(scans):
            if i + 1 < len(scans):
                prev = scans[i + 1]
                changes = self.get_scan_diff(scan["scan_id"], prev["scan_id"])
                scan["changes"] = _summarize_changes(changes)
            else:
                scan["changes"] = None  # oldest scan in window — no prior to diff against

        return scans

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def _execute_clear_pg(self, cur: object) -> None:
        """Delete all scan/result/workflow data and reset scan_state on *cur*.

        Assumes the caller owns the transaction — does NOT commit.
        Mirrors ``SqliteStorage._execute_clear`` without the
        ``sqlite_sequence`` step (Postgres sequences are unaffected by
        row deletion).
        """
        cur.execute("DELETE FROM audit_results")  # type: ignore[union-attr]
        cur.execute("DELETE FROM documents")  # type: ignore[union-attr]
        cur.execute("DELETE FROM scans")  # type: ignore[union-attr]
        cur.execute("DELETE FROM finding_workflow")  # type: ignore[union-attr]
        cur.execute(  # type: ignore[union-attr]
            "UPDATE scan_state SET in_progress = FALSE, owner_token = NULL, "
            "lease_expires_at = NULL, last_scan_id = NULL, scan_error = NULL "
            "WHERE id = 1"
        )

    def clear_all(self) -> None:
        """Delete all scans, documents, results, and workflow findings.

        Unlike ``clear_all_if_idle``, this does not check for a live lease.
        Mirrors ``SqliteStorage.clear_all``.
        """
        conn = self.conn
        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                self._execute_clear_pg(cur)
            conn.commit()  # type: ignore[union-attr]
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def clear_all_if_idle(self, now: str | None = None) -> bool:
        """Atomically check for a live lease and, if absent, clear all scan data.

        Returns ``True`` when clearing succeeds (idle or expired lease).
        Returns ``False`` when a live, unexpired lease is present.
        An expired lease is treated as idle and does not block clearing.

        Uses ``SELECT … FOR UPDATE`` on the singleton ``scan_state`` row to
        prevent a concurrent ``try_start_scan`` from racing between the check
        and the delete.  Mirrors ``SqliteStorage.clear_all_if_idle``.
        """
        now_dt = self._now_dt(now)
        conn = self.conn
        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(  # type: ignore[union-attr]
                    "SELECT in_progress, lease_expires_at "
                    "FROM scan_state WHERE id = 1 FOR UPDATE"
                )
                row = cur.fetchone()  # type: ignore[union-attr]
                # Live lease: in_progress and expiry is in the future.
                if row and row[0] and (row[1] is None or row[1] > now_dt):
                    conn.rollback()  # type: ignore[union-attr]
                    return False
                self._execute_clear_pg(cur)
            conn.commit()  # type: ignore[union-attr]
            return True
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise

    def _do_prune_scans_pg(self, cur: object, keep: int) -> int:
        """Delete terminal scans beyond the most recent *keep* on *cur*.

        Running scans are never deleted.  Returns the number of scan rows
        removed.  Does NOT commit — caller owns the transaction.

        Mirrors ``SqliteStorage._do_prune_scans`` using ``%s`` placeholders
        and ``LIMIT 1 OFFSET %s`` (supported by PostgreSQL).
        """
        cur.execute(  # type: ignore[union-attr]
            "SELECT id FROM scans WHERE status IN ('completed', 'failed') "
            "ORDER BY id DESC LIMIT 1 OFFSET %s",
            (keep - 1,),
        )
        row = cur.fetchone()  # type: ignore[union-attr]
        if not row:
            return 0
        cutoff_id = row[0]
        cur.execute(  # type: ignore[union-attr]
            "DELETE FROM audit_results WHERE scan_id IN "
            "(SELECT id FROM scans WHERE id < %s AND status IN ('completed', 'failed'))",
            (cutoff_id,),
        )
        cur.execute(  # type: ignore[union-attr]
            "DELETE FROM documents WHERE scan_id IN "
            "(SELECT id FROM scans WHERE id < %s AND status IN ('completed', 'failed'))",
            (cutoff_id,),
        )
        cur.execute(  # type: ignore[union-attr]
            "DELETE FROM scans WHERE id < %s AND status IN ('completed', 'failed')",
            (cutoff_id,),
        )
        return cur.rowcount  # type: ignore[union-attr]

    def prune_scans(
        self,
        keep: int = 10,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> int:
        """Delete terminal scans beyond the most recent *keep*, returning count removed.

        Running scans are never pruned; only completed and failed scans count
        toward the retention window and are eligible for deletion.

        When *owner_token* is supplied the active lease is verified first via
        ``_check_lease``.  Mirrors ``SqliteStorage.prune_scans``.
        """
        now_dt = self._now_dt(now)
        conn = self.conn
        try:
            with conn.cursor() as cur:  # type: ignore[union-attr]
                if owner_token is not None:
                    self._check_lease(cur, owner_token, now_dt)
                count = self._do_prune_scans_pg(cur, keep)
            conn.commit()  # type: ignore[union-attr]
            return count
        except Exception:
            try:
                conn.rollback()  # type: ignore[union-attr]
            except Exception:
                pass
            raise
