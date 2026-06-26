"""Tests for data models."""


from kb_audit.models import AuditResult, Document, Severity, StalenessSignal


def test_document_content_hash():
    doc = Document(id="1", title="Test", content="hello world", source_type="test")
    assert doc.content_hash
    assert len(doc.content_hash) == 64  # SHA-256 hex digest


def test_document_same_content_same_hash():
    doc1 = Document(id="1", title="A", content="same content", source_type="test")
    doc2 = Document(id="2", title="B", content="same content", source_type="test")
    assert doc1.content_hash == doc2.content_hash


def test_document_different_content_different_hash():
    doc1 = Document(id="1", title="A", content="content a", source_type="test")
    doc2 = Document(id="2", title="B", content="content b", source_type="test")
    assert doc1.content_hash != doc2.content_hash


def test_audit_result_status_field():
    """AuditResult.status is set at construction, overall_status is an alias."""
    doc = Document(id="1", title="Test", content="ok", source_type="test")
    result = AuditResult(document=doc, status="current", confidence=0.8)
    assert result.status == "current"
    assert result.overall_status == "current"


def test_audit_result_overall_status_alias():
    """overall_status property returns the same value as status."""
    doc = Document(id="1", title="Test", content="ok", source_type="test")
    result = AuditResult(document=doc, status="needs_review", confidence=0.5)
    assert result.overall_status == "needs_review"


def test_audit_result_default_unknown():
    """Default status is unknown."""
    doc = Document(id="1", title="Test", content="ok", source_type="test")
    result = AuditResult(document=doc)
    assert result.status == "unknown"
    assert result.overall_status == "unknown"


def test_audit_result_stale_with_signals():
    """An AuditResult can be constructed with stale status and signals."""
    doc = Document(id="1", title="Test", content="ok", source_type="test")
    result = AuditResult(
        document=doc,
        signals=[
            StalenessSignal("age", Severity.WARNING, "Old doc"),
            StalenessSignal("duplicate", Severity.CRITICAL, "Exact duplicate"),
        ],
        status="stale",
        confidence=0.7,
    )
    assert result.status == "stale"
    assert result.overall_status == "stale"
