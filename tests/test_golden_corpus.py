"""Golden corpus test suite — end-to-end scenarios that exercise the full
trust classifier with realistic document content and metadata combinations.

Each scenario is a self-contained unit test with a clear expected outcome
and reasoning.  These tests serve as the release-quality acceptance suite.
"""

from datetime import datetime, timezone

from kb_audit.models import Document, Severity, StalenessSignal
from kb_audit.trust import classify


def _doc(
    id: str = "1",
    title: str = "Test Doc",
    content: str = "Some content.",
    last_modified: datetime | None = None,
    **metadata: object,
) -> Document:
    doc = Document(
        id=id, title=title, content=content, source_type="test",
        last_modified=last_modified or datetime.now(timezone.utc),
    )
    doc.metadata.update(metadata)
    return doc


def _resolved_ref(target_id: str, target_title: str) -> StalenessSignal:
    return StalenessSignal(
        signal_type="resolved_reference", severity=Severity.INFO,
        message=f"References '{target_title}' -> '{target_title}'",
        details={
            "referenced_title": target_title,
            "resolved_doc_id": target_id,
            "resolved_title": target_title,
        },
    )


def _unresolved_ref(title: str) -> StalenessSignal:
    return StalenessSignal(
        signal_type="unresolved_reference", severity=Severity.WARNING,
        message=f"References '{title}' but no matching document found",
        details={"referenced_title": title},
    )


# ---------------------------------------------------------------------------
# Scenario 1: Gold-standard current document
# ---------------------------------------------------------------------------

class TestGoldenCurrent:
    """A document with Status: Current, recent review, owner, and incoming refs
    should be classified current with high confidence."""

    def test_full_trust_signals(self):
        doc = _doc(content=(
            "Status: Current\n"
            "Owner: Platform Team\n"
            "Last reviewed: 2026-06-01\n"
            "Review cadence: Quarterly\n"
            "Canonical: true\n"
            "\nThis is the authoritative guide.\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=3)
        assert verdict.status == "current"
        assert verdict.confidence >= 0.80
        assert verdict.metadata.declared_status == "Current"
        assert verdict.metadata.owner == "Platform Team"
        assert verdict.metadata.canonical is True
        assert verdict.metadata.review_cadence == "Quarterly"
        assert len(verdict.evidence.positive_evidence) >= 2


# ---------------------------------------------------------------------------
# Scenario 2: Explicitly deprecated document
# ---------------------------------------------------------------------------

class TestGoldenDeprecated:
    """A document with Status: Deprecated should be stale regardless of
    other positive signals (when no strong trust evidence contradicts)."""

    def test_deprecated_status(self):
        doc = _doc(content="Status: Deprecated\nOwner: Legacy Team\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "Deprecated" in verdict.reason

    def test_deprecated_with_incoming_refs_is_contradiction(self):
        """Status: Deprecated + high incoming refs → needs_review (contradiction)."""
        doc = _doc(content="Status: Deprecated\nOwner: Legacy Team\n")
        verdict = classify(doc, [], incoming_ref_count=3)
        assert verdict.status == "needs_review"
        assert "contradict" in verdict.reason.lower()


# ---------------------------------------------------------------------------
# Scenario 3: Replaced-by metadata
# ---------------------------------------------------------------------------

class TestGoldenReplacedBy:
    """A document with 'Replaced by: ...' metadata should be stale."""

    def test_replaced_by_field(self):
        doc = _doc(content=(
            "Status: Active\n"
            "Replaced by: New Payment Guide v2\n"
            "\nOld content.\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "Replaced by" in verdict.reason
        assert verdict.metadata.replaced_by == "New Payment Guide v2"

    def test_superseded_by_field(self):
        doc = _doc(content="Superseded by: API Guide v3\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert verdict.metadata.replaced_by == "API Guide v3"


# ---------------------------------------------------------------------------
# Scenario 4: Deprecated-as-of metadata
# ---------------------------------------------------------------------------

class TestGoldenDeprecatedAsOf:
    """A document with 'Deprecated as of: ...' body metadata should be stale."""

    def test_deprecated_as_of_field(self):
        doc = _doc(content="Deprecated as of: 2025-01-15\nOld content.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "2025-01-15" in verdict.reason
        assert verdict.metadata.deprecated_as_of == "2025-01-15"


# ---------------------------------------------------------------------------
# Scenario 5: Canonical document
# ---------------------------------------------------------------------------

class TestGoldenCanonical:
    """A document marked Canonical: true should be current (strong trust)."""

    def test_canonical_true(self):
        doc = _doc(content="Canonical: true\nThis is the source of truth.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "current"
        assert "canonical" in verdict.reason.lower()
        assert verdict.metadata.canonical is True

    def test_canonical_yes(self):
        doc = _doc(content="Canonical: yes\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "current"


# ---------------------------------------------------------------------------
# Scenario 6: Corpus version supersession
# ---------------------------------------------------------------------------

class TestGoldenCorpusVersions:
    """Year-suffixed docs should be stale when a newer sibling exists."""

    def test_older_year_stale(self):
        doc = _doc(id="old", title="Migration Guide 2021")
        corpus = {"old": "Migration Guide 2021", "new": "Migration Guide 2024"}
        verdict = classify(doc, [], incoming_ref_count=0, corpus_titles=corpus)
        assert verdict.status == "stale"
        assert "2024" in verdict.reason

    def test_newer_year_not_stale(self):
        doc = _doc(id="new", title="Migration Guide 2024")
        corpus = {"old": "Migration Guide 2021", "new": "Migration Guide 2024"}
        verdict = classify(doc, [], incoming_ref_count=0, corpus_titles=corpus)
        assert verdict.status != "stale"

    def test_stale_suffix_old(self):
        doc = _doc(id="old", title="API Guide (old)")
        corpus = {"old": "API Guide (old)", "cur": "API Guide"}
        verdict = classify(doc, [], incoming_ref_count=0, corpus_titles=corpus)
        assert verdict.status == "stale"


# ---------------------------------------------------------------------------
# Scenario 7: No evidence → unknown
# ---------------------------------------------------------------------------

class TestGoldenUnknown:
    """A bare document with no metadata, no signals, no refs → unknown."""

    def test_bare_document(self):
        doc = _doc(content="Just some text with no metadata.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "unknown"
        assert len(verdict.evidence.missing_evidence) >= 3


# ---------------------------------------------------------------------------
# Scenario 8: Unresolved references → needs_review
# ---------------------------------------------------------------------------

class TestGoldenUnresolvedRefs:
    """Unresolved references are hard risks → needs_review."""

    def test_unresolved_ref(self):
        doc = _doc(content="See the Migration Guide for details.")
        signals = [_unresolved_ref("Migration Guide")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert "Migration Guide" in verdict.reason


# ---------------------------------------------------------------------------
# Scenario 9: Near-duplicate → needs_review (not stale)
# ---------------------------------------------------------------------------

class TestGoldenNearDuplicate:
    """Near-duplicates should be needs_review, not stale."""

    def test_near_duplicate(self):
        doc = _doc()
        signals = [StalenessSignal(
            signal_type="near_duplicate", severity=Severity.WARNING,
            message="Similar",
            details={"similarity": 88, "similar_title": "Other Doc", "similar_to": "2"},
        )]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert verdict.status != "stale"


# ---------------------------------------------------------------------------
# Scenario 10: Soft risk (old review) + Status: Current → current, lower conf
# ---------------------------------------------------------------------------

class TestGoldenSoftRiskWithCurrent:
    """Old last-reviewed with Status: Current → needs_review; positive
    evidence is preserved.  A recently-reviewed doc stays current."""

    def test_stale_review_becomes_needs_review(self):
        fresh = _doc(content="Status: Current\nLast reviewed: 2026-06-01\n")
        stale_review = _doc(content="Status: Current\nLast reviewed: 2024-01-01\n")
        v_fresh = classify(fresh, [], incoming_ref_count=2)
        v_stale = classify(stale_review, [], incoming_ref_count=2)
        assert v_fresh.status == "current"
        assert v_stale.status == "needs_review"
        # Positive evidence still present on the needs_review doc
        assert len(v_stale.evidence.positive_evidence) > 0


# ---------------------------------------------------------------------------
# Scenario 11: Soft risk without Status: Current → needs_review
# ---------------------------------------------------------------------------

class TestGoldenSoftRiskWithoutCurrent:
    """Old last-reviewed without Status: Current → needs_review even with
    incoming refs."""

    def test_old_review_supported_status(self):
        doc = _doc(content="Status: Supported\nLast reviewed: 2024-01-01\n")
        verdict = classify(doc, [], incoming_ref_count=3)
        assert verdict.status == "needs_review"


# ---------------------------------------------------------------------------
# Scenario 12: Notion archived metadata → stale
# ---------------------------------------------------------------------------

class TestGoldenArchivedMetadata:
    """A Notion page with archived=True → stale."""

    def test_archived_flag(self):
        doc = _doc(content="Some content.", archived=True)
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
        assert "archived" in verdict.reason.lower()


# ---------------------------------------------------------------------------
# Scenario 13: Body-text supersession phrases
# ---------------------------------------------------------------------------

class TestGoldenBodySupersession:
    """Body text explicitly stating obsolescence → stale."""

    def test_superseded_by_phrase(self):
        doc = _doc(content="This document is superseded by the new API Guide.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_no_longer_maintained(self):
        doc = _doc(content="This document is no longer maintained.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"

    def test_do_not_use(self):
        doc = _doc(content="Do not use this document.\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"


# ---------------------------------------------------------------------------
# Scenario 14: Review cadence overdue → soft risk
# ---------------------------------------------------------------------------

class TestGoldenReviewCadenceOverdue:
    """A document with a review cadence that's overdue should be flagged."""

    def test_quarterly_overdue(self):
        doc = _doc(content=(
            "Status: Current\n"
            "Review cadence: Quarterly\n"
            "Last reviewed: 2025-12-01\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=2)
        # Quarterly max is 120 days; 2025-12-01 → 2026-06-22 is ~203 days
        assert verdict.status == "needs_review"
        assert "cadence" in verdict.reason.lower() or "cadence" in str(verdict.evidence)

    def test_monthly_overdue_without_current_status(self):
        """Monthly cadence overdue + no Status: Current → needs_review."""
        doc = _doc(content=(
            "Review cadence: Monthly\n"
            "Last reviewed: 2026-01-01\n"
        ))
        verdict = classify(doc, [], incoming_ref_count=2)
        # Monthly max is 45 days; well overdue
        assert verdict.status == "needs_review"


# ---------------------------------------------------------------------------
# Scenario 15: Contradiction — stale status + high incoming refs
# ---------------------------------------------------------------------------

class TestGoldenContradiction:
    """Contradictory signals (stale evidence + trust evidence) → needs_review."""

    def test_legacy_status_with_many_refs(self):
        doc = _doc(content="Status: Legacy\n")
        verdict = classify(doc, [], incoming_ref_count=5)
        assert verdict.status == "needs_review"
        assert "contradict" in verdict.reason.lower()

    def test_archived_metadata_with_current_status(self):
        """Archived flag + Status: Current → contradiction → needs_review."""
        doc = _doc(content="Status: Current\n", archived=True)
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "needs_review"
        assert "contradict" in verdict.reason.lower()


# ---------------------------------------------------------------------------
# Scenario 16: Evidence structure completeness
# ---------------------------------------------------------------------------

class TestGoldenEvidenceStructure:
    """Every verdict should have well-formed evidence with all fields."""

    def test_current_evidence(self):
        doc = _doc(content="Status: Current\nCanonical: true\n")
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.evidence.summary
        assert verdict.evidence.recommended_action
        assert isinstance(verdict.evidence.positive_evidence, list)
        assert isinstance(verdict.evidence.review_risks, list)
        assert isinstance(verdict.evidence.missing_evidence, list)

    def test_stale_evidence(self):
        doc = _doc(content="Replaced by: New Guide v2\n")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert "Stale" in verdict.evidence.summary or "stale" in verdict.evidence.summary.lower()
        assert verdict.evidence.recommended_action

    def test_unknown_evidence(self):
        doc = _doc(content="Just text.")
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.evidence.summary
        assert len(verdict.evidence.missing_evidence) >= 1

    def test_needs_review_evidence(self):
        doc = _doc()
        signals = [_unresolved_ref("Missing")]
        verdict = classify(doc, signals, incoming_ref_count=0)
        assert "review" in verdict.evidence.summary.lower()
        assert len(verdict.evidence.review_risks) >= 1
