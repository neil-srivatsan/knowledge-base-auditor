"""Storage package for kb_audit — SQLite persistence layer."""

from kb_audit.storage.factory import create_storage
from kb_audit.storage.sqlite import SqliteStorage
from kb_audit.storage.contracts import (
    AuditStorage,
    ConnectionLifecycle,
    DocumentResultStore,
    MaintenanceStore,
    ScanLeaseStore,
    ScanStore,
    WorkflowStore,
)
from kb_audit.storage.schema import initialize_schema
from kb_audit.storage.serialization import (
    deserialize_document_metadata,
    deserialize_signal_records,
    deserialize_signals,
    deserialize_trust_data,
    deserialize_trust_data_blob,
    sanitize_error,
    serialize_document_metadata,
    serialize_signals,
    serialize_trust_data,
)

__all__ = [
    # Factory
    "create_storage",
    # Implementation
    "SqliteStorage",
    # Contracts
    "AuditStorage",
    "ConnectionLifecycle",
    "DocumentResultStore",
    "MaintenanceStore",
    "ScanLeaseStore",
    "ScanStore",
    "WorkflowStore",
    # Schema
    "initialize_schema",
    # Serialization
    "deserialize_document_metadata",
    "deserialize_signal_records",
    "deserialize_signals",
    "deserialize_trust_data",
    "deserialize_trust_data_blob",
    "sanitize_error",
    "serialize_document_metadata",
    "serialize_signals",
    "serialize_trust_data",
]
