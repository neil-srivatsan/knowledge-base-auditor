"""Tests for trust classification."""

from datetime import datetime, timezone

from kb_audit.models import Document, Severity, StalenessSignal
from kb_audit.trust import (
    classify,
    compute_incoming_ref_counts,
    normalize_title,
)


def _doc(id: str = "1", title: str = "Test Doc", content: str = "Some content.",
         last_modified: datetime | None = None) -> Document:
    return Document(
        id=id, title=title, content=content, source_type="test",
        last_modified=last_modified or datetime.now(timezone.utc),
    )


def _resolved_ref(target_id: str, target_title: str) -> StalenessSignal:
    return StalenessSignal(
        signal_type="resolved_reference",
        severity=Severity.INFO,
        message=f"References '{target_title}' → '{target_title}'",
        details={
            "referenced_title": target_title,
            "resolved_doc_id": target_id,
            "resolved_title": target_title,
        },
    )


def _unresolved_ref(title: str) -> StalenessSignal:
    return StalenessSignal(
        signal_type="unresolved_reference",
        severity=Severity.WARNING,
        message=f"References '{title}' but no matching document found",
        details={"referenced_title": title},
    )


# --- Core classification rules ---


class TestNoSignalsNoTrust:
    """Two recent docs with no signals should be unknown, not current."""

    def test_recent_doc_no_signals_is_unknown(self):
        doc = _doc(last_modified=datetime.now(timezone.utc))
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "unknown"

    def test_two_recent_docs_no_signals_are_unknown(self):
        """Both docs are recent but have no strong trust evidence."""
        d1 = _doc(id="1", title="Doc A", last_modified=datetime.now(timezone.utc))
        d2 = _doc(id="2", title="Doc B", last_modified=datetime.now(timezone.utc))
        v1 = classify(d1, [], incoming_ref_count=0)
        v2 = classify(d2, [], incoming_ref_count=0)
        assert v1.status == "unknown"
        assert v2.status == "unknown"

    def test_reason_does_not_say_no_staleness_indicators(self):
        doc = _doc()
        verdict = classify(doc, [], incoming_ref_count=0)
        assert "No staleness indicators detected" not in verdict.reason


class TestIncomingReferenceTrust:
    """Incoming references are the strongest positive trust signal."""

    def test_two_incoming_refs_makes_current(self):
        doc = _doc()
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "current"
        assert "Referenced by 2" in verdict.reason

    def test_three_incoming_refs_higher_confidence(self):
        v2 = classify(_doc(), [], incoming_ref_count=2)
        v3 = classify(_doc(), [], incoming_ref_count=3)
        assert v3.confidence >= v2.confidence

    def test_one_incoming_ref_not_enough(self):
        doc = _doc()
        verdict = classify(doc, [], incoming_ref_count=1)
        assert verdict.status == "unknown"
        assert "Only 1 incoming reference" in verdict.reason


class TestResolvedReferences:
    """Resolved outgoing refs are supporting trust only (not strong)."""

    def test_resolved_refs_alone_not_enough_for_current(self):
        """Outgoing resolved refs are supporting — need strong signal too."""
        doc = _doc()
        signals = [_resolved_ref("2", "Other Doc")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "unknown"

    def test_resolved_refs_boost_current_with_strong_signal(self):
        """Outgoing resolved refs appear in reason when combined with strong signal."""
        doc = _doc()
        signals = [_resolved_ref("2", "Other Doc")]
        verdict = classify(doc, signals, incoming_ref_count=2)
        assert verdict.status == "current"
        assert "outgoing references resolve" in verdict.reason

    def test_resolved_plus_unresolved_is_needs_review(self):
        """One resolved + one unresolved → risk wins."""
        doc = _doc()
        signals = [
            _resolved_ref("2", "Other Doc"),
            _unresolved_ref("Missing Doc"),
        ]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert "unresolved reference" in verdict.reason


class TestUnresolvedReference:
    """Unresolved references produce needs_review."""

    def test_unresolved_ref_is_needs_review(self):
        doc = _doc()
        signals = [_unresolved_ref("Missing Guide")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert "Missing Guide" in verdict.reason


class TestStaleEvidence:
    """Explicit stale signals produce stale."""

    def test_duplicate_is_stale(self):
        doc = _doc()
        signals = [StalenessSignal(
            signal_type="duplicate", severity=Severity.CRITICAL,
            message="Exact duplicate",
            details={"duplicate_title": "Other Doc", "duplicate_of": "2"},
        )]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_version_ref_is_stale(self):
        doc = _doc()
        signals = [StalenessSignal(
            signal_type="version_ref", severity=Severity.WARNING,
            message="Outdated version",
            details={"found_version": "v1", "current_version": "v2"},
        )]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_version_marker_is_stale(self):
        doc = _doc()
        signals = [StalenessSignal(
            signal_type="version_marker", severity=Severity.WARNING,
            message="Old version marker",
            details={"found_version": "v1", "current_version": "v2",
                     "current_doc_title": "API Guide v2"},
        )]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "stale"


class TestContentRiskMarkers:
    """Status field in body content is parsed."""

    def test_legacy_status_is_stale(self):
        doc = _doc(content="Some docs.\nStatus: Legacy\nMore text.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "Legacy" in verdict.reason

    def test_deprecated_status_is_stale(self):
        doc = _doc(content="Status: Deprecated")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_current_status_is_trust_evidence(self):
        doc = _doc(content="Status: Current\nOwner: Alice")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "current"
        assert "Current" in verdict.reason

    def test_supported_status_alone_is_unknown(self):
        """Supported is a supporting signal — needs strong signal for current."""
        doc = _doc(content="Status: Supported\nMaintained by: Platform Team")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "unknown"

    def test_supported_status_with_incoming_refs_is_current(self):
        """Supported + incoming refs (strong) → current."""
        doc = _doc(content="Status: Supported\nMaintained by: Platform Team")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "current"


class TestSimilarity:
    """Similarity alone should not make a doc stale or current."""

    def test_near_duplicate_is_needs_review(self):
        doc = _doc()
        signals = [StalenessSignal(
            signal_type="near_duplicate", severity=Severity.WARNING,
            message="Similar",
            details={"similarity": 85, "similar_title": "Other Doc"},
        )]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert verdict.status != "stale"
        assert verdict.status != "current"


class TestComputeIncomingRefCounts:
    """Test the cross-document reference counter."""

    def test_counts_resolved_refs(self):
        all_signals = {
            "doc-a": [_resolved_ref("doc-b", "Doc B")],
            "doc-b": [_resolved_ref("doc-c", "Doc C")],
            "doc-c": [_resolved_ref("doc-b", "Doc B")],
        }
        counts = compute_incoming_ref_counts(all_signals)
        assert counts["doc-b"] == 2  # referenced by doc-a and doc-c
        assert counts["doc-c"] == 1  # referenced by doc-b only
        assert counts.get("doc-a", 0) == 0  # not referenced

    def test_ignores_self_references(self):
        all_signals = {
            "doc-a": [_resolved_ref("doc-a", "Doc A")],  # self-ref
        }
        counts = compute_incoming_ref_counts(all_signals)
        assert counts.get("doc-a", 0) == 0

    def test_ignores_unresolved(self):
        all_signals = {
            "doc-a": [_unresolved_ref("Missing")],
        }
        counts = compute_incoming_ref_counts(all_signals)
        assert not counts


class TestReasonStrings:
    """Reason strings should be human-readable and never say 'No staleness indicators'."""

    def test_unknown_reason_is_descriptive(self):
        doc = _doc()
        verdict = classify(doc, [], incoming_ref_count=0)
        assert "No staleness indicators detected" not in verdict.reason
        # Should mention the actual gaps
        assert "incoming reference" in verdict.reason.lower() or "evidence" in verdict.reason.lower()

    def test_stale_reason_mentions_evidence(self):
        doc = _doc()
        signals = [StalenessSignal(
            signal_type="duplicate", severity=Severity.CRITICAL,
            message="Exact duplicate",
            details={"duplicate_title": "Newer Doc"},
        )]
        verdict = classify(doc, signals)
        assert "Newer Doc" in verdict.reason

    def test_current_reason_mentions_references(self):
        doc = _doc()
        verdict = classify(doc, [], incoming_ref_count=3)
        assert "Referenced by 3" in verdict.reason


class TestSoftRiskOverridesStrongTrust:
    """Any active review risk forces needs_review, even with Status: Current."""

    def test_old_review_with_current_status_becomes_needs_review(self):
        doc = _doc(content="Status: Current\nOwner: Team\nLast reviewed: 2024-01-01\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "needs_review"
        # Positive evidence preserved
        assert verdict.evidence is not None
        assert any("Current" in e for e in verdict.evidence.positive_evidence)

    def test_old_review_with_current_status_preserves_positive_evidence(self):
        doc = _doc(content="Status: Current\nLast reviewed: 2024-01-01\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "needs_review"
        assert len(verdict.evidence.positive_evidence) > 0

    def test_old_review_without_current_status_is_needs_review(self):
        doc = _doc(content="Status: Supported\nLast reviewed: 2024-01-01\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "needs_review"


class TestNeedsReviewContextEnrichment:
    """Needs_review reasons should include trust context, not just the risk."""

    def test_soft_risk_includes_incoming_ref_context(self):
        doc = _doc(content="Status: Supported\nLast reviewed: 2024-01-01\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert "Referenced by 2 documents" in verdict.reason

    def test_soft_risk_includes_status_context(self):
        doc = _doc(content="Status: Supported\nLast reviewed: 2024-01-01\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert "Status: Supported" in verdict.reason

    def test_hard_risk_includes_no_incoming_refs(self):
        doc = _doc()
        signals = [_unresolved_ref("Missing Doc")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert "No incoming references" in verdict.reason

    def test_soft_risk_includes_outgoing_resolve_context(self):
        doc = _doc(content="Status: Supported\nLast reviewed: 2024-01-01\n")
        signals = [_resolved_ref("2", "Other Doc")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert "outgoing references resolve" in verdict.reason.lower()


class TestParsedLastReviewed:
    """Last reviewed date should be parsed and appear in classification reasons."""

    def test_last_reviewed_appears_in_current_reason(self):
        doc = _doc(content="Status: Current\nLast reviewed: 2026-03-01\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert "Last reviewed 2026-03-01" in verdict.reason

    def test_last_reviewed_appears_in_needs_review_reason(self):
        doc = _doc(content="Last reviewed: 2024-06-01\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert "Last reviewed 2024-06-01" in verdict.reason

    def test_parse_body_metadata_extracts_last_reviewed(self):
        from kb_audit.trust import parse_body_metadata
        doc = _doc(content="Some text.\nLast reviewed: 2025-09-10\nMore text.\n")
        meta = parse_body_metadata(doc)
        assert meta["parsed_last_reviewed"] == "2025-09-10"

    def test_parse_body_metadata_extracts_status_and_owner(self):
        from kb_audit.trust import parse_body_metadata
        doc = _doc(content="Status: Current\nOwner: Payments Team\n")
        meta = parse_body_metadata(doc)
        assert meta["parsed_status"] == "Current"
        assert meta["parsed_owner"] == "Payments Team"


class TestStructuredMetadata:
    """TrustVerdict should contain structured metadata and evidence."""

    def test_metadata_has_last_reviewed(self):
        doc = _doc(content="Status: Current\nLast reviewed: 2026-03-01\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.metadata.last_reviewed == "2026-03-01"

    def test_metadata_has_owner(self):
        doc = _doc(content="Status: Current\nOwner: Payments Team\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.metadata.owner == "Payments Team"

    def test_metadata_has_declared_status(self):
        doc = _doc(content="Status: Current\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.declared_status == "Current"

    def test_metadata_has_last_modified(self):
        ts = datetime(2026, 1, 15, tzinfo=timezone.utc)
        doc = _doc(last_modified=ts)
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.last_modified is not None

    def test_metadata_none_when_not_present(self):
        doc = _doc(content="Just some text.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.last_reviewed is None
        assert verdict.metadata.owner is None
        assert verdict.metadata.declared_status is None

    def test_metadata_has_replaced_by(self):
        doc = _doc(content="Replaced by: New Guide v2\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.replaced_by == "New Guide v2"

    def test_metadata_has_superseded_by(self):
        doc = _doc(content="Superseded by: API Guide v3\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.replaced_by == "API Guide v3"

    def test_metadata_has_deprecated_as_of(self):
        doc = _doc(content="Deprecated as of: 2025-06-01\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.deprecated_as_of == "2025-06-01"

    def test_metadata_has_canonical(self):
        doc = _doc(content="Canonical: true\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.canonical is True

    def test_metadata_canonical_yes(self):
        doc = _doc(content="Canonical: yes\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.canonical is True

    def test_metadata_has_review_cadence(self):
        doc = _doc(content="Review cadence: Quarterly\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.review_cadence == "Quarterly"

    def test_metadata_has_applies_to(self):
        doc = _doc(content="Applies to: Payment Processing API\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.applies_to == "Payment Processing API"

    def test_parse_body_metadata_includes_new_fields(self):
        from kb_audit.trust import parse_body_metadata
        doc = _doc(content=(
            "Status: Current\nOwner: Team\n"
            "Replaced by: New Guide\n"
            "Canonical: true\n"
            "Review cadence: Monthly\n"
            "Applies to: Billing API\n"
        ))
        meta = parse_body_metadata(doc)
        assert meta["parsed_replaced_by"] == "New Guide"
        assert meta["parsed_canonical"] is True
        assert meta["parsed_review_cadence"] == "Monthly"
        assert meta["parsed_applies_to"] == "Billing API"


class TestStructuredEvidence:
    """TrustVerdict should contain structured evidence for all statuses."""

    def test_current_has_positive_evidence(self):
        doc = _doc(content="Status: Current\nOwner: Team\nLast reviewed: 2026-03-01\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "current"
        assert len(verdict.evidence.positive_evidence) > 0
        assert verdict.evidence.summary != ""

    def test_stale_has_summary(self):
        doc = _doc(content="Status: Legacy\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert verdict.evidence.summary != ""

    def test_needs_review_has_risks(self):
        doc = _doc()
        signals = [_unresolved_ref("Missing Doc")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert len(verdict.evidence.review_risks) > 0

    def test_unknown_has_missing_evidence(self):
        doc = _doc()
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "unknown"
        assert len(verdict.evidence.missing_evidence) > 0

    def test_recommended_action_present(self):
        doc = _doc()
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.evidence.recommended_action != ""

    def test_current_recommended_action(self):
        doc = _doc(content="Status: Current\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "current"
        assert verdict.evidence.recommended_action != ""


# --- Expanded stale status keywords ---


class TestExpandedStaleKeywords:
    """All stale status keywords should classify stale."""

    def test_deprecated_status_is_stale(self):
        doc = _doc(content="Status: Deprecated")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_archived_status_is_stale(self):
        doc = _doc(content="Status: Archived")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_superseded_status_is_stale(self):
        doc = _doc(content="Status: Superseded")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_retired_status_is_stale(self):
        doc = _doc(content="Status: Retired")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_obsolete_status_is_stale(self):
        doc = _doc(content="Status: Obsolete")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_eol_status_is_stale(self):
        doc = _doc(content="Status: EOL")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_end_of_life_status_is_stale(self):
        doc = _doc(content="Status: End-of-Life")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_sunset_status_is_stale(self):
        doc = _doc(content="Status: Sunset")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"


# --- Body-text supersession detection ---


class TestBodyTextSupersession:
    """Body phrases indicating obsolescence should classify stale."""

    def test_superseded_by_classifies_stale(self):
        doc = _doc(content="This document is superseded by the new Migration Guide.\nOld content.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "superseded" in verdict.reason.lower()

    def test_replaced_by_classifies_stale(self):
        doc = _doc(content="This guide has been replaced by the v2 API Guide.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "replaced" in verdict.reason.lower()

    def test_use_instead_classifies_stale(self):
        doc = _doc(content="Please use the New Payment Guide instead.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "instead" in verdict.reason.lower()

    def test_no_longer_maintained_classifies_stale(self):
        doc = _doc(content="This document is no longer maintained.\nSee new docs.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "no longer maintained" in verdict.reason.lower()

    def test_deprecated_as_of_classifies_stale(self):
        doc = _doc(content="Deprecated as of 2025-06-01.\nUse new guide.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_do_not_use_classifies_stale(self):
        doc = _doc(content="Do not use this guide. It is outdated.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_no_longer_authoritative_classifies_stale(self):
        doc = _doc(content="This document is no longer authoritative.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_mere_discussion_of_deprecated_apis_not_stale(self):
        """A page discussing deprecated APIs should NOT be flagged stale."""
        doc = _doc(content=(
            "Status: Current\n"
            "This guide explains how to migrate from deprecated API v1 endpoints.\n"
            "The deprecated endpoints will be removed in Q4.\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "current"


# --- Notion archived metadata ---


class TestArchivedMetadata:
    """Notion archived flag should classify stale."""

    def test_archived_metadata_is_stale(self):
        doc = _doc(content="Some content.")
        doc.metadata["archived"] = True
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "archived" in verdict.reason.lower()

    def test_not_archived_metadata_is_not_stale(self):
        doc = _doc(content="Some content.")
        doc.metadata["archived"] = False
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status != "stale"


# --- Product rules: weak evidence must NOT classify stale ---


class TestWeakEvidenceRemainsNonStale:
    """Old last-reviewed and broken links must NOT make a doc stale."""

    def test_old_last_reviewed_alone_is_not_stale(self):
        doc = _doc(content="Last reviewed: 2022-01-01\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status != "stale"
        assert verdict.status in ("needs_review", "unknown")

    def test_broken_link_alone_is_not_stale(self):
        doc = _doc()
        signals = [StalenessSignal(
            signal_type="broken_link", severity=Severity.WARNING,
            message="Broken link", details={"url": "https://example.com/404"},
        )]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status != "stale"
        assert verdict.status == "needs_review"

    def test_unresolved_reference_alone_is_not_stale(self):
        doc = _doc()
        signals = [_unresolved_ref("Missing Doc")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status != "stale"
        assert verdict.status == "needs_review"


# --- Title normalization ---


class TestTitleNormalization:
    """normalize_title should strip trailing year/version/stale suffixes."""

    def test_trailing_year(self):
        base, ver, stale = normalize_title("Payment Platform Migration Guide 2021")
        assert base == "payment platform migration guide"
        assert ver == "2021"
        assert stale is None

    def test_trailing_version_v(self):
        base, ver, stale = normalize_title("API Guide v1")
        assert base == "api guide"
        assert ver == "v1"

    def test_trailing_version_word(self):
        base, ver, stale = normalize_title("API Guide version 2.0")
        assert base == "api guide"
        assert ver is not None
        assert "2.0" in ver

    def test_stale_suffix_old(self):
        base, ver, stale = normalize_title("API Guide (old)")
        assert base == "api guide"
        assert stale is not None
        assert "old" in stale.lower()

    def test_stale_suffix_deprecated(self):
        base, ver, stale = normalize_title("Payments Docs (deprecated)")
        assert base == "payments docs"
        assert stale is not None

    def test_no_suffix(self):
        base, ver, stale = normalize_title("Payment Processing Guide")
        assert base == "payment processing guide"
        assert ver is None
        assert stale is None

    def test_year_and_stale_suffix(self):
        base, ver, stale = normalize_title("Migration Guide 2021 (old)")
        assert base == "migration guide"
        assert ver == "2021"
        assert stale is not None


# --- Scan-local supersession ---


class TestScanSupersession:
    """Year/version-suffixed docs should be stale when a newer sibling exists."""

    def test_older_year_stale_when_newer_exists(self):
        doc = _doc(id="old", title="Migration Guide 2021")
        scan_titles = {"old": "Migration Guide 2021", "new": "Migration Guide 2024"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"
        assert "2024" in verdict.reason

    def test_newer_year_not_stale(self):
        doc = _doc(id="new", title="Migration Guide 2024")
        scan_titles = {"old": "Migration Guide 2021", "new": "Migration Guide 2024"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status != "stale"

    def test_older_version_stale_when_newer_exists(self):
        doc = _doc(id="v1", title="API Guide v1")
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"
        assert "v2" in verdict.reason

    def test_stale_suffix_with_base_sibling(self):
        doc = _doc(id="old", title="API Guide (old)")
        scan_titles = {"old": "API Guide (old)", "current": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"
        assert "stale suffix" in verdict.reason.lower()

    def test_year_suffix_alone_without_sibling_not_stale(self):
        """A year-suffixed doc with no sibling should NOT be stale."""
        doc = _doc(id="only", title="Migration Guide 2021")
        scan_titles = {"only": "Migration Guide 2021"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status != "stale"

    def test_no_scan_titles_no_supersession(self):
        """Without scan_titles, no supersession check happens."""
        doc = _doc(id="old", title="Migration Guide 2021")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status != "stale"

    def test_reference_resolution_scoped_to_scan(self):
        """ReferenceAnalyzer resolves only within passed document list."""
        from kb_audit.analyzers.references import ReferenceAnalyzer
        from kb_audit.models import Document as D

        d1 = D(id="1", title="Guide A",
               content="For details, see Guide B.\nMore content.",
               source_type="test", last_modified=datetime.now(timezone.utc))
        d2 = D(id="2", title="Guide B", content="Reference docs.",
               source_type="test", last_modified=datetime.now(timezone.utc))

        analyzer = ReferenceAnalyzer()
        # Only pass d1 — Guide B is NOT in the scan
        signals = analyzer.analyze([d1])
        d1_signals = signals.get("1", [])
        resolved = [s for s in d1_signals if s.signal_type == "resolved_reference"]
        assert len(resolved) == 0

        # Now pass both — should resolve
        signals2 = analyzer.analyze([d1, d2])
        d1_signals2 = signals2.get("1", [])
        resolved2 = [s for s in d1_signals2 if s.signal_type == "resolved_reference"]
        assert len(resolved2) == 1


# --- Canonical trust evidence ---


class TestCanonicalTrust:
    """Canonical: true/yes should be strong trust evidence → current."""

    def test_canonical_true_is_current(self):
        doc = _doc(content="Canonical: true\nSome content.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "current"
        assert "canonical" in verdict.reason.lower()

    def test_canonical_yes_is_current(self):
        doc = _doc(content="Canonical: yes\nSome content.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "current"

    def test_canonical_with_incoming_refs_high_confidence(self):
        doc = _doc(content="Canonical: true\n")
        verdict = classify(doc, [], incoming_ref_count=3)
        assert verdict.status == "current"
        assert verdict.confidence >= 0.75


# --- Replaced by / Superseded by metadata ---


class TestReplacedByMetadata:
    """Replaced by / Superseded by metadata → stale."""

    def test_replaced_by_is_stale(self):
        doc = _doc(content="Replaced by: New Payment Guide\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "Replaced by" in verdict.reason

    def test_superseded_by_is_stale(self):
        doc = _doc(content="Superseded by: API Guide v3\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_deprecated_as_of_is_stale(self):
        doc = _doc(content="Deprecated as of: 2025-01-15\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "2025-01-15" in verdict.reason


# --- Contradiction detection ---


class TestContradictionDetection:
    """Contradictory stale + trust signals → needs_review."""

    def test_legacy_status_plus_incoming_refs(self):
        doc = _doc(content="Status: Legacy\n")
        verdict = classify(doc, [], incoming_ref_count=3)
        assert verdict.status == "needs_review"
        assert "contradict" in verdict.reason.lower()

    def test_archived_metadata_plus_current_status(self):
        doc = _doc(content="Status: Current\n")
        doc.metadata["archived"] = True
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert "contradict" in verdict.reason.lower()

    def test_replaced_by_plus_canonical(self):
        """Replaced by + Canonical → contradiction."""
        doc = _doc(content="Replaced by: New Guide\nCanonical: true\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert "contradict" in verdict.reason.lower()

    def test_no_contradiction_when_no_trust_evidence(self):
        """Stale signals without trust evidence → stale (not contradiction)."""
        doc = _doc(content="Status: Legacy\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"


# --- Review cadence ---


class TestReviewCadence:
    """Review cadence overdue should be a soft risk."""

    def test_cadence_overdue_with_current_status(self):
        doc = _doc(content=(
            "Status: Current\n"
            "Review cadence: Monthly\n"
            "Last reviewed: 2026-01-01\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=2)
        # Monthly max is 45 days; 2026-01-01 → 2026-06-22 is ~172 days → overdue
        assert verdict.status == "needs_review"  # Any active risk → needs_review
        assert "cadence" in verdict.reason.lower()

    def test_cadence_overdue_without_current_status(self):
        doc = _doc(content=(
            "Review cadence: Monthly\n"
            "Last reviewed: 2026-01-01\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "needs_review"
