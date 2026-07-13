"""Tests for storage contracts (protocols)."""

from __future__ import annotations

import kb_audit.storage as storage_pkg
from kb_audit.db import Database
from kb_audit.storage.contracts import (
    AuditStorage,
    ConnectionLifecycle,
    DocumentResultStore,
    MaintenanceStore,
    ScanLeaseStore,
    ScanStore,
    WorkflowStore,
)


class TestPackageImports:
    def test_storage_package_importable(self):
        # Importing the package must not raise.
        import kb_audit.storage  # noqa: F401

    def test_all_contracts_re_exported(self):
        for name in [
            "AuditStorage",
            "ConnectionLifecycle",
            "DocumentResultStore",
            "MaintenanceStore",
            "ScanLeaseStore",
            "ScanStore",
            "WorkflowStore",
        ]:
            assert hasattr(storage_pkg, name), f"kb_audit.storage missing {name!r}"

    def test_helpers_re_exported(self):
        for name in [
            "initialize_schema",
            "sanitize_error",
            "serialize_signals",
            "deserialize_signals",
            "serialize_trust_data",
            "deserialize_trust_data",
        ]:
            assert hasattr(storage_pkg, name), f"kb_audit.storage missing {name!r}"


class TestDatabaseStructuralCompatibility:
    """Database must be structurally compatible with AuditStorage and each sub-protocol."""

    def _db(self) -> Database:
        db = Database(":memory:")
        db.connect()
        return db

    def test_isinstance_connection_lifecycle(self):
        db = self._db()
        assert isinstance(db, ConnectionLifecycle)
        db.close()

    def test_isinstance_scan_lease_store(self):
        db = self._db()
        assert isinstance(db, ScanLeaseStore)
        db.close()

    def test_isinstance_scan_store(self):
        db = self._db()
        assert isinstance(db, ScanStore)
        db.close()

    def test_isinstance_document_result_store(self):
        db = self._db()
        assert isinstance(db, DocumentResultStore)
        db.close()

    def test_isinstance_workflow_store(self):
        db = self._db()
        assert isinstance(db, WorkflowStore)
        db.close()

    def test_isinstance_maintenance_store(self):
        db = self._db()
        assert isinstance(db, MaintenanceStore)
        db.close()

    def test_isinstance_audit_storage(self):
        db = self._db()
        assert isinstance(db, AuditStorage)
        db.close()

    def test_assignable_as_audit_storage(self):
        # Exercise connect/close through the protocol type.
        store: AuditStorage = Database(":memory:")  # type: ignore[assignment]
        store.connect()
        store.close()


class TestProtocolMethodPresence:
    """Spot-check that the protocol methods we care about actually exist on Database."""

    def _methods(self) -> set[str]:
        return {name for name in dir(Database) if not name.startswith("_")}

    def test_connection_methods(self):
        m = self._methods()
        assert "connect" in m
        assert "close" in m

    def test_scan_lease_methods(self):
        m = self._methods()
        for name in ("try_start_scan", "renew_lease", "owns_live_lease",
                     "end_scan", "reset_scan_state", "get_scan_state"):
            assert name in m, f"Database missing {name!r}"

    def test_scan_store_methods(self):
        m = self._methods()
        for name in ("start_scan", "finish_scan", "fail_scan",
                     "complete_scan_with_findings", "get_scan_history", "get_scan_diff"):
            assert name in m, f"Database missing {name!r}"

    def test_document_result_methods(self):
        m = self._methods()
        for name in ("store_document", "get_previous_hashes", "store_result",
                     "carry_forward_results", "load_audit_results", "get_scan_results"):
            assert name in m, f"Database missing {name!r}"

    def test_workflow_methods(self):
        m = self._methods()
        for name in ("sync_findings", "update_workflow", "get_findings",
                     "get_finding", "get_workflow_summary"):
            assert name in m, f"Database missing {name!r}"

    def test_maintenance_methods(self):
        m = self._methods()
        for name in ("clear_all", "clear_all_if_idle", "prune_scans"):
            assert name in m, f"Database missing {name!r}"
