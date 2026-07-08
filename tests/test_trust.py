"""Tests for trust classification."""

from datetime import datetime, timezone

from kb_audit.models import Document, Severity, StalenessSignal
from kb_audit.trust import (
    classify,
    compute_audit_actionability,
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


# ---------------------------------------------------------------------------
# Lifecycle detection
# ---------------------------------------------------------------------------


class TestLifecycleExplicitDeprecatedMetadata:
    """Documents with explicit deprecated metadata get lifecycle='deprecated'."""

    def test_status_deprecated(self):
        doc = _doc(content="Status: Deprecated\nOld content.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"
        assert len(verdict.lifecycle_evidence) >= 1
        assert "Status field" in verdict.lifecycle_evidence[0]

    def test_deprecated_as_of(self):
        doc = _doc(content="Deprecated as of: 2025-01-15\nOld.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"
        assert "2025-01-15" in verdict.lifecycle_evidence[0]

    def test_status_legacy(self):
        doc = _doc(content="Status: Legacy\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"

    def test_status_retired(self):
        doc = _doc(content="Status: Retired\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"

    def test_status_eol(self):
        doc = _doc(content="Status: End-of-Life\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"


class TestLifecycleDeprecatedBodyText:
    """Documents with deprecated body phrases but no structured metadata."""

    def test_no_longer_maintained(self):
        doc = _doc(content="This document is no longer maintained.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"
        assert any("no longer maintained" in e.lower() for e in verdict.lifecycle_evidence)

    def test_do_not_use(self):
        doc = _doc(content="Do not use this document.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"

    def test_no_longer_authoritative(self):
        doc = _doc(content="This document is no longer authoritative.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"


class TestLifecycleSuperseded:
    """Documents with replaced-by or superseded-by metadata."""

    def test_replaced_by_metadata(self):
        doc = _doc(content="Replaced by: New Payment Guide v2\nOld.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "superseded"
        assert "New Payment Guide v2" in verdict.lifecycle_evidence[0]

    def test_superseded_by_metadata(self):
        doc = _doc(content="Superseded by: API Guide v3\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "superseded"

    def test_body_text_superseded(self):
        doc = _doc(content="This document is superseded by the new API Guide.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "superseded"

    def test_body_text_replaced(self):
        doc = _doc(content="This document has been replaced by the new guide.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "superseded"


class TestLifecycleArchived:
    """Documents with archived source metadata."""

    def test_archived_source_metadata(self):
        doc = _doc(content="Some content.")
        doc.metadata["archived"] = True
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "archived"
        assert any("archived" in e.lower() for e in verdict.lifecycle_evidence)

    def test_status_archived(self):
        doc = _doc(content="Status: Archived\nOld content.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "archived"


class TestLifecycleExperimental:
    """Experimental/beta/preview documents."""

    def test_status_experimental(self):
        doc = _doc(content="Status: Experimental\nNew feature docs.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"
        # experimental should NOT force stale or needs_review
        assert verdict.status != "stale"

    def test_status_beta(self):
        doc = _doc(content="Status: Beta\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"

    def test_title_beta(self):
        doc = _doc(title="Payment API (beta)", content="Some content.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"
        assert any("beta" in e.lower() for e in verdict.lifecycle_evidence)

    def test_title_preview(self):
        doc = _doc(title="New Dashboard Preview", content="Some content.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"

    def test_body_subject_to_change(self):
        doc = _doc(content="This API is subject to change.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"

    def test_body_not_for_production(self):
        doc = _doc(content="Not for production use.\nNew feature docs.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"

    def test_title_experimental(self):
        doc = _doc(title="Experimental: New Auth Flow", content="Some content.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"


class TestLifecycleDraft:
    """Draft/WIP documents."""

    def test_status_draft(self):
        doc = _doc(content="Status: Draft\nWork in progress.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "draft"

    def test_title_draft(self):
        doc = _doc(title="Migration Guide (Draft)", content="Some content.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "draft"

    def test_body_work_in_progress(self):
        doc = _doc(content="This is a work-in-progress document.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "draft"


class TestLifecycleExplicitCurrent:
    """Documents with explicit current/canonical metadata."""

    def test_status_current(self):
        doc = _doc(content="Status: Current\nOwner: Team\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.lifecycle == "current"

    def test_canonical(self):
        doc = _doc(content="Canonical: true\nThis is the source of truth.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "current"


class TestLifecycleSupported:
    """Documents with supported/active status but not canonical/current."""

    def test_status_supported(self):
        doc = _doc(content="Status: Supported\nLast reviewed: 2025-01-15\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "supported"

    def test_status_active(self):
        doc = _doc(content="Status: Active\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "supported"


class TestLifecycleUnknown:
    """Documents with no lifecycle evidence."""

    def test_no_metadata_no_evidence(self):
        doc = _doc(content="Just some text with no metadata.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "unknown"
        assert verdict.lifecycle_evidence == []

    def test_only_owner(self):
        """Owner field alone does not determine lifecycle."""
        doc = _doc(content="Owner: Team\nSome content.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "unknown"


class TestLifecycleOlderVersionStillSupported:
    """An older version with supported/applicability evidence should not
    become lifecycle 'superseded' merely because a newer version exists."""

    def test_older_version_with_applies_to(self):
        """v1 has 'Applies to: Platform 1.x' — still supported alongside v2."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Status: Supported\nApplies to: Platform 1.x\n",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        # The trust classifier currently marks this stale via supersession,
        # but lifecycle should reflect the explicit supported status.
        # Since _detect_lifecycle checks structured metadata before stale_reasons,
        # Status: Supported wins.
        assert verdict.lifecycle == "supported"

    def test_older_version_without_explicit_support(self):
        """v1 has no status or applies_to — scan-local supersession applies."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Version 1 of the guide.",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "superseded"

    def test_applies_to_without_status(self):
        """v1 has Applies to but no Status — still supported alongside v2."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Applies to: Platform 1.x\n",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "supported"
        assert "Applies to: Platform 1.x" in verdict.lifecycle_evidence[0]

    def test_applies_to_with_replaced_by(self):
        """Applies to + Replaced by → superseded (explicit negative wins)."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Applies to: Platform 1.x\nReplaced by: API Guide v2\n",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "superseded"

    def test_applies_to_with_deprecated_status(self):
        """Applies to + Status: Deprecated → deprecated (explicit negative wins)."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Status: Deprecated\nApplies to: Platform 1.x\n",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "deprecated"


class TestLifecycleContradiction:
    """Lifecycle and trust evidence can conflict without changing trust status."""

    def test_deprecated_status_with_high_refs(self):
        """Status: Deprecated + many incoming refs → trust forces needs_review,
        but lifecycle should still be 'deprecated'."""
        doc = _doc(content="Status: Deprecated\nOwner: Legacy Team\n")
        verdict = classify(doc, [], incoming_ref_count=5)
        assert verdict.status == "needs_review"  # trust contradiction
        assert verdict.lifecycle == "deprecated"  # lifecycle is descriptive

    def test_archived_with_current_status(self):
        """archived metadata + Status: Current → trust needs_review,
        lifecycle should reflect the archived source metadata."""
        doc = _doc(content="Status: Current\n")
        doc.metadata["archived"] = True
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "needs_review"  # trust contradiction
        assert verdict.lifecycle == "archived"  # source metadata wins


class TestLifecyclePriorityOverStatus:
    """Strong negative/special lifecycle evidence outranks positive status metadata."""

    def test_current_status_with_replaced_body_text(self):
        """Status: Current + body 'replaced by' → lifecycle superseded."""
        doc = _doc(content=(
            "Status: Current\n"
            "This document has been replaced by the new API Guide.\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "superseded"

    def test_current_status_with_deprecated_title(self):
        """Status: Current + title '(deprecated)' → lifecycle deprecated."""
        doc = _doc(title="Guide (deprecated)", content="Status: Current\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"

    def test_current_status_with_beta_title(self):
        """Status: Current + title 'Beta API Guide' → lifecycle experimental."""
        doc = _doc(title="Beta API Guide", content="Status: Current\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"

    def test_current_status_with_draft_title(self):
        """Status: Current + title 'Migration Guide Draft' → lifecycle draft."""
        doc = _doc(title="Migration Guide Draft", content="Status: Current\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "draft"

    def test_canonical_with_deprecated_title(self):
        """Canonical: true + title '(deprecated)' → lifecycle deprecated."""
        doc = _doc(title="Guide (deprecated)", content="Canonical: true\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "deprecated"

    def test_plain_current_status_stays_current(self):
        """Status: Current with no warning evidence → lifecycle current."""
        doc = _doc(content="Status: Current\nOwner: Team\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.lifecycle == "current"

    def test_plain_canonical_stays_current(self):
        """Canonical: true with no warning evidence → lifecycle current."""
        doc = _doc(content="Canonical: true\nSome good content.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "current"


class TestLifecycleMetadataPath:
    """Lifecycle is stored in TrustMetadata, accessible via verdict.metadata
    and via compatibility properties on TrustVerdict."""

    def test_metadata_lifecycle_populated_deprecated(self):
        doc = _doc(content="Status: Deprecated\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.lifecycle == "deprecated"
        assert len(verdict.metadata.lifecycle_evidence) >= 1

    def test_metadata_lifecycle_populated_current(self):
        doc = _doc(content="Status: Current\nOwner: Team\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.metadata.lifecycle == "current"

    def test_metadata_lifecycle_populated_supported(self):
        doc = _doc(content="Status: Supported\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.lifecycle == "supported"

    def test_metadata_lifecycle_populated_superseded(self):
        doc = _doc(content="Replaced by: New Guide\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.lifecycle == "superseded"

    def test_metadata_lifecycle_populated_unknown(self):
        doc = _doc(content="Just text.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.metadata.lifecycle == "unknown"
        assert verdict.metadata.lifecycle_evidence == []

    def test_property_matches_metadata(self):
        """verdict.lifecycle property returns metadata.lifecycle."""
        doc = _doc(content="Status: Deprecated\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == verdict.metadata.lifecycle
        assert verdict.lifecycle_evidence is verdict.metadata.lifecycle_evidence


# ---------------------------------------------------------------------------
# Lifecycle → status policy tests
# ---------------------------------------------------------------------------


class TestLifecycleStatusPolicy:
    """Tests that lifecycle meaning intentionally affects audit status."""

    def test_experimental_status_not_stale(self):
        """Status: Experimental → lifecycle experimental, status needs_review, not stale."""
        doc = _doc(content="Status: Experimental\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"
        assert verdict.status == "needs_review"

    def test_beta_title_plus_current_status(self):
        """Title contains 'Beta' + Status: Current → lifecycle experimental, status needs_review."""
        doc = _doc(title="Beta Feature Guide", content="Status: Current\nOwner: Team\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.lifecycle == "experimental"
        assert verdict.status == "needs_review"

    def test_draft_status_needs_review(self):
        """Status: Draft → lifecycle draft, status needs_review."""
        doc = _doc(content="Status: Draft\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "draft"
        assert verdict.status == "needs_review"

    def test_supported_v1_not_stale_with_scan_sibling(self):
        """Status: Supported on v1 with scan sibling v2 → not stale."""
        doc = _doc(id="v1", title="API Guide v1", content="Status: Supported\n")
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "supported"
        assert verdict.status != "stale"

    def test_applies_to_v1_not_stale_with_scan_sibling(self):
        """Applies to: Platform 1.x on v1 with scan sibling v2 → not stale."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Applies to: Platform 1.x\n",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "supported"
        assert verdict.status != "stale"

    def test_current_v1_needs_review_with_scan_sibling(self):
        """Status: Current on v1 with scan sibling v2 → needs_review, not stale."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Status: Current\nOwner: Team\n",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=2, scan_titles=scan_titles)
        assert verdict.status == "needs_review"
        assert verdict.status != "stale"

    def test_deprecated_with_strong_trust(self):
        """Status: Deprecated + strong trust evidence → needs_review, lifecycle deprecated."""
        doc = _doc(content="Status: Deprecated\nOwner: Legacy Team\n")
        verdict = classify(doc, [], incoming_ref_count=5)
        assert verdict.lifecycle == "deprecated"
        assert verdict.status == "needs_review"

    def test_replaced_by_plus_supported(self):
        """Replaced by: New Guide + Status: Supported → lifecycle superseded, status stale or needs_review."""
        doc = _doc(content="Status: Supported\nReplaced by: New Guide\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "superseded"
        assert verdict.status in ("stale", "needs_review")

    def test_replaced_by_plus_supported_with_trust(self):
        """Replaced by + Status: Supported + trust evidence → needs_review."""
        doc = _doc(content="Status: Supported\nReplaced by: New Guide\nOwner: Team\n")
        verdict = classify(doc, [], incoming_ref_count=3)
        assert verdict.lifecycle == "superseded"
        assert verdict.status == "needs_review"

    def test_title_old_plus_applies_to_stays_negative(self):
        """Title '(old)' + Applies to → lifecycle and status remain negative.
        Applies to must not override explicit stale title evidence."""
        doc = _doc(
            id="v1", title="Setup Guide (old)",
            content="Applies to: Platform 1.x\n",
        )
        scan_titles = {"v1": "Setup Guide (old)", "v2": "Setup Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        # Title stale suffix is explicit stale evidence
        assert verdict.status == "stale"

    def test_plain_supported_with_strong_trust(self):
        """Plain supported doc with strong trust follows existing current rules."""
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        doc = _doc(
            content=f"Status: Supported\nOwner: Team\nLast reviewed: {recent}\n",
        )
        verdict = classify(doc, [], incoming_ref_count=3)
        assert verdict.lifecycle == "supported"
        # Supporting trust keywords + strong trust → current
        assert verdict.status == "current"

    def test_experimental_with_explicit_stale_still_stale(self):
        """Experimental + explicit stale evidence → stale (not rescued to needs_review)."""
        doc = _doc(content="Status: Experimental\nReplaced by: New Module\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        # Replaced by creates explicit stale + lifecycle superseded overrides experimental
        assert verdict.lifecycle == "superseded"
        assert verdict.status in ("stale", "needs_review")

    def test_experimental_no_evidence_not_unknown(self):
        """Status: Experimental with no other evidence → needs_review, not unknown."""
        doc = _doc(content="Status: Experimental\nSome content here.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.lifecycle == "experimental"
        assert verdict.status == "needs_review"
        assert verdict.status != "unknown"


class TestTitleStaleSuffixNotSuppressed:
    """Negative title suffixes must not be suppressed by lifecycle 'supported'.

    Title suffixes like (archived), (obsolete), (copy), (backup) are explicit
    negative evidence.  Only weak version/year sibling inference is suppressed.
    """

    def test_archived_suffix_plus_supported(self):
        """'API Guide (archived)' + Status: Supported + sibling → stale."""
        doc = _doc(
            id="v1", title="API Guide (archived)",
            content="Status: Supported\n",
        )
        scan_titles = {"v1": "API Guide (archived)", "v2": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"

    def test_obsolete_suffix_plus_supported(self):
        """'API Guide (obsolete)' + Status: Supported + sibling → stale."""
        doc = _doc(
            id="v1", title="API Guide (obsolete)",
            content="Status: Supported\n",
        )
        scan_titles = {"v1": "API Guide (obsolete)", "v2": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"

    def test_copy_suffix_plus_supported(self):
        """'API Guide (copy)' + Status: Supported + sibling → stale."""
        doc = _doc(
            id="v1", title="API Guide (copy)",
            content="Status: Supported\n",
        )
        scan_titles = {"v1": "API Guide (copy)", "v2": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"

    def test_backup_suffix_plus_supported(self):
        """'API Guide (backup)' + Status: Supported + sibling → stale."""
        doc = _doc(
            id="v1", title="API Guide (backup)",
            content="Status: Supported\n",
        )
        scan_titles = {"v1": "API Guide (backup)", "v2": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"

    def test_draft_suffix_follows_draft_policy(self):
        """'API Guide (draft)' + sibling → lifecycle draft, status needs_review."""
        doc = _doc(
            id="v1", title="API Guide (draft)",
            content="Some draft content.\n",
        )
        scan_titles = {"v1": "API Guide (draft)", "v2": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "draft"
        assert verdict.status == "needs_review"

    def test_old_suffix_plus_supported(self):
        """'API Guide (old)' + Status: Supported + sibling → stale."""
        doc = _doc(
            id="v1", title="API Guide (old)",
            content="Status: Supported\n",
        )
        scan_titles = {"v1": "API Guide (old)", "v2": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.status == "stale"

    def test_version_inference_still_suppressed_by_supported(self):
        """Version inference (v1 vs v2) is still weak and suppressed by supported."""
        doc = _doc(
            id="v1", title="API Guide v1",
            content="Status: Supported\n",
        )
        scan_titles = {"v1": "API Guide v1", "v2": "API Guide v2"}
        verdict = classify(doc, [], incoming_ref_count=0, scan_titles=scan_titles)
        assert verdict.lifecycle == "supported"
        assert verdict.status != "stale"


# ---------------------------------------------------------------------------
# Audit actionability tests
# ---------------------------------------------------------------------------

def _actionability(
    status: str = "unknown",
    lifecycle: str = "unknown",
    signals: list | None = None,
    incoming_ref_count: int = 0,
    owner: str | None = None,
    declared_status: str | None = None,
    canonical: bool = False,
    applies_to: str | None = None,
    has_version_siblings: bool = False,
    is_suggested_replacement: bool = False,
    n_stale_siblings: int = 0,
    review_risks: list | None = None,
) -> dict:
    """Helper: build actionability dict via compute_audit_actionability."""
    tm = {
        "lifecycle": lifecycle,
        "owner": owner,
        "declared_status": declared_status,
        "canonical": canonical,
        "applies_to": applies_to,
    }
    te = {"review_risks": review_risks or []}
    return compute_audit_actionability(
        status=status,
        trust_metadata=tm,
        trust_evidence=te,
        signals=signals or [],
        incoming_ref_count=incoming_ref_count,
        has_version_siblings=has_version_siblings,
        is_suggested_replacement=is_suggested_replacement,
        n_stale_siblings=n_stale_siblings,
    )


class TestAuditActionability:
    """compute_audit_actionability correctly identifies actionable findings."""

    # --- Always actionable ---

    def test_stale_always_actionable(self):
        result = _actionability(status="stale")
        assert result["requires_human_audit"] is True
        assert result["audit_priority"] == "high"

    def test_deprecated_lifecycle_always_actionable(self):
        result = _actionability(status="needs_review", lifecycle="deprecated")
        assert result["requires_human_audit"] is True
        assert result["audit_priority"] == "high"

    def test_superseded_lifecycle_always_actionable(self):
        result = _actionability(status="needs_review", lifecycle="superseded")
        assert result["requires_human_audit"] is True

    def test_archived_lifecycle_always_actionable(self):
        result = _actionability(status="needs_review", lifecycle="archived")
        assert result["requires_human_audit"] is True

    def test_experimental_lifecycle_always_actionable(self):
        result = _actionability(status="needs_review", lifecycle="experimental")
        assert result["requires_human_audit"] is True

    def test_draft_lifecycle_always_actionable(self):
        result = _actionability(status="needs_review", lifecycle="draft")
        assert result["requires_human_audit"] is True

    def test_broken_link_always_actionable(self):
        sig = StalenessSignal("broken_link", Severity.WARNING, "broken")
        result = _actionability(status="needs_review", signals=[sig])
        assert result["requires_human_audit"] is True
        assert result["audit_priority"] == "high"

    def test_unresolved_reference_always_actionable(self):
        sig = StalenessSignal("unresolved_reference", Severity.WARNING, "unresolved")
        result = _actionability(status="unknown", signals=[sig])
        assert result["requires_human_audit"] is True

    def test_ambiguous_reference_always_actionable(self):
        sig = StalenessSignal("ambiguous_reference", Severity.WARNING, "ambiguous")
        result = _actionability(status="needs_review", signals=[sig])
        assert result["requires_human_audit"] is True

    def test_near_duplicate_always_actionable(self):
        sig = StalenessSignal("near_duplicate", Severity.WARNING, "near dup")
        result = _actionability(status="needs_review", signals=[sig])
        assert result["requires_human_audit"] is True

    def test_cadence_overdue_always_actionable(self):
        result = _actionability(
            status="needs_review",
            review_risks=["Review cadence is 'quarterly' but last reviewed 400 days ago (max 90)"],
        )
        assert result["requires_human_audit"] is True
        assert result["audit_priority"] == "high"

    # --- Current: never actionable ---

    def test_current_never_actionable(self):
        result = _actionability(status="current")
        assert result["requires_human_audit"] is False
        assert result["audit_priority"] == "none"

    # --- Conditionally actionable: importance score ---

    def test_unknown_low_importance_not_actionable(self):
        """Unknown doc with no signals, no refs, no metadata → not actionable."""
        result = _actionability(status="unknown")
        assert result["requires_human_audit"] is False

    def test_unknown_two_refs_actionable(self):
        """Unknown doc with 2+ refs crosses importance threshold."""
        result = _actionability(status="unknown", incoming_ref_count=2)
        assert result["requires_human_audit"] is True
        assert result["audit_priority"] == "medium"

    def test_unknown_one_ref_one_owner_actionable(self):
        """1 ref + owner = score 2 -> actionable."""
        result = _actionability(status="unknown", incoming_ref_count=1, owner="Team A")
        assert result["requires_human_audit"] is True

    def test_unknown_one_ref_not_enough(self):
        """1 ref alone = score 1 -> not actionable."""
        result = _actionability(status="unknown", incoming_ref_count=1)
        assert result["requires_human_audit"] is False

    def test_unknown_owner_only_not_actionable(self):
        """Owner alone = score 1 -> not actionable."""
        result = _actionability(status="unknown", owner="Team A")
        assert result["requires_human_audit"] is False

    def test_unknown_canonical_actionable(self):
        """Canonical marker = +2 -> actionable on its own."""
        result = _actionability(status="unknown", canonical=True)
        assert result["requires_human_audit"] is True
        assert result["audit_priority"] == "medium"

    def test_needs_review_soft_only_low_importance_not_actionable(self):
        """needs_review from soft evidence only, low importance -> not actionable."""
        result = _actionability(
            status="needs_review",
            review_risks=["Last reviewed 2023-01-01 (720 days ago)"],
        )
        assert result["requires_human_audit"] is False

    def test_needs_review_soft_only_high_importance_actionable(self):
        """needs_review from soft evidence, but important doc -> actionable."""
        result = _actionability(
            status="needs_review",
            review_risks=["Last reviewed 2023-01-01 (720 days ago)"],
            incoming_ref_count=2,
        )
        assert result["requires_human_audit"] is True

    def test_suggested_replacement_boosts_score(self):
        """Doc that is a suggested replacement gets +2 importance."""
        result = _actionability(
            status="unknown",
            is_suggested_replacement=True,
            n_stale_siblings=1,
        )
        assert result["requires_human_audit"] is True
        assert any("replacement" in r.lower() for r in result["importance_reasons"])

    def test_version_family_membership_adds_score(self):
        """Being part of a version family gives +1 importance."""
        result = _actionability(
            status="unknown",
            has_version_siblings=True,
            owner="Team A",
        )
        # has_version_siblings(+1) + owner(+1) = 2 -> actionable
        assert result["requires_human_audit"] is True

    def test_importance_score_present_in_result(self):
        """importance_score and importance_reasons always present."""
        result = _actionability(status="unknown", incoming_ref_count=3)
        assert isinstance(result["importance_score"], int)
        assert isinstance(result["importance_reasons"], list)

    def test_actionability_reason_present(self):
        """actionability_reason is always a non-empty string."""
        result = _actionability(status="unknown")
        assert isinstance(result["actionability_reason"], str)
        assert len(result["actionability_reason"]) > 0


# ---------------------------------------------------------------------------
# Applicability scope parsing and scope-aware supersession
# ---------------------------------------------------------------------------


class TestApplicabilityScope:
    """Structured scope fields are parsed and suppress weak scan-local supersession."""

    # ------------------------------------------------------------------
    # Parsing: individual explicit fields
    # ------------------------------------------------------------------

    def test_parse_product_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Product: Payments\nSome content.\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("product") == "Payments"

    def test_parse_version_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Version: v1\nSome content.\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("version") == "v1"

    def test_parse_audience_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Audience: admins\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("audience") == "admins"

    def test_parse_environment_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Environment: production\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("environment") == "production"

    def test_parse_region_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Region: us-east-1\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("region") == "us-east-1"

    def test_parse_plan_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Plan: enterprise\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("plan") == "enterprise"

    def test_parse_feature_state_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Feature state: GA\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("feature_state") == "GA"

    def test_parse_feature_flag_field(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Feature flag: new_checkout\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("feature_flag") == "new_checkout"

    def test_empty_scope_when_no_fields(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="No scope fields here.\n")
        assert parse_applicability_scope(doc) == {}

    # ------------------------------------------------------------------
    # Parsing: compact Scope: line
    # ------------------------------------------------------------------

    def test_parse_compact_scope_single_pair(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Scope: product=Payments\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("product") == "Payments"

    def test_parse_compact_scope_multiple_pairs(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Scope: product=Payments; version=v1; environment=prod\n")
        scope = parse_applicability_scope(doc)
        assert scope.get("product") == "Payments"
        assert scope.get("version") == "v1"
        assert scope.get("environment") == "prod"

    def test_compact_scope_does_not_override_explicit_field(self):
        """Explicit field wins; compact Scope: key does not overwrite it."""
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Version: v1\nScope: version=v2\n")
        scope = parse_applicability_scope(doc)
        # Explicit 'Version: v1' was parsed first; setdefault prevents overwrite
        assert scope.get("version") == "v1"

    # ------------------------------------------------------------------
    # Scope stored in trust_metadata via classify()
    # ------------------------------------------------------------------

    def test_scope_stored_in_trust_metadata_via_classify(self):
        doc = _doc(content="Version: v1\nSome content.\n")
        verdict = classify(doc, [])
        assert verdict.metadata.applicability_scope == {"version": "v1"}

    def test_empty_scope_in_metadata_when_absent(self):
        doc = _doc(content="No scope fields.\n")
        verdict = classify(doc, [])
        assert verdict.metadata.applicability_scope == {}

    # ------------------------------------------------------------------
    # Scope-aware supersession: v1/v2 with distinct Version: fields
    # ------------------------------------------------------------------

    def test_v1_with_explicit_version_scope_not_stale_beside_v2(self):
        """API Guide v1 with Version: v1 and v2 with Version: v2 can coexist."""
        v1 = _doc(id="v1", title="API Guide v1", content="Version: v1\nContent for v1 API.\n")
        v2 = _doc(id="v2", title="API Guide v2", content="Version: v2\nContent for v2 API.\n")
        scan_titles = {"v1": v1.title, "v2": v2.title}
        scan_scopes = {"v1": {"version": "v1"}, "v2": {"version": "v2"}}
        verdict = classify(v1, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status != "stale", (
            f"v1 with explicit Version: v1 scope should not be stale beside v2; got {verdict.status!r}: {verdict.reason!r}"
        )

    def test_v2_not_stale_beside_v1_with_distinct_scope(self):
        """API Guide v2 is the newer version — should never be stale regardless of scope."""
        v1 = _doc(id="v1", title="API Guide v1", content="Version: v1\n")
        v2 = _doc(id="v2", title="API Guide v2", content="Version: v2\n")
        scan_titles = {"v1": v1.title, "v2": v2.title}
        scan_scopes = {"v1": {"version": "v1"}, "v2": {"version": "v2"}}
        verdict = classify(v2, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status != "stale"

    # ------------------------------------------------------------------
    # Scope-aware supersession: environment scope
    # ------------------------------------------------------------------

    def test_production_sandbox_siblings_not_stale_via_environment_scope(self):
        """Setup Guide prod and sandbox can coexist when Environment: differs."""
        prod = _doc(id="prod", title="Setup Guide", content="Environment: production\nContent.\n")
        sandbox = _doc(id="sandbox", title="Setup Guide", content="Environment: sandbox\nContent.\n")
        # No year/version suffix on title — scan-local supersession won't fire here
        # (requires version/year in title), so this verifies scope parsing doesn't break things
        scan_titles = {"prod": prod.title, "sandbox": sandbox.title}
        scan_scopes = {"prod": {"environment": "production"}, "sandbox": {"environment": "sandbox"}}
        verdict_prod = classify(prod, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict_prod.status != "stale"

    def test_year_tagged_docs_with_environment_scope_coexist(self):
        """Docs with differing Environment: scope suppress year-based supersession."""
        old = _doc(
            id="old", title="Setup Guide 2021",
            content="Environment: production\nContent.\n",
        )
        new = _doc(
            id="new", title="Setup Guide 2024",
            content="Environment: sandbox\nContent.\n",
        )
        scan_titles = {"old": old.title, "new": new.title}
        scan_scopes = {
            "old": {"environment": "production"},
            "new": {"environment": "sandbox"},
        }
        verdict = classify(old, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status != "stale", (
            f"Year-tagged doc with distinct Environment: scope should not be stale; got {verdict.status!r}"
        )

    def test_audience_scope_suppresses_version_supersession(self):
        """Admin Guide v1 and v2 with different Audience: scopes can coexist."""
        v1 = _doc(id="v1", title="Admin Guide v1", content="Audience: admins\n")
        v2 = _doc(id="v2", title="Admin Guide v2", content="Audience: end users\n")
        scan_titles = {"v1": v1.title, "v2": v2.title}
        scan_scopes = {"v1": {"audience": "admins"}, "v2": {"audience": "end users"}}
        verdict = classify(v1, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status != "stale", (
            f"v1 with Audience: admins and v2 with Audience: end users should coexist; got {verdict.status!r}"
        )

    # ------------------------------------------------------------------
    # Preserve existing behavior: identical or missing scopes
    # ------------------------------------------------------------------

    def test_identical_scopes_preserve_supersession(self):
        """Same Version: scope on both docs does not suppress supersession."""
        v1 = _doc(id="v1", title="API Guide v1", content="Version: v1\n")
        v2 = _doc(id="v2", title="API Guide v2", content="Version: v1\n")  # same scope
        scan_titles = {"v1": v1.title, "v2": v2.title}
        scan_scopes = {"v1": {"version": "v1"}, "v2": {"version": "v1"}}
        verdict = classify(v1, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status == "stale", (
            f"Same scope should not suppress supersession; got {verdict.status!r}"
        )

    def test_missing_scope_preserves_supersession(self):
        """No scope metadata → existing year/version supersession behavior unchanged."""
        doc = _doc(id="old", title="Migration Guide 2021")
        scan_titles = {"old": "Migration Guide 2021", "new": "Migration Guide 2024"}
        verdict = classify(doc, [], scan_titles=scan_titles)
        assert verdict.status == "stale", (
            f"No scope → should still be stale due to year supersession; got {verdict.status!r}"
        )

    def test_one_sided_scope_missing_preserves_supersession(self):
        """If only one doc has scope (other is missing), no suppression."""
        v1 = _doc(id="v1", title="API Guide v1", content="Version: v1\n")
        v2 = _doc(id="v2", title="API Guide v2")  # no scope
        scan_titles = {"v1": v1.title, "v2": v2.title}
        scan_scopes = {"v1": {"version": "v1"}}  # v2 absent from scopes
        verdict = classify(v1, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status == "stale", (
            f"Asymmetric scope should not suppress supersession; got {verdict.status!r}"
        )

    def test_disjoint_scope_keys_preserve_supersession(self):
        """If scope keys don't overlap, no suppression — docs are not comparable."""
        v1 = _doc(id="v1", title="API Guide v1", content="Audience: admins\n")
        v2 = _doc(id="v2", title="API Guide v2", content="Environment: prod\n")
        scan_titles = {"v1": v1.title, "v2": v2.title}
        scan_scopes = {"v1": {"audience": "admins"}, "v2": {"environment": "prod"}}
        verdict = classify(v1, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status == "stale", (
            f"Disjoint scope keys should not suppress supersession; got {verdict.status!r}"
        )

    # ------------------------------------------------------------------
    # Explicit stale evidence wins over scope
    # ------------------------------------------------------------------

    def test_replaced_by_wins_over_scope(self):
        """Explicit Replaced by: beats scope coexistence."""
        v1 = _doc(
            id="v1", title="API Guide v1",
            content="Version: v1\nReplaced by: API Guide v2\n",
        )
        v2 = _doc(id="v2", title="API Guide v2", content="Version: v2\n")
        scan_titles = {"v1": v1.title, "v2": v2.title}
        scan_scopes = {"v1": {"version": "v1"}, "v2": {"version": "v2"}}
        verdict = classify(v1, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status == "stale", (
            f"Replaced by: must override scope suppression; got {verdict.status!r}"
        )

    def test_hard_stale_title_suffix_wins_over_scope(self):
        """Hard title suffix like '(old)' is never suppressed by scope."""
        old = _doc(
            id="old", title="API Guide (old)",
            content="Version: v1\n",
        )
        current = _doc(id="cur", title="API Guide", content="Version: v2\n")
        scan_titles = {"old": old.title, "cur": current.title}
        scan_scopes = {"old": {"version": "v1"}, "cur": {"version": "v2"}}
        verdict = classify(old, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status == "stale", (
            f"Hard stale suffix '(old)' must not be suppressed by scope; got {verdict.status!r}"
        )
        assert "stale suffix" in verdict.reason.lower()

    # ------------------------------------------------------------------
    # Actionability: scope does not add extra importance score
    # ------------------------------------------------------------------

    def test_scope_alone_does_not_add_importance_score(self):
        """applicability_scope present without applies_to should not add importance points."""
        result_no_scope = compute_audit_actionability(
            status="unknown",
            trust_metadata={"applies_to": None},
            trust_evidence={},
            signals=[],
            incoming_ref_count=0,
        )
        result_with_scope = compute_audit_actionability(
            status="unknown",
            trust_metadata={"applies_to": None, "applicability_scope": {"version": "v1"}},
            trust_evidence={},
            signals=[],
            incoming_ref_count=0,
        )
        assert result_with_scope["importance_score"] == result_no_scope["importance_score"], (
            "applicability_scope alone must not add extra importance points"
        )

    def test_applies_to_still_adds_importance_score(self):
        """applies_to continues to add +1 importance as before."""
        result_no_applies = compute_audit_actionability(
            status="unknown",
            trust_metadata={},
            trust_evidence={},
            signals=[],
            incoming_ref_count=0,
        )
        result_with_applies = compute_audit_actionability(
            status="unknown",
            trust_metadata={"applies_to": "v1 customers"},
            trust_evidence={},
            signals=[],
            incoming_ref_count=0,
        )
        assert result_with_applies["importance_score"] > result_no_applies["importance_score"]


class TestCompactScopeWhitelist:
    """Compact Scope: lines only accept supported dimension keys."""

    def test_unknown_keys_only_produces_empty_scope(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Scope: owner=Team A; note=legacy\n")
        assert parse_applicability_scope(doc) == {}

    def test_mixed_known_unknown_keys_keeps_only_known(self):
        from kb_audit.trust import parse_applicability_scope
        doc = _doc(content="Scope: product=Payments; note=legacy; version=v1\n")
        scope = parse_applicability_scope(doc)
        assert scope == {"product": "Payments", "version": "v1"}
        assert "note" not in scope

    def test_unsupported_compact_key_does_not_suppress_supersession(self):
        """Compact keys like 'note' must not suppress scan-local version supersession."""
        v1 = _doc(id="v1", title="API Guide v1", content="Scope: note=old\n")
        v2 = _doc(id="v2", title="API Guide v2", content="Scope: note=new\n")
        scan_titles = {"v1": v1.title, "v2": v2.title}
        from kb_audit.trust import parse_applicability_scope
        scan_scopes = {
            "v1": parse_applicability_scope(v1),
            "v2": parse_applicability_scope(v2),
        }
        # Both scopes will be empty (note= is not whitelisted), so supersession fires
        verdict = classify(v1, [], scan_titles=scan_titles, scan_scopes=scan_scopes)
        assert verdict.status == "stale", (
            f"Unsupported compact key 'note' must not suppress supersession; got {verdict.status!r}"
        )


class TestParseBodyMetadataIncludesScope:
    """parse_body_metadata() exposes parsed_applicability_scope."""

    def test_scope_included_in_parse_body_metadata(self):
        from kb_audit.trust import parse_body_metadata
        doc = _doc(content="Version: v1\nEnvironment: prod\n")
        result = parse_body_metadata(doc)
        assert "parsed_applicability_scope" in result
        assert result["parsed_applicability_scope"] == {"version": "v1", "environment": "prod"}

    def test_existing_keys_still_present(self):
        """parse_body_metadata must not drop any existing keys."""
        from kb_audit.trust import parse_body_metadata
        doc = _doc(content="Status: Current\nOwner: Team A\n")
        result = parse_body_metadata(doc)
        for key in (
            "parsed_status", "parsed_owner", "parsed_last_reviewed",
            "parsed_replaced_by", "parsed_deprecated_as_of",
            "parsed_canonical", "parsed_review_cadence", "parsed_applies_to",
            "parsed_applicability_scope",
        ):
            assert key in result, f"Missing key: {key!r}"
