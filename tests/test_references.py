"""Tests for reference resolution improvements in ReferenceAnalyzer."""

from datetime import datetime, timezone

from kb_audit.analyzers.references import ReferenceAnalyzer
from kb_audit.models import Document


def _doc(id: str, title: str, content: str = "") -> Document:
    return Document(
        id=id, title=title, content=content, source_type="test",
        last_modified=datetime.now(timezone.utc),
    )


class TestExactResolution:
    """Exact case-insensitive matches should produce resolved_reference."""

    def test_exact_match(self):
        d1 = _doc("1", "Guide A", "For details, see Guide B.")
        d2 = _doc("2", "Guide B", "Content.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2])
        s = signals["1"][0]
        assert s.signal_type == "resolved_reference"
        assert s.details["resolved_doc_id"] == "2"
        assert s.details["resolved_title"] == "Guide B"

    def test_case_insensitive_match(self):
        d1 = _doc("1", "Guide A", "For details, see guide b.")
        d2 = _doc("2", "Guide B", "Content.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2])
        s = signals["1"][0]
        assert s.signal_type == "resolved_reference"


class TestBaseitleVariantResolution:
    """References to versioned/suffixed titles should resolve to the base document."""

    def test_year_suffix_resolves_to_base(self):
        """'Migration Guide 2021' should resolve to 'Migration Guide'."""
        d1 = _doc("1", "Payments", "See Migration Guide 2021.")
        d2 = _doc("2", "Migration Guide", "Main guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2])
        s = signals["1"][0]
        assert s.signal_type == "resolved_reference"
        assert s.details["resolved_doc_id"] == "2"
        assert s.details.get("match_type") == "base_title_variant"

    def test_version_suffix_resolves(self):
        """'API Guide v1' should resolve to 'API Guide' if it's the only match."""
        d1 = _doc("1", "Payments", "Refer to API Guide v1.")
        d2 = _doc("2", "API Guide", "Main guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2])
        s = signals["1"][0]
        assert s.signal_type == "resolved_reference"
        assert s.details["resolved_doc_id"] == "2"

    def test_old_suffix_resolves(self):
        """'API Guide (old)' should resolve to 'API Guide'."""
        d1 = _doc("1", "Payments", "See API Guide (old).")
        d2 = _doc("2", "API Guide", "Main guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2])
        s = signals["1"][0]
        assert s.signal_type == "resolved_reference"
        assert s.details["resolved_doc_id"] == "2"

    def test_base_ref_resolves_to_versioned(self):
        """'Migration Guide' should resolve to 'Migration Guide 2024' when it's the only sibling."""
        d1 = _doc("1", "Payments", "See Migration Guide.")
        d2 = _doc("2", "Migration Guide 2024", "Latest guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2])
        s = signals["1"][0]
        assert s.signal_type == "resolved_reference"
        assert s.details["resolved_doc_id"] == "2"


class TestAmbiguousVariantResolution:
    """When multiple documents share the same base title, emit ambiguous."""

    def test_multiple_versions_are_ambiguous(self):
        """'Migration Guide 2021' matches both 'Migration Guide' and 'Migration Guide 2024'."""
        d1 = _doc("1", "Payments", "See Migration Guide 2021.")
        d2 = _doc("2", "Migration Guide", "Base guide.")
        d3 = _doc("3", "Migration Guide 2024", "Latest guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2, d3])
        s = signals["1"][0]
        assert s.signal_type == "ambiguous_reference"
        assert len(s.details["matching_doc_ids"]) == 2

    def test_ambiguous_includes_normalized_reference(self):
        d1 = _doc("1", "Payments", "See Migration Guide 2021.")
        d2 = _doc("2", "Migration Guide", "Base guide.")
        d3 = _doc("3", "Migration Guide 2024", "Latest guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2, d3])
        s = signals["1"][0]
        assert s.details["normalized_reference"] == "migration guide"
        assert s.details["resolution_scope"] == "scan_corpus"


class TestUnresolvedReferences:
    """Genuinely missing documents should be unresolved."""

    def test_no_match_is_unresolved(self):
        d1 = _doc("1", "Payments", "See Nonexistent Guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1])
        s = signals["1"][0]
        assert s.signal_type == "unresolved_reference"

    def test_unresolved_includes_structured_details(self):
        d1 = _doc("1", "Payments", "See Payment Platform Migration Guide 2021.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1])
        s = signals["1"][0]
        assert s.signal_type == "unresolved_reference"
        assert s.details["referenced_title"] == "Payment Platform Migration Guide 2021"
        assert s.details["normalized_reference"] == "payment platform migration guide"
        assert s.details["resolution_scope"] == "scan_corpus"

    def test_unresolved_message_mentions_scan_pages(self):
        d1 = _doc("1", "Payments", "See Missing Guide.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1])
        s = signals["1"][0]
        assert "pages in this scan" in s.message


class TestCorpusScoping:
    """Resolution must only use documents passed to the analyzer."""

    def test_not_in_corpus_is_unresolved(self):
        """A reference to a real title, but the doc isn't in the corpus list."""
        d1 = _doc("1", "Guide A", "For details, see Guide B.")
        # Guide B is NOT passed to the analyzer
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1])
        s = signals["1"][0]
        assert s.signal_type == "unresolved_reference"

    def test_self_reference_excluded(self):
        d1 = _doc("1", "Guide A", "See Guide A.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1])
        if signals.get("1"):
            # If any signal is produced, it should be unresolved (not resolved to self)
            for s in signals["1"]:
                assert s.signal_type != "resolved_reference" or s.details["resolved_doc_id"] != "1"


class TestPaymentsCorpusReferences:
    """Verify the payments corpus reference behavior with improved matching."""

    def test_migration_guide_2021_resolves_or_is_unresolved(self):
        """'Payment Platform Migration Guide 2021' — if no matching doc exists, unresolved.
        If a 'Payment Platform Migration Notes' doc exists, it should NOT
        resolve via base-title matching since the base titles differ."""
        d1 = _doc("lpi-1", "Legacy Payment Integration",
                   "For migration steps, see Payment Platform Migration Guide 2021.")
        d2 = _doc("pmn-1", "Payment Platform Migration Notes",
                   "Migration guide content.")
        analyzer = ReferenceAnalyzer()
        signals = analyzer.analyze([d1, d2])
        lpi_signals = signals.get("lpi-1", [])
        # The reference to "Migration Guide 2021" should NOT match "Migration Notes"
        # because the base titles are different
        for s in lpi_signals:
            if s.details.get("referenced_title") == "Payment Platform Migration Guide 2021":
                assert s.signal_type in ("unresolved_reference", "ambiguous_reference")
