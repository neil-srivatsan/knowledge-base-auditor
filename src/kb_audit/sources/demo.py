"""Demo document source — ten realistic pages requiring no credentials or network access."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from kb_audit.models import Document
from kb_audit.sources.base import DocumentSource

_BASE_URL = "https://demo.example/pages/"


def _iso(now: datetime, days_ago: int) -> str:
    """Return an ISO YYYY-MM-DD date string for *days_ago* days before *now*."""
    return (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _build_pages(now: datetime | None = None) -> list[Document]:
    """Build all ten demo pages relative to *now*.

    Passing a fixed *now* makes date-sensitive tests deterministic.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    return [
        # 1 ─ Payment Processing Guide
        Document(
            id="payment-processing-guide",
            title="Payment Processing Guide",
            content=(
                f"Status: Current\n"
                f"Owner: Payments Platform Team\n"
                f"Last reviewed: {_iso(now, 30)}\n"
                f"Review cadence: Quarterly\n"
                f"Canonical: true\n"
                f"Applies to: Production payment processing\n"
                f"\n"
                f"Use this guide for the supported payment lifecycle.\n"
                f"See Payment Service Authentication.\n"
                f"See Payment API Guide v3.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}payment-processing-guide",
            last_modified=now - timedelta(days=5),
        ),
        # 2 ─ Payment API Guide v1
        Document(
            id="payment-api-guide-v1",
            title="Payment API Guide v1",
            content=(
                "Payment API v1 endpoint and request guide.\n"
                "\n"
                "Create charges with the original synchronous payment endpoint. "
                "The API accepts amount, currency, merchant, and card-token fields. "
                "Handle success, decline, and validation responses in the client.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}payment-api-guide-v1",
            last_modified=now - timedelta(days=520),
        ),
        # 3 ─ Payment API Guide v2
        Document(
            id="payment-api-guide-v2",
            title="Payment API Guide v2",
            content=(
                "Payment API v2 endpoint and request guide.\n"
                "\n"
                "Create charges with the version 2 payment endpoint. "
                "The API accepts amount, currency, merchant, payment-method, and idempotency fields. "
                "Handle success, decline, validation, and retry responses in the client.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}payment-api-guide-v2",
            last_modified=now - timedelta(days=260),
        ),
        # 4 ─ Payment API Guide v3
        Document(
            id="payment-api-guide-v3",
            title="Payment API Guide v3",
            content=(
                f"Status: Current\n"
                f"Owner: Payments API Team\n"
                f"Last reviewed: {_iso(now, 20)}\n"
                f"Review cadence: Quarterly\n"
                f"Applies to: Payment API v3\n"
                f"\n"
                f"Create and confirm payment intents with Payment API v3. "
                f"Use idempotency keys, structured error handling, and asynchronous status updates "
                f"for every integration.\n"
                f"See Payment Service Authentication.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}payment-api-guide-v3",
            last_modified=now - timedelta(days=3),
        ),
        # 5 ─ Legacy Payment Integration
        Document(
            id="legacy-payment-integration",
            title="Legacy Payment Integration",
            content=(
                f"Status: Legacy\n"
                f"Owner: Payments Platform Team\n"
                f"Last reviewed: {_iso(now, 700)}\n"
                f"Replaced by: Payment API Guide v3\n"
                f"Deprecated as of: {_iso(now, 400)}\n"
                f"\n"
                f"This page describes the retired API-key integration. "
                f"New implementations must use the supported payment API.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}legacy-payment-integration",
            last_modified=now - timedelta(days=500),
        ),
        # 6 ─ Payment Service Authentication
        Document(
            id="payment-service-authentication",
            title="Payment Service Authentication",
            content=(
                f"Status: Supported\n"
                f"Owner: Identity Platform Team\n"
                f"Last reviewed: {_iso(now, 500)}\n"
                f"Review cadence: Quarterly\n"
                f"Applies to: OAuth 2.0 service authentication\n"
                f"\n"
                f"Configure a service client, request a short-lived access token, "
                f"and rotate credentials through the approved secrets workflow.\n"
                f"See Payment Processing Guide.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}payment-service-authentication",
            last_modified=now - timedelta(days=12),
        ),
        # 7 ─ Merchant Retry Policy
        Document(
            id="merchant-retry-policy",
            title="Merchant Retry Policy",
            content=(
                f"Status: Current\n"
                f"Owner: Payments Reliability Team\n"
                f"Last reviewed: {_iso(now, 45)}\n"
                f"Review cadence: Quarterly\n"
                f"\n"
                f"Retry only transient payment failures. "
                f"Use exponential backoff, preserve idempotency keys, "
                f"and stop retrying permanent declines.\n"
                f"See Payment Retry Runbook.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}merchant-retry-policy",
            last_modified=now - timedelta(days=8),
        ),
        # 8 ─ Merchant Onboarding Checklist
        Document(
            id="merchant-onboarding-checklist",
            title="Merchant Onboarding Checklist",
            content=(
                f"Status: Current\n"
                f"Owner: Merchant Operations\n"
                f"Last reviewed: {_iso(now, 25)}\n"
                f"Review cadence: Quarterly\n"
                f"\n"
                f"Before launch, confirm the merchant profile, settlement bank account, "
                f"supported currencies, payment methods, webhook endpoint, and production credentials. "
                f"Record the launch owner and complete a test transaction.\n"
                f"See Payment Processing Guide.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}merchant-onboarding-checklist",
            last_modified=now - timedelta(days=6),
        ),
        # 9 ─ Merchant Launch Checklist Draft
        Document(
            id="merchant-launch-checklist-draft",
            title="Merchant Launch Checklist Draft",
            content=(
                f"Status: Draft\n"
                f"Owner: Merchant Operations\n"
                f"Last reviewed: {_iso(now, 80)}\n"
                f"\n"
                f"Before launch, confirm the merchant profile, settlement bank account, "
                f"supported currencies, payment methods, webhook endpoint, and production credential. "
                f"Record the launch owner and complete one test transaction.\n"
                f"See Payment Processing Guide.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}merchant-launch-checklist-draft",
            last_modified=now - timedelta(days=120),
        ),
        # 10 ─ Payments Team Notes
        Document(
            id="payments-team-notes",
            title="Payments Team Notes",
            content=(
                "Notes from a planning session about possible improvements to payment operations. "
                "Ideas include clearer ownership, simpler handoffs, and additional dashboards.\n"
            ),
            source_type="demo",
            url=f"{_BASE_URL}payments-team-notes",
            last_modified=now - timedelta(days=10),
        ),
    ]


class DemoSource(DocumentSource):
    """Built-in demo source: ten realistic pages, no credentials or network required."""

    @classmethod
    def source_type(cls) -> str:
        return "demo"

    def fetch_documents(self) -> Iterator[Document]:
        """Yield all demo pages built relative to the current UTC time."""
        yield from _build_pages(datetime.now(timezone.utc))

    def close(self) -> None:
        """No-op: DemoSource holds no connections."""
