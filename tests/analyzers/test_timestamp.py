"""Tests for timestamp analyzer."""

from datetime import datetime, timedelta, timezone

from kb_audit.analyzers.timestamp import TimestampAnalyzer
from kb_audit.models import Document, Severity


def test_recent_doc_no_signals():
    doc = Document(
        id="1",
        title="Fresh",
        content="new content",
        source_type="test",
        last_modified=datetime.now(timezone.utc) - timedelta(days=10),
    )
    analyzer = TimestampAnalyzer(warning_days=90, critical_days=180)
    results = analyzer.analyze([doc])
    assert doc.id not in results


def test_warning_threshold():
    doc = Document(
        id="1",
        title="Getting Old",
        content="aging content",
        source_type="test",
        last_modified=datetime.now(timezone.utc) - timedelta(days=100),
    )
    analyzer = TimestampAnalyzer(warning_days=90, critical_days=180)
    results = analyzer.analyze([doc])
    assert doc.id in results
    assert results[doc.id][0].severity == Severity.WARNING


def test_critical_threshold():
    doc = Document(
        id="1",
        title="Very Old",
        content="ancient content",
        source_type="test",
        last_modified=datetime.now(timezone.utc) - timedelta(days=200),
    )
    analyzer = TimestampAnalyzer(warning_days=90, critical_days=180)
    results = analyzer.analyze([doc])
    assert doc.id in results
    assert results[doc.id][0].severity == Severity.CRITICAL


def test_no_last_modified():
    doc = Document(id="1", title="No Date", content="content", source_type="test")
    analyzer = TimestampAnalyzer()
    results = analyzer.analyze([doc])
    assert doc.id in results
    assert results[doc.id][0].severity == Severity.WARNING
    assert "No last-modified" in results[doc.id][0].message
