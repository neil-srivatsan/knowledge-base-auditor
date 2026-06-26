"""Tests for similarity analyzer."""

from datetime import datetime, timezone

from kb_audit.analyzers.similarity import SimilarityAnalyzer
from kb_audit.models import Document, Severity


def test_exact_duplicates():
    doc1 = Document(
        id="1",
        title="Doc A",
        content="The quick brown fox jumps over the lazy dog.",
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    doc2 = Document(
        id="2",
        title="Doc A Copy",
        content="The quick brown fox jumps over the lazy dog.",
        source_type="test",
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    analyzer = SimilarityAnalyzer()
    results = analyzer.analyze([doc1, doc2])
    # Older doc should be flagged
    assert "2" in results
    assert results["2"][0].signal_type == "duplicate"
    assert results["2"][0].severity == Severity.CRITICAL


def test_no_duplicates():
    doc1 = Document(
        id="1",
        title="Apples",
        content="Apples are a type of fruit that grow on trees.",
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    doc2 = Document(
        id="2",
        title="Databases",
        content="PostgreSQL is a relational database management system.",
        source_type="test",
        last_modified=datetime(2025, 5, 1, tzinfo=timezone.utc),
    )
    analyzer = SimilarityAnalyzer()
    results = analyzer.analyze([doc1, doc2])
    assert not results


def test_near_duplicates():
    doc1 = Document(
        id="1",
        title="Setup Guide",
        content="Step 1: Install Python. Step 2: Run pip install. Step 3: Configure settings.",
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    doc2 = Document(
        id="2",
        title="Setup Guide Old",
        content="Step 1: Install Python. Step 2: Run pip install. Step 3: Configure.",
        source_type="test",
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    analyzer = SimilarityAnalyzer(threshold=0.75)
    results = analyzer.analyze([doc1, doc2])
    assert "2" in results
    assert results["2"][0].signal_type == "near_duplicate"
