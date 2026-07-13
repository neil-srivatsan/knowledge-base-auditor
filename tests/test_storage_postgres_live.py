"""Live PostgreSQL conformance tests for PostgresStorage.

These tests require a running PostgreSQL server, psycopg, and the
``KB_AUDIT_POSTGRES_TEST_URL`` environment variable pointing at a
**dedicated test database**.  They are skipped automatically when either
precondition is absent, so the default pytest run remains green without
any Postgres infrastructure.

Usage
-----
1. Create a local test database (once)::

       createdb kbaudit_test
       # or: psql -c 'CREATE DATABASE kbaudit_test;'

2. Install the postgres extra (once)::

       pip install -e ".[postgres]"

3. Run the live suite::

       KB_AUDIT_POSTGRES_TEST_URL=postgresql://localhost/kbaudit_test \\
           .venv/bin/pytest -q -m postgres_live tests/test_storage_postgres_live.py

The fixture calls ``clear_all()`` before and after each test to prevent
cross-test contamination.  Use a dedicated test database — not your
development or production database.

``PostgresStorage`` is constructed directly (not through ``create_storage()``)
to keep live conformance focused on the backend itself and avoid coupling every
test to factory routing.  Separate unit tests in ``test_storage_factory.py``
and ``test_storage_postgres.py`` cover factory routing for Postgres URLs.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Guard 1: psycopg availability.
# The try/except must come before any import that uses psycopg so that the
# module is safe to import even when psycopg is not installed.
# ---------------------------------------------------------------------------

try:
    import psycopg as _psycopg_probe  # noqa: F401
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False

# ---------------------------------------------------------------------------
# Guard 2: live URL.
# ---------------------------------------------------------------------------

_POSTGRES_URL: str | None = os.environ.get("KB_AUDIT_POSTGRES_TEST_URL")

if not _PSYCOPG_AVAILABLE:
    pytest.skip(
        "psycopg not installed — install with: pip install -e '.[postgres]'",
        allow_module_level=True,
    )

if not _POSTGRES_URL:
    pytest.skip(
        "Set KB_AUDIT_POSTGRES_TEST_URL to run live Postgres tests, "
        "e.g. KB_AUDIT_POSTGRES_TEST_URL=postgresql://localhost/kbaudit_test",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Everything below only executes when both guards pass.
# ---------------------------------------------------------------------------

from kb_audit.models import AuditResult, Document, Severity, StalenessSignal  # noqa: E402
from kb_audit.storage import AuditStorage  # noqa: E402
from kb_audit.storage.postgres import PostgresStorage  # noqa: E402

pytestmark = pytest.mark.postgres_live


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_storage_conformance.py, kept self-contained)
# ---------------------------------------------------------------------------

def _doc(
    doc_id: str = "doc-1",
    title: str = "Test Document",
    content: str = "some content",
    source_type: str = "conformance",
    url: str | None = "https://example.com/doc-1",
) -> Document:
    return Document(id=doc_id, title=title, content=content,
                    source_type=source_type, url=url)


def _signal(
    signal_type: str = "age",
    severity: Severity = Severity.WARNING,
    message: str = "Document is old",
) -> StalenessSignal:
    return StalenessSignal(signal_type=signal_type, severity=severity,
                           message=message, details={"days": 200})


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
        confidence_reason="live conformance test",
        trust_metadata=trust_metadata if trust_metadata is not None else {},
        trust_evidence=trust_evidence if trust_evidence is not None else {},
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def storage() -> PostgresStorage:  # type: ignore[return]
    """Connected PostgresStorage cleared before and after each test.

    Constructs ``PostgresStorage`` directly — not through ``create_storage()``
    — to keep live conformance isolated to the backend; factory routing is
    covered separately in the unit test suite.
    """
    assert _POSTGRES_URL is not None  # satisfied by module-level skip
    store = PostgresStorage(_POSTGRES_URL)
    store.connect()
    store.clear_all()
    yield store  # type: ignore[misc]
    try:
        store.clear_all()
    except Exception:
        pass
    store.close()


# ---------------------------------------------------------------------------
# Factory wiring guard
# ---------------------------------------------------------------------------

class TestFactoryWired:
    """Postgres URLs are wired to PostgresStorage (Step 10)."""

    def test_create_storage_returns_postgres_storage(self):
        from kb_audit.storage import create_storage
        store = create_storage(_POSTGRES_URL)  # type: ignore[arg-type]
        assert isinstance(store, PostgresStorage)
        assert not store.is_connected  # factory must not call .connect()


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:
    def test_connected_store_satisfies_protocol(self, storage: PostgresStorage):
        assert isinstance(storage, AuditStorage)

    def test_close_and_reconnect_preserves_data(self):
        """Committed data survives a close/reconnect cycle."""
        assert _POSTGRES_URL is not None
        store = PostgresStorage(_POSTGRES_URL)
        store.connect()
        store.clear_all()

        token = store.try_start_scan()
        assert token is not None
        scan_id = store.start_scan(owner_token=token)
        doc = _doc()
        store.store_document(scan_id, doc, owner_token=token)
        store.store_result(scan_id, _result(doc), owner_token=token)
        store.finish_scan(scan_id, document_count=1, owner_token=token)
        store.end_scan(token, last_scan_id=scan_id, error=None)
        store.close()

        store2 = PostgresStorage(_POSTGRES_URL)
        store2.connect()
        try:
            history = store2.get_scan_history(limit=1)
            assert len(history) == 1
            assert history[0]["scan_id"] == scan_id
        finally:
            store2.clear_all()
            store2.close()


# ---------------------------------------------------------------------------
# Scan lifecycle
# ---------------------------------------------------------------------------

class TestScanLifecycle:
    def test_initial_scan_state_reports_idle(self, storage: PostgresStorage):
        state = storage.get_scan_state()
        assert state["in_progress"] is False
        assert state["last_scan_id"] is None

    def test_try_start_scan_returns_token(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        assert isinstance(token, str) and len(token) > 0

    def test_start_scan_returns_int_id(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        assert isinstance(storage.start_scan(owner_token=token), int)

    def test_finish_scan_appears_in_history(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.finish_scan(scan_id, document_count=3, owner_token=token)
        storage.end_scan(token, last_scan_id=scan_id, error=None)
        history = storage.get_scan_history(limit=1)
        assert len(history) == 1
        assert history[0]["scan_id"] == scan_id
        assert history[0]["document_count"] == 3

    def test_end_scan_releases_lease(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.finish_scan(scan_id, document_count=0, owner_token=token)
        assert storage.end_scan(token, last_scan_id=scan_id, error=None) is True
        assert storage.get_scan_state()["in_progress"] is False

    def test_second_try_start_blocked_while_lease_active(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        assert storage.try_start_scan() is None

    def test_owns_live_lease_true_while_held(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        assert storage.owns_live_lease(token) is True

    def test_fail_scan_returns_true(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        assert storage.fail_scan(scan_id, error="test error", owner_token=token) is True

    def test_scan_history_timestamps_are_iso_strings(self, storage: PostgresStorage):
        """psycopg TIMESTAMPTZ values must be normalized to ISO strings."""
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.finish_scan(scan_id, document_count=0, owner_token=token)
        storage.end_scan(token, last_scan_id=scan_id, error=None)
        history = storage.get_scan_history(limit=1)
        assert len(history) == 1
        entry = history[0]
        assert isinstance(entry["started_at"], str), (
            f"started_at should be str, got {type(entry['started_at'])}"
        )
        assert isinstance(entry["finished_at"], str), (
            f"finished_at should be str, got {type(entry['finished_at'])}"
        )


# ---------------------------------------------------------------------------
# Document and result persistence
# ---------------------------------------------------------------------------

class TestDocumentResultPersistence:
    def _run_scan(
        self,
        storage: PostgresStorage,
        doc: Document,
        result: AuditResult,
    ) -> int:
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.store_document(scan_id, doc, owner_token=token)
        storage.store_result(scan_id, result, owner_token=token)
        storage.finish_scan(scan_id, document_count=1, owner_token=token)
        storage.end_scan(token, last_scan_id=scan_id, error=None)
        return scan_id

    def test_store_document_persists(self, storage: PostgresStorage):
        doc = _doc()
        scan_id = self._run_scan(storage, doc, _result(doc))
        results = storage.get_scan_results(scan_id)
        assert len(results) == 1
        assert results[0]["id"] == doc.id

    def test_scan_results_contain_expected_fields(self, storage: PostgresStorage):
        doc = _doc(doc_id="doc-fields", title="Fields Test", content="abc",
                   url="https://example.com/fields")
        trust_meta = {"lifecycle": "active"}
        trust_ev = {"positive_signals": ["recent"]}
        result = _result(doc, status="stale", confidence=0.75,
                         trust_metadata=trust_meta, trust_evidence=trust_ev)
        scan_id = self._run_scan(storage, doc, result)
        results = storage.get_scan_results(scan_id)
        assert len(results) == 1
        r = results[0]
        assert r["id"] == "doc-fields"
        assert r["title"] == "Fields Test"
        assert r["url"] == "https://example.com/fields"
        assert r["overall_status"] == "stale"
        assert abs(r["confidence"] - 0.75) < 0.001
        assert isinstance(r["signals"], list) and len(r["signals"]) == 1
        assert r["signals"][0]["signal_type"] == "age"
        assert r["trust_metadata"] == trust_meta
        assert r["trust_evidence"] == trust_ev

    def test_get_previous_hashes_returns_last_completed(self, storage: PostgresStorage):
        doc = _doc(content="hashed content")
        self._run_scan(storage, doc, _result(doc))
        hashes = storage.get_previous_hashes()
        assert doc.id in hashes
        assert hashes[doc.id] == doc.content_hash


# ---------------------------------------------------------------------------
# Carry-forward
# ---------------------------------------------------------------------------

class TestCarryForward:
    def test_carry_forward_and_load_audit_results(self, storage: PostgresStorage):
        doc = _doc()
        result = _result(doc)

        # Scan 1
        token1 = storage.try_start_scan()
        assert token1 is not None
        scan1 = storage.start_scan(owner_token=token1)
        storage.store_document(scan1, doc, owner_token=token1)
        storage.store_result(scan1, result, owner_token=token1)
        storage.finish_scan(scan1, document_count=1, owner_token=token1)
        storage.end_scan(token1, last_scan_id=scan1, error=None)

        # Scan 2: carry forward
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


# ---------------------------------------------------------------------------
# Workflow sync and findings
# ---------------------------------------------------------------------------

class TestWorkflowSync:
    def _complete_scan_actionable(
        self, storage: PostgresStorage
    ) -> tuple[int, AuditResult]:
        doc = _doc()
        result = _result(doc, status="stale",
                         trust_metadata={"requires_human_audit": True})
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

    def test_complete_scan_creates_open_finding(self, storage: PostgresStorage):
        _, result = self._complete_scan_actionable(storage)
        findings = storage.get_findings(include_all=True)
        assert len(findings) == 1
        assert findings[0]["document_id"] == result.document.id
        assert findings[0]["workflow_state"] == "open"

    def test_update_workflow_acknowledge(self, storage: PostgresStorage):
        self._complete_scan_actionable(storage)
        findings = storage.get_findings(include_all=True)
        key = findings[0]["finding_key"]
        assert storage.update_workflow(key, state="acknowledged", note="reviewed") is True
        finding = storage.get_finding(key)
        assert finding is not None
        assert finding["workflow_state"] == "acknowledged"
        assert finding["note"] == "reviewed"

    def test_update_workflow_missing_key_returns_false(self, storage: PostgresStorage):
        assert storage.update_workflow("nonexistent-key-xyz", state="acknowledged") is False

    def test_get_workflow_summary(self, storage: PostgresStorage):
        self._complete_scan_actionable(storage)
        summary = storage.get_workflow_summary(include_all=True)
        assert "open" in summary
        assert summary["open"] >= 1

    def test_changed_evidence_reopens_dismissed_finding(self, storage: PostgresStorage):
        doc = _doc()
        result1 = _result(doc, status="stale",
                          trust_metadata={"requires_human_audit": True},
                          signals=[_signal("age", Severity.WARNING, "old")])
        token1 = storage.try_start_scan()
        assert token1 is not None
        scan1 = storage.start_scan(owner_token=token1)
        storage.store_document(scan1, doc, owner_token=token1)
        storage.store_result(scan1, result1, owner_token=token1)
        storage.complete_scan_with_findings(
            scan_id=scan1, document_count=1, results=[result1],
            scanned_doc_ids={doc.id}, owner_token=token1,
        )
        storage.end_scan(token1, last_scan_id=scan1, error=None)

        key = storage.get_findings(include_all=True)[0]["finding_key"]
        storage.update_workflow(key, state="dismissed", dismissal_reason="known")

        result2 = _result(doc, status="stale",
                          trust_metadata={"requires_human_audit": True},
                          signals=[_signal("duplicate", Severity.CRITICAL, "copy found")])
        token2 = storage.try_start_scan()
        assert token2 is not None
        scan2 = storage.start_scan(owner_token=token2)
        storage.store_document(scan2, doc, owner_token=token2)
        storage.store_result(scan2, result2, owner_token=token2)
        storage.complete_scan_with_findings(
            scan_id=scan2, document_count=1, results=[result2],
            scanned_doc_ids={doc.id}, owner_token=token2,
        )
        storage.end_scan(token2, last_scan_id=scan2, error=None)

        reopened = storage.get_finding(key)
        assert reopened is not None
        assert reopened["workflow_state"] == "open"


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

class TestMaintenance:
    def test_clear_all_if_idle_when_no_scan(self, storage: PostgresStorage):
        assert storage.clear_all_if_idle() is True

    def test_clear_all_if_idle_removes_scan_data(self, storage: PostgresStorage):
        doc = _doc()
        result = _result(doc)
        token = storage.try_start_scan()
        assert token is not None
        scan_id = storage.start_scan(owner_token=token)
        storage.store_document(scan_id, doc, owner_token=token)
        storage.store_result(scan_id, result, owner_token=token)
        storage.complete_scan_with_findings(
            scan_id=scan_id, document_count=1, results=[result],
            scanned_doc_ids={doc.id}, owner_token=token,
        )
        storage.end_scan(token, last_scan_id=scan_id, error=None)

        assert storage.clear_all_if_idle() is True
        assert storage.get_scan_history() == []
        assert storage.get_findings(include_all=True) == []

    def test_clear_all_if_idle_blocked_by_live_lease(self, storage: PostgresStorage):
        token = storage.try_start_scan()
        assert token is not None
        assert storage.clear_all_if_idle() is False

    def test_prune_scans_keeps_newest_terminal(self, storage: PostgresStorage):
        """prune_scans(keep=1) removes older completed scans."""
        scan_ids = []
        for _ in range(3):
            token = storage.try_start_scan()
            assert token is not None
            sid = storage.start_scan(owner_token=token)
            storage.finish_scan(sid, document_count=0, owner_token=token)
            storage.end_scan(token, last_scan_id=sid, error=None)
            scan_ids.append(sid)

        deleted = storage.prune_scans(keep=1)
        assert deleted == 2

        history_ids = {h["scan_id"] for h in storage.get_scan_history(limit=10)}
        assert scan_ids[-1] in history_ids    # newest kept
        assert scan_ids[0] not in history_ids  # oldest removed

    def test_prune_scans_never_deletes_running_scans(self, storage: PostgresStorage):
        """A running scan must survive prune_scans(keep=0)."""
        # Create one completed scan
        token1 = storage.try_start_scan()
        assert token1 is not None
        sid1 = storage.start_scan(owner_token=token1)
        storage.finish_scan(sid1, document_count=0, owner_token=token1)
        storage.end_scan(token1, last_scan_id=sid1, error=None)

        # Start (but don't finish) a second scan
        token2 = storage.try_start_scan()
        assert token2 is not None
        sid2 = storage.start_scan(owner_token=token2)

        storage.prune_scans(keep=0)

        # Finish the running scan — it must survive
        storage.finish_scan(sid2, document_count=0, owner_token=token2)
        storage.end_scan(token2, last_scan_id=sid2, error=None)
        history_ids = {h["scan_id"] for h in storage.get_scan_history(limit=10)}
        assert sid2 in history_ids


# ---------------------------------------------------------------------------
# Scan history and diff
# ---------------------------------------------------------------------------

class TestScanHistoryAndDiff:
    def test_get_scan_diff_returns_status_changes(self, storage: PostgresStorage):
        doc = _doc()

        token1 = storage.try_start_scan()
        assert token1 is not None
        scan1 = storage.start_scan(owner_token=token1)
        storage.store_document(scan1, doc, owner_token=token1)
        storage.store_result(scan1, _result(doc, status="current"), owner_token=token1)
        storage.finish_scan(scan1, document_count=1, owner_token=token1)
        storage.end_scan(token1, last_scan_id=scan1, error=None)

        token2 = storage.try_start_scan()
        assert token2 is not None
        scan2 = storage.start_scan(owner_token=token2)
        storage.store_document(scan2, doc, owner_token=token2)
        storage.store_result(scan2, _result(doc, status="stale"), owner_token=token2)
        storage.finish_scan(scan2, document_count=1, owner_token=token2)
        storage.end_scan(token2, last_scan_id=scan2, error=None)

        diff = storage.get_scan_diff(scan2, scan1)
        assert len(diff) == 1
        assert diff[0]["document_id"] == doc.id
        assert diff[0]["old_status"] == "current"
        assert diff[0]["new_status"] == "stale"
        assert "title" in diff[0]

    def test_get_scan_history_populates_changes(self, storage: PostgresStorage):
        doc = _doc()

        token1 = storage.try_start_scan()
        assert token1 is not None
        scan1 = storage.start_scan(owner_token=token1)
        storage.store_document(scan1, doc, owner_token=token1)
        storage.store_result(scan1, _result(doc, status="current"), owner_token=token1)
        storage.finish_scan(scan1, document_count=1, owner_token=token1)
        storage.end_scan(token1, last_scan_id=scan1, error=None)

        token2 = storage.try_start_scan()
        assert token2 is not None
        scan2 = storage.start_scan(owner_token=token2)
        storage.store_document(scan2, doc, owner_token=token2)
        storage.store_result(scan2, _result(doc, status="stale"), owner_token=token2)
        storage.finish_scan(scan2, document_count=1, owner_token=token2)
        storage.end_scan(token2, last_scan_id=scan2, error=None)

        history = storage.get_scan_history(limit=10)
        assert len(history) == 2
        newest = next(h for h in history if h["scan_id"] == scan2)
        oldest = next(h for h in history if h["scan_id"] == scan1)
        assert isinstance(newest["changes"], list)
        assert oldest["changes"] is None
