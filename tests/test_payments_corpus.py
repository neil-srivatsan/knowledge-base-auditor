"""Payments corpus integration tests — classify realistic payment documentation.

Tests use in-memory Document fixtures modeled on a real Notion payments workspace.
The full pipeline (analyzers + trust classifier) is exercised.
"""

from datetime import datetime, timedelta, timezone


from kb_audit.analyzers.references import ReferenceAnalyzer
from kb_audit.analyzers.timestamp import TimestampAnalyzer
from kb_audit.auditor import Auditor
from kb_audit.models import AuditResult, Document
from kb_audit.sources.base import DocumentSource


# ---------------------------------------------------------------------------
# Fixtures: realistic payment documentation corpus
# ---------------------------------------------------------------------------

def _payments_corpus() -> list[Document]:
    """Build the Payments corpus — 6 documents with cross-references."""
    now = datetime.now(timezone.utc)

    payment_processing_guide = Document(
        id="ppg-1",
        title="Payment Processing Guide",
        content=(
            "This guide covers the end-to-end payment processing flow.\n"
            "Status: Current\n"
            "Owner: Payments Team\n"
            "Last reviewed: 2026-03-01\n\n"
            "For authentication details, see Payment Service Authentication.\n"
            "For migration from the old platform, see Payment Platform Migration Notes.\n"
        ),
        source_type="test",
        last_modified=now - timedelta(days=5),
    )

    migration_notes = Document(
        id="pmn-1",
        title="Payment Platform Migration Notes",
        content=(
            "Migration guide for moving from Platform v1 to Platform v2.\n"
            "Status: Current\n"
            "Owner: Platform Team\n"
            "Last reviewed: 2025-01-15\n\n"
            "All new integrations should follow the Payment Processing Guide.\n"
            "Legacy integrations using API key auth should refer to "
            "Payment Service Authentication. The new OAuth flow is documented there.\n"
        ),
        source_type="test",
        last_modified=now - timedelta(days=10),
    )

    auth_doc = Document(
        id="psa-1",
        title="Payment Service Authentication",
        content=(
            "This document describes the OAuth 2.0 authentication flow.\n"
            "Status: Supported\n"
            "Last reviewed: 2025-01-15\n"
            "For the full payment flow, see Payment Processing Guide.\n"
        ),
        source_type="test",
        last_modified=now - timedelta(days=15),
    )

    merchant_checklist = Document(
        id="moc-1",
        title="Merchant Onboarding Checklist",
        content=(
            "Checklist for onboarding new merchants.\n"
            "Status: Supported\n"
            "Last reviewed: 2025-01-15\n"
            "Step 1: Complete KYC verification.\n"
            "Step 2: Configure payment methods.\n"
            "For operational requirements, see Payment Processing Guide.\n"
        ),
        source_type="test",
        last_modified=now - timedelta(days=20),
    )

    legacy_integration = Document(
        id="lpi-1",
        title="Legacy Payment Integration",
        content=(
            "Status: Legacy\n"
            "This integration uses API key authentication.\n"
            "For migration steps, see Payment Platform Migration Guide 2021.\n"
            "Refer to Payment API Key Setup for key configuration.\n"
        ),
        source_type="test",
        last_modified=now - timedelta(days=200),
    )

    api_key_setup = Document(
        id="aks-1",
        title="Payment API Key Setup",
        content=(
            "How to configure API keys for the legacy payment gateway.\n"
            "See Legacy Payment Integration for full integration steps.\n"
            "For the migration guide, see Payment Platform Migration Guide 2021.\n"
        ),
        source_type="test",
        last_modified=now - timedelta(days=180),
    )

    return [
        payment_processing_guide,
        migration_notes,
        auth_doc,
        merchant_checklist,
        legacy_integration,
        api_key_setup,
    ]


class FakeSource(DocumentSource):
    def __init__(self, docs: list[Document]):
        self._docs = docs

    @classmethod
    def source_type(cls) -> str:
        return "fake"

    def fetch_documents(self):
        yield from self._docs


def _run_corpus() -> dict[str, AuditResult]:
    """Run the full pipeline on the Payments corpus and return results by ID."""
    docs = _payments_corpus()
    auditor = Auditor(
        sources=[FakeSource(docs)],
        analyzers=[
            TimestampAnalyzer(warning_days=90, critical_days=180),
            ReferenceAnalyzer(),
        ],
        reporters=[],
    )
    results = auditor.run()
    return {r.document.id: r for r in results}


# ---------------------------------------------------------------------------
# Corpus-level tests
# ---------------------------------------------------------------------------


class TestPaymentProcessingGuide:
    """Payment Processing Guide — referenced by multiple docs, has Status: Current."""

    def test_is_current(self):
        results = _run_corpus()
        r = results["ppg-1"]
        assert r.status == "current", f"Expected current, got {r.status}: {r.confidence_reason}"

    def test_reason_mentions_references(self):
        results = _run_corpus()
        r = results["ppg-1"]
        assert "Referenced by" in r.confidence_reason

    def test_reason_includes_last_reviewed(self):
        results = _run_corpus()
        r = results["ppg-1"]
        assert "Last reviewed 2026-03-01" in r.confidence_reason

    def test_reason_includes_owner(self):
        results = _run_corpus()
        r = results["ppg-1"]
        assert "Payments Team" in r.confidence_reason


class TestMigrationNotes:
    """Payment Platform Migration Notes — Status: Current + old Last reviewed.

    Any active review risk forces needs_review.  Positive evidence is
    preserved so reviewers can see the trust signals.
    """

    def test_old_review_forces_needs_review(self):
        results = _run_corpus()
        r = results["pmn-1"]
        assert r.status == "needs_review", f"Expected needs_review, got {r.status}: {r.confidence_reason}"

    def test_positive_evidence_preserved(self):
        results = _run_corpus()
        r = results["pmn-1"]
        pos = r.trust_evidence.get("positive_evidence", [])
        assert len(pos) > 0, "Positive evidence should be preserved on needs_review"

    def test_reason_mentions_old_review_date(self):
        results = _run_corpus()
        r = results["pmn-1"]
        assert "Last reviewed" in r.confidence_reason and "days ago" in r.confidence_reason


class TestPaymentServiceAuth:
    """Payment Service Authentication — Status: Supported + old Last reviewed.

    Has 2 incoming refs and resolved outgoing refs, but Supported (not Current)
    combined with old Last reviewed means soft risk is not overridden.
    """

    def test_is_needs_review(self):
        """Supported + old review → needs_review despite incoming refs."""
        results = _run_corpus()
        r = results["psa-1"]
        assert r.status == "needs_review", (
            f"Expected needs_review, got {r.status}: {r.confidence_reason}"
        )

    def test_reason_mentions_last_reviewed(self):
        results = _run_corpus()
        r = results["psa-1"]
        assert "Last reviewed" in r.confidence_reason

    def test_reason_includes_trust_context(self):
        """PSA should explain more than just old review date."""
        results = _run_corpus()
        r = results["psa-1"]
        assert "Referenced by" in r.confidence_reason
        assert "Supported" in r.confidence_reason


class TestMerchantChecklist:
    """Merchant Onboarding Checklist — Status: Supported + old Last reviewed + no incoming refs."""

    def test_is_needs_review(self):
        results = _run_corpus()
        r = results["moc-1"]
        assert r.status == "needs_review", (
            f"Expected needs_review, got {r.status}: {r.confidence_reason}"
        )

    def test_reason_includes_context(self):
        results = _run_corpus()
        r = results["moc-1"]
        assert "Last reviewed" in r.confidence_reason
        assert "No incoming references" in r.confidence_reason
        assert "Supported" in r.confidence_reason


class TestLegacyIntegration:
    """Legacy Payment Integration — Status: Legacy → stale."""

    def test_is_stale(self):
        results = _run_corpus()
        r = results["lpi-1"]
        assert r.status == "stale", f"Expected stale, got {r.status}: {r.confidence_reason}"

    def test_reason_mentions_legacy(self):
        results = _run_corpus()
        r = results["lpi-1"]
        assert "Legacy" in r.confidence_reason


class TestApiKeySetup:
    """Payment API Key Setup — references legacy doc + unresolved migration guide → needs_review."""

    def test_is_needs_review(self):
        results = _run_corpus()
        r = results["aks-1"]
        assert r.status == "needs_review", f"Expected needs_review, got {r.status}: {r.confidence_reason}"

    def test_reason_mentions_unresolved(self):
        results = _run_corpus()
        r = results["aks-1"]
        assert "unresolved" in r.confidence_reason.lower()


class TestCorpusNeverSaysNoStalenessIndicators:
    """No document in the corpus should have 'No staleness indicators detected' as reason."""

    def test_all_reasons_are_descriptive(self):
        results = _run_corpus()
        for doc_id, r in results.items():
            assert "No staleness indicators detected" not in r.confidence_reason, (
                f"{r.document.title} ({doc_id}) has forbidden reason: {r.confidence_reason}"
            )
