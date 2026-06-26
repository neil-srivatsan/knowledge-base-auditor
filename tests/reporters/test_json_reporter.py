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
