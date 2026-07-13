"""Backend-neutral storage conformance tests.

These tests exercise storage only through the AuditStorage protocol.  They
document the behavioral contract that any future storage backend (SQLite,
PostgreSQL, …) must satisfy.

To add a new backend, supply a new fixture that yields a connected AuditStorage
instance and run this module against it.  The tests contain no SQLite-specific
assertions, no raw connection inspection, and no SQL table reads.
"""

from __future__ import annotations

import pytest

from kb_audit.models import AuditResult, Document, Severity, StalenessSignal
from kb_audit.storage import AuditStorage, create_storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(
    doc_id: str = "doc-1",
    title: str = "Test Document",
    content: str = "some content",
    source_type: str = "conformance",
    url: str | None = "https://example.com/doc-1",
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        content=content,
        source_type=source_type,
        url=url,
    )


def _signal(
    signal_type: str = "age",
    severity: Severity = Severity.WARNING,
    message: str = "Document is old",
) -> StalenessSignal:
    return StalenessSignal(
        signal_type=signal_type,
        severity=severity,
        message=message,
        details={"days": 200},
    )


def _result(
    doc: Document,
    status: str = "stale",
    confidence: float = 0.8,
    trust_metadata: dict | None = None,
    trust_evidence: dict | None = None,
    signals: list[StalenessSignal] | None = None,
) -> AuditResult:
    return AuditResult(
        document=doc,
        signals=signals if signals is not None else [_signal()],
        status=status,
        confidence=confidence,
        confidence_reason="conformance test",
        trust_metadata=trust_metadata if trust_metadata is not None else {},
        trust_evidence=trust_evidence if trust_evidence is not None else {},
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def storage(tmp_path) -> AuditStorage:
    """A connected AuditStorage backed by a file-based database.

    File-based (not :memory:) so that close/reconnect and multi-connection
    lease semantics map cleanly to server-backed backends.

    These tests are written against the ``AuditStorage`` protocol only.
    SQLite is the only available backend today.  Future backends (Postgres,
    MongoDB, …) can reuse this entire suite by supplying a different fixture
    that yields a connected ``AuditStorage`` instance.
    """
    store = create_storage(tmp_path / "conformance.db")
    store.connect()
    yield store  # type: ignore[misc]
    store.close()


# ---------------------------------------------------------------------------
# Connection / lifecycle
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:
    def test_connected_store_satisfies_protocol(self, storage: AuditStorage):
        assert isinstance(storage, AuditStorage)

    def test_close_and_reconnect_preserves_data(self, tmp_path):
        """Committed data survives a close/reconnect cycle."""
        path = tmp_path / "persist.db"

        store = create_storage(path)
        store.connect()
        token = store.try_start_scan()
        assert token is not None
        scan_id = store.start_scan(owner_token=token)
        doc = _doc()
        store.store_document(scan_id, doc, owner_token=token)
        result = _result(doc)
        store.store_result(scan_id, result, owner_token=token)
        store.finish_scan(scan_id, document_count=1, owner_token=token)
        store.end_scan(token, last_scan_id=scan_id, error=None)
        store.close()

        store2 = create_storage(path)
        store2.connect()
        history = store2.get_scan_history(limit=1)
        assert len(history) == 1
        assert history[0]["scan_id"] == scan_id
        store2.close()


# ---------------------------------------------------------------------------
# Scan lifecycle
# ---------------------------------------------------------------------------

class TestScanLifecycle:
    def test_initial_scan_state_reports_idle(self, storage: AuditStorage):
        state = storage.get_scan_state()
        assert state["in_progress"] is False
        assert state["last_scan_id"] is None

    def test_try_start_scan_returns_token(self, storage: AuditStorage):
        token = storage.try_start_scan()
        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0

    def test_start_scan_returns_scan_id(self, storage: AuditStorage):
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        assert isinstance(scan_id, int)

    def test_finish_scan_appears_in_history(self, storage: AuditStorage):
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.finish_scan(scan_id, document_count=3, owner_token=token)
        storage.end_scan(token, last_scan_id=scan_id, error=None)

        history = storage.get_scan_history(limit=1)
        assert len(history) == 1
        entry = history[0]
        assert entry["scan_id"] == scan_id
        assert entry["document_count"] == 3

    def test_end_scan_releases_lease(self, storage: AuditStorage):
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.finish_scan(scan_id, document_count=0, owner_token=token)
        released = storage.end_scan(token, last_scan_id=scan_id, error=None)
        assert released is True

        state = storage.get_scan_state()
        assert state["in_progress"] is False

    def test_second_try_start_scan_blocked_while_lease_active(self, storage: AuditStorage):
        token = storage.try_start_scan()
        assert token is not None
        token2 = storage.try_start_scan()
        assert token2 is None

    def test_fail_scan_returns_true(self, storage: AuditStorage):
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        result = storage.fail_scan(scan_id, error="something went wrong", owner_token=token)
        assert result is True


# ---------------------------------------------------------------------------
# Document and result persistence
# ---------------------------------------------------------------------------

class TestDocumentResultPersistence:
    def _run_scan(self, storage: AuditStorage, doc: Document, result: AuditResult) -> int:
        """Helper: store one doc+result in a completed scan and return scan_id."""
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.store_document(scan_id, doc, owner_token=token)
        storage.store_result(scan_id, result, owner_token=token)
        storage.finish_scan(scan_id, document_count=1, owner_token=token)
        storage.end_scan(token, last_scan_id=scan_id, error=None)
        return scan_id

    def test_store_document_persists(self, storage: AuditStorage):
        doc = _doc()
        result = _result(doc)
        scan_id = self._run_scan(storage, doc, result)
        results = storage.get_scan_results(scan_id)
        assert len(results) == 1
        assert results[0]["id"] == doc.id

    def test_scan_results_contain_expected_fields(self, storage: AuditStorage):
        doc = _doc(
            doc_id="doc-fields",
            title="Fields Test",
            content="abc",
            url="https://example.com/fields",
        )
        trust_meta = {"lifecycle": "active"}
        trust_ev = {"positive_signals": ["recent"]}
        result = _result(
            doc,
            status="stale",
            confidence=0.75,
            trust_metadata=trust_meta,
            trust_evidence=trust_ev,
        )
        scan_id = self._run_scan(storage, doc, result)
        results = storage.get_scan_results(scan_id)
        assert len(results) == 1
        r = results[0]
        assert r["id"] == "doc-fields"
        assert r["title"] == "Fields Test"
        assert r["source_type"] == "conformance"
        assert r["url"] == "https://example.com/fields"
        assert r["overall_status"] == "stale"
        assert abs(r["confidence"] - 0.75) < 0.001
        assert isinstance(r["signals"], list)
        assert len(r["signals"]) == 1
        assert r["signals"][0]["signal_type"] == "age"
        assert r["trust_metadata"] == trust_meta
        assert r["trust_evidence"] == trust_ev

    def test_get_previous_hashes_returns_last_completed(self, storage: AuditStorage):
        doc = _doc(content="hashed content")
        result = _result(doc)
        self._run_scan(storage, doc, result)
        hashes = storage.get_previous_hashes()
        assert doc.id in hashes
        assert hashes[doc.id] == doc.content_hash


# ---------------------------------------------------------------------------
# Carry-forward
# ---------------------------------------------------------------------------

class TestCarryForward:
    def test_carry_forward_results_loads_correctly(self, storage: AuditStorage):
        """Results from scan 1 can be carried forward into scan 2."""
        doc = _doc()
        result = _result(doc)

        # Scan 1: store original result
        token1 = storage.try_start_scan()
        assert token1 is not None
        scan1 = storage.start_scan(owner_token=token1)
        storage.store_document(scan1, doc, owner_token=token1)
        storage.store_result(scan1, result, owner_token=token1)
        storage.finish_scan(scan1, document_count=1, owner_token=token1)
        storage.end_scan(token1, last_scan_id=scan1, error=None)

        # Scan 2: carry forward the unchanged document
        token2 = storage.try_start_scan()
        assert token2 is not None
        scan2 = storage.start_scan(owner_token=token2)
        storage.store_document(scan2, doc, owner_token=token2)
        count = storage.carry_forward_results(scan2, [doc.id], owner_token=token2)
        assert count == 1

        storage.finish_scan(scan2, document_count=1, owner_token=token2)
        storage.end_scan(token2, last_scan_id=scan2, error=None)

        loaded = storage.load_audit_results(scan2, [doc.id])
        assert len(loaded) == 1
        assert loaded[0].document.id == doc.id
        assert loaded[0].status == "stale"

    def test_get_previous_hashes_after_two_scans(self, storage: AuditStorage):
        doc = _doc(content="v1")
        result = _result(doc)

        token1 = storage.try_start_scan()
        assert token1 is not None
        scan1 = storage.start_scan(owner_token=token1)
        storage.store_document(scan1, doc, owner_token=token1)
        storage.store_result(scan1, result, owner_token=token1)
        storage.finish_scan(scan1, document_count=1, owner_token=token1)
        storage.end_scan(token1, last_scan_id=scan1, error=None)

        hashes = storage.get_previous_hashes()
        assert hashes.get(doc.id) == doc.content_hash


# ---------------------------------------------------------------------------
# Workflow synchronization
# ---------------------------------------------------------------------------

class TestWorkflowSync:
    def _complete_scan_actionable(self, storage: AuditStorage) -> tuple[int, AuditResult]:
        """Run a scan that creates an actionable workflow finding."""
        doc = _doc()
        result = _result(
            doc,
            status="stale",
            trust_metadata={"requires_human_audit": True},
        )
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.store_document(scan_id, doc, owner_token=token)
        storage.store_result(scan_id, result, owner_token=token)
        storage.complete_scan_with_findings(
            scan_id=scan_id,
            document_count=1,
            results=[result],
            scanned_doc_ids={doc.id},
            owner_token=token,
        )
        storage.end_scan(token, last_scan_id=scan_id, error=None)
        return scan_id, result

    def test_complete_scan_creates_finding(self, storage: AuditStorage):
        scan_id, result = self._complete_scan_actionable(storage)
        findings = storage.get_findings(include_all=True)
        assert len(findings) == 1
        assert findings[0]["document_id"] == result.document.id
        assert findings[0]["workflow_state"] == "open"

    def test_update_workflow_acknowledge(self, storage: AuditStorage):
        _, result = self._complete_scan_actionable(storage)
        findings = storage.get_findings(include_all=True)
        key = findings[0]["finding_key"]

        updated = storage.update_workflow(
            key, state="acknowledged", note="reviewed by team"
        )
        assert updated is True

        finding = storage.get_finding(key)
        assert finding is not None
        assert finding["workflow_state"] == "acknowledged"
        assert finding["note"] == "reviewed by team"

    def test_update_workflow_missing_key_returns_false(self, storage: AuditStorage):
        result = storage.update_workflow("nonexistent-key-xyz", state="acknowledged")
        assert result is False

    def test_changed_evidence_reopens_terminal_finding(self, storage: AuditStorage):
        """A dismissed finding should reopen when evidence changes on rescan."""
        doc = _doc()

        # Scan 1: create finding
        result1 = _result(
            doc,
            status="stale",
            trust_metadata={"requires_human_audit": True},
            signals=[_signal("age", Severity.WARNING, "old")],
        )
        token1 = storage.try_start_scan()
        assert token1 is not None
        scan1 = storage.start_scan(owner_token=token1)
        storage.store_document(scan1, doc, owner_token=token1)
        storage.store_result(scan1, result1, owner_token=token1)
        storage.complete_scan_with_findings(
            scan_id=scan1,
            document_count=1,
            results=[result1],
            scanned_doc_ids={doc.id},
            owner_token=token1,
        )
        storage.end_scan(token1, last_scan_id=scan1, error=None)

        # Dismiss the finding
        findings = storage.get_findings(include_all=True)
        key = findings[0]["finding_key"]
        storage.update_workflow(key, state="dismissed", dismissal_reason="won't fix")

        # Scan 2: same doc, different signals → evidence hash changes
        result2 = _result(
            doc,
            status="stale",
            trust_metadata={"requires_human_audit": True},
            signals=[_signal("duplicate", Severity.CRITICAL, "exact copy found")],
        )
        token2 = storage.try_start_scan()
        assert token2 is not None
        scan2 = storage.start_scan(owner_token=token2)
        storage.store_document(scan2, doc, owner_token=token2)
        storage.store_result(scan2, result2, owner_token=token2)
        storage.complete_scan_with_findings(
            scan_id=scan2,
            document_count=1,
            results=[result2],
            scanned_doc_ids={doc.id},
            owner_token=token2,
        )
        storage.end_scan(token2, last_scan_id=scan2, error=None)

        reopened = storage.get_finding(key)
        assert reopened is not None
        assert reopened["workflow_state"] == "open"

    def test_get_workflow_summary(self, storage: AuditStorage):
        self._complete_scan_actionable(storage)
        summary = storage.get_workflow_summary(include_all=True)
        assert "open" in summary
        assert summary["open"] >= 1


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

class TestMaintenance:
    def test_clear_all_if_idle_when_no_scan(self, storage: AuditStorage):
        result = storage.clear_all_if_idle()
        assert result is True

    def test_clear_all_if_idle_removes_data(self, storage: AuditStorage):
        doc = _doc()
        result = _result(doc)

        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.store_document(scan_id, doc, owner_token=token)
        storage.store_result(scan_id, result, owner_token=token)
        storage.complete_scan_with_findings(
            scan_id=scan_id,
            document_count=1,
            results=[result],
            scanned_doc_ids={doc.id},
            owner_token=token,
        )
        storage.end_scan(token, last_scan_id=scan_id, error=None)

        cleared = storage.clear_all_if_idle()
        assert cleared is True

        history = storage.get_scan_history()
        assert history == []
        findings = storage.get_findings(include_all=True)
        assert findings == []

    def test_clear_all_if_idle_blocked_by_live_lease(self, storage: AuditStorage):
        token = storage.try_start_scan()
        assert token is not None
        # Lease is live — clear should be blocked
        result = storage.clear_all_if_idle()
        assert result is False
