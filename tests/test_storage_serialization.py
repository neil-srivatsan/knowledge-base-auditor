"""Tests for storage serialization helpers."""

from __future__ import annotations

import json

from kb_audit.models import Severity, StalenessSignal
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


# ---------------------------------------------------------------------------
# sanitize_error
# ---------------------------------------------------------------------------

class TestSanitizeError:
    def test_none_returns_none(self):
        assert sanitize_error(None) is None

    def test_clean_message_unchanged(self):
        msg = "Something went wrong connecting to the server"
        assert sanitize_error(msg) == msg

    def test_redacts_bearer_token(self):
        result = sanitize_error("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret")
        assert "eyJhbGciOiJIUzI1NiJ9.secret" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_basic_token(self):
        result = sanitize_error("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in result
        assert "Basic [REDACTED]" in result

    def test_redacts_key_equals_value(self):
        result = sanitize_error("password=hunter2 was wrong")
        assert "hunter2" not in result
        assert "password=[REDACTED]" in result

    def test_redacts_api_key_colon_form(self):
        result = sanitize_error("api_key: supersecretvalue123")
        assert "supersecretvalue123" not in result
        assert "[REDACTED]" in result

    def test_redacts_url_query_param(self):
        result = sanitize_error("GET /api?token=abc123&foo=bar failed")
        assert "abc123" not in result
        assert "foo=bar" in result  # non-credential params preserved

    def test_truncates_to_500_chars(self):
        long_msg = "x" * 600
        result = sanitize_error(long_msg)
        assert result is not None
        assert len(result) == 500

    def test_non_string_input_converted(self):
        result = sanitize_error("42")  # type: ignore[arg-type]
        assert result == "42"


# ---------------------------------------------------------------------------
# serialize_signals / deserialize_signals
# ---------------------------------------------------------------------------

class TestSignalsSerialization:
    def _make_signal(self, signal_type: str, severity: Severity = Severity.WARNING) -> StalenessSignal:
        return StalenessSignal(
            signal_type=signal_type,
            severity=severity,
            message=f"Test {signal_type}",
            details={"key": "value"},
        )

    def test_roundtrip_empty(self):
        blob = serialize_signals([])
        result = deserialize_signals(blob)
        assert result == []

    def test_roundtrip_single_signal(self):
        signal = self._make_signal("age", Severity.WARNING)
        blob = serialize_signals([signal])
        result = deserialize_signals(blob)
        assert len(result) == 1
        assert result[0].signal_type == "age"
        assert result[0].severity == Severity.WARNING
        assert result[0].message == "Test age"
        assert result[0].details == {"key": "value"}

    def test_roundtrip_multiple_signals(self):
        signals = [
            self._make_signal("age", Severity.WARNING),
            self._make_signal("duplicate", Severity.CRITICAL),
            self._make_signal("resolved_internal_link", Severity.INFO),
        ]
        blob = serialize_signals(signals)
        result = deserialize_signals(blob)
        assert len(result) == 3
        assert result[1].severity == Severity.CRITICAL

    def test_deserialize_none_returns_empty(self):
        assert deserialize_signals(None) == []

    def test_deserialize_empty_string_returns_empty(self):
        assert deserialize_signals("") == []

    def test_blob_is_valid_json(self):
        signal = self._make_signal("age")
        blob = serialize_signals([signal])
        parsed = json.loads(blob)
        assert isinstance(parsed, list)
        assert parsed[0]["signal_type"] == "age"
        assert parsed[0]["severity"] == Severity.WARNING.value

    def test_missing_details_defaults_to_empty_dict(self):
        # Simulate a blob stored without 'details' key (legacy)
        blob = json.dumps([{"signal_type": "age", "severity": "warning", "message": "Old"}])
        result = deserialize_signals(blob)
        assert result[0].details == {}


# ---------------------------------------------------------------------------
# deserialize_signal_records
# ---------------------------------------------------------------------------

class TestDeserializeSignalRecords:
    def test_returns_raw_dicts(self):
        raw = [{"signal_type": "age", "severity": "warning", "message": "M", "details": {}}]
        blob = json.dumps(raw)
        result = deserialize_signal_records(blob)
        assert result == raw

    def test_none_returns_empty(self):
        assert deserialize_signal_records(None) == []

    def test_empty_string_returns_empty(self):
        assert deserialize_signal_records("") == []


# ---------------------------------------------------------------------------
# serialize_trust_data / deserialize_trust_data / deserialize_trust_data_blob
# ---------------------------------------------------------------------------

class TestTrustDataSerialization:
    def test_roundtrip(self):
        meta = {"requires_human_audit": True, "ref_count": 3}
        evidence = {"stale_signals": ["age"], "review_risks": ["broken_link"]}
        blob = serialize_trust_data(meta, evidence)
        result_meta, result_evidence = deserialize_trust_data(blob)
        assert result_meta == meta
        assert result_evidence == evidence

    def test_roundtrip_empty_dicts(self):
        blob = serialize_trust_data({}, {})
        meta, evidence = deserialize_trust_data(blob)
        assert meta == {}
        assert evidence == {}

    def test_deserialize_none(self):
        meta, evidence = deserialize_trust_data(None)
        assert meta == {}
        assert evidence == {}

    def test_deserialize_empty_string(self):
        meta, evidence = deserialize_trust_data("")
        assert meta == {}
        assert evidence == {}

    def test_blob_structure(self):
        blob = serialize_trust_data({"a": 1}, {"b": 2})
        parsed = json.loads(blob)
        assert parsed == {"metadata": {"a": 1}, "evidence": {"b": 2}}

    def test_deserialize_blob_raw(self):
        blob = serialize_trust_data({"a": 1}, {"b": 2})
        raw = deserialize_trust_data_blob(blob)
        assert raw == {"metadata": {"a": 1}, "evidence": {"b": 2}}

    def test_deserialize_blob_none(self):
        assert deserialize_trust_data_blob(None) == {}

    def test_deserialize_missing_keys(self):
        # Blob with only 'metadata' key (partial legacy row)
        blob = json.dumps({"metadata": {"x": 1}})
        meta, evidence = deserialize_trust_data(blob)
        assert meta == {"x": 1}
        assert evidence == {}


# ---------------------------------------------------------------------------
# serialize_document_metadata
# ---------------------------------------------------------------------------

class TestDocumentMetadataSerialization:
    def test_roundtrip(self):
        meta = {"created_by": "Alice", "version": 5, "links": ["https://example.com"]}
        blob = serialize_document_metadata(meta)
        assert deserialize_document_metadata(blob) == meta

    def test_empty_dict(self):
        blob = serialize_document_metadata({})
        assert deserialize_document_metadata(blob) == {}

    def test_deserialize_none_returns_empty(self):
        assert deserialize_document_metadata(None) == {}

    def test_deserialize_empty_string_returns_empty(self):
        assert deserialize_document_metadata("") == {}
