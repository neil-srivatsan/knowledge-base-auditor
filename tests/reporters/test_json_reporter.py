"""Tests for JSON reporter."""

import json
from datetime import datetime, timezone

from kb_audit.models import AuditResult, Document, Severity, StalenessSignal
from kb_audit.reporters.json_reporter import JsonReporter


def test_json_output_to_file(tmp_path):
    doc = Document(
        id="1",
        title="Test",
        content="content",
        source_type="test",
        last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    result = AuditResult(
        document=doc,
        signals=[StalenessSignal("duplicate", Severity.CRITICAL, "Exact duplicate")],
        status="stale",
        confidence=0.5,
        confidence_reason="Exact duplicate of 'Other Doc'",
    )

    output_file = tmp_path / "report.json"
    reporter = JsonReporter(output_path=output_file)
    reporter.report([result])

    data = json.loads(output_file.read_text())
    assert data["total"] == 1
    assert data["stale"] == 1
    assert data["documents"][0]["title"] == "Test"
    assert data["documents"][0]["overall_status"] == "stale"
    assert data["documents"][0]["confidence"] == 0.5
    assert data["documents"][0]["confidence_reason"] == "Exact duplicate of 'Other Doc'"


def test_json_empty_results(tmp_path):
    output_file = tmp_path / "report.json"
    reporter = JsonReporter(output_path=output_file)
    reporter.report([])

    data = json.loads(output_file.read_text())
    assert data["total"] == 0
    assert data["documents"] == []


def test_json_includes_trust_metadata(tmp_path):
    """trust_metadata (including lifecycle) appears in JSON output."""
    doc = Document(
        id="2",
        title="Deprecated Guide",
        content="content",
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    result = AuditResult(
        document=doc,
        status="stale",
        confidence=0.65,
        confidence_reason="Status field indicates 'Deprecated'",
        trust_metadata={
            "declared_status": "Deprecated",
            "lifecycle": "deprecated",
            "lifecycle_evidence": ["Status field indicates 'Deprecated'"],
            "owner": None,
            "canonical": False,
        },
    )

    output_file = tmp_path / "report.json"
    reporter = JsonReporter(output_path=output_file)
    reporter.report([result])

    data = json.loads(output_file.read_text())
    doc_data = data["documents"][0]
    assert "trust_metadata" in doc_data
    assert doc_data["trust_metadata"]["lifecycle"] == "deprecated"
    assert doc_data["trust_metadata"]["lifecycle_evidence"] == [
        "Status field indicates 'Deprecated'",
    ]
    assert doc_data["trust_metadata"]["declared_status"] == "Deprecated"


def test_json_lifecycle_unknown_when_no_metadata(tmp_path):
    """Documents with no lifecycle evidence have lifecycle='unknown'."""
    doc = Document(
        id="3",
        title="Plain Doc",
        content="content",
        source_type="test",
    )
    result = AuditResult(
        document=doc,
        status="unknown",
        confidence=0.25,
        confidence_reason="Insufficient positive trust evidence",
        trust_metadata={
            "lifecycle": "unknown",
            "lifecycle_evidence": [],
        },
    )

    output_file = tmp_path / "report.json"
    reporter = JsonReporter(output_path=output_file)
    reporter.report([result])

    data = json.loads(output_file.read_text())
    doc_data = data["documents"][0]
    assert doc_data["trust_metadata"]["lifecycle"] == "unknown"
    assert doc_data["trust_metadata"]["lifecycle_evidence"] == []
