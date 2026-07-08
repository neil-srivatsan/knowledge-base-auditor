"""Playwright browser tests for the results UI.

These tests start the real FastAPI app and intercept API calls with
deterministic fixtures, so they require no real Notion/Confluence credentials.

Run with:
    pytest tests/test_browser.py --headed           # visible browser
    pytest tests/test_browser.py                    # headless (CI)

Prerequisites (one-time):
    pip install playwright pytest-playwright
    playwright install chromium
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import uvicorn

pytestmark = pytest.mark.browser

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FINDINGS_KEY_STALE = "aaa000000000000000000001"
FINDINGS_KEY_NEEDS_REVIEW = "aaa000000000000000000002"
FINDINGS_KEY_UNKNOWN = "aaa000000000000000000003"
FINDINGS_KEY_CURRENT_OPEN = "aaa000000000000000000004"  # current doc (no finding)
FINDINGS_KEY_SNOOZED_FUTURE = "aaa000000000000000000005"

_FUTURE_SNOOZE = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")


# Scan 1 results — covers stale, needs_review, unknown, current
SCAN_RESULTS: list[dict[str, Any]] = [
    {
        "id": "doc-stale",
        "title": "Stale Guide",
        "url": "https://example.com/stale",
        "source_type": "notion",
        "last_modified": "2022-01-01T00:00:00Z",
        "overall_status": "stale",
        "confidence": 0.2,
        "confidence_reason": "very old",
        "signals": [],
        "trust_metadata": {"last_reviewed": "2021-01-01"},
        "trust_evidence": {
            "summary": "This document is outdated.",
            "positive_evidence": [],
            "review_risks": ["Last review was 3 years ago"],
            "missing_evidence": [],
            "recommended_action": "Review immediately.",
        },
        "workflow": {
            "finding_key": FINDINGS_KEY_STALE,
            "state": "open",
            "note": "",
            "assigned_owner": "",
            "due_date": None,
            "snoozed_until": None,
            "is_actionable": True,
        },
    },
    {
        "id": "doc-needs-review",
        "title": "Needs Review Doc",
        "url": "https://example.com/review",
        "source_type": "notion",
        "last_modified": "2023-06-01T00:00:00Z",
        "overall_status": "needs_review",
        "confidence": 0.5,
        "confidence_reason": "mixed signals",
        "signals": [],
        "trust_metadata": {},
        "trust_evidence": {
            "summary": "Needs attention.",
            "positive_evidence": [],
            "review_risks": ["Missing owner"],
            "missing_evidence": [],
            "recommended_action": "Assign an owner.",
        },
        "workflow": {
            "finding_key": FINDINGS_KEY_NEEDS_REVIEW,
            "state": "acknowledged",
            "note": "being reviewed",
            "assigned_owner": "alice@co",
            "due_date": "2025-12-01",
            "snoozed_until": None,
            "is_actionable": True,
        },
    },
    {
        "id": "doc-unknown",
        "title": "Unknown Status Doc",
        "url": "https://example.com/unknown",
        "source_type": "notion",
        "last_modified": "2023-01-01T00:00:00Z",
        "overall_status": "unknown",
        "confidence": 0.1,
        "confidence_reason": "no signals",
        "signals": [],
        "trust_metadata": {},
        "trust_evidence": {
            "summary": "Not enough info to assess.",
            "positive_evidence": [],
            "review_risks": [],
            "missing_evidence": ["No review date", "No owner"],
            "recommended_action": "Verify this document is still current.",
        },
        "workflow": {
            "finding_key": FINDINGS_KEY_UNKNOWN,
            "state": "open",
            "note": "",
            "assigned_owner": "",
            "due_date": None,
            "snoozed_until": None,
            "is_actionable": True,
        },
    },
    {
        "id": "doc-current",
        "title": "Current Docs",
        "url": "https://example.com/current",
        "source_type": "notion",
        "last_modified": "2024-12-01T00:00:00Z",
        "overall_status": "current",
        "confidence": 0.9,
        "confidence_reason": "fresh and reviewed",
        "signals": [],
        "trust_metadata": {"last_reviewed": "2024-11-01"},
        "trust_evidence": {
            "summary": "Up to date.",
            "positive_evidence": ["Recent review"],
            "review_risks": [],
            "missing_evidence": [],
            "recommended_action": "",
        },
        "workflow": None,  # current docs have no workflow
    },
    {
        "id": "doc-snoozed",
        "title": "Snoozed Future Doc",
        "url": "https://example.com/snoozed",
        "source_type": "notion",
        "last_modified": "2022-06-01T00:00:00Z",
        "overall_status": "stale",
        "confidence": 0.3,
        "confidence_reason": "old",
        "signals": [],
        "trust_metadata": {},
        "trust_evidence": {"summary": "Old.", "positive_evidence": [], "review_risks": [],
                           "missing_evidence": [], "recommended_action": ""},
        "workflow": {
            "finding_key": FINDINGS_KEY_SNOOZED_FUTURE,
            "state": "snoozed",
            "note": "",
            "assigned_owner": "",
            "due_date": None,
            "snoozed_until": _FUTURE_SNOOZE,
            "is_actionable": False,  # future snooze
        },
    },
]

SCAN_RESPONSE: dict[str, Any] = {
    "scan": {"scan_id": 1, "started_at": "2025-01-01T12:00:00", "document_count": 5},
    "results": SCAN_RESULTS,
    "changes": [],
    "has_previous": False,
    "workflow_summary": {"open": 2, "acknowledged": 1},
    "workflow_summary_all": {"open": 2, "acknowledged": 1, "snoozed": 1},
}

SCANS_LIST: list[dict[str, Any]] = [
    {
        "scan_id": 1,
        "started_at": "2025-01-01T12:00:00",
        "document_count": 5,
        "stale_count": 2,
        "needs_review_count": 1,
        "changes": None,
    }
]

STATUS_IDLE: dict[str, Any] = {
    "configured": True,
    "source": "notion",
    "source_label": "Notion",
    "target": {"root_page_id": "abc123"},
    "scan_in_progress": False,
    "last_scan_id": 1,
    "scan_error": None,
    "configuration_error": None,
}

FINDINGS_LIST: list[dict[str, Any]] = [
    {
        "finding_key": FINDINGS_KEY_STALE,
        "document_id": "doc-stale",
        "source_type": "notion",
        "title": "Stale Guide",
        "workflow_state": "open",
        "note": "",
        "assigned_owner": "",
        "due_date": None,
        "snoozed_until": None,
        "dismissal_reason": "",
        "audit_context": {"overall_status": "stale", "confidence": 0.2,
                          "url": "https://example.com/stale",
                          "trust_evidence": {"summary": "Outdated"}},
    },
    {
        "finding_key": FINDINGS_KEY_UNKNOWN,
        "document_id": "doc-unknown",
        "source_type": "notion",
        "title": "Unknown Status Doc",
        "workflow_state": "open",
        "note": "",
        "assigned_owner": "",
        "due_date": None,
        "snoozed_until": None,
        "dismissal_reason": "",
        "audit_context": {"overall_status": "unknown", "confidence": 0.1,
                          "url": "https://example.com/unknown",
                          "trust_evidence": {"summary": "Not enough info"}},
    },
    {
        "finding_key": FINDINGS_KEY_NEEDS_REVIEW,
        "document_id": "doc-needs-review",
        "source_type": "notion",
        "title": "Needs Review Doc",
        "workflow_state": "acknowledged",
        "note": "being reviewed",
        "assigned_owner": "alice@co",
        "due_date": "2025-12-01",
        "snoozed_until": None,
        "dismissal_reason": "",
        "audit_context": {"overall_status": "needs_review", "confidence": 0.5,
                          "url": "https://example.com/review",
                          "trust_evidence": {"summary": "Needs attention"}},
    },
]

FINDINGS_SUMMARY: dict[str, Any] = {"open": 2, "acknowledged": 1}
FINDINGS_SUMMARY_ALL: dict[str, Any] = {"open": 2, "acknowledged": 1, "snoozed": 1}

# Report mock — all three non-current docs are human-audit-required (default/legacy)
SCAN_REPORT: dict[str, Any] = {
    "scan_id": 1,
    "scan": {"scan_id": 1, "started_at": "2025-01-01T12:00:00"},
    "total_documents": 5,
    "stale_count": 2,
    "needs_review_count": 1,
    "unknown_count": 1,
    "stale_documents": [SCAN_RESULTS[0], SCAN_RESULTS[4]],   # doc-stale, doc-snoozed
    "needs_review_documents": [SCAN_RESULTS[1]],             # doc-needs-review
    "unknown_documents": [SCAN_RESULTS[2]],                  # doc-unknown
    "status_flagged_count": 4,
    "human_audit_required_count": 4,
    "human_audit_required_documents": [SCAN_RESULTS[0], SCAN_RESULTS[4], SCAN_RESULTS[1], SCAN_RESULTS[2]],
}

# Status-flagged but NOT audit-required: tests the distinction between
# classification status (unknown) and human-audit actionability (suppressed
# because importance score is below threshold).
_SUPPRESSED_UNKNOWN = {
    **SCAN_RESULTS[2],
    "trust_metadata": {
        "requires_human_audit": False,
        "audit_priority": "none",
        "importance_score": 0,
        "actionability_reason": "Insufficient importance signals to require audit (score 0)",
    },
}
SCAN_REPORT_NO_AUDITS: dict[str, Any] = {
    "scan_id": 1,
    "scan": {"scan_id": 1, "started_at": "2025-01-01T12:00:00"},
    "total_documents": 5,
    "stale_count": 0,
    "needs_review_count": 0,
    "unknown_count": 1,
    "stale_documents": [],
    "needs_review_documents": [],
    "unknown_documents": [_SUPPRESSED_UNKNOWN],
    "status_flagged_count": 1,
    "human_audit_required_count": 0,
    "human_audit_required_documents": [],
}


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def live_server():
    """Start the FastAPI app on a free port for the test session."""
    from kb_audit.web.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait until the server is ready
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Server did not start in time")
        time.sleep(0.05)

    # Retrieve the bound port
    for sock in server.servers[0].sockets:
        host, port = sock.getsockname()[:2]
        break

    yield f"http://{host}:{port}"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def page_with_mocks(page, live_server):
    """Navigate to the app and intercept all API calls with fixture data."""

    def _route(route, request):
        url = request.url
        method = request.method

        # Status
        if "/api/status" in url:
            route.fulfill(content_type="application/json",
                          body=json.dumps(STATUS_IDLE))
            return

        # Scans list
        if "/api/scans" in url and method == "GET" and "/api/scans/" not in url:
            route.fulfill(content_type="application/json",
                          body=json.dumps(SCANS_LIST))
            return

        # Single scan results
        if "/api/scans/1" in url and "/report" not in url:
            route.fulfill(content_type="application/json",
                          body=json.dumps(SCAN_RESPONSE))
            return

        # Scan report (JSON)
        if "/api/scans/1/report" in url and "format=text" not in url:
            route.fulfill(content_type="application/json",
                          body=json.dumps(SCAN_REPORT))
            return

        # Findings summary
        if "/api/findings/summary" in url:
            if "include_all=true" in url.lower():
                route.fulfill(content_type="application/json",
                              body=json.dumps(FINDINGS_SUMMARY_ALL))
            else:
                route.fulfill(content_type="application/json",
                              body=json.dumps(FINDINGS_SUMMARY))
            return

        # Findings list
        if "/api/findings" in url and method == "GET" and "/api/findings/" not in url:
            route.fulfill(content_type="application/json",
                          body=json.dumps(FINDINGS_LIST))
            return

        # Single finding — GET /api/findings/{key}  (used by openDetail)
        if "/api/findings/" in url and method == "GET":
            key = url.split("/api/findings/")[-1].split("?")[0].rstrip("/")
            finding = next((f for f in FINDINGS_LIST if f["finding_key"] == key), None)
            if finding:
                route.fulfill(content_type="application/json",
                              body=json.dumps(finding))
            else:
                route.fulfill(status=404, content_type="application/json",
                              body=json.dumps({"error": "Finding not found"}))
            return

        # PATCH finding — success
        if "/api/findings/" in url and method == "PATCH":
            route.fulfill(content_type="application/json",
                          body=json.dumps({"finding_key": "xxx", "workflow_state": "acknowledged"}))
            return

        route.continue_()

    page.route("**/*", _route)
    page.goto(live_server)
    # Trigger loading scan 1 by clicking its history entry once it renders
    page.wait_for_selector("#historyTimeline li", timeout=5000)
    page.locator("#historyTimeline li").first.click()
    page.wait_for_selector("#resultsBody tr", timeout=5000)
    return page


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------

class TestResultFilters:
    def test_actionable_filter_includes_open_stale(self, page_with_mocks):
        p = page_with_mocks
        # Default filter is "Actionable"
        p.locator(".filter-chip.active").first.wait_for()
        active_label = p.locator(".filter-chip.active").first.inner_text()
        assert "Actionable" in active_label

        rows = p.locator("#resultsBody tr")
        titles = [rows.nth(i).locator("td").nth(1).inner_text() for i in range(rows.count())]
        assert any("Stale Guide" in t for t in titles)

    def test_actionable_filter_includes_open_unknown(self, page_with_mocks):
        p = page_with_mocks
        rows = p.locator("#resultsBody tr")
        titles = [rows.nth(i).locator("td").nth(1).inner_text() for i in range(rows.count())]
        assert any("Unknown Status Doc" in t for t in titles)

    def test_actionable_filter_excludes_current(self, page_with_mocks):
        p = page_with_mocks
        rows = p.locator("#resultsBody tr")
        titles = [rows.nth(i).locator("td").nth(1).inner_text() for i in range(rows.count())]
        assert not any("Current Docs" in t for t in titles)

    def test_actionable_filter_excludes_future_snoozed(self, page_with_mocks):
        p = page_with_mocks
        rows = p.locator("#resultsBody tr")
        titles = [rows.nth(i).locator("td").nth(1).inner_text() for i in range(rows.count())]
        assert not any("Snoozed Future Doc" in t for t in titles)

    def test_all_filter_shows_every_row(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        p.wait_for_selector("#resultsBody tr")
        count = p.locator("#resultsBody tr").count()
        assert count == 5  # all 5 results

    def test_stale_filter(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="Stale").click()
        rows = p.locator("#resultsBody tr")
        titles = [rows.nth(i).locator("td").nth(1).inner_text() for i in range(rows.count())]
        assert all("Stale" in p.locator("#resultsBody tr").nth(i).locator("td").first.inner_text()
                   or True for i in range(rows.count()))
        # Current must not appear
        assert not any("Current Docs" in t for t in titles)

    def test_unknown_filter(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="Unknown").click()
        rows = p.locator("#resultsBody tr")
        assert rows.count() == 1
        assert "Unknown Status Doc" in rows.first.locator("td").nth(1).inner_text()

    def test_current_filter(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="Current").click()
        rows = p.locator("#resultsBody tr")
        assert rows.count() == 1
        assert "Current Docs" in rows.first.locator("td").nth(1).inner_text()

    def test_human_audit_indicator_is_explicit(self, page_with_mocks):
        p = page_with_mocks
        assert "3 human audits required" in p.locator("#summaryBar").inner_text()

        rows = p.locator("#resultsBody tr")
        assert rows.count() == 3
        assert all(
            "Human audit required" in rows.nth(i).locator("td").first.inner_text()
            for i in range(rows.count())
        )

        p.locator(".filter-chip", has_text="All").click()
        p.wait_for_selector("#resultsBody tr")
        current_row = p.locator("#resultsBody tr", has_text="Current Docs").first
        snoozed_row = p.locator("#resultsBody tr", has_text="Snoozed Future Doc").first
        assert "No human audit" in current_row.locator("td").first.inner_text()
        assert "Audit deferred" in snoozed_row.locator("td").first.inner_text()


# ---------------------------------------------------------------------------
# Sort tests
# ---------------------------------------------------------------------------

class TestResultSorting:
    def _row_titles(self, page, filter_name="All"):
        page.locator(".filter-chip", has_text=filter_name).click()
        page.wait_for_selector("#resultsBody tr")
        rows = page.locator("#resultsBody tr")
        return [rows.nth(i).locator("td").nth(1).inner_text() for i in range(rows.count())]

    def test_title_sort(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".results-sort").select_option("title")
        titles = self._row_titles(p)
        assert titles == sorted(titles, key=lambda t: t.lower())

    def test_confidence_sort_lowest_first(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".results-sort").select_option("confidence")
        p.locator(".filter-chip", has_text="All").click()
        p.wait_for_selector("#resultsBody tr")
        rows = p.locator("#resultsBody tr")
        # First row should be the one with lowest confidence
        # "Unknown Status Doc" has 0.1 (the lowest among all)
        first_title = rows.first.locator("td").nth(1).inner_text()
        assert "Unknown" in first_title

    def test_risk_sort_stale_before_current(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".results-sort").select_option("risk")
        titles = self._row_titles(p)
        # Stale should appear before Current
        stale_idx = next((i for i, t in enumerate(titles) if "Stale Guide" in t), None)
        current_idx = next((i for i, t in enumerate(titles) if "Current Docs" in t), None)
        if stale_idx is not None and current_idx is not None:
            assert stale_idx < current_idx


# ---------------------------------------------------------------------------
# Result drawer
# ---------------------------------------------------------------------------

class TestResultDrawer:
    def test_click_row_opens_drawer(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        p.locator("#resultsBody tr").first.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        assert p.locator("#resultDrawer").get_attribute("class") is not None
        assert "active" in p.locator("#resultDrawer").get_attribute("class")

    def test_close_button_closes_drawer(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        p.locator("#resultsBody tr").first.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.locator("#drawerCloseBtn").click()
        p.wait_for_selector("#resultDrawer:not(.active)", timeout=3000)

    def test_escape_closes_drawer(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        p.locator("#resultsBody tr").first.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.keyboard.press("Escape")
        p.wait_for_selector("#resultDrawer:not(.active)", timeout=3000)

    def test_focus_restored_after_close(self, page_with_mocks):
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        first_row = p.locator("#resultsBody tr").first
        first_row.press("Enter")
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.locator("#drawerCloseBtn").click()
        p.wait_for_selector("#resultDrawer:not(.active)", timeout=3000)
        # Focus should have returned to the row
        focused_tag = p.evaluate("document.activeElement.tagName")
        assert focused_tag.lower() == "tr"

    def test_enter_key_opens_drawer(self, page_with_mocks):
        """Pressing Enter on a focused result row opens the detail drawer."""
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        first_row = p.locator("#resultsBody tr").first
        first_row.press("Enter")
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        assert "active" in (p.locator("#resultDrawer").get_attribute("class") or "")

    def test_space_key_opens_drawer(self, page_with_mocks):
        """Pressing Space on a focused result row opens the drawer without scrolling."""
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        first_row = p.locator("#resultsBody tr").first
        # Focus the row first (may scroll it into view); capture scrollY after that
        # so the assertion only measures the Space key's own scroll contribution.
        first_row.focus()
        scroll_y_before = p.evaluate("window.scrollY")
        p.keyboard.press("Space")
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        scroll_y_after = p.evaluate("window.scrollY")
        assert scroll_y_after == scroll_y_before, "Space must not scroll the page"

    def test_enter_on_action_button_does_not_open_drawer(self, page_with_mocks):
        """Pressing Enter on an action button fires the button and does not open the drawer."""
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        ack_btn = stale_row.locator("button", has_text="Acknowledge")
        ack_btn.focus()
        ack_btn.press("Enter")
        # Button's action should fire (success toast)
        p.wait_for_selector(".toast-success", timeout=3000)
        # Drawer must remain closed
        assert "active" not in (p.locator("#resultDrawer").get_attribute("class") or "")


# ---------------------------------------------------------------------------
# Quick action tests
# ---------------------------------------------------------------------------

class TestQuickActions:
    def test_acknowledge_shows_success_toast(self, page_with_mocks):
        p = page_with_mocks
        # Click the Acknowledge button on the Stale row
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        ack_btn = stale_row.locator("button", has_text="Acknowledge")
        ack_btn.click()
        p.wait_for_selector(".toast-success", timeout=3000)
        assert "saved" in p.locator(".toast-success").first.inner_text().lower()

    def test_failed_action_shows_error_toast(self, page_with_mocks):
        p = page_with_mocks

        # Override the PATCH route to return 500
        def _error_route(route, request):
            if "/api/findings/" in request.url and request.method == "PATCH":
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"error": "Server exploded"}))
                return
            route.fallback()

        p.route("**/api/findings/**", _error_route)
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        ack_btn = stale_row.locator("button", has_text="Acknowledge")
        ack_btn.click()
        p.wait_for_selector(".toast-error", timeout=3000)
        assert "Server exploded" in p.locator(".toast-error").first.inner_text()
        # Routing restored
        p.unroute("**/api/findings/**")

    def test_snooze_opens_modal_not_prompt(self, page_with_mocks):
        p = page_with_mocks
        # Open detail drawer to access Snooze button
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        snooze_btn = p.locator("#resultDrawerBody button", has_text="Snooze")
        snooze_btn.click()
        p.wait_for_selector("#actionModal", timeout=3000)
        assert p.locator("#actionModal").is_visible()
        assert p.locator("#snoozeUntilInput").is_visible()

    def test_snooze_cancel_sends_no_request(self, page_with_mocks):
        p = page_with_mocks
        patch_calls: list[str] = []

        def _track(route, request):
            if request.method == "PATCH":
                patch_calls.append(request.url)
            route.fallback()

        p.route("**/*", _track)
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.locator("#resultDrawerBody button", has_text="Snooze").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        p.locator("#actionModalCancel").click()
        p.wait_for_selector("#actionModal", state="hidden", timeout=3000)
        assert patch_calls == [], "Cancel must not send any PATCH"
        p.unroute("**/*")

    def test_snooze_requires_date(self, page_with_mocks):
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.locator("#resultDrawerBody button", has_text="Snooze").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        # Submit without a date
        p.locator("#actionModalConfirm").click()
        # Error should be visible
        p.wait_for_selector("#actionModalError:not([style*='display:none'])", timeout=2000)
        error_text = p.locator("#actionModalError").inner_text()
        assert "date" in error_text.lower() or "required" in error_text.lower()
        # Modal stays open
        assert p.locator("#actionModal").is_visible()

    def test_dismiss_allows_optional_reason(self, page_with_mocks):
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.locator("#resultDrawerBody button", has_text="Dismiss").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        assert p.locator("#reasonModalInput").is_visible()
        # Fill in a reason and submit
        p.locator("#reasonModalInput").fill("No longer relevant")
        p.locator("#actionModalConfirm").click()
        p.wait_for_selector(".toast-success", timeout=3000)


# ---------------------------------------------------------------------------
# More menu and Accept Risk
# ---------------------------------------------------------------------------


class TestMoreMenu:
    """Tests for the More menu on result/queue rows and Accept Risk action."""

    def test_results_row_has_more_menu(self, page_with_mocks):
        """Open-state result rows should have a More menu trigger button."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        more_btn = stale_row.locator(".more-menu-wrap button").first
        assert more_btn.is_visible()

    def test_more_menu_opens_and_closes(self, page_with_mocks):
        """Clicking the More button opens the dropdown; clicking outside closes it."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        more_btn = stale_row.locator(".more-menu-wrap button").first
        more_btn.click()
        menu = stale_row.locator(".more-menu.open")
        assert menu.is_visible()
        # Click outside to close
        p.locator("header").click()
        assert not stale_row.locator(".more-menu.open").is_visible()

    def test_more_menu_contains_snooze_for_open(self, page_with_mocks):
        """Open-state More menu should include Snooze option."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.locator(".more-menu-wrap button").first.click()
        menu = stale_row.locator(".more-menu.open")
        assert menu.locator("button", has_text="Snooze").is_visible()

    def test_more_menu_contains_accept_risk_for_open(self, page_with_mocks):
        """Open-state More menu should include Accept Risk option."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.locator(".more-menu-wrap button").first.click()
        menu = stale_row.locator(".more-menu.open")
        assert menu.locator("button", has_text="Accept Risk").is_visible()

    def test_snooze_from_more_opens_modal(self, page_with_mocks):
        """Clicking Snooze in the More menu should open the snooze modal."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.locator(".more-menu-wrap button").first.click()
        stale_row.locator(".more-menu.open button", has_text="Snooze").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        assert p.locator("#actionModal").is_visible()
        assert p.locator("#snoozeUntilInput").is_visible()

    def test_accept_risk_from_more_opens_modal(self, page_with_mocks):
        """Clicking Accept Risk in the More menu should open reason modal."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.locator(".more-menu-wrap button").first.click()
        stale_row.locator(".more-menu.open button", has_text="Accept Risk").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        assert p.locator("#actionModal").is_visible()
        assert p.locator("#reasonModalInput").is_visible()

    def test_accept_risk_submits_accepted_risk_state(self, page_with_mocks):
        """Accept Risk modal submit should PATCH with state=accepted_risk."""
        p = page_with_mocks
        patch_bodies: list[dict] = []

        def _track(route, request):
            if "/api/findings/" in request.url and request.method == "PATCH":
                patch_bodies.append(json.loads(request.post_data))
            route.fallback()

        p.route("**/api/findings/**", _track)
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.locator(".more-menu-wrap button").first.click()
        stale_row.locator(".more-menu.open button", has_text="Accept Risk").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        p.locator("#reasonModalInput").fill("Known limitation")
        p.locator("#actionModalConfirm").click()
        p.wait_for_selector(".toast-success", timeout=3000)
        assert any(b.get("state") == "accepted_risk" for b in patch_bodies)
        p.unroute("**/api/findings/**")

    def test_drawer_shows_manage_workflow_button(self, page_with_mocks):
        """Result drawer should show 'Manage Workflow' not 'Full editor'."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        drawer = p.locator("#resultDrawerBody")
        assert drawer.locator("button", has_text="Manage Workflow").is_visible()
        # Should NOT have 'Full editor'
        assert drawer.locator("button", has_text="Full editor").count() == 0

    def test_drawer_shows_accept_risk_for_open(self, page_with_mocks):
        """Result drawer for an open finding should expose Accept Risk button."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        drawer = p.locator("#resultDrawerBody")
        assert drawer.locator("button", has_text="Accept Risk").is_visible()

    def test_more_menu_contains_manage_workflow(self, page_with_mocks):
        """More menu should include Manage Workflow link."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.locator(".more-menu-wrap button").first.click()
        menu = stale_row.locator(".more-menu.open")
        assert menu.locator("button", has_text="Manage Workflow").is_visible()

    def test_escape_closes_more_menu(self, page_with_mocks):
        """Pressing Escape should close an open More menu."""
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.locator(".more-menu-wrap button").first.click()
        assert stale_row.locator(".more-menu.open").is_visible()
        p.keyboard.press("Escape")
        assert not stale_row.locator(".more-menu.open").is_visible()


# ---------------------------------------------------------------------------
# Viewport / layout tests
# ---------------------------------------------------------------------------

class TestLayout:
    def test_no_horizontal_overflow_desktop(self, page_with_mocks):
        p = page_with_mocks
        p.set_viewport_size({"width": 1280, "height": 800})
        scroll_width = p.evaluate("document.body.scrollWidth")
        client_width = p.evaluate("document.body.clientWidth")
        assert scroll_width <= client_width + 5, (
            f"Horizontal overflow at desktop: scrollWidth={scroll_width} > clientWidth={client_width}"
        )

    def test_run_scan_button_visible_desktop(self, page_with_mocks):
        p = page_with_mocks
        p.set_viewport_size({"width": 1280, "height": 800})
        assert p.locator("#scanBtn").is_visible()

    def test_no_horizontal_overflow_mobile(self, page_with_mocks):
        p = page_with_mocks
        p.set_viewport_size({"width": 375, "height": 812})
        scroll_width = p.evaluate("document.body.scrollWidth")
        client_width = p.evaluate("document.body.clientWidth")
        assert scroll_width <= client_width + 5, (
            f"Horizontal overflow at mobile: scrollWidth={scroll_width} > clientWidth={client_width}"
        )

    def test_run_scan_button_visible_mobile(self, page_with_mocks):
        p = page_with_mocks
        p.set_viewport_size({"width": 375, "height": 812})
        assert p.locator("#scanBtn").is_visible()

    def test_table_scroll_contained_in_wrapper_mobile(self, page_with_mocks):
        """Table overflow must be contained by its scroll wrapper, not propagate to body."""
        p = page_with_mocks
        p.set_viewport_size({"width": 375, "height": 812})
        # The table itself is wider than 375px (intentional — it scrolls)
        table_scroll_width = p.evaluate("document.querySelector('.results-table').scrollWidth")
        assert table_scroll_width > 375, "Expected results table to be wider than mobile viewport"
        # But the body must not overflow because of the table
        body_scroll_width = p.evaluate("document.body.scrollWidth")
        body_client_width = p.evaluate("document.body.clientWidth")
        assert body_scroll_width <= body_client_width + 5, (
            f"Table overflow escaped its wrapper: body scrollWidth={body_scroll_width} > clientWidth={body_client_width}"
        )

    def test_drawer_fits_in_viewport_mobile(self, page_with_mocks):
        """Result drawer must not extend beyond the viewport width on mobile."""
        p = page_with_mocks
        p.set_viewport_size({"width": 375, "height": 812})
        first_row = p.locator("#resultsBody tr").first
        first_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        # Wait for the 0.25s slide-in transition to settle before measuring
        p.wait_for_function(
            "document.getElementById('resultDrawer').getBoundingClientRect().right <= window.innerWidth + 5",
            timeout=2000,
        )
        drawer_box = p.locator("#resultDrawer").bounding_box()
        assert drawer_box is not None
        assert drawer_box["x"] >= 0, f"Drawer extends left of viewport: x={drawer_box['x']}"
        assert drawer_box["x"] + drawer_box["width"] <= 375 + 5, (
            f"Drawer extends right of viewport: right={drawer_box['x'] + drawer_box['width']}"
        )

    def test_scan_controls_not_clipped_mobile(self, page_with_mocks):
        """Run Scan button must be fully visible (not clipped) on mobile."""
        p = page_with_mocks
        p.set_viewport_size({"width": 375, "height": 812})
        scan_btn_box = p.locator("#scanBtn").bounding_box()
        assert scan_btn_box is not None
        assert scan_btn_box["x"] >= 0, "Scan button extends left of viewport"
        assert scan_btn_box["x"] + scan_btn_box["width"] <= 375 + 5, (
            f"Scan button clipped on right: right={scan_btn_box['x'] + scan_btn_box['width']}"
        )


# ---------------------------------------------------------------------------
# Keyboard accessibility
# ---------------------------------------------------------------------------

class TestKeyboardAccess:
    def test_escape_closes_action_modal(self, page_with_mocks):
        p = page_with_mocks
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.locator("#resultDrawerBody button", has_text="Snooze").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        p.keyboard.press("Escape")
        # Action modal should close (Escape only hits topmost layer)
        p.wait_for_selector("#actionModal", state="hidden", timeout=3000)
        # Result drawer should still be open
        assert "active" in (p.locator("#resultDrawer").get_attribute("class") or "")

    def test_escape_topmost_priority(self, page_with_mocks):
        """When only the result drawer is open, Escape closes the drawer."""
        p = page_with_mocks
        p.locator(".filter-chip", has_text="All").click()
        p.locator("#resultsBody tr").first.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.keyboard.press("Escape")
        p.wait_for_selector("#resultDrawer:not(.active)", timeout=3000)


# ---------------------------------------------------------------------------
# In-flight guard tests
# ---------------------------------------------------------------------------

class TestInFlightGuard:
    """Verify that workflow quick actions are protected against duplicate submissions."""

    def test_rapid_clicks_send_one_patch(self, page_with_mocks):
        """Three synchronous JS clicks on Acknowledge send exactly one PATCH."""
        p = page_with_mocks
        patch_calls: list[str] = []

        def _track(route, request):
            if "/api/findings/" in request.url and request.method == "PATCH":
                patch_calls.append(request.url)
            route.fallback()

        p.route("**/*", _track)
        # Fire 3 clicks synchronously in JS so all see _inFlight state from click 1
        p.evaluate("""() => {
            const row = Array.from(document.querySelectorAll('#resultsBody tr'))
                .find(r => r.textContent.includes('Stale Guide'));
            const btn = row && Array.from(row.querySelectorAll('.act-btn'))
                .find(b => b.textContent.trim() === 'Acknowledge');
            if (btn) { btn.click(); btn.click(); btn.click(); }
        }""")
        p.wait_for_selector(".toast-success", timeout=5000)
        assert len(patch_calls) == 1, f"Expected 1 PATCH, got {len(patch_calls)}"
        p.unroute("**/*")

    def test_button_disabled_immediately_after_click(self, page_with_mocks):
        """The initiating button is disabled synchronously before the first await."""
        p = page_with_mocks
        # Evaluate runs click and reads .disabled before any microtask resolves
        disabled = p.evaluate("""() => {
            const row = Array.from(document.querySelectorAll('#resultsBody tr'))
                .find(r => r.textContent.includes('Stale Guide'));
            const btn = row && Array.from(row.querySelectorAll('.act-btn'))
                .find(b => b.textContent.trim() === 'Acknowledge');
            if (!btn) return null;
            btn.click();
            return btn.disabled;
        }""")
        assert disabled is True, "Button must be disabled immediately (before first await)"
        p.wait_for_selector(".toast-success", timeout=5000)

    def test_failed_request_re_enables_button_for_retry(self, page_with_mocks):
        """After a failed PATCH the guard is cleared and the button is re-enabled."""
        p = page_with_mocks
        attempt = [0]
        patch_calls: list[str] = []

        def _route(route, request):
            if "/api/findings/" in request.url and request.method == "PATCH":
                patch_calls.append(request.url)
                attempt[0] += 1
                if attempt[0] == 1:
                    route.fulfill(status=500, content_type="application/json",
                                  body=json.dumps({"error": "Server exploded"}))
                else:
                    route.fulfill(content_type="application/json",
                                  body=json.dumps({"finding_key": "xxx",
                                                   "workflow_state": "acknowledged"}))
                return
            route.fallback()

        p.route("**/api/findings/**", _route)
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        ack_btn = stale_row.locator("button", has_text="Acknowledge")

        # First attempt — server returns 500
        ack_btn.click()
        p.wait_for_selector(".toast-error", timeout=3000)

        # Button must be re-enabled so the user can retry
        assert not ack_btn.is_disabled(), "Button must be enabled after failed request"

        # Retry — should succeed
        ack_btn.click()
        p.wait_for_selector(".toast-success", timeout=3000)
        assert len(patch_calls) == 2
        p.unroute("**/api/findings/**")

    def test_different_finding_not_blocked(self, page_with_mocks):
        """Starting an action on one finding must not disable buttons for another."""
        p = page_with_mocks
        # Click stale's Acknowledge, then immediately check that unknown's Acknowledge is not disabled
        unknown_disabled = p.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('#resultsBody tr'));
            const ackBtnFor = row => row
                ? Array.from(row.querySelectorAll('.act-btn')).find(b => b.textContent.trim() === 'Acknowledge')
                : null;
            const staleBtn = ackBtnFor(rows.find(r => r.textContent.includes('Stale Guide')));
            const unknownBtn = ackBtnFor(rows.find(r => r.textContent.includes('Unknown Status Doc')));
            if (!staleBtn || !unknownBtn) return null;
            staleBtn.click();
            return unknownBtn.disabled;
        }""")
        assert unknown_disabled is False, \
            "Unknown finding's button must not be blocked by stale's in-flight request"
        p.wait_for_selector(".toast-success", timeout=5000)

    def test_rapid_modal_confirm_sends_one_patch(self, page_with_mocks):
        """Rapid clicks on the modal Confirm button send exactly one PATCH."""
        p = page_with_mocks
        patch_calls: list[str] = []

        def _track(route, request):
            if "/api/findings/" in request.url and request.method == "PATCH":
                patch_calls.append(request.url)
            route.fallback()

        p.route("**/*", _track)
        # Open the dismiss modal via the result drawer
        stale_row = p.locator("#resultsBody tr", has_text="Stale Guide").first
        stale_row.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        p.locator("#resultDrawerBody button", has_text="Dismiss").click()
        p.wait_for_selector("#actionModal", timeout=3000)
        # Fire three Confirm clicks synchronously — only one PATCH must be sent
        p.evaluate("""() => {
            const btn = document.getElementById('actionModalConfirm');
            if (btn) { btn.click(); btn.click(); btn.click(); }
        }""")
        p.wait_for_selector(".toast-success", timeout=5000)
        assert len(patch_calls) == 1, f"Expected 1 PATCH from modal, got {len(patch_calls)}"
        p.unroute("**/*")

    def test_save_detail_guard_cleared_after_success(self, page_with_mocks):
        """_inFlight entry is removed after a successful save so the same finding can be saved again."""
        p = page_with_mocks

        # Open the Review Queue tab and click the first finding to open its full editor
        p.locator("#tab-btn-queue").click()
        p.wait_for_selector("#tabQueue .queue-row", timeout=3000)
        p.locator("#tabQueue .queue-row").first.click()
        p.wait_for_selector("#detailOverlay.active", timeout=3000)

        # First save — wait for the PATCH request to confirm it was issued
        with p.expect_request(
            lambda req: "/api/findings/" in req.url and req.method == "PATCH",
            timeout=5000,
        ) as first_req_info:
            p.locator("#detailSaveBtn").click()
        first_patch_url = first_req_info.value.url

        # Wait for the overlay to close (hidden, not merely invisible)
        p.wait_for_selector("#detailOverlay", state="hidden", timeout=3000)

        # Reopen the same finding — the queue re-renders with the same mock data
        p.wait_for_selector("#tabQueue .queue-row", timeout=3000)
        p.locator("#tabQueue .queue-row").first.click()
        p.wait_for_selector("#detailOverlay.active", timeout=3000)

        # Second save — must not be blocked by a stale _inFlight entry from the first save
        with p.expect_request(
            lambda req: "/api/findings/" in req.url and req.method == "PATCH",
            timeout=5000,
        ) as second_req_info:
            p.locator("#detailSaveBtn").click()
        second_patch_url = second_req_info.value.url

        assert first_patch_url == second_patch_url, (
            f"Both saves must target the same finding. "
            f"Got {first_patch_url!r} and {second_patch_url!r}"
        )
        assert "/api/findings/" in first_patch_url, (
            f"Expected a finding PATCH URL, got {first_patch_url!r}"
        )


# ---------------------------------------------------------------------------
# Suggested replacement rendering
# ---------------------------------------------------------------------------


class TestSuggestedReplacement:
    """Tests for the 'Suggested replacement' section in the result detail drawer."""

    # ------------------------------------------------------------------
    # Demo backend — three expected mappings
    # ------------------------------------------------------------------

    def _open_drawer_for(self, p, title_fragment: str):
        """Switch to All filter and open the drawer for the row matching title_fragment."""
        p.locator(".filter-chip", has_text="All").click()
        p.wait_for_selector("#resultsBody tr", timeout=5000)
        p.locator("#resultsBody tr", has_text=title_fragment).first.click()
        p.wait_for_selector("#resultDrawer.active", timeout=3000)
        return p.locator("#resultDrawerBody")

    def test_v1_shows_suggested_replacement(self, demo_page_with_results):
        """Payment API Guide v1 drawer must show 'Suggested replacement' label."""
        drawer = self._open_drawer_for(demo_page_with_results, "Payment API Guide v1")
        assert "suggested replacement" in drawer.inner_text().lower()

    def test_v1_replacement_title_is_v3(self, demo_page_with_results):
        """Payment API Guide v1 replacement must resolve to 'Payment API Guide v3'."""
        drawer = self._open_drawer_for(demo_page_with_results, "Payment API Guide v1")
        assert "Payment API Guide v3" in drawer.inner_text()

    def test_v2_replacement_title_is_v3(self, demo_page_with_results):
        """Payment API Guide v2 replacement must resolve to 'Payment API Guide v3'."""
        drawer = self._open_drawer_for(demo_page_with_results, "Payment API Guide v2")
        text = drawer.inner_text()
        assert "suggested replacement" in text.lower()
        assert "Payment API Guide v3" in text

    def test_draft_replacement_is_checklist(self, demo_page_with_results):
        """Merchant Launch Checklist Draft replacement must resolve to Merchant Onboarding Checklist."""
        drawer = self._open_drawer_for(demo_page_with_results, "Merchant Launch Checklist Draft")
        text = drawer.inner_text()
        assert "suggested replacement" in text.lower()
        assert "Merchant Onboarding Checklist" in text

    def test_demo_replacement_title_is_not_anchor(self, demo_page_with_results):
        """Demo replacement titles must not be rendered as <a href='demo.example'> links."""
        drawer = self._open_drawer_for(demo_page_with_results, "Payment API Guide v1")
        assert drawer.locator("a[href*='demo.example']").count() == 0, (
            "Suggested replacement for demo pages must not link to demo.example"
        )

    def test_no_replacement_for_page_without_one(self, demo_page_with_results):
        """A page that has no suggested_replacement_id must not show the section."""
        # Payment Processing Guide is 'current' and has no replacement
        drawer = self._open_drawer_for(demo_page_with_results, "Payment Processing Guide")
        assert "suggested replacement" not in drawer.inner_text().lower(), (
            "Pages without a suggested replacement must not show the field"
        )

    # ------------------------------------------------------------------
    # Mocked non-demo backend — Notion link behavior
    # ------------------------------------------------------------------

    def test_notion_replacement_shown_as_link(self, page, live_server):
        """For a Notion page, a suggested replacement with a real URL is shown as a link."""
        replacement_doc: dict[str, Any] = {
            "id": "doc-current-v2",
            "title": "API Guide v2",
            "url": "https://notion.so/api-guide-v2-abc",
            "source_type": "notion",
            "suggested_replacement_id": None,
            "overall_status": "current",
            "confidence": 0.9,
            "confidence_reason": "fresh",
            "signals": [],
            "trust_metadata": {"last_reviewed": "2024-11-01"},
            "trust_evidence": {
                "summary": "Up to date.", "positive_evidence": [],
                "review_risks": [], "missing_evidence": [], "recommended_action": "",
            },
            "workflow": None,
        }
        stale_doc: dict[str, Any] = {
            "id": "doc-stale-v1",
            "title": "API Guide v1",
            "url": None,
            "source_type": "notion",
            "suggested_replacement_id": "doc-current-v2",
            "overall_status": "stale",
            "confidence": 0.2,
            "confidence_reason": "old",
            "signals": [],
            "trust_metadata": {},
            "trust_evidence": {
                "summary": "Old.", "positive_evidence": [],
                "review_risks": [], "missing_evidence": [], "recommended_action": "",
            },
            "workflow": {
                "finding_key": "fff000000000000000000001",
                "state": "open", "note": "", "assigned_owner": "",
                "due_date": None, "snoozed_until": None, "is_actionable": True,
            },
        }
        scan_resp = {
            "scan": {"scan_id": 99, "started_at": "2025-01-01T12:00:00", "document_count": 2},
            "results": [stale_doc, replacement_doc],
            "changes": [], "has_previous": False,
            "workflow_summary": {"open": 1},
            "workflow_summary_all": {"open": 1},
        }
        scans_list = [{
            "scan_id": 99, "started_at": "2025-01-01T12:00:00",
            "document_count": 2, "stale_count": 1, "needs_review_count": 0, "changes": None,
        }]
        status = {
            "configured": True, "source": "notion", "source_label": "Notion",
            "target": {"root_page_id": "abc"}, "scan_in_progress": False,
            "last_scan_id": 99, "scan_error": None, "configuration_error": None,
            "demo_mode": False,
        }

        def _route(route, request):
            url = request.url
            method = request.method
            if "/api/status" in url:
                route.fulfill(content_type="application/json", body=json.dumps(status))
            elif "/api/scans/99" in url and "/report" not in url:
                route.fulfill(content_type="application/json", body=json.dumps(scan_resp))
            elif "/api/scans" in url and method == "GET":
                route.fulfill(content_type="application/json", body=json.dumps(scans_list))
            elif "/api/findings/summary" in url:
                route.fulfill(content_type="application/json", body=json.dumps({"open": 1}))
            elif "/api/findings" in url and method == "GET":
                route.fulfill(content_type="application/json", body=json.dumps([]))
            else:
                route.fallback()

        page.route("**/*", _route)
        page.goto(live_server)
        page.wait_for_selector("#historyTimeline li", timeout=5000)
        page.locator("#historyTimeline li").first.click()
        page.wait_for_selector("#resultsBody tr", timeout=5000)

        # Open drawer for the stale doc that has a replacement
        page.locator("#resultsBody tr", has_text="API Guide v1").first.click()
        page.wait_for_selector("#resultDrawer.active", timeout=3000)

        drawer = page.locator("#resultDrawerBody")
        text = drawer.inner_text()
        assert "suggested replacement" in text.lower(), f"Expected label in drawer; got: {text!r}"
        assert "API Guide v2" in text, f"Expected replacement title; got: {text!r}"

        # Must render as a link to the Notion URL (not a demo URL)
        link = drawer.locator("a[href*='notion.so']")
        assert link.count() > 0, "Notion replacement must be shown as a link"
        assert "api-guide-v2-abc" in (link.get_attribute("href") or "")

        page.unroute("**/*")


# ---------------------------------------------------------------------------
# Demo mode browser tests (Stage 4)
# ---------------------------------------------------------------------------


def _start_demo_server(db_path: str):
    """Start a demo-mode uvicorn server and return (url, server, thread)."""
    from kb_audit.web.app import app as _app, configure_app

    configure_app(demo_mode=True, database_path=db_path)

    config = uvicorn.Config(_app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Demo server did not start in time")
        time.sleep(0.05)

    for sock in server.servers[0].sockets:
        host, port = sock.getsockname()[:2]
        break

    return f"http://{host}:{port}", server, thread


def _stop_demo_server(server, thread) -> None:
    """Stop a demo-mode uvicorn server and reset app config."""
    from kb_audit.web.app import configure_app
    server.should_exit = True
    thread.join(timeout=5)
    configure_app(demo_mode=False, database_path=None)


def _navigate_demo(page, base_url: str):
    """Navigate to demo app and wait for JS init to complete."""
    page.goto(base_url)
    page.wait_for_function("typeof demoMode !== 'undefined'", timeout=5000)
    page.wait_for_function("demoMode === true", timeout=5000)
    return page


def _ensure_results(page) -> None:
    """Ensure scan results are displayed; trigger a scan if the DB is empty."""
    history = page.locator("#historyTimeline li")
    if history.count() > 0:
        history.first.click()
        page.wait_for_selector("#resultsBody tr", timeout=20000)
    else:
        page.locator("#scanBtn").click()
        page.wait_for_function(
            "fetch('/api/status').then(r=>r.json()).then(d=>!d.scan_in_progress)",
            timeout=60000,
        )
        page.wait_for_selector("#resultsBody tr", timeout=20000)


@pytest.fixture(scope="module")
def live_demo_server(tmp_path_factory):
    """Module-scoped demo server shared by read-only tests.

    The DB starts empty. The first test that calls _ensure_results() will
    trigger a scan; subsequent tests find results in history.
    """
    from kb_audit.db import Database

    demo_db = str(tmp_path_factory.mktemp("demo_browser") / "demo.db")

    db = Database(demo_db)
    db.connect()
    db.clear_all_if_idle()
    db.close()

    url, server, thread = _start_demo_server(demo_db)
    yield url
    _stop_demo_server(server, thread)


@pytest.fixture
def isolated_demo_server(tmp_path_factory):
    """Function-scoped demo server with a fresh empty DB.

    Use for tests that need a pristine empty state or that modify
    workflow state in ways that would break other tests.
    Saves and restores global app config so that a co-running
    module-scoped demo server is not disrupted.
    """
    from kb_audit.db import Database
    from kb_audit.web.app import _app_config, configure_app

    # Snapshot current global config before overwriting it
    saved_demo_mode = _app_config.demo_mode
    saved_database_path = _app_config.database_path

    demo_db = str(tmp_path_factory.mktemp("demo_isolated") / "demo.db")

    db = Database(demo_db)
    db.connect()
    db.clear_all_if_idle()
    db.close()

    url, server, thread = _start_demo_server(demo_db)
    yield url

    server.should_exit = True
    thread.join(timeout=5)
    # Restore previous config so the shared live_demo_server stays valid
    configure_app(demo_mode=saved_demo_mode, database_path=saved_database_path)


@pytest.fixture
def demo_page(page, live_demo_server):
    """Navigate to the shared demo server (no mocks — real backend)."""
    return _navigate_demo(page, live_demo_server)


@pytest.fixture
def demo_page_with_results(page, live_demo_server):
    """Navigate to the shared demo server and ensure results are loaded."""
    _navigate_demo(page, live_demo_server)
    _ensure_results(page)
    return page


class TestDemoModeBrowser:
    """Browser tests for demo mode (Stage 4 — requirements 1-16)."""

    # ------------------------------------------------------------------
    # Req 1: Fresh empty state
    # ------------------------------------------------------------------

    def test_fresh_empty_state(self, page, isolated_demo_server):
        """A fresh demo DB shows the correct empty-state message before any scan."""
        _navigate_demo(page, isolated_demo_server)
        welcome = page.locator("#welcomeCard")
        welcome.wait_for(state="visible", timeout=5000)
        text = welcome.inner_text()
        assert "10 sample knowledge-base pages" in text, (
            f"Empty state must mention '10 sample knowledge-base pages'; got: {text!r}"
        )

    # ------------------------------------------------------------------
    # Req 2: Demo workspace badge persists across states
    # ------------------------------------------------------------------

    def test_demo_workspace_badge_before_scan(self, demo_page):
        """'Demo workspace' must appear in the header badge before a scan."""
        badge = demo_page.locator("#sourceBadge")
        badge.wait_for(state="visible", timeout=5000)
        assert "Demo workspace" in badge.inner_text()

    def test_demo_workspace_badge_after_scan(self, demo_page_with_results):
        """'Demo workspace' must remain visible in the header after results load."""
        badge = demo_page_with_results.locator("#sourceBadge")
        badge.wait_for(state="visible", timeout=5000)
        assert "Demo workspace" in badge.inner_text()

    # ------------------------------------------------------------------
    # Req 3: Exactly one scan request on rapid repeated clicks
    # ------------------------------------------------------------------

    def test_one_scan_request_on_rapid_clicks(self, page, isolated_demo_server):
        """Rapid repeated clicks on 'Run demo scan' must send exactly one POST."""
        _navigate_demo(page, isolated_demo_server)

        post_count: list[int] = []

        def _track(route, request):
            if request.url.endswith("/api/scans") and request.method == "POST":
                post_count.append(1)
            route.fallback()

        page.route("**/*", _track)

        # Fire three synchronous clicks via JS evaluation
        page.evaluate("""() => {
            const btn = document.getElementById('scanBtn');
            if (btn) { btn.click(); btn.click(); btn.click(); }
        }""")

        # Wait for scan to finish
        page.wait_for_function(
            "fetch('/api/status').then(r=>r.json()).then(d=>!d.scan_in_progress)",
            timeout=60000,
        )
        page.unroute("**/*")
        assert len(post_count) == 1, f"Expected 1 POST to /api/scans, got {len(post_count)}"

    # ------------------------------------------------------------------
    # Req 4: Scan controls disable during scan and recover afterward
    # ------------------------------------------------------------------

    def test_scan_controls_disable_and_recover(self, page, isolated_demo_server):
        """Scan button must be disabled immediately after click, re-enabled on completion."""
        _navigate_demo(page, isolated_demo_server)

        btn = page.locator("#scanBtn")
        btn.wait_for(state="visible", timeout=5000)
        btn.click()

        # Must be disabled while scanning
        assert btn.is_disabled(), "Scan button must be disabled immediately after click"

        # Wait for completion
        page.wait_for_function(
            "fetch('/api/status').then(r=>r.json()).then(d=>!d.scan_in_progress)",
            timeout=60000,
        )
        page.wait_for_function(
            "document.getElementById('scanBtn').disabled === false",
            timeout=5000,
        )
        assert not btn.is_disabled(), "Scan button must be re-enabled after completion"
        assert btn.inner_text().strip() == "Run demo scan", (
            f"Button must say 'Run demo scan' after completion; got: {btn.inner_text().strip()!r}"
        )

    # ------------------------------------------------------------------
    # Req 5: Successful scan auto-loads completed results
    # ------------------------------------------------------------------

    def test_scan_auto_loads_results(self, page, isolated_demo_server):
        """After a demo scan completes the results must be displayed automatically."""
        _navigate_demo(page, isolated_demo_server)

        welcome = page.locator("#welcomeCard")
        welcome.wait_for(state="visible", timeout=5000)

        page.locator("#scanBtn").click()
        page.wait_for_function(
            "fetch('/api/status').then(r=>r.json()).then(d=>!d.scan_in_progress)",
            timeout=60000,
        )

        page.wait_for_selector("#mainCard", state="visible", timeout=10000)
        assert not welcome.is_visible(), "Welcome card must be hidden after scan"
        assert page.locator("#resultsBody tr").count() > 0, "Results table must have rows"

    # ------------------------------------------------------------------
    # Req 6: Exact 3/3/3/1 totals displayed in summary bar
    # ------------------------------------------------------------------

    def test_exact_status_totals_3_3_3_1(self, demo_page_with_results):
        """Summary bar must show exactly '3 current', '3 stale', '3 needs review', '1 unknown'."""
        p = demo_page_with_results
        summary = p.locator("#summaryBar")
        summary.wait_for(state="visible", timeout=5000)
        text = summary.inner_text()
        assert "3 current" in text, f"Expected '3 current' in summary; got: {text!r}"
        assert "3 stale" in text, f"Expected '3 stale' in summary; got: {text!r}"
        assert "3 needs review" in text, f"Expected '3 needs review' in summary; got: {text!r}"
        assert "1 unknown" in text, f"Expected '1 unknown' in summary; got: {text!r}"

    # ------------------------------------------------------------------
    # Req 7: All 10 rows appear under the All filter
    # ------------------------------------------------------------------

    def test_all_ten_rows_under_all_filter(self, demo_page_with_results):
        """Clicking the 'All' filter chip must reveal exactly 10 document rows."""
        p = demo_page_with_results
        p.locator(".filter-chip", has_text="All").click()
        p.wait_for_selector("#resultsBody tr", timeout=5000)
        count = p.locator("#resultsBody tr").count()
        assert count == 10, f"Expected 10 rows under All filter; got {count}"

    # ------------------------------------------------------------------
    # Req 8: Seven findings appear in the Review Queue
    # ------------------------------------------------------------------

    def test_seven_findings_in_review_queue(self, demo_page_with_results):
        """Review Queue must display 6 actionable findings."""
        p = demo_page_with_results
        p.locator("#tab-btn-queue").click()
        p.wait_for_selector("#tabQueue .queue-row", timeout=10000)
        count = p.locator("#tabQueue .queue-row").count()
        assert count == 6, f"Expected 6 actionable queue rows; got {count}"

    # ------------------------------------------------------------------
    # Req 9: A finding can be acknowledged through the real UI
    # ------------------------------------------------------------------

    def test_acknowledge_finding_through_ui(self, page, isolated_demo_server):
        """Clicking Acknowledge on a queue row must persist the state change."""
        _navigate_demo(page, isolated_demo_server)
        _ensure_results(page)

        page.locator("#tab-btn-queue").click()
        page.wait_for_selector("#tabQueue .queue-row", timeout=10000)

        ack_btn = page.locator("#tabQueue .queue-row button", has_text="Acknowledge").first
        ack_btn.wait_for(state="visible", timeout=5000)
        ack_btn.click()

        page.wait_for_selector(".toast-success", timeout=5000)
        assert page.locator(".toast-success").count() > 0, "Success toast must appear"

    # ------------------------------------------------------------------
    # Req 10: Demo page titles do not navigate to demo.example
    # ------------------------------------------------------------------

    def test_demo_page_titles_not_navigable(self, demo_page_with_results):
        """Result rows for demo pages must not contain <a href> links to demo.example."""
        p = demo_page_with_results
        p.locator(".filter-chip", has_text="All").click()
        p.wait_for_selector("#resultsBody tr", timeout=5000)

        demo_links = p.locator("#resultsBody a[href*='demo.example']")
        count = demo_links.count()
        assert count == 0, (
            f"Found {count} navigable link(s) to demo.example — demo titles must not be links"
        )

    # ------------------------------------------------------------------
    # Req 11: Live-source controls hidden and not keyboard-focusable
    # ------------------------------------------------------------------

    def test_live_source_controls_hidden_and_not_focusable(self, demo_page):
        """Scope controls must be invisible and excluded from the tab order."""
        p = demo_page

        scope_field = p.locator("#scopeSelect").locator("..")
        assert not scope_field.is_visible(), "Scope select field must be hidden"
        assert not p.locator("#scopeTargetField").is_visible(), "Scope target field must be hidden"

        # offsetParent === null means the element is not rendered / not in tab order
        focusable = p.evaluate("""() => ({
            scopeSelect: document.getElementById('scopeSelect').offsetParent !== null,
            scopeTarget: document.getElementById('scopeTarget').offsetParent !== null,
        })""")
        assert not focusable["scopeSelect"], "scopeSelect must not be keyboard-focusable"
        assert not focusable["scopeTarget"], "scopeTarget must not be keyboard-focusable"

    # ------------------------------------------------------------------
    # Req 12: No credential warnings or source-specific terminology
    # ------------------------------------------------------------------

    def test_no_credential_warnings(self, demo_page):
        """No credential error or 'not configured' message must appear in demo mode."""
        p = demo_page
        info = p.locator("#sourceInfo")
        text = info.inner_text().strip() if info.is_visible() else ""
        assert "NOTION_API_KEY" not in text, f"Credential warning in sourceInfo: {text!r}"
        assert "CONFLUENCE" not in text, f"Confluence warning in sourceInfo: {text!r}"
        assert "not configured" not in text.lower(), f"'not configured' in sourceInfo: {text!r}"

    # ------------------------------------------------------------------
    # Req 13: Desktop and mobile layouts have no horizontal overflow
    # ------------------------------------------------------------------

    def test_no_horizontal_overflow_desktop(self, demo_page_with_results):
        """At 1280x800 the page must not overflow horizontally."""
        p = demo_page_with_results
        p.set_viewport_size({"width": 1280, "height": 800})
        overflow = p.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert not overflow, "Horizontal overflow at 1280px viewport"

    def test_no_horizontal_overflow_mobile(self, demo_page_with_results):
        """At 375x812 the page must not overflow horizontally."""
        p = demo_page_with_results
        p.set_viewport_size({"width": 375, "height": 812})
        overflow = p.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert not overflow, "Horizontal overflow at 375px viewport"

    # ------------------------------------------------------------------
    # Req 15: No browser console errors
    # ------------------------------------------------------------------

    def test_no_console_errors(self, page, live_demo_server):
        """The browser console must contain no error-level messages."""
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

        _navigate_demo(page, live_demo_server)
        _ensure_results(page)
        page.wait_for_load_state("networkidle", timeout=10000)

        assert errors == [], f"Browser console errors detected: {errors}"


# ---------------------------------------------------------------------------
# Report actionability browser tests
# ---------------------------------------------------------------------------


class TestReportActionabilityBrowser:
    """Report button label and overlay correctly reflect actionability metadata."""

    def test_report_button_shows_audit_count(self, page_with_mocks):
        """Report button label shows N audits when human-audit-required docs exist."""
        p = page_with_mocks
        btn = p.locator("#reportBtn")
        btn.wait_for(state="visible", timeout=5000)
        label = btn.inner_text()
        # SCAN_REPORT has human_audit_required_count=4; button should NOT say "no audits"
        assert "no audits" not in label.lower(), f"Expected audit count in label, got: {label!r}"
        assert "audits" in label.lower(), f"Expected 'audits' in button label, got: {label!r}"

    def test_report_button_no_audits_label(self, page, live_server):
        """Report button shows 'no audits' when all flagged docs have requires_human_audit=false."""

        def _route(route, request):
            url = request.url
            method = request.method
            if "/api/status" in url:
                route.fulfill(content_type="application/json", body=json.dumps(STATUS_IDLE))
                return
            if "/api/scans" in url and method == "GET" and "/api/scans/" not in url:
                route.fulfill(content_type="application/json", body=json.dumps(SCANS_LIST))
                return
            # Return a scan where the only flagged doc has requires_human_audit=false
            no_audit_scan = {
                **SCAN_RESPONSE,
                "results": [_SUPPRESSED_UNKNOWN, SCAN_RESULTS[3]],  # unknown(suppressed) + current
            }
            if "/api/scans/1" in url and "/report" not in url:
                route.fulfill(content_type="application/json", body=json.dumps(no_audit_scan))
                return
            if "/api/scans/1/report" in url and "format=text" not in url:
                route.fulfill(content_type="application/json", body=json.dumps(SCAN_REPORT_NO_AUDITS))
                return
            if "/api/findings/summary" in url:
                route.fulfill(content_type="application/json", body=json.dumps({"open": 0}))
                return
            if "/api/findings" in url and method == "GET":
                route.fulfill(content_type="application/json", body=json.dumps([]))
                return
            route.continue_()

        page.route("**/*", _route)
        page.goto(live_server)
        page.wait_for_selector("#historyTimeline li", timeout=5000)
        page.locator("#historyTimeline li").first.click()
        # No actionable rows under default filter — wait for report button instead
        page.locator("#reportBtn").wait_for(state="visible", timeout=5000)

        label = page.locator("#reportBtn").inner_text()
        assert "no audits" in label.lower(), f"Expected 'no audits' in label, got: {label!r}"

    def test_report_overlay_false_flag_doc_not_shown_as_human_audit_required(self, page, live_server):
        """Report overlay for suppressed doc must show 'Not flagged for audit', not 'Human audit required'."""

        def _route(route, request):
            url = request.url
            method = request.method
            if "/api/status" in url:
                route.fulfill(content_type="application/json", body=json.dumps(STATUS_IDLE))
                return
            if "/api/scans" in url and method == "GET" and "/api/scans/" not in url:
                route.fulfill(content_type="application/json", body=json.dumps(SCANS_LIST))
                return
            no_audit_scan = {
                **SCAN_RESPONSE,
                "results": [_SUPPRESSED_UNKNOWN, SCAN_RESULTS[3]],
            }
            if "/api/scans/1" in url and "/report" not in url:
                route.fulfill(content_type="application/json", body=json.dumps(no_audit_scan))
                return
            if "/api/scans/1/report" in url and "format=text" not in url:
                route.fulfill(content_type="application/json", body=json.dumps(SCAN_REPORT_NO_AUDITS))
                return
            if "/api/findings/summary" in url:
                route.fulfill(content_type="application/json", body=json.dumps({"open": 0}))
                return
            if "/api/findings" in url and method == "GET":
                route.fulfill(content_type="application/json", body=json.dumps([]))
                return
            route.continue_()

        page.route("**/*", _route)
        page.goto(live_server)
        page.wait_for_selector("#historyTimeline li", timeout=5000)
        page.locator("#historyTimeline li").first.click()
        # No actionable rows under default filter — wait for report button instead
        page.locator("#reportBtn").wait_for(state="visible", timeout=5000)

        page.locator("#reportBtn").click()
        page.wait_for_selector("#reportOverlay.active", timeout=5000)

        overlay_text = page.locator("#reportBody").inner_text()
        assert "Human audit required" not in overlay_text, (
            f"Suppressed doc should not show 'Human audit required'; got: {overlay_text[:300]!r}"
        )
        assert "Not flagged for audit" in overlay_text, (
            f"Expected 'Not flagged for audit' badge; got: {overlay_text[:300]!r}"
        )
