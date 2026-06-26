"""Tests for the reference analyzer."""

from datetime import datetime, timezone

from kb_audit.analyzers.references import ReferenceAnalyzer, _extract_references
from kb_audit.models import Document


def _make_doc(id: str, title: str, content: str) -> Document:
    return Document(
        id=id,
        title=title,
        content=content,
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )


def test_resolved_reference_emits_info_signal():
    """A reference that matches exactly one other document produces a resolved_reference signal."""
    docs = [
        _make_doc("1", "Setup Guide", "For details, see API Reference."),
        _make_doc("2", "API Reference", "This is the API docs."),
    ]
    results = ReferenceAnalyzer().analyze(docs)
    assert "1" in results
    signal = results["1"][0]
    assert signal.signal_type == "resolved_reference"
    assert signal.severity.value == "info"
    assert signal.details["resolved_doc_id"] == "2"
    assert signal.details["resolved_title"] == "API Reference"


def test_unresolved_reference():
    """A reference to a title that doesn't exist emits unresolved_reference."""
    docs = [
        _make_doc("1", "Setup Guide", "See Deployment Runbook for next steps"),
    ]
    results = ReferenceAnalyzer().analyze(docs)
    assert "1" in results
    signal = results["1"][0]
    assert signal.signal_type == "unresolved_reference"
    assert "Deployment Runbook" in signal.message


def test_ambiguous_reference():
    """A reference matching multiple documents emits ambiguous_reference."""
    docs = [
        _make_doc("1", "Setup Guide", "Refer to API Reference."),
        _make_doc("2", "API Reference", "Version 1 of the API docs."),
        _make_doc("3", "API Reference", "Version 2 of the API docs."),
    ]
    results = ReferenceAnalyzer().analyze(docs)
    assert "1" in results
    signal = results["1"][0]
    assert signal.signal_type == "ambiguous_reference"
    assert signal.details["matching_doc_ids"] == ["2", "3"]


def test_normalized_match_resolves():
    """A reference resolves via normalized title (ignoring version suffixes)."""
    docs = [
        _make_doc("1", "Setup Guide", "See API Reference."),
        _make_doc("2", "API Reference v2", "Latest API docs."),
    ]
    results = ReferenceAnalyzer().analyze(docs)
    # "API Reference" normalizes to "api reference", matching "API Reference v2"
    assert "1" in results
    signal = results["1"][0]
    assert signal.signal_type == "resolved_reference"
    assert signal.details["resolved_doc_id"] == "2"


def test_self_reference_ignored():
    """A document referencing its own title does not produce a signal."""
    docs = [
        _make_doc("1", "Setup Guide", "See Setup Guide for more context"),
    ]
    results = ReferenceAnalyzer().analyze(docs)
    # Self-reference is excluded; since no other doc matches, it's unresolved
    assert "1" in results
    assert results["1"][0].signal_type == "unresolved_reference"


def test_multiple_patterns_extracted():
    """All supported reference patterns are extracted from content."""
    content = (
        "See Alpha Guide\n"
        "Refer to Beta Manual\n"
        "For details, see Gamma Docs\n"
        "For more information, see Delta Reference"
    )
    refs = _extract_references(content)
    titles = [r.strip() for r in refs]
    assert "Alpha Guide" in titles
    assert "Beta Manual" in titles
    assert "Gamma Docs" in titles
    assert "Delta Reference" in titles


def test_reference_stops_at_punctuation():
    """Reference capture stops at period, comma, semicolon."""
    refs = _extract_references("See Some Doc. Then do something else.")
    assert refs == ["Some Doc"]

    refs = _extract_references("See First Doc, Second Doc; Third Doc")
    assert refs == ["First Doc"]
