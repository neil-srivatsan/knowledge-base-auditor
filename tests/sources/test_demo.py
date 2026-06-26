"""Unit tests for DemoSource — structure, isolation, and date consistency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kb_audit.sources.base import DocumentSource
from kb_audit.sources.demo import DemoSource, _build_pages

# ---------------------------------------------------------------------------
# Constants: expected page order
# ---------------------------------------------------------------------------

_EXPECTED_IDS = [
    "payment-processing-guide",
    "payment-api-guide-v1",
    "payment-api-guide-v2",
    "payment-api-guide-v3",
    "legacy-payment-integration",
    "payment-service-authentication",
    "merchant-retry-policy",
    "merchant-onboarding-checklist",
    "merchant-launch-checklist-draft",
    "payments-team-notes",
]

_EXPECTED_TITLES = [
    "Payment Processing Guide",
    "Payment API Guide v1",
    "Payment API Guide v2",
    "Payment API Guide v3",
    "Legacy Payment Integration",
    "Payment Service Authentication",
    "Merchant Retry Policy",
    "Merchant Onboarding Checklist",
    "Merchant Launch Checklist Draft",
    "Payments Team Notes",
]

_FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test 1 & 2: interface and source_type
# ---------------------------------------------------------------------------

class TestDemoSourceInterface:
    def test_implements_document_source(self):
        assert issubclass(DemoSource, DocumentSource)

    def test_source_type_is_demo(self):
        assert DemoSource.source_type() == "demo"

    def test_instance_source_type(self):
        assert DemoSource().source_type() == "demo"


# ---------------------------------------------------------------------------
# Tests 3 & 4: IDs, titles, order, URLs, source_types, uniqueness
# ---------------------------------------------------------------------------

class TestDemoPageStructure:
    @pytest.fixture(autouse=True)
    def pages(self):
        self._pages = _build_pages(_FIXED_NOW)

    def test_exactly_ten_pages(self):
        assert len(self._pages) == 10

    def test_ids_are_exact_and_in_order(self):
        assert [p.id for p in self._pages] == _EXPECTED_IDS

    def test_titles_are_exact_and_in_order(self):
        assert [p.title for p in self._pages] == _EXPECTED_TITLES

    def test_ids_are_unique(self):
        ids = [p.id for p in self._pages]
        assert len(ids) == len(set(ids))

    def test_titles_are_unique(self):
        titles = [p.title for p in self._pages]
        assert len(titles) == len(set(titles))

    def test_all_source_types_are_demo(self):
        for page in self._pages:
            assert page.source_type == "demo", f"{page.id} has source_type={page.source_type!r}"

    def test_urls_follow_expected_format(self):
        for page in self._pages:
            expected_url = f"https://demo.example/pages/{page.id}"
            assert page.url == expected_url, f"{page.id}: url={page.url!r}"

    def test_urls_contain_document_id(self):
        for page in self._pages:
            assert page.url is not None
            assert page.id in page.url


# ---------------------------------------------------------------------------
# Test 5 & 6: fresh instances on every fetch; mutation isolation
# ---------------------------------------------------------------------------

class TestDemoSourceIsolation:
    def test_each_fetch_returns_new_list(self):
        src = DemoSource()
        run1 = list(src.fetch_documents())
        run2 = list(src.fetch_documents())
        assert run1 is not run2

    def test_fetched_documents_are_distinct_objects(self):
        src = DemoSource()
        run1 = list(src.fetch_documents())
        run2 = list(src.fetch_documents())
        for a, b in zip(run1, run2):
            assert a is not b

    def test_fetched_documents_have_equal_content(self):
        """Two fetches at near-identical times yield the same page content."""
        # Directly compare pages built from the same fixed now (content only).
        pages1 = _build_pages(_FIXED_NOW)
        pages2 = _build_pages(_FIXED_NOW)
        for p1, p2 in zip(pages1, pages2):
            assert p1.id == p2.id
            assert p1.title == p2.title
            assert p1.content == p2.content
            assert p1.url == p2.url

    def test_mutating_fetched_page_does_not_affect_next_fetch(self):
        pages1 = _build_pages(_FIXED_NOW)
        pages1[0].title = "MUTATED"
        pages2 = _build_pages(_FIXED_NOW)
        assert pages2[0].title == "Payment Processing Guide"


# ---------------------------------------------------------------------------
# Test 7: no credentials, environment variables, or network access required
# ---------------------------------------------------------------------------

class TestDemoSourceNoCredentials:
    def test_fetch_requires_no_environment_variables(self, monkeypatch):
        """DemoSource must work with all Notion/Confluence env vars unset."""
        for var in (
            "NOTION_API_KEY",
            "CONFLUENCE_BASE_URL",
            "CONFLUENCE_EMAIL",
            "CONFLUENCE_API_TOKEN",
            "DATABASE_URL",
        ):
            monkeypatch.delenv(var, raising=False)
        pages = list(DemoSource().fetch_documents())
        assert len(pages) == 10

    def test_fetch_in_empty_tempdir_succeeds(self, tmp_path, monkeypatch):
        """DemoSource must not depend on any file in the working directory."""
        monkeypatch.chdir(tmp_path)
        pages = list(DemoSource().fetch_documents())
        assert len(pages) == 10


# ---------------------------------------------------------------------------
# Test 8: date offsets and body metadata consistency with fixed now
# ---------------------------------------------------------------------------

class TestDemoPageDates:
    @pytest.fixture(autouse=True)
    def pages(self):
        self._pages = {p.id: p for p in _build_pages(_FIXED_NOW)}

    def _days_ago(self, days: int) -> str:
        return (_FIXED_NOW - timedelta(days=days)).strftime("%Y-%m-%d")

    def test_payment_processing_guide_last_modified(self):
        p = self._pages["payment-processing-guide"]
        expected = _FIXED_NOW - timedelta(days=5)
        assert p.last_modified == expected

    def test_payment_processing_guide_last_reviewed_in_body(self):
        p = self._pages["payment-processing-guide"]
        assert self._days_ago(30) in p.content

    def test_payment_api_v1_last_modified(self):
        p = self._pages["payment-api-guide-v1"]
        expected = _FIXED_NOW - timedelta(days=520)
        assert p.last_modified == expected

    def test_payment_api_v2_last_modified(self):
        p = self._pages["payment-api-guide-v2"]
        expected = _FIXED_NOW - timedelta(days=260)
        assert p.last_modified == expected

    def test_payment_api_v3_last_modified(self):
        p = self._pages["payment-api-guide-v3"]
        expected = _FIXED_NOW - timedelta(days=3)
        assert p.last_modified == expected

    def test_payment_api_v3_last_reviewed_in_body(self):
        p = self._pages["payment-api-guide-v3"]
        assert self._days_ago(20) in p.content

    def test_legacy_last_modified(self):
        p = self._pages["legacy-payment-integration"]
        expected = _FIXED_NOW - timedelta(days=500)
        assert p.last_modified == expected

    def test_legacy_last_reviewed_in_body(self):
        p = self._pages["legacy-payment-integration"]
        assert self._days_ago(700) in p.content

    def test_legacy_deprecated_as_of_in_body(self):
        p = self._pages["legacy-payment-integration"]
        assert self._days_ago(400) in p.content

    def test_payment_auth_last_modified(self):
        p = self._pages["payment-service-authentication"]
        expected = _FIXED_NOW - timedelta(days=12)
        assert p.last_modified == expected

    def test_payment_auth_last_reviewed_in_body(self):
        p = self._pages["payment-service-authentication"]
        assert self._days_ago(500) in p.content

    def test_merchant_retry_last_reviewed_in_body(self):
        p = self._pages["merchant-retry-policy"]
        assert self._days_ago(45) in p.content

    def test_merchant_onboarding_last_reviewed_in_body(self):
        p = self._pages["merchant-onboarding-checklist"]
        assert self._days_ago(25) in p.content

    def test_merchant_draft_last_reviewed_in_body(self):
        p = self._pages["merchant-launch-checklist-draft"]
        assert self._days_ago(80) in p.content

    def test_merchant_draft_last_modified(self):
        p = self._pages["merchant-launch-checklist-draft"]
        expected = _FIXED_NOW - timedelta(days=120)
        assert p.last_modified == expected

    def test_payments_team_notes_last_modified(self):
        p = self._pages["payments-team-notes"]
        expected = _FIXED_NOW - timedelta(days=10)
        assert p.last_modified == expected


# ---------------------------------------------------------------------------
# Test 8 (continued): body metadata fields
# ---------------------------------------------------------------------------

class TestDemoBodyMetadata:
    @pytest.fixture(autouse=True)
    def pages(self):
        self._pages = {p.id: p for p in _build_pages(_FIXED_NOW)}

    def test_payment_processing_guide_has_status_current(self):
        assert "Status: Current" in self._pages["payment-processing-guide"].content

    def test_payment_processing_guide_has_canonical(self):
        assert "Canonical: true" in self._pages["payment-processing-guide"].content

    def test_payment_api_v3_has_status_current(self):
        assert "Status: Current" in self._pages["payment-api-guide-v3"].content

    def test_legacy_has_status_legacy(self):
        assert "Status: Legacy" in self._pages["legacy-payment-integration"].content

    def test_legacy_has_replaced_by(self):
        assert "Replaced by: Payment API Guide v3" in self._pages["legacy-payment-integration"].content

    def test_legacy_has_deprecated_as_of(self):
        assert "Deprecated as of:" in self._pages["legacy-payment-integration"].content

    def test_payment_auth_has_status_supported(self):
        assert "Status: Supported" in self._pages["payment-service-authentication"].content

    def test_merchant_draft_has_status_draft(self):
        assert "Status: Draft" in self._pages["merchant-launch-checklist-draft"].content

    def test_payments_team_notes_has_no_status(self):
        assert "Status:" not in self._pages["payments-team-notes"].content

    def test_payments_team_notes_has_no_owner(self):
        assert "Owner:" not in self._pages["payments-team-notes"].content


# ---------------------------------------------------------------------------
# Tests 9 & 10: reference targets
# ---------------------------------------------------------------------------

class TestDemoReferenceTargets:
    @pytest.fixture(autouse=True)
    def pages(self):
        self._pages = {p.id: p for p in _build_pages(_FIXED_NOW)}
        self._titles = {p.title for p in self._pages.values()}

    def test_payment_service_authentication_exists(self):
        assert "Payment Service Authentication" in self._titles

    def test_payment_api_guide_v3_exists(self):
        assert "Payment API Guide v3" in self._titles

    def test_payment_processing_guide_exists(self):
        assert "Payment Processing Guide" in self._titles

    def test_payment_retry_runbook_does_not_exist(self):
        """The unresolved reference target must intentionally be absent."""
        assert "Payment Retry Runbook" not in self._titles

    def test_no_page_has_id_payment_retry_runbook(self):
        assert "payment-retry-runbook" not in self._pages

    def test_merchant_onboarding_checklist_exists(self):
        assert "Merchant Onboarding Checklist" in self._titles

    def test_see_payment_service_auth_in_processing_guide(self):
        content = self._pages["payment-processing-guide"].content
        assert "See Payment Service Authentication" in content

    def test_see_payment_api_v3_in_processing_guide(self):
        content = self._pages["payment-processing-guide"].content
        assert "See Payment API Guide v3" in content

    def test_see_payment_retry_runbook_in_retry_policy(self):
        content = self._pages["merchant-retry-policy"].content
        assert "See Payment Retry Runbook" in content
