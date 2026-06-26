"""Edge case tests for staleness detection and trust classification."""

from datetime import datetime, timedelta, timezone

from kb_audit.analyzers.similarity import SimilarityAnalyzer
from kb_audit.analyzers.timestamp import TimestampAnalyzer
from kb_audit.analyzers.version_refs import VersionRefsAnalyzer
from kb_audit.auditor import Auditor
from kb_audit.models import Document
from kb_audit.sources.base import DocumentSource
from kb_audit.trust import classify


class FakeSource(DocumentSource):
    def __init__(self, docs):
        self._docs = docs

    @classmethod
    def source_type(cls):
        return "fake"

    def fetch_documents(self):
        yield from self._docs


# --- Trust classification edge cases ---


def test_single_doc_no_signals_is_unknown():
    """A lone document with no signals should be unknown — no positive trust evidence."""
    doc = Document(
        id="1", title="Solo Doc", content="Some content.",
        source_type="test",
        last_modified=datetime.now(timezone.utc),
    )
    verdict = classify(doc, [], incoming_ref_count=0)
    assert verdict.status == "unknown"
    assert "No incoming references" in verdict.reason


def test_doc_without_last_modified_is_low_confidence():
    """No modification date means we can't assess freshness — should be unknown."""
    doc = Document(id="1", title="No Date", content="Content", source_type="test")
    verdict = classify(doc, [], incoming_ref_count=0)
    assert verdict.status == "unknown"
    assert verdict.confidence <= 0.25
    assert "incoming reference" in verdict.reason.lower() or "Last reviewed" in verdict.reason


def test_recently_edited_old_doc_still_flagged_for_version():
    """A doc edited today but referencing v1 (when v2 is current) should be stale.
    Recent edit + outdated version ref = stale with moderate confidence."""
    doc = Document(
        id="1",
        title="API Docs",
        content="Describes API v1.0 endpoints.",
        source_type="test",
        last_modified=datetime.now(timezone.utc),
    )
    docs = [doc, Document(
        id="2", title="Other", content="Something else about API v2.0.",
        source_type="test",
        last_modified=datetime.now(timezone.utc),
    )]
    # Timestamp won't flag it (recently edited), but version refs will
    ts = TimestampAnalyzer()
    vr = VersionRefsAnalyzer(current_versions={"API": "v2.0"}, patterns=[r"v\d+\.\d+"])
    ts_signals = ts.analyze(docs)
    vr_signals = vr.analyze(docs)

    signals = ts_signals.get("1", []) + vr_signals.get("1", [])
    assert len(signals) >= 1  # at least the version ref signal
    assert any(s.signal_type == "version_ref" for s in signals)

    verdict = classify(doc, signals, incoming_ref_count=0)
    assert verdict.status == "stale"
    assert verdict.confidence >= 0.3


def test_duplicate_highest_version_detected_by_similarity():
    """Two docs with identical content — the older one should be flagged as stale
    with high confidence."""
    content = "The API v3 endpoints for authentication and authorization."
    doc1 = Document(
        id="1", title="API Reference", content=content,
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    doc2 = Document(
        id="2", title="API Reference (old)", content=content,
        source_type="test",
        last_modified=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    sim = SimilarityAnalyzer()
    results = sim.analyze([doc1, doc2])

    # Older doc (doc2) should be flagged as exact duplicate
    assert "2" in results
    assert results["2"][0].signal_type == "duplicate"
    # Newer doc should NOT be flagged
    assert "1" not in results

    # Confidence for the older doc should be high
    verdict = classify(doc2, results["2"], incoming_ref_count=0)
    assert verdict.status == "stale"
    assert verdict.confidence >= 0.45


def test_full_pipeline_recently_edited_old_version():
    """End-to-end: a recently edited doc referencing an old version.
    Timestamp won't flag it, but version refs will."""
    docs = [
        Document(
            id="1", title="API Guide",
            content="Updated formatting. Describes API v1 interface.",
            source_type="test",
            last_modified=datetime.now(timezone.utc),  # just edited
        ),
        Document(
            id="2", title="API Guide v2",
            content="New API v2 interface with improved auth.",
            source_type="test",
            last_modified=datetime.now(timezone.utc) - timedelta(days=30),
        ),
    ]

    class Collector:
        def __init__(self):
            self.results = None
        def report(self, results):
            self.results = results

    reporter = Collector()
    auditor = Auditor(
        sources=[FakeSource(docs)],
        analyzers=[
            TimestampAnalyzer(),
            VersionRefsAnalyzer(current_versions={"API": "v2"}, patterns=[r"v\d+(?:\.\d+)*"]),
        ],
        reporters=[reporter],
    )
    results = auditor.run()
    result_by_id = {r.document.id: r for r in results}

    # Doc 1 references v1 (old), should be flagged
    assert any(s.signal_type == "version_ref" for s in result_by_id["1"].signals)
    assert result_by_id["1"].status == "stale"
