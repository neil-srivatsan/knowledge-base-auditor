"""Tests for version reference analyzer, including edge cases."""

from datetime import datetime, timezone

from kb_audit.analyzers.version_refs import VersionRefsAnalyzer
from kb_audit.models import Document


def test_outdated_version_detected():
    doc = Document(
        id="1",
        title="API Docs",
        content="This document describes the API v2.0 endpoints.",
        source_type="test",
        last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v3.0"},
        patterns=[r"v\d+\.\d+"],
    )
    results = analyzer.analyze([doc])
    assert "1" in results
    assert results["1"][0].signal_type == "version_ref"
    assert "v3.0" in results["1"][0].message


def test_current_version_not_flagged():
    doc = Document(
        id="1",
        title="API Docs",
        content="This document describes the API v3.0 endpoints.",
        source_type="test",
        last_modified=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v3.0"},
        patterns=[r"v\d+\.\d+"],
    )
    results = analyzer.analyze([doc])
    assert "1" not in results


def test_no_current_versions_configured():
    doc = Document(
        id="1",
        title="Doc",
        content="Some content with v1.0 in it.",
        source_type="test",
    )
    analyzer = VersionRefsAnalyzer()
    results = analyzer.analyze([doc])
    assert not results


# --- Edge cases ---


def test_no_version_number_in_document():
    """Document with no version references should not be flagged."""
    doc = Document(
        id="1",
        title="General Guide",
        content="This is a guide about using the API. No version numbers here.",
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v3.0"},
        patterns=[r"v\d+(?:\.\d+)*"],
    )
    results = analyzer.analyze([doc])
    assert "1" not in results


def test_bare_version_without_minor():
    """v1 (no minor version) should still be detected."""
    doc = Document(
        id="1",
        title="API Docs",
        content="This document covers the API v1 interface.",
        source_type="test",
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v3"},
        patterns=[r"v\d+(?:\.\d+)*"],
    )
    results = analyzer.analyze([doc])
    assert "1" in results
    assert results["1"][0].details["found_version"] == "v1"


def test_duplicate_highest_version_both_current():
    """Two docs both referencing the current version should not be flagged
    by the version analyzer."""
    doc1 = Document(
        id="1",
        title="API Reference",
        content="The API v3 endpoints for authentication.",
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    doc2 = Document(
        id="2",
        title="API Reference (copy)",
        content="The API v3 endpoints for authentication.",
        source_type="test",
        last_modified=datetime(2025, 3, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v3"},
        patterns=[r"v\d+(?:\.\d+)*"],
    )
    results = analyzer.analyze([doc1, doc2])
    # Neither should be flagged for version refs (both reference current)
    assert "1" not in results
    assert "2" not in results


def test_migration_from_old_version_not_flagged():
    """A v2 doc mentioning 'migration from v1' should not be flagged —
    the old version is mentioned in historical context, not as the doc's version."""
    doc = Document(
        id="1",
        title="API v2 Migration Guide",
        content=(
            "This guide covers the API v2 interface. "
            "For users migrating from API v1, the key changes are: "
            "new authentication flow and updated response format."
        ),
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v2"},
        patterns=[r"v\d+(?:\.\d+)*"],
    )
    results = analyzer.analyze([doc])
    # v1 is in historical context ("migrating from"), should not be flagged
    assert "1" not in results


def test_upgrade_from_old_version_not_flagged():
    """'Upgraded from v1.0' should not flag the old version."""
    doc = Document(
        id="1",
        title="Release Notes",
        content=(
            "API v2.0 release notes. Users upgrading from API v1.0 should "
            "review the breaking changes below."
        ),
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v2.0"},
        patterns=[r"v\d+\.\d+"],
    )
    results = analyzer.analyze([doc])
    assert "1" not in results


def test_old_version_without_historical_context_is_flagged():
    """A doc that uses an old version as its actual version should still be flagged."""
    doc = Document(
        id="1",
        title="API Docs",
        content=(
            "Welcome to the API v1 documentation. "
            "This describes all available endpoints."
        ),
        source_type="test",
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v2"},
        patterns=[r"v\d+(?:\.\d+)*"],
    )
    results = analyzer.analyze([doc])
    assert "1" in results
    assert results["1"][0].details["found_version"] == "v1"


def test_recently_edited_doc_with_old_version_still_flagged():
    """A recently edited doc that references an old version should still be flagged.
    Editing doesn't make the version reference current."""
    doc = Document(
        id="1",
        title="API Docs",
        content="This describes the API v1.0 endpoints. Last typo fix.",
        source_type="test",
        last_modified=datetime.now(timezone.utc),  # just edited
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v2.0"},
        patterns=[r"v\d+\.\d+"],
    )
    results = analyzer.analyze([doc])
    # Should still be flagged — recent edit doesn't fix the outdated version ref
    assert "1" in results


def test_v1_2_vs_v1_different_granularity():
    """v1.2 and v1 are different version formats. If current is v2,
    both v1.2 and v1 should be flagged."""
    doc1 = Document(
        id="1",
        title="API Guide A",
        content="This covers API v1.2 features.",
        source_type="test",
    )
    doc2 = Document(
        id="2",
        title="API Guide B",
        content="This covers API v1 features.",
        source_type="test",
    )
    analyzer = VersionRefsAnalyzer(
        current_versions={"API": "v2"},
        patterns=[r"v\d+(?:\.\d+)*"],
    )
    results = analyzer.analyze([doc1, doc2])
    assert "1" in results  # v1.2 != v2
    assert "2" in results  # v1 != v2
