"""Pipeline integration tests for DemoSource.

Runs the real production analyzers and trust classifier against the ten demo
pages and asserts expected statuses, evidence, and terminology.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kb_audit.analyzers.references import ReferenceAnalyzer
from kb_audit.analyzers.similarity import SimilarityAnalyzer
from kb_audit.analyzers.timestamp import TimestampAnalyzer
from kb_audit.analyzers.version_refs import VersionRefsAnalyzer
from kb_audit.auditor import Auditor
from kb_audit.models import AuditResult
from kb_audit.sources.base import DocumentSource
from kb_audit.sources.demo import DemoSource, _build_pages

# ---------------------------------------------------------------------------
# Fixed reference point for determinism tests
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Pipeline helper
# ---------------------------------------------------------------------------

_ANALYZERS = [
    TimestampAnalyzer(),
    SimilarityAnalyzer(threshold=0.80),
    VersionRefsAnalyzer(),
    ReferenceAnalyzer(),
]


def _run_pipeline(pages=None) -> dict[str, AuditResult]:
    """Run the full pipeline and return results keyed by document ID."""
    from kb_audit.sources.base import DocumentSource

    class _FixedSource(DocumentSource):
        @classmethod
        def source_type(cls) -> str:
            return "demo"

        def fetch_documents(self):
            yield from pages

    source = _FixedSource() if pages is not None else DemoSource()
    auditor = Auditor(
        sources=[source],
        analyzers=_ANALYZERS,
        reporters=[],
        db=None,
    )
    results = auditor.run()
    return {r.document.id: r for r in results}


@pytest.fixture(scope="module")
def pipeline_results() -> dict[str, AuditResult]:
    """Run the pipeline once for the whole module using the real current time."""
    return _run_pipeline()


@pytest.fixture(scope="module")
def fixed_results() -> dict[str, AuditResult]:
    """Run the pipeline with a fixed now for determinism tests."""
    return _run_pipeline(_build_pages(_FIXED_NOW))


# ---------------------------------------------------------------------------
# Test 11: status counts
# ---------------------------------------------------------------------------

class TestStatusCounts:
    def test_exactly_three_current(self, pipeline_results):
        current = [r for r in pipeline_results.values() if r.status == "current"]
        assert len(current) == 3, (
            f"Expected 3 current, got {len(current)}: "
            + str([r.document.title for r in current])
        )

    def test_exactly_three_stale(self, pipeline_results):
        stale = [r for r in pipeline_results.values() if r.status == "stale"]
        assert len(stale) == 3, (
            f"Expected 3 stale, got {len(stale)}: "
            + str([r.document.title for r in stale])
        )

    def test_exactly_three_needs_review(self, pipeline_results):
        nr = [r for r in pipeline_results.values() if r.status == "needs_review"]
        assert len(nr) == 3, (
            f"Expected 3 needs_review, got {len(nr)}: "
            + str([r.document.title for r in nr])
        )

    def test_exactly_one_unknown(self, pipeline_results):
        unknown = [r for r in pipeline_results.values() if r.status == "unknown"]
        assert len(unknown) == 1, (
            f"Expected 1 unknown, got {len(unknown)}: "
            + str([r.document.title for r in unknown])
        )


# ---------------------------------------------------------------------------
# Test 12: per-page expected statuses
# ---------------------------------------------------------------------------

class TestPerPageStatuses:
    @pytest.mark.parametrize("doc_id, expected_status", [
        ("payment-processing-guide",      "current"),
        ("payment-api-guide-v1",          "stale"),
        ("payment-api-guide-v2",          "stale"),
        ("payment-api-guide-v3",          "current"),
        ("legacy-payment-integration",    "stale"),
        ("payment-service-authentication","needs_review"),
        ("merchant-retry-policy",         "needs_review"),
        ("merchant-onboarding-checklist", "current"),
        ("merchant-launch-checklist-draft","needs_review"),
        ("payments-team-notes",           "unknown"),
    ])
    def test_page_status(self, pipeline_results, doc_id, expected_status):
        result = pipeline_results[doc_id]
        assert result.status == expected_status, (
            f"{doc_id}: expected {expected_status!r}, "
            f"got {result.status!r}. Reason: {result.confidence_reason}"
        )


# ---------------------------------------------------------------------------
# Tests 13 & 14: suggested replacements
# ---------------------------------------------------------------------------

class TestSuggestedReplacements:
    def test_v1_suggests_v3(self, pipeline_results):
        result = pipeline_results["payment-api-guide-v1"]
        assert result.suggested_replacement is not None, (
            "Payment API Guide v1 should suggest a replacement"
        )
        assert result.suggested_replacement.id == "payment-api-guide-v3", (
            f"Expected v3, got {result.suggested_replacement.id!r}"
        )

    def test_v2_suggests_v3(self, pipeline_results):
        result = pipeline_results["payment-api-guide-v2"]
        assert result.suggested_replacement is not None, (
            "Payment API Guide v2 should suggest a replacement"
        )
        assert result.suggested_replacement.id == "payment-api-guide-v3", (
            f"Expected v3, got {result.suggested_replacement.id!r}"
        )

    def test_draft_checklist_suggests_onboarding_checklist(self, pipeline_results):
        result = pipeline_results["merchant-launch-checklist-draft"]
        assert result.suggested_replacement is not None, (
            "Merchant Launch Checklist Draft should suggest a replacement"
        )
        assert result.suggested_replacement.id == "merchant-onboarding-checklist", (
            f"Expected merchant-onboarding-checklist, got {result.suggested_replacement.id!r}"
        )


# ---------------------------------------------------------------------------
# Test 15: positive evidence on trusted pages
# ---------------------------------------------------------------------------

class TestPositiveEvidence:
    def test_processing_guide_has_positive_evidence(self, pipeline_results):
        ev = pipeline_results["payment-processing-guide"].trust_evidence
        pos = ev.get("positive_evidence", [])
        assert any("current" in p.lower() for p in pos), (
            f"Expected 'current' in positive_evidence, got: {pos}"
        )

    def test_processing_guide_has_canonical_evidence(self, pipeline_results):
        ev = pipeline_results["payment-processing-guide"].trust_evidence
        pos = ev.get("positive_evidence", [])
        assert any("canonical" in p.lower() for p in pos), (
            f"Expected canonical evidence, got: {pos}"
        )

    def test_v3_has_latest_version_evidence(self, pipeline_results):
        ev = pipeline_results["payment-api-guide-v3"].trust_evidence
        all_text = " ".join(
            ev.get("positive_evidence", []) + [ev.get("summary", "")]
        ).lower()
        assert "latest version" in all_text, (
            f"Expected 'latest version' in evidence for v3: {all_text!r}"
        )

    def test_v3_evidence_mentions_related_pages_in_this_scan(self, pipeline_results):
        ev = pipeline_results["payment-api-guide-v3"].trust_evidence
        pos_text = " ".join(ev.get("positive_evidence", [])).lower()
        assert "related pages in this scan" in pos_text, (
            f"Expected 'related pages in this scan' in positive_evidence: {pos_text!r}"
        )


# ---------------------------------------------------------------------------
# Test 16: Payment Service Authentication overdue review
# ---------------------------------------------------------------------------

class TestPaymentServiceAuthentication:
    def test_overdue_review_is_in_risks(self, pipeline_results):
        ev = pipeline_results["payment-service-authentication"].trust_evidence
        risks = ev.get("review_risks", [])
        risk_text = " ".join(risks).lower()
        assert "last reviewed" in risk_text or "review cadence" in risk_text, (
            f"Expected overdue review risk, got: {risks}"
        )

    def test_status_is_needs_review(self, pipeline_results):
        assert pipeline_results["payment-service-authentication"].status == "needs_review"


# ---------------------------------------------------------------------------
# Test 17: Merchant Retry Policy — unresolved reference
# ---------------------------------------------------------------------------

class TestMerchantRetryPolicy:
    def test_unresolved_runbook_reference_in_signals(self, pipeline_results):
        result = pipeline_results["merchant-retry-policy"]
        unresolved = [
            s for s in result.signals
            if s.signal_type == "unresolved_reference"
            and "Payment Retry Runbook" in s.details.get("referenced_title", "")
        ]
        assert unresolved, (
            "Expected an unresolved_reference signal for 'Payment Retry Runbook'"
        )

    def test_unresolved_reference_in_review_risks(self, pipeline_results):
        ev = pipeline_results["merchant-retry-policy"].trust_evidence
        risks = ev.get("review_risks", [])
        assert any("Payment Retry Runbook" in r for r in risks), (
            f"Expected 'Payment Retry Runbook' in review_risks: {risks}"
        )


# ---------------------------------------------------------------------------
# Test 18: Payments Team Notes — missing evidence
# ---------------------------------------------------------------------------

class TestPaymentsTeamNotes:
    def test_missing_evidence_is_non_empty(self, pipeline_results):
        ev = pipeline_results["payments-team-notes"].trust_evidence
        missing = ev.get("missing_evidence", [])
        assert missing, "Payments Team Notes should have missing-evidence entries"

    def test_missing_status_field_noted(self, pipeline_results):
        ev = pipeline_results["payments-team-notes"].trust_evidence
        missing_text = " ".join(ev.get("missing_evidence", [])).lower()
        assert "status" in missing_text, (
            f"Expected 'status' in missing_evidence: {ev.get('missing_evidence', [])}"
        )

    def test_missing_owner_noted(self, pipeline_results):
        ev = pipeline_results["payments-team-notes"].trust_evidence
        missing_text = " ".join(ev.get("missing_evidence", [])).lower()
        assert "owner" in missing_text, (
            f"Expected 'owner' in missing_evidence: {ev.get('missing_evidence', [])}"
        )

    def test_missing_last_reviewed_noted(self, pipeline_results):
        ev = pipeline_results["payments-team-notes"].trust_evidence
        missing_text = " ".join(ev.get("missing_evidence", [])).lower()
        assert "last reviewed" in missing_text, (
            f"Expected 'last reviewed' in missing_evidence: {ev.get('missing_evidence', [])}"
        )



# ---------------------------------------------------------------------------
# Test 19: two runs with fixed now produce identical results
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_two_fixed_now_runs_produce_identical_statuses(self):
        run1 = _run_pipeline(_build_pages(_FIXED_NOW))
        run2 = _run_pipeline(_build_pages(_FIXED_NOW))
        for doc_id in run1:
            assert run1[doc_id].status == run2[doc_id].status, (
                f"{doc_id}: run1={run1[doc_id].status!r}, run2={run2[doc_id].status!r}"
            )

    def test_two_fixed_now_runs_produce_identical_confidence(self):
        run1 = _run_pipeline(_build_pages(_FIXED_NOW))
        run2 = _run_pipeline(_build_pages(_FIXED_NOW))
        for doc_id in run1:
            assert run1[doc_id].confidence == run2[doc_id].confidence, (
                f"{doc_id}: confidence differs between runs"
            )

    def test_two_fixed_now_runs_produce_identical_reasons(self):
        run1 = _run_pipeline(_build_pages(_FIXED_NOW))
        run2 = _run_pipeline(_build_pages(_FIXED_NOW))
        for doc_id in run1:
            assert run1[doc_id].confidence_reason == run2[doc_id].confidence_reason, (
                f"{doc_id}: reason differs between runs"
            )

    def test_two_fixed_now_runs_produce_identical_signal_counts(self):
        run1 = _run_pipeline(_build_pages(_FIXED_NOW))
        run2 = _run_pipeline(_build_pages(_FIXED_NOW))
        for doc_id in run1:
            assert len(run1[doc_id].signals) == len(run2[doc_id].signals), (
                f"{doc_id}: signal count differs between runs"
            )


# ---------------------------------------------------------------------------
# Test 21: DemoSource satisfies DocumentSource contract
# ---------------------------------------------------------------------------

class TestDemoSourceContract:
    def test_demo_source_is_document_source(self):
        assert isinstance(DemoSource(), DocumentSource)

    def test_pipeline_accepts_demo_source(self):
        src = DemoSource()
        auditor = Auditor(sources=[src], analyzers=_ANALYZERS, reporters=[], db=None)
        results = auditor.run()
        assert len(results) == 10
