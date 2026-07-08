"""Tests for SQLite persistence."""

from datetime import datetime, timezone

import pytest

from kb_audit.db import Database, LeaseLostError
from kb_audit.models import AuditResult, Document, Severity, StalenessSignal


def test_scan_lifecycle(tmp_path):
    db = Database(tmp_path / "test.db")
    db.connect()

    scan_id = db.start_scan()
    assert scan_id is not None

    doc = Document(
        id="doc-1",
        title="Test Doc",
        content="Test content",
        source_type="notion",
        last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    db.store_document(scan_id, doc)

    result = AuditResult(
        document=doc,
        signals=[StalenessSignal("duplicate", Severity.CRITICAL, "Exact duplicate")],
        status="stale",
        confidence=0.5,
        confidence_reason="Exact duplicate of 'Other Doc'",
    )
    db.store_result(scan_id, result)

    db.finish_scan(scan_id, 1)

    history = db.get_scan_history()
    assert len(history) == 1
    assert history[0]["document_count"] == 1
    assert history[0]["stale_count"] == 1

    db.close()


def test_finding_audit_context_includes_trust_metadata(tmp_path):
    db = Database(tmp_path / "test.db")
    db.connect()

    scan_id = db.start_scan()
    doc = Document(
        id="doc-lifecycle",
        title="Deprecated Guide",
        content="Status: Deprecated",
        source_type="notion",
        last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    result = AuditResult(
        document=doc,
        signals=[],
        status="stale",
        confidence=0.9,
        confidence_reason="Deprecated status",
        trust_metadata={
            "lifecycle": "deprecated",
            "lifecycle_evidence": ["Status field indicates 'Deprecated'"],
            "declared_status": "Deprecated",
        },
        trust_evidence={
            "summary": "Marked as deprecated",
            "positive_evidence": [],
            "review_risks": ["Deprecated status"],
            "missing_evidence": [],
            "recommended_action": "Audit before relying on this document.",
        },
    )

    db.store_document(scan_id, doc)
    db.store_result(scan_id, result)
    db.sync_findings(scan_id, [result])

    finding = db.get_finding(result.finding_key)
    assert finding is not None
    assert finding["audit_context"]["trust_metadata"]["lifecycle"] == "deprecated"
    assert finding["audit_context"]["trust_metadata"]["declared_status"] == "Deprecated"

    db.close()


def test_previous_hashes(tmp_path):
    db = Database(tmp_path / "test.db")
    db.connect()

    scan_id = db.start_scan()
    doc = Document(id="doc-1", title="Test", content="hello", source_type="test")
    db.store_document(scan_id, doc)
    db.finish_scan(scan_id, 1)

    hashes = db.get_previous_hashes()
    assert hashes["doc-1"] == doc.content_hash

    db.close()


def test_prune_scans(tmp_path):
    db = Database(tmp_path / "test.db")
    db.connect()

    # Create 5 scans with one document each
    for i in range(5):
        scan_id = db.start_scan()
        doc = Document(
            id=f"doc-{i}", title=f"Doc {i}", content=f"content {i}", source_type="test"
        )
        db.store_document(scan_id, doc)
        db.finish_scan(scan_id, 1)

    history = db.get_scan_history(limit=10)
    assert len(history) == 5

    # Keep only the 2 most recent
    pruned = db.prune_scans(keep=2)
    assert pruned == 3

    history = db.get_scan_history(limit=10)
    assert len(history) == 2

    db.close()


def test_prune_scans_nothing_to_prune(tmp_path):
    db = Database(tmp_path / "test.db")
    db.connect()

    scan_id = db.start_scan()
    db.finish_scan(scan_id, 0)

    pruned = db.prune_scans(keep=10)
    assert pruned == 0

    db.close()


def test_db_url_parsing(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/test.db")
    db.connect()
    assert db._path == f"{tmp_path}/test.db"
    db.close()


# ---------------------------------------------------------------------------
# Two-connection atomicity tests for leased writes
# ---------------------------------------------------------------------------

class TestLeasedWriteAtomicity:
    """Prove that every scan-owned write is atomic with its lease guard.

    All tests use two separate Database objects (two connections) on the same
    file-based DB so that the race condition is observable: old worker checks
    live ownership, replacement takes over, old worker's write attempt fails.

    Timestamps:
      T0 — old worker's acquisition time (year 2020, lease expires 5 min later)
      T1 — replacement's time / "now" for all guarded writes (year 2030)
           old lease already expired;  new lease (T1 + 5 min) is still live.
    """

    T0 = "2020-01-01T00:00:00+00:00"   # old lease acquired — expired by T1
    T1 = "2030-01-01T00:00:00+00:00"   # replacement acquired — valid through T1

    def _open(self, path: str) -> Database:
        db = Database(path)
        db.connect()
        return db

    def _setup(self, tmp_path) -> tuple[str, str, str]:
        """Acquire lease for old worker, then let replacement take over.

        Returns (db_path, old_token, new_token).
        """
        db_path = str(tmp_path / "audit.db")

        old_db = self._open(db_path)
        old_token = old_db.try_start_scan(now=self.T0)
        old_db.close()
        assert old_token is not None

        new_db = self._open(db_path)
        new_token = new_db.try_start_scan(now=self.T1)
        new_db.close()
        assert new_token is not None
        assert new_token != old_token

        return db_path, old_token, new_token

    # ------------------------------------------------------------------
    # Positive: leased writes succeed for the current owner
    # ------------------------------------------------------------------

    def test_start_scan_with_valid_lease_succeeds(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        db = self._open(db_path)
        token = db.try_start_scan(now=self.T1)
        assert token is not None
        scan_id = db.start_scan(owner_token=token, now=self.T1)
        assert isinstance(scan_id, int)
        row = db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()
        assert row[0] == 1
        db.close()

    def test_unleased_methods_work_without_owner_token(self, tmp_path):
        """CLI / unleased path must be unaffected by leased-write changes."""
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()               # no owner_token
        doc = Document(id="d1", title="T", content="", source_type="notion")
        db.store_document(scan_id, doc)          # no owner_token
        result = AuditResult(
            document=doc,
            signals=[StalenessSignal("age", Severity.WARNING, "old")],
            status="stale",
            confidence=0.7,
            confidence_reason="old",
        )
        db.store_result(scan_id, result)         # no owner_token
        db.sync_findings(scan_id, [result])      # no owner_token — must run before finish
        db.finish_scan(scan_id, 1)               # no owner_token
        pruned = db.prune_scans(keep=5)          # no owner_token
        assert pruned == 0
        db.close()

    # ------------------------------------------------------------------
    # Negative: expired / replaced owner is blocked before any mutation
    # ------------------------------------------------------------------

    def test_start_scan_blocked_after_takeover(self, tmp_path):
        """Old worker cannot insert a scan row after replacement acquires lease."""
        db_path, old_token, _ = self._setup(tmp_path)
        db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            db.start_scan(owner_token=old_token, now=self.T1)
        row = db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()
        assert row[0] == 0, "No scan row must be created after rejection"
        db.close()

    def test_store_document_blocked_after_takeover(self, tmp_path):
        db_path, old_token, new_token = self._setup(tmp_path)
        # New owner creates a scan first
        new_db = self._open(db_path)
        scan_id = new_db.start_scan(owner_token=new_token, now=self.T1)
        new_db.close()

        doc = Document(id="d1", title="T", content="", source_type="notion")
        old_db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db.store_document(scan_id, doc, owner_token=old_token, now=self.T1)
        row = old_db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()
        assert row[0] == 0
        old_db.close()

    def test_carry_forward_blocked_after_takeover(self, tmp_path):
        db_path, old_token, new_token = self._setup(tmp_path)
        # Create two scans with a completed one as the source for carry-forward
        new_db = self._open(db_path)
        s1 = new_db.start_scan(owner_token=new_token, now=self.T1)
        doc = Document(id="d1", title="T", content="x", source_type="notion")
        new_db.store_document(s1, doc, owner_token=new_token, now=self.T1)
        result = AuditResult(
            document=doc, signals=[], status="current", confidence=1.0,
            confidence_reason="ok",
        )
        new_db.store_result(s1, result, owner_token=new_token, now=self.T1)
        new_db.finish_scan(s1, 1, owner_token=new_token, now=self.T1)
        new_db.close()

        # Re-acquire for the old_token path — trick: we just try without a real lease
        # The point is old_token is expired, new_token holds the lease.
        old_db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db.carry_forward_results(s1 + 1, ["d1"], owner_token=old_token, now=self.T1)
        row = old_db.conn.execute(
            "SELECT COUNT(*) FROM audit_results WHERE scan_id = ?", (s1 + 1,)
        ).fetchone()
        assert row[0] == 0
        old_db.close()

    def test_store_result_blocked_after_takeover(self, tmp_path):
        db_path, old_token, new_token = self._setup(tmp_path)
        new_db = self._open(db_path)
        scan_id = new_db.start_scan(owner_token=new_token, now=self.T1)
        doc = Document(id="d1", title="T", content="", source_type="notion")
        new_db.store_document(scan_id, doc, owner_token=new_token, now=self.T1)
        new_db.close()

        result = AuditResult(
            document=doc, signals=[], status="current", confidence=1.0,
            confidence_reason="ok",
        )
        old_db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db.store_result(scan_id, result, owner_token=old_token, now=self.T1)
        row = old_db.conn.execute("SELECT COUNT(*) FROM audit_results").fetchone()
        assert row[0] == 0
        old_db.close()

    def test_finish_scan_blocked_after_takeover(self, tmp_path):
        db_path, old_token, new_token = self._setup(tmp_path)
        new_db = self._open(db_path)
        scan_id = new_db.start_scan(owner_token=new_token, now=self.T1)
        new_db.close()

        old_db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db.finish_scan(scan_id, 0, owner_token=old_token, now=self.T1)
        row = old_db.conn.execute(
            "SELECT finished_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row[0] is None, "finished_at must remain NULL after rejection"
        old_db.close()

    def test_sync_findings_blocked_after_takeover(self, tmp_path):
        db_path, old_token, new_token = self._setup(tmp_path)
        doc = Document(id="d1", title="T", content="", source_type="notion")
        result = AuditResult(
            document=doc,
            signals=[StalenessSignal("age", Severity.CRITICAL, "very old")],
            status="stale",
            confidence=0.9,
            confidence_reason="age",
        )
        old_db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db.sync_findings(1, [result], owner_token=old_token, now=self.T1)
        row = old_db.conn.execute("SELECT COUNT(*) FROM finding_workflow").fetchone()
        assert row[0] == 0, "No workflow rows must be created after rejection"
        old_db.close()

    def test_prune_scans_blocked_after_takeover(self, tmp_path):
        db_path, old_token, new_token = self._setup(tmp_path)
        # Create several scans so there is something to prune
        new_db = self._open(db_path)
        for _ in range(5):
            s = new_db.start_scan(owner_token=new_token, now=self.T1)
            new_db.finish_scan(s, 0, owner_token=new_token, now=self.T1)
        new_db.close()

        old_db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db.prune_scans(keep=2, owner_token=old_token, now=self.T1)
        row = old_db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()
        assert row[0] == 5, "All scans must still exist after rejected pruning"
        old_db.close()

    # ------------------------------------------------------------------
    # Partial-write and replacement-state invariants
    # ------------------------------------------------------------------

    def test_no_partial_workflow_changes_after_rejection(self, tmp_path):
        """sync_findings is all-or-nothing: zero rows when ownership is lost."""
        db_path, old_token, _ = self._setup(tmp_path)
        results = [
            AuditResult(
                document=Document(id=f"d{i}", title=f"T{i}", content="", source_type="notion"),
                signals=[StalenessSignal("age", Severity.CRITICAL, "old")],
                status="stale", confidence=0.9, confidence_reason="age",
            )
            for i in range(10)
        ]
        old_db = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db.sync_findings(99, results, owner_token=old_token, now=self.T1)
        row = old_db.conn.execute("SELECT COUNT(*) FROM finding_workflow").fetchone()
        assert row[0] == 0, "Entire sync_findings must roll back on rejection"
        old_db.close()

    def test_replacement_lease_unchanged_after_rejection(self, tmp_path):
        """Rejecting an old worker must not modify the replacement's scan_state row."""
        db_path, old_token, new_token = self._setup(tmp_path)

        # Record replacement's state before any rejected operation
        reader = self._open(db_path)
        before = reader.conn.execute(
            "SELECT owner_token, lease_expires_at, in_progress FROM scan_state WHERE id=1"
        ).fetchone()
        reader.close()

        # Old worker attempts (and fails) several writes
        old_db = self._open(db_path)
        for fn in [
            lambda: old_db.start_scan(owner_token=old_token, now=self.T1),
            lambda: old_db.finish_scan(1, 0, owner_token=old_token, now=self.T1),
            lambda: old_db.prune_scans(keep=1, owner_token=old_token, now=self.T1),
        ]:
            with pytest.raises(LeaseLostError):
                fn()
        old_db.close()

        # scan_state must be unchanged
        reader = self._open(db_path)
        after = reader.conn.execute(
            "SELECT owner_token, lease_expires_at, in_progress FROM scan_state WHERE id=1"
        ).fetchone()
        reader.close()
        assert before == after, "scan_state must be identical before and after rejections"


# ---------------------------------------------------------------------------
# Atomic clear-if-idle tests
# ---------------------------------------------------------------------------

class TestClearAllIfIdle:
    """Prove that clear_all_if_idle is atomic and respects live leases.

    Two-connection tests use file-backed DBs.  Single-connection tests use
    :memory: for convenience where atomicity across processes is not the focus.

    Timestamps:
      T_LIVE  — a time well before the lease expires (year 2020, lease valid
                 through 2020 + 5 min, so T_LIVE is "before expiry")
      T_PAST  — a time far in the future relative to an old lease (year 2030),
                 causing that lease to appear expired.
    """

    T_LIVE = "2020-01-01T00:00:00+00:00"    # used as `now` when acquiring
    T_PAST = "2030-01-01T00:00:00+00:00"    # used as `now` when checking; old lease expired

    def _open(self, path: str) -> Database:
        db = Database(path)
        db.connect()
        return db

    # ------------------------------------------------------------------
    # Positive: clearing succeeds when idle
    # ------------------------------------------------------------------

    def test_idle_db_clears_successfully(self, tmp_path):
        db_path = str(tmp_path / "audit.db")
        db = self._open(db_path)
        # Insert a scan so we have data to clear
        scan_id = db.start_scan()
        doc = Document(id="d1", title="T", content="x", source_type="notion")
        db.store_document(scan_id, doc)
        db.finish_scan(scan_id, 1)
        db.close()

        clearer = self._open(db_path)
        result = clearer.clear_all_if_idle()
        assert result is True
        row = clearer.conn.execute("SELECT COUNT(*) FROM scans").fetchone()
        assert row[0] == 0
        clearer.close()

    def test_idle_clears_all_tables(self, tmp_path):
        """All data tables and scan_state are reset after clearing."""
        db_path = str(tmp_path / "audit.db")
        db = self._open(db_path)
        scan_id = db.start_scan()
        doc = Document(id="d1", title="T", content="x", source_type="notion")
        db.store_document(scan_id, doc)
        result = AuditResult(
            document=doc,
            signals=[StalenessSignal("age", Severity.WARNING, "old")],
            status="stale",
            confidence=0.7,
            confidence_reason="old",
        )
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)
        db.close()

        clearer = self._open(db_path)
        assert clearer.clear_all_if_idle() is True
        assert clearer.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
        assert clearer.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert clearer.conn.execute("SELECT COUNT(*) FROM audit_results").fetchone()[0] == 0
        assert clearer.conn.execute("SELECT COUNT(*) FROM finding_workflow").fetchone()[0] == 0
        state_row = clearer.conn.execute(
            "SELECT in_progress, owner_token, lease_expires_at, last_scan_id FROM scan_state WHERE id=1"
        ).fetchone()
        assert state_row[0] == 0
        assert state_row[1] is None
        assert state_row[2] is None
        assert state_row[3] is None
        clearer.close()

    def test_expired_lease_does_not_block_clear(self, tmp_path):
        """A lease that has expired is treated as idle."""
        db_path = str(tmp_path / "audit.db")
        acquirer = self._open(db_path)
        token = acquirer.try_start_scan(now=self.T_LIVE)
        assert token is not None
        acquirer.close()

        # Clear with T_PAST — the lease acquired at T_LIVE expired long ago
        clearer = self._open(db_path)
        result = clearer.clear_all_if_idle(now=self.T_PAST)
        assert result is True
        clearer.close()

    def test_memory_db_idle_clears_successfully(self):
        """clear_all_if_idle works on :memory: databases."""
        db = Database(":memory:")
        db.connect()
        scan_id = db.start_scan()
        doc = Document(id="d1", title="T", content="x", source_type="notion")
        db.store_document(scan_id, doc)
        db.finish_scan(scan_id, 1)
        assert db.clear_all_if_idle() is True
        assert db.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
        db.close()

    # ------------------------------------------------------------------
    # Negative: live lease blocks clearing
    # ------------------------------------------------------------------

    def test_live_lease_blocks_clear(self, tmp_path):
        """clear_all_if_idle returns False when a live lease is held."""
        db_path = str(tmp_path / "audit.db")
        owner = self._open(db_path)
        token = owner.try_start_scan(now=self.T_LIVE)
        assert token is not None
        owner.close()

        # Attempt to clear while the lease is still live (use T_LIVE as now — expiry is T_LIVE + 5 min)
        clearer = self._open(db_path)
        result = clearer.clear_all_if_idle(now=self.T_LIVE)
        assert result is False
        clearer.close()

    def test_live_lease_leaves_data_intact(self, tmp_path):
        """When blocked, no data is removed."""
        db_path = str(tmp_path / "audit.db")
        db = self._open(db_path)
        scan_id = db.start_scan()
        doc = Document(id="d1", title="T", content="x", source_type="notion")
        db.store_document(scan_id, doc)
        db.finish_scan(scan_id, 1)
        db.close()

        # Acquire a live lease then try to clear
        owner = self._open(db_path)
        token = owner.try_start_scan(now=self.T_LIVE)
        assert token is not None
        owner.close()

        clearer = self._open(db_path)
        result = clearer.clear_all_if_idle(now=self.T_LIVE)
        assert result is False
        # Scan data must still exist
        assert clearer.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
        clearer.close()

    def test_memory_db_live_lease_blocks_clear(self):
        """Live lease blocks clearing on :memory: databases."""
        db = Database(":memory:")
        db.connect()
        token = db.try_start_scan(now=self.T_LIVE)
        assert token is not None
        result = db.clear_all_if_idle(now=self.T_LIVE)
        assert result is False
        db.close()

    # ------------------------------------------------------------------
    # Rollback: injected failure leaves data intact
    # ------------------------------------------------------------------

    def test_injected_failure_rolls_back(self, tmp_path, monkeypatch):
        """If _execute_clear raises, no data is removed and the exception propagates."""
        db_path = str(tmp_path / "audit.db")
        db = self._open(db_path)
        scan_id = db.start_scan()
        doc = Document(id="d1", title="T", content="x", source_type="notion")
        db.store_document(scan_id, doc)
        db.finish_scan(scan_id, 1)
        db.close()

        def _boom(self, conn):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(Database, "_execute_clear", _boom)

        clearer = self._open(db_path)
        with pytest.raises(RuntimeError, match="injected failure"):
            clearer.clear_all_if_idle()
        # Data must still be present
        assert clearer.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
        clearer.close()

    # ------------------------------------------------------------------
    # Two-connection ordering: clear wins over subsequent start_scan
    # ------------------------------------------------------------------

    def test_clear_before_acquire_wins(self, tmp_path):
        """When clear completes first, a subsequent try_start_scan sees empty DB."""
        db_path = str(tmp_path / "audit.db")
        db = self._open(db_path)
        scan_id = db.start_scan()
        db.finish_scan(scan_id, 0)
        db.close()

        clearer = self._open(db_path)
        assert clearer.clear_all_if_idle() is True
        clearer.close()

        acquirer = self._open(db_path)
        token = acquirer.try_start_scan()
        assert token is not None  # clear left DB in idle state
        acquirer.close()

    def test_acquire_before_clear_blocks_it(self, tmp_path):
        """When try_start_scan wins the BEGIN IMMEDIATE first, clear returns False."""
        db_path = str(tmp_path / "audit.db")
        db = self._open(db_path)
        db.close()

        acquirer = self._open(db_path)
        token = acquirer.try_start_scan(now=self.T_LIVE)
        assert token is not None
        acquirer.close()

        clearer = self._open(db_path)
        result = clearer.clear_all_if_idle(now=self.T_LIVE)
        assert result is False
        clearer.close()


# ---------------------------------------------------------------------------
# Scan status lifecycle tests
# ---------------------------------------------------------------------------

class TestScanStatusLifecycle:
    """Verify the running → completed / failed status lifecycle.

    Uses file-backed DBs for takeover tests and :memory: for single-worker
    tests.  T_OLD/T_NEW follow the same convention as TestLeasedWriteAtomicity.
    """

    T_OLD = "2020-01-01T00:00:00+00:00"   # old lease time — expires before T_NEW
    T_NEW = "2030-01-01T00:00:00+00:00"   # replacement time — live through T_NEW

    def _open(self, path: str) -> Database:
        db = Database(path)
        db.connect()
        return db

    def _make_doc(self, doc_id: str = "d1") -> Document:
        return Document(id=doc_id, title="T", content="x", source_type="notion")

    def _make_result(self, doc: Document, status: str = "stale") -> AuditResult:
        return AuditResult(
            document=doc,
            signals=[StalenessSignal("age", Severity.WARNING, "old")],
            status=status,
            confidence=0.7,
            confidence_reason="old",
        )

    # ------------------------------------------------------------------
    # Successful scan reaches completed
    # ------------------------------------------------------------------

    def test_successful_scan_marked_completed(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "running"

        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.finish_scan(scan_id, 1)
        row = db.conn.execute("SELECT status, finished_at FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "completed"
        assert row[1] is not None
        db.close()

    def test_completed_scan_appears_in_history(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.finish_scan(scan_id, 0)
        history = db.get_scan_history()
        assert len(history) == 1
        assert history[0]["scan_id"] == scan_id
        db.close()

    def test_completed_scan_results_visible(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.store_result(scan_id, self._make_result(doc))
        db.finish_scan(scan_id, 1)
        results = db.get_scan_results(scan_id)
        assert len(results) == 1
        db.close()

    # ------------------------------------------------------------------
    # fail_scan marks failed and removes partial data
    # ------------------------------------------------------------------

    def test_fail_scan_marked_failed(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.fail_scan(scan_id, "something went wrong")
        row = db.conn.execute("SELECT status, error, finished_at FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "failed"
        assert "something went wrong" in row[1]
        assert row[2] is not None
        db.close()

    def test_fail_scan_removes_documents(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.fail_scan(scan_id, "error")
        count = db.conn.execute("SELECT COUNT(*) FROM documents WHERE scan_id=?", (scan_id,)).fetchone()[0]
        assert count == 0
        db.close()

    def test_fail_scan_removes_results(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.store_result(scan_id, self._make_result(doc))
        db.fail_scan(scan_id, "error")
        count = db.conn.execute("SELECT COUNT(*) FROM audit_results WHERE scan_id=?", (scan_id,)).fetchone()[0]
        assert count == 0
        db.close()

    # ------------------------------------------------------------------
    # Excluded from history and results when not completed
    # ------------------------------------------------------------------

    def test_running_scan_excluded_from_history(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        history = db.get_scan_history()
        assert all(s["scan_id"] != scan_id for s in history)
        db.close()

    def test_failed_scan_excluded_from_history(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.fail_scan(scan_id, "boom")
        history = db.get_scan_history()
        assert all(s["scan_id"] != scan_id for s in history)
        db.close()

    def test_running_scan_excluded_from_results(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.store_result(scan_id, self._make_result(doc))
        assert db.get_scan_results(scan_id) == []
        db.close()

    def test_failed_scan_excluded_from_results(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.store_result(scan_id, self._make_result(doc))
        db.fail_scan(scan_id, "boom")
        assert db.get_scan_results(scan_id) == []
        db.close()

    def test_previous_hashes_only_from_completed(self, tmp_path):
        """get_previous_hashes ignores running and failed scans."""
        db = self._open(str(tmp_path / "audit.db"))
        s1 = db.start_scan()
        doc_a = Document(id="doc-a", title="A", content="hello", source_type="notion")
        db.store_document(s1, doc_a)
        db.finish_scan(s1, 1)

        s2 = db.start_scan()
        doc_b = Document(id="doc-b", title="B", content="world", source_type="notion")
        db.store_document(s2, doc_b)
        # s2 remains running

        hashes = db.get_previous_hashes()
        assert "doc-a" in hashes
        assert "doc-b" not in hashes
        db.close()

    # ------------------------------------------------------------------
    # Replacement takeover abandons running scan atomically
    # ------------------------------------------------------------------

    def test_takeover_abandons_running_scan(self, tmp_path):
        """When replacement acquires the lease, the old running scan is marked failed."""
        db_path = str(tmp_path / "audit.db")
        old_db = self._open(db_path)
        old_token = old_db.try_start_scan(now=self.T_OLD)
        assert old_token is not None
        scan_id = old_db.start_scan(owner_token=old_token, now=self.T_OLD)
        doc = self._make_doc()
        old_db.store_document(scan_id, doc, owner_token=old_token, now=self.T_OLD)
        old_db.close()

        new_db = self._open(db_path)
        new_token = new_db.try_start_scan(now=self.T_NEW)
        assert new_token is not None

        row = new_db.conn.execute(
            "SELECT status, error FROM scans WHERE id=?", (scan_id,)
        ).fetchone()
        assert row[0] == "failed"
        assert "abandoned" in row[1]
        doc_count = new_db.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE scan_id=?", (scan_id,)
        ).fetchone()[0]
        assert doc_count == 0
        new_db.close()

    def test_completed_scan_survives_takeover(self, tmp_path):
        """A completed scan is never touched by the takeover cleanup."""
        db_path = str(tmp_path / "audit.db")
        old_db = self._open(db_path)
        old_token = old_db.try_start_scan(now=self.T_OLD)
        assert old_token is not None
        scan_id = old_db.start_scan(owner_token=old_token, now=self.T_OLD)
        doc = self._make_doc()
        old_db.store_document(scan_id, doc, owner_token=old_token, now=self.T_OLD)
        old_db.store_result(scan_id, self._make_result(doc), owner_token=old_token, now=self.T_OLD)
        old_db.finish_scan(scan_id, 1, owner_token=old_token, now=self.T_OLD)
        old_db.end_scan(old_token, scan_id, None, now=self.T_OLD)
        old_db.close()

        new_db = self._open(db_path)
        new_token = new_db.try_start_scan(now=self.T_NEW)
        assert new_token is not None

        row = new_db.conn.execute(
            "SELECT status FROM scans WHERE id=?", (scan_id,)
        ).fetchone()
        assert row[0] == "completed"
        new_db.close()

    def test_abandoned_scan_excluded_from_history(self, tmp_path):
        """Abandoned (failed) scans from takeover are invisible in history."""
        db_path = str(tmp_path / "audit.db")
        old_db = self._open(db_path)
        old_token = old_db.try_start_scan(now=self.T_OLD)
        scan_id = old_db.start_scan(owner_token=old_token, now=self.T_OLD)
        old_db.close()

        new_db = self._open(db_path)
        new_db.try_start_scan(now=self.T_NEW)
        history = new_db.get_scan_history()
        assert all(s["scan_id"] != scan_id for s in history)
        new_db.close()

    # ------------------------------------------------------------------
    # Leased fail_scan is blocked after takeover
    # ------------------------------------------------------------------

    def test_leased_fail_scan_blocked_after_takeover(self, tmp_path):
        """Expired owner's fail_scan raises LeaseLostError (global lease guard)."""
        db_path = str(tmp_path / "audit.db")
        old_db = self._open(db_path)
        old_token = old_db.try_start_scan(now=self.T_OLD)
        scan_id = old_db.start_scan(owner_token=old_token, now=self.T_OLD)
        old_db.close()

        new_db = self._open(db_path)
        new_db.try_start_scan(now=self.T_NEW)
        new_db.close()

        old_db2 = self._open(db_path)
        with pytest.raises(LeaseLostError):
            old_db2.fail_scan(scan_id, "error", owner_token=old_token, now=self.T_NEW)
        old_db2.close()

    # ------------------------------------------------------------------
    # Migration: legacy databases retain completed scan history
    # ------------------------------------------------------------------

    def test_legacy_finished_scans_treated_as_completed(self, tmp_path):
        """Scans with finished_at set but no status column migrate to completed."""
        import sqlite3
        db_path = str(tmp_path / "legacy.db")

        raw = sqlite3.connect(db_path)
        raw.executescript("""
            CREATE TABLE scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                document_count INTEGER DEFAULT 0
            );
            INSERT INTO scans (started_at, finished_at, document_count)
            VALUES ('2020-01-01T00:00:00+00:00', '2020-01-01T01:00:00+00:00', 5);
            INSERT INTO scans (started_at, finished_at, document_count)
            VALUES ('2020-01-02T00:00:00+00:00', NULL, 0);
        """)
        raw.close()

        db = self._open(db_path)
        rows = db.conn.execute("SELECT id, status FROM scans ORDER BY id").fetchall()
        assert rows[0][1] == "completed"
        assert rows[1][1] == "failed"
        history = db.get_scan_history()
        assert len(history) == 1
        assert history[0]["scan_id"] == rows[0][0]
        db.close()

    # ------------------------------------------------------------------
    # CLI (unleased) lifecycle
    # ------------------------------------------------------------------

    def test_cli_unleased_complete_lifecycle(self, tmp_path):
        """Unleased scans go running → completed without any token checks."""
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.store_result(scan_id, self._make_result(doc))
        db.finish_scan(scan_id, 1)
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "completed"
        history = db.get_scan_history()
        assert len(history) == 1
        db.close()

    def test_cli_unleased_fail_lifecycle(self, tmp_path):
        """Unleased fail_scan cleans up and marks the scan failed."""
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.fail_scan(scan_id, "fetch failed")
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "failed"
        assert db.conn.execute("SELECT COUNT(*) FROM documents WHERE scan_id=?", (scan_id,)).fetchone()[0] == 0
        assert db.get_scan_history() == []


class TestScanWriteGuard:
    """Defect 1 + 2: scan-level ownership guard prevents cross-scan writes."""

    def _open(self, db_path: str) -> Database:
        db = Database(db_path)
        db.connect()
        return db

    def _make_doc(self, doc_id: str = "d1") -> Document:
        return Document(id=doc_id, title="T", content="x", source_type="notion")

    def _make_result(self, doc: Document, status: str = "stale") -> AuditResult:
        return AuditResult(
            document=doc,
            signals=[StalenessSignal("age", Severity.WARNING, "old")],
            status=status,
            confidence=0.7,
            confidence_reason="old",
        )

    # ------------------------------------------------------------------
    # Writes rejected after scan reaches a terminal state (unleased)
    # ------------------------------------------------------------------

    def test_store_document_blocked_for_completed_scan(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.finish_scan(scan_id, 0)
        with pytest.raises(LeaseLostError):
            db.store_document(scan_id, self._make_doc())
        db.close()

    def test_store_result_blocked_for_completed_scan(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.finish_scan(scan_id, 1)
        with pytest.raises(LeaseLostError):
            db.store_result(scan_id, self._make_result(doc))
        db.close()

    def test_store_document_blocked_for_failed_scan(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.fail_scan(scan_id, "error")
        with pytest.raises(LeaseLostError):
            db.store_document(scan_id, self._make_doc())
        db.close()

    def test_carry_forward_blocked_for_completed_scan(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.finish_scan(scan_id, 0)
        with pytest.raises(LeaseLostError):
            db.carry_forward_results(scan_id, ["doc-a"])
        db.close()

    def test_sync_findings_blocked_for_completed_scan(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.finish_scan(scan_id, 0)
        with pytest.raises(LeaseLostError):
            db.sync_findings(scan_id, [])
        db.close()

    def test_sync_findings_blocked_for_failed_scan(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.fail_scan(scan_id, "boom")
        with pytest.raises(LeaseLostError):
            db.sync_findings(scan_id, [])
        db.close()

    # ------------------------------------------------------------------
    # complete_scan_with_findings: atomic, idempotent guards
    # ------------------------------------------------------------------

    def test_complete_scan_with_findings_succeeds_once(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.store_result(scan_id, self._make_result(doc))
        stats = db.complete_scan_with_findings(scan_id, 1, [self._make_result(doc)])
        assert isinstance(stats, dict)
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "completed"
        db.close()

    def test_complete_scan_with_findings_blocked_after_completion(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.complete_scan_with_findings(scan_id, 0, [])
        with pytest.raises(LeaseLostError):
            db.complete_scan_with_findings(scan_id, 0, [])
        db.close()

    def test_fail_scan_blocked_after_completion(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.complete_scan_with_findings(scan_id, 0, [])
        with pytest.raises(LeaseLostError):
            db.fail_scan(scan_id, "error")
        db.close()

    def test_fail_scan_blocked_after_fail(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.fail_scan(scan_id, "first error")
        with pytest.raises(LeaseLostError):
            db.fail_scan(scan_id, "second error")
        db.close()

    def test_complete_scan_with_findings_visible_in_history(self, tmp_path):
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        db.complete_scan_with_findings(scan_id, 1, [self._make_result(doc)])
        history = db.get_scan_history()
        assert len(history) == 1
        assert history[0]["scan_id"] == scan_id
        db.close()

    def test_complete_scan_with_findings_workflow_entries_atomic(self, tmp_path):
        """Workflow entries are only visible after the scan completes atomically."""
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        result = self._make_result(doc, status="stale")
        # Before completion: no workflow entries, scan still running
        assert db.get_findings() == []
        db.complete_scan_with_findings(scan_id, 1, [result])
        # After: workflow entry exists, scan is completed
        findings = db.get_findings(include_all=True)
        assert len(findings) == 1
        db.close()

    def test_store_document_succeeds_while_running(self, tmp_path):
        """Guard allows writes when scan is genuinely running."""
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)  # must not raise
        count = db.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE scan_id=?", (scan_id,)
        ).fetchone()[0]
        assert count == 1
        db.close()

    # ------------------------------------------------------------------
    # Unleased caller cannot access a leased scan
    # ------------------------------------------------------------------

    T_OLD = "2020-01-01T00:00:00+00:00"
    T_NEW = "2030-01-01T00:00:00+00:00"

    def _setup_leased_scan(self, db_path: str):
        """Acquire a live lease and create a running scan. Returns (db, token, scan_id)."""
        db = self._open(db_path)
        token = db.try_start_scan(now=self.T_NEW)
        assert token is not None
        scan_id = db.start_scan(owner_token=token, now=self.T_NEW)
        return db, token, scan_id

    def test_unleased_store_document_blocked_on_leased_scan(self, tmp_path):
        """store_document without owner_token must not write to a leased scan."""
        db, token, scan_id = self._setup_leased_scan(str(tmp_path / "audit.db"))
        with pytest.raises(LeaseLostError):
            db.store_document(scan_id, self._make_doc())
        count = db.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE scan_id=?", (scan_id,)
        ).fetchone()[0]
        assert count == 0
        db.close()

    def test_unleased_store_result_blocked_on_leased_scan(self, tmp_path):
        """store_result without owner_token must not write to a leased scan."""
        db, token, scan_id = self._setup_leased_scan(str(tmp_path / "audit.db"))
        doc = self._make_doc()
        db.store_document(scan_id, doc, owner_token=token, now=self.T_NEW)
        with pytest.raises(LeaseLostError):
            db.store_result(scan_id, self._make_result(doc))
        count = db.conn.execute(
            "SELECT COUNT(*) FROM audit_results WHERE scan_id=?", (scan_id,)
        ).fetchone()[0]
        assert count == 0
        db.close()

    def test_unleased_carry_forward_blocked_on_leased_scan(self, tmp_path):
        """carry_forward_results without owner_token must not write to a leased scan."""
        db, token, scan_id = self._setup_leased_scan(str(tmp_path / "audit.db"))
        with pytest.raises(LeaseLostError):
            db.carry_forward_results(scan_id, ["doc-x"])
        db.close()

    def test_unleased_sync_findings_blocked_on_leased_scan(self, tmp_path):
        """sync_findings without owner_token must not write to a leased scan."""
        db, token, scan_id = self._setup_leased_scan(str(tmp_path / "audit.db"))
        with pytest.raises(LeaseLostError):
            db.sync_findings(scan_id, [])
        db.close()

    def test_unleased_finish_scan_blocked_on_leased_scan(self, tmp_path):
        """finish_scan without owner_token must not complete a leased scan."""
        db, token, scan_id = self._setup_leased_scan(str(tmp_path / "audit.db"))
        with pytest.raises(LeaseLostError):
            db.finish_scan(scan_id, 0)
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "running"
        db.close()

    def test_unleased_fail_scan_blocked_on_leased_scan(self, tmp_path):
        """fail_scan without owner_token must not fail a leased scan."""
        db, token, scan_id = self._setup_leased_scan(str(tmp_path / "audit.db"))
        with pytest.raises(LeaseLostError):
            db.fail_scan(scan_id, "oops")
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "running"
        db.close()

    def test_unleased_complete_blocked_on_leased_scan(self, tmp_path):
        """complete_scan_with_findings without owner_token must not complete a leased scan."""
        db, token, scan_id = self._setup_leased_scan(str(tmp_path / "audit.db"))
        with pytest.raises(LeaseLostError):
            db.complete_scan_with_findings(scan_id, 0, [])
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "running"
        db.close()

    # ------------------------------------------------------------------
    # Leased caller cannot mutate a different token's scan
    # ------------------------------------------------------------------

    def test_leased_store_document_blocked_for_wrong_token(self, tmp_path):
        """A leased caller cannot write to a scan owned by a different token."""
        db_path = str(tmp_path / "audit.db")
        db1 = self._open(db_path)
        old_token = db1.try_start_scan(now=self.T_OLD)
        assert old_token is not None
        db1.start_scan(owner_token=old_token, now=self.T_OLD)
        db1.close()

        # Replacement acquires the lease
        db2 = self._open(db_path)
        new_token = db2.try_start_scan(now=self.T_NEW)
        assert new_token is not None
        new_scan_id = db2.start_scan(owner_token=new_token, now=self.T_NEW)

        # old_token tries to write to new_scan_id (old_token's lease is expired)
        with pytest.raises(LeaseLostError):
            db2.store_document(new_scan_id, self._make_doc(), owner_token=old_token, now=self.T_NEW)
        db2.close()

    # ------------------------------------------------------------------
    # Duplicate terminal transitions are rejected
    # ------------------------------------------------------------------

    def test_finish_scan_blocked_after_finish(self, tmp_path):
        """Calling finish_scan a second time on an already-completed scan raises."""
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.finish_scan(scan_id, 0)
        with pytest.raises(LeaseLostError):
            db.finish_scan(scan_id, 0)
        db.close()

    def test_finish_scan_blocked_after_fail(self, tmp_path):
        """Calling finish_scan on a failed scan raises LeaseLostError."""
        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        db.fail_scan(scan_id, "boom")
        with pytest.raises(LeaseLostError):
            db.finish_scan(scan_id, 0)
        db.close()

    # ------------------------------------------------------------------
    # Atomic rollback: workflow entries and scan status roll back together
    # ------------------------------------------------------------------

    def test_atomic_rollback_when_completion_fails(self, tmp_path):
        """If the status UPDATE fails, the workflow sync is also rolled back."""
        import sqlite3 as _sqlite3

        db = self._open(str(tmp_path / "audit.db"))
        scan_id = db.start_scan()
        doc = self._make_doc()
        db.store_document(scan_id, doc)
        result = self._make_result(doc, status="stale")

        # Patch _do_sync_findings to succeed but then make the UPDATE fail
        original_sync = db._do_sync_findings

        def _sync_then_fail(conn, sid, results, scanned_doc_ids, reanalyzed_doc_ids):
            # Run the real sync (inserts workflow rows)
            original_sync(conn, sid, results, scanned_doc_ids, reanalyzed_doc_ids)
            # Now corrupt the UPDATE so the outer transaction rolls back
            raise _sqlite3.OperationalError("injected failure")

        db._do_sync_findings = _sync_then_fail
        with pytest.raises(_sqlite3.OperationalError, match="injected failure"):
            db.complete_scan_with_findings(scan_id, 1, [result])

        # Both the workflow entry and the status change must be rolled back
        wf_count = db.conn.execute("SELECT COUNT(*) FROM finding_workflow").fetchone()[0]
        assert wf_count == 0, "Workflow row must not persist after rollback"
        row = db.conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        assert row[0] == "running", "Scan must remain 'running' after rollback"
        db.close()


class TestSanitizeError:
    """Defect 4: _sanitize_error redacts credentials."""

    def setup_method(self):
        from kb_audit.db import _sanitize_error
        self._fn = _sanitize_error

    def test_none_returns_none(self):
        assert self._fn(None) is None

    def test_long_error_truncated_to_500(self):
        result = self._fn("x" * 1000)
        assert result is not None
        assert len(result) <= 500

    def test_bearer_token_redacted(self):
        err = "HTTP 401: Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.abc123"
        result = self._fn(err)
        assert result is not None
        assert "eyJhbGciOiJSUzI1NiJ9" not in result
        assert "Bearer [REDACTED]" in result
        assert "401" in result

    def test_basic_token_redacted(self):
        err = "Auth failed with Basic dXNlcjpwYXNz"
        result = self._fn(err)
        assert result is not None
        assert "dXNlcjpwYXNz" not in result
        assert "Basic [REDACTED]" in result

    def test_api_key_assignment_redacted(self):
        err = "api_key=sk-secret-1234 caused HTTP 403"
        result = self._fn(err)
        assert result is not None
        assert "sk-secret-1234" not in result
        assert "403" in result

    def test_password_assignment_redacted(self):
        err = "Connection failed with password=s3cr3tP@ss! retrying"
        result = self._fn(err)
        assert result is not None
        assert "s3cr3tP@ss!" not in result
        assert "retrying" in result

    def test_secret_assignment_redacted(self):
        err = "secret=MYSECRETVALUE123 in request"
        result = self._fn(err)
        assert result is not None
        assert "MYSECRETVALUE123" not in result

    def test_non_secret_context_preserved(self):
        err = "Connection refused to host 10.0.0.1 port 5432"
        result = self._fn(err)
        assert result is not None
        assert "10.0.0.1" in result
        assert "5432" in result

    def test_non_secret_error_unchanged_except_truncation(self):
        err = "FileNotFoundError: /var/log/audit.log not found"
        result = self._fn(err)
        assert result == err

    def test_colon_form_password_redacted(self):
        err = "Auth header password: supersecret123 caused rejection"
        result = self._fn(err)
        assert result is not None
        assert "supersecret123" not in result
        assert "caused rejection" in result

    def test_colon_form_token_redacted(self):
        err = "Sending token: myPrivateToken1234 to upstream"
        result = self._fn(err)
        assert result is not None
        assert "myPrivateToken1234" not in result
        assert "upstream" in result

    def test_url_query_token_redacted(self):
        err = "GET https://api.example.com/data?token=s3cr3tT0k3n&format=json failed"
        result = self._fn(err)
        assert result is not None
        assert "s3cr3tT0k3n" not in result
        assert "format=json" in result

    def test_url_query_api_key_redacted(self):
        err = "Request https://svc.io/v1?api_key=APIKEY12345&page=1 returned 403"
        result = self._fn(err)
        assert result is not None
        assert "APIKEY12345" not in result
        assert "403" in result


# ---------------------------------------------------------------------------
# Actionability-aware _actionable_results tests
# ---------------------------------------------------------------------------


def _make_result(doc_id: str, status: str, trust_metadata: dict | None = None) -> AuditResult:
    doc = Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        content="content",
        source_type="notion",
        last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    return AuditResult(
        document=doc,
        signals=[],
        status=status,
        confidence=0.5,
        confidence_reason="test",
        trust_metadata=trust_metadata or {},
    )


class TestActionableResults:
    """_actionable_results respects requires_human_audit flag when present."""

    def test_stale_no_flag_included_legacy(self, tmp_path):
        """Legacy: stale result without flag is included."""
        from kb_audit.db import Database
        r = _make_result("doc-1", "stale")
        assert r in Database._actionable_results([r])

    def test_needs_review_no_flag_included_legacy(self, tmp_path):
        """Legacy: needs_review without flag is included."""
        from kb_audit.db import Database
        r = _make_result("doc-1", "needs_review")
        assert r in Database._actionable_results([r])

    def test_unknown_no_flag_included_legacy(self, tmp_path):
        """Legacy: unknown without flag is included."""
        from kb_audit.db import Database
        r = _make_result("doc-1", "unknown")
        assert r in Database._actionable_results([r])

    def test_current_never_included(self, tmp_path):
        """Current docs are never actionable."""
        from kb_audit.db import Database
        r = _make_result("doc-1", "current")
        assert r not in Database._actionable_results([r])

    def test_explicit_flag_true_included(self, tmp_path):
        """Explicit requires_human_audit=True → included."""
        from kb_audit.db import Database
        r = _make_result("doc-1", "unknown", {"requires_human_audit": True})
        assert r in Database._actionable_results([r])

    def test_explicit_flag_false_excluded(self, tmp_path):
        """Explicit requires_human_audit=False → excluded (no workflow finding created).

        Status-flagged (unknown) but not audit-required: classification ≠ actionability.
        """
        from kb_audit.db import Database
        r = _make_result("doc-1", "unknown", {"requires_human_audit": False})
        assert r not in Database._actionable_results([r])

    def test_low_importance_unknown_no_workflow_finding(self, tmp_path):
        """Unknown doc with requires_human_audit=False does not create a finding in DB."""
        db = Database(tmp_path / "test.db")
        db.connect()
        scan_id = db.start_scan()

        doc = Document(
            id="low-importance-unknown",
            title="Payments Team Notes",
            content="Some notes.",
            source_type="demo",
            last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        result = AuditResult(
            document=doc,
            signals=[],
            status="unknown",
            confidence=0.1,
            confidence_reason="No metadata",
            trust_metadata={
                "requires_human_audit": False,
                "audit_priority": "none",
                "importance_score": 0,
                "importance_reasons": [],
                "actionability_reason": "Insufficient importance signals",
            },
        )
        db.store_document(scan_id, doc)
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)

        findings = db.get_findings(scan_id=scan_id)
        assert len(findings) == 0, (
            "Low-importance unknown doc must not create a workflow finding"
        )
        db.close()

    def test_important_unknown_creates_workflow_finding(self, tmp_path):
        """Unknown doc with requires_human_audit=True creates a finding."""
        db = Database(tmp_path / "test.db")
        db.connect()
        scan_id = db.start_scan()

        doc = Document(
            id="important-unknown",
            title="Critical Undocumented Guide",
            content="Referenced extensively.",
            source_type="notion",
            last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        result = AuditResult(
            document=doc,
            signals=[],
            status="unknown",
            confidence=0.3,
            confidence_reason="No evidence",
            trust_metadata={
                "requires_human_audit": True,
                "audit_priority": "medium",
                "importance_score": 3,
                "importance_reasons": ["Referenced by 2 documents (+2)", "Has designated owner (+1)"],
                "actionability_reason": "Important document requiring review (score 3)",
            },
        )
        db.store_document(scan_id, doc)
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)

        findings = db.get_findings(scan_id=scan_id)
        assert len(findings) == 1
        db.close()

    def test_needs_review_soft_only_low_importance_no_finding(self, tmp_path):
        """needs_review from soft evidence on unimportant doc → no finding."""
        db = Database(tmp_path / "test.db")
        db.connect()
        scan_id = db.start_scan()

        doc = Document(
            id="soft-needs-review",
            title="Obscure Internal Note",
            content="Some stale content.",
            source_type="notion",
            last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        result = AuditResult(
            document=doc,
            signals=[],
            status="needs_review",
            confidence=0.35,
            confidence_reason="Old last-reviewed date",
            trust_metadata={
                "requires_human_audit": False,
                "audit_priority": "none",
                "importance_score": 0,
                "importance_reasons": [],
                "actionability_reason": "Insufficient importance signals",
            },
            trust_evidence={
                "summary": "Needs review because last reviewed is old.",
                "review_risks": ["Last reviewed 2022-01-01 (1200 days ago)"],
                "positive_evidence": [],
                "missing_evidence": [],
                "recommended_action": "Review before relying on this document",
            },
        )
        db.store_document(scan_id, doc)
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)

        findings = db.get_findings(scan_id=scan_id)
        assert len(findings) == 0, (
            "Soft-evidence needs_review on unimportant doc must not create a finding"
        )
        db.close()

    def test_needs_review_broken_link_always_creates_finding(self, tmp_path):
        """needs_review with broken link → always creates a finding."""
        db = Database(tmp_path / "test.db")
        db.connect()
        scan_id = db.start_scan()

        doc = Document(
            id="broken-link-doc",
            title="Guide With Broken Link",
            content="See https://dead.example/link",
            source_type="notion",
            last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        result = AuditResult(
            document=doc,
            signals=[StalenessSignal("broken_link", Severity.WARNING, "broken link")],
            status="needs_review",
            confidence=0.5,
            confidence_reason="Broken link",
            trust_metadata={
                "requires_human_audit": True,
                "audit_priority": "high",
                "importance_score": 0,
                "importance_reasons": [],
                "actionability_reason": "Has hard review risk",
            },
        )
        db.store_document(scan_id, doc)
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)

        findings = db.get_findings(scan_id=scan_id)
        assert len(findings) == 1
        db.close()


# ---------------------------------------------------------------------------
# Carry-forward trust_metadata preservation
# ---------------------------------------------------------------------------


class TestCarryForwardMetadataPreservation:
    """load_audit_results must restore trust_metadata from stored trust_data."""

    def _setup_scan1(self, tmp_path, doc_id: str, result: AuditResult) -> tuple:
        """Create scan 1, store doc+result, finish it. Returns (db, doc)."""
        db = Database(str(tmp_path / "audit.db"))
        db.connect()
        scan_id = db.start_scan()
        db.store_document(scan_id, result.document)
        db.store_result(scan_id, result)
        db.finish_scan(scan_id, 1)
        return db, scan_id

    def test_carry_forward_preserves_false_flag(self, tmp_path):
        """Carried-forward result with requires_human_audit=False keeps that flag."""
        metadata = {
            "requires_human_audit": False,
            "audit_priority": "none",
            "importance_score": 0,
            "importance_reasons": [],
            "actionability_reason": "Insufficient importance signals to require audit (score 0)",
        }
        result = _make_result("low-importance-doc", "unknown", metadata)
        db, _scan1 = self._setup_scan1(tmp_path, "low-importance-doc", result)

        scan2 = db.start_scan()
        db.store_document(scan2, result.document)
        db.carry_forward_results(scan2, ["low-importance-doc"])

        loaded = db.load_audit_results(scan2, ["low-importance-doc"])
        assert len(loaded) == 1
        assert loaded[0].trust_metadata.get("requires_human_audit") is False
        assert loaded[0].trust_metadata.get("importance_score") == 0
        db.close()

    def test_carry_forward_false_flag_not_actionable(self, tmp_path):
        """Carried-forward low-importance result must not appear in _actionable_results."""
        metadata = {
            "requires_human_audit": False,
            "audit_priority": "none",
            "importance_score": 0,
            "importance_reasons": [],
            "actionability_reason": "Insufficient importance signals to require audit (score 0)",
        }
        result = _make_result("low-importance-doc", "unknown", metadata)
        db, _scan1 = self._setup_scan1(tmp_path, "low-importance-doc", result)

        scan2 = db.start_scan()
        db.store_document(scan2, result.document)
        db.carry_forward_results(scan2, ["low-importance-doc"])

        loaded = db.load_audit_results(scan2, ["low-importance-doc"])
        assert Database._actionable_results(loaded) == []
        db.close()

    def test_carry_forward_preserves_true_flag(self, tmp_path):
        """Carried-forward result with requires_human_audit=True is actionable."""
        metadata = {
            "requires_human_audit": True,
            "audit_priority": "medium",
            "importance_score": 3,
            "importance_reasons": ["Referenced by 2 documents (+2)", "Has designated owner (+1)"],
            "actionability_reason": "Important document requiring review (score 3)",
        }
        result = _make_result("important-doc", "unknown", metadata)
        db, _scan1 = self._setup_scan1(tmp_path, "important-doc", result)

        scan2 = db.start_scan()
        db.store_document(scan2, result.document)
        db.carry_forward_results(scan2, ["important-doc"])

        loaded = db.load_audit_results(scan2, ["important-doc"])
        assert len(loaded) == 1
        assert loaded[0].trust_metadata.get("requires_human_audit") is True
        assert loaded[0].trust_metadata.get("importance_score") == 3
        assert loaded[0] in Database._actionable_results(loaded)
        db.close()

    def test_carry_forward_legacy_no_metadata(self, tmp_path):
        """Legacy stored result with no metadata key returns {} trust_metadata."""
        import json
        db = Database(str(tmp_path / "audit.db"))
        db.connect()
        scan_id = db.start_scan()

        doc = Document(
            id="legacy-doc",
            title="Legacy Doc",
            content="x",
            source_type="notion",
            last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        # Store legacy trust_data with only "evidence" key (no "metadata")
        legacy_trust_data = json.dumps({"evidence": {"summary": "old format", "review_risks": []}})
        db.store_document(scan_id, doc)
        db.conn.execute(
            """INSERT OR REPLACE INTO audit_results
               (document_id, scan_id, overall_status, signals,
                suggested_replacement_id, confidence, confidence_reason, trust_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("legacy-doc", scan_id, "unknown", "[]", None, 0.5, "no evidence", legacy_trust_data),
        )
        db.conn.commit()
        db.finish_scan(scan_id, 1)

        scan2 = db.start_scan()
        db.store_document(scan2, doc)
        db.carry_forward_results(scan2, ["legacy-doc"])

        loaded = db.load_audit_results(scan2, ["legacy-doc"])
        assert len(loaded) == 1
        assert loaded[0].trust_metadata == {}
        # Legacy fallback: unknown with no flag → included in actionable
        assert loaded[0] in Database._actionable_results(loaded)
        db.close()
