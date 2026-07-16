"""Storage contracts for kb_audit.

Runtime-checkable Protocol definitions for the persistence boundary.
These describe what callers depend on, not how the storage is implemented.

Structural compatibility note
------------------------------
Both ``SqliteStorage`` and ``PostgresStorage`` satisfy the ``AuditStorage``
protocol below structurally (duck-typing).  They do not explicitly inherit
from it.  Structural compatibility is sufficient for both runtime isinstance
checks (via ``@runtime_checkable``) and as a documentation contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kb_audit.models import AuditResult, Document, WorkflowState


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


#: Singleton sentinel used as the default for every keyword argument of
#: ``update_workflow()``.  Import this alongside the storage class when
#: you need to pass ``_UNSET`` explicitly.
_UNSET: _UnsetType = _UnsetType()


@runtime_checkable
class ConnectionLifecycle(Protocol):
    def connect(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class ScanLeaseStore(Protocol):
    def try_start_scan(self, now: str | None = None) -> str | None: ...
    def renew_lease(self, owner_token: str, now: str | None = None) -> bool: ...
    def owns_live_lease(self, owner_token: str, now: str | None = None) -> bool: ...
    def end_scan(
        self,
        owner_token: str,
        last_scan_id: int | None,
        error: str | None,
        now: str | None = None,
    ) -> bool: ...
    def reset_scan_state(self) -> None: ...
    def get_scan_state(self, now: str | None = None) -> dict: ...


@runtime_checkable
class ScanStore(Protocol):
    def start_scan(
        self, owner_token: str | None = None, now: str | None = None
    ) -> int: ...
    def finish_scan(
        self,
        scan_id: int,
        document_count: int,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None: ...
    def fail_scan(
        self,
        scan_id: int,
        error: str | None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> bool: ...
    def complete_scan_with_findings(
        self,
        scan_id: int,
        document_count: int,
        results: list[AuditResult],
        scanned_doc_ids: set[str] | None = None,
        reanalyzed_doc_ids: set[str] | None = None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> dict: ...
    def get_scan_history(self, limit: int = 10) -> list[dict]: ...
    def get_scan_diff(self, scan_id: int, prev_scan_id: int) -> list[dict]: ...


@runtime_checkable
class DocumentResultStore(Protocol):
    def store_document(
        self,
        scan_id: int,
        doc: Document,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None: ...
    def get_previous_hashes(self) -> dict[str, str]: ...
    def store_result(
        self,
        scan_id: int,
        result: AuditResult,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> None: ...
    def carry_forward_results(
        self,
        scan_id: int,
        doc_ids: list[str],
        owner_token: str | None = None,
        now: str | None = None,
    ) -> int: ...
    def load_audit_results(
        self, scan_id: int, doc_ids: list[str]
    ) -> list[AuditResult]: ...
    def get_scan_results(self, scan_id: int) -> list[dict]: ...


@runtime_checkable
class WorkflowStore(Protocol):
    def sync_findings(
        self,
        scan_id: int,
        results: list[AuditResult],
        scanned_doc_ids: set[str] | None = None,
        reanalyzed_doc_ids: set[str] | None = None,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> dict: ...
    def update_workflow(
        self,
        finding_key: str,
        *,
        state: WorkflowState | None | _UnsetType = ...,
        note: str | None | _UnsetType = ...,
        assigned_owner: str | None | _UnsetType = ...,
        due_date: str | None | _UnsetType = ...,
        snoozed_until: str | None | _UnsetType = ...,
        dismissal_reason: str | None | _UnsetType = ...,
    ) -> bool: ...
    def get_findings(
        self,
        *,
        scan_id: int | None = None,
        states: list[str] | None = None,
        include_all: bool = False,
    ) -> list[dict]: ...
    def get_finding(self, finding_key: str) -> dict | None: ...
    def get_workflow_summary(
        self,
        scan_id: int | None = None,
        include_all: bool = False,
    ) -> dict: ...


@runtime_checkable
class MaintenanceStore(Protocol):
    def clear_all(self) -> None: ...
    def clear_all_if_idle(self, now: str | None = None) -> bool: ...
    def prune_scans(
        self,
        keep: int = 10,
        owner_token: str | None = None,
        now: str | None = None,
    ) -> int: ...


@runtime_checkable
class AuditStorage(
    ConnectionLifecycle,
    ScanLeaseStore,
    ScanStore,
    DocumentResultStore,
    WorkflowStore,
    MaintenanceStore,
    Protocol,
):
    """Combined storage protocol covering the full kb_audit persistence surface."""
