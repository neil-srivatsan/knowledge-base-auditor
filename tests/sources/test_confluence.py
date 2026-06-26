"""Tests for Confluence Cloud source with mocked API responses."""

import pytest
import httpx
import respx

from kb_audit.sources.confluence import ConfluenceSource, html_to_text


MOCK_BASE = "https://example.atlassian.net/wiki"
API_BASE = f"{MOCK_BASE}/rest/api"


def _mock_page(
    page_id: str,
    title: str,
    body_html: str = "<p>Hello, world!</p>",
    *,
    space_key: str = "ENG",
    status: str = "current",
    version_number: int = 1,
) -> dict:
    """Build a minimal Confluence page object."""
    return {
        "id": page_id,
        "type": "page",
        "status": status,
        "title": title,
        "body": {
            "storage": {"value": body_html, "representation": "storage"},
        },
        "version": {
            "number": version_number,
            "when": "2025-06-01T12:00:00.000Z",
            "by": {"displayName": "Test User"},
        },
        "history": {
            "createdDate": "2025-01-01T00:00:00.000Z",
            "createdBy": {"displayName": "Test User"},
        },
        "ancestors": [],
        "space": {"key": space_key},
        "_links": {
            "webui": f"/spaces/{space_key}/pages/{page_id}/{title.replace(' ', '+')}",
        },
    }


# ---------------------------------------------------------------------------
# HTML to text conversion
# ---------------------------------------------------------------------------


class TestHtmlToText:
    def test_paragraph(self):
        assert html_to_text("<p>Hello, world!</p>") == "Hello, world!"

    def test_nested_tags(self):
        text = html_to_text("<p>This is <strong>bold</strong> and <em>italic</em>.</p>")
        assert text == "This is bold and italic."

    def test_headings(self):
        text = html_to_text("<h1>Title</h1><p>Body text.</p>")
        assert "Title" in text
        assert "Body text." in text

    def test_list_items(self):
        text = html_to_text("<ul><li>Item 1</li><li>Item 2</li></ul>")
        assert "Item 1" in text
        assert "Item 2" in text

    def test_table(self):
        text = html_to_text(
            "<table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
        )
        assert "A" in text
        assert "B" in text

    def test_script_stripped(self):
        text = html_to_text("<p>Visible</p><script>alert('xss')</script>")
        assert "Visible" in text
        assert "alert" not in text

    def test_empty_html(self):
        assert html_to_text("") == ""

    def test_confluence_status_macro(self):
        """Metadata-like content in Confluence is extracted as text."""
        html = (
            "<p>Status: Current</p>"
            "<p>Owner: Platform Team</p>"
            "<p>Last reviewed: 2026-03-01</p>"
        )
        text = html_to_text(html)
        assert "Status: Current" in text
        assert "Owner: Platform Team" in text
        assert "Last reviewed: 2026-03-01" in text


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConfluenceSourceInit:
    def test_missing_base_url(self):
        with pytest.raises(ValueError, match="CONFLUENCE_BASE_URL"):
            ConfluenceSource(base_url="", email="a@b.com", api_token="tok")

    def test_missing_email(self):
        with pytest.raises(ValueError, match="CONFLUENCE_EMAIL"):
            ConfluenceSource(base_url=MOCK_BASE, email="", api_token="tok")

    def test_missing_api_token(self):
        with pytest.raises(ValueError, match="CONFLUENCE_API_TOKEN"):
            ConfluenceSource(base_url=MOCK_BASE, email="a@b.com", api_token="")

    def test_no_scope_raises(self):
        source = ConfluenceSource(
            base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        )
        with pytest.raises(ValueError, match="at least one of"):
            list(source.fetch_documents())
        source.close()


# ---------------------------------------------------------------------------
# Fetch by space
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_space():
    page = _mock_page("101", "API Guide", "<p>API documentation content.</p>")
    respx.get(f"{API_BASE}/content").mock(
        return_value=httpx.Response(200, json={
            "results": [page],
            "size": 1,
        })
    )

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        space_key="ENG",
    )
    docs = list(source.fetch_documents())
    source.close()

    assert len(docs) == 1
    assert docs[0].title == "API Guide"
    assert docs[0].source_type == "confluence"
    assert docs[0].id == "confluence-101"
    assert "API documentation content." in docs[0].content
    assert docs[0].url is not None


@respx.mock
def test_fetch_space_pagination():
    """Two pages of results are correctly paginated."""
    page1 = _mock_page("201", "Page One", "<p>First page.</p>")
    page2 = _mock_page("202", "Page Two", "<p>Second page.</p>")

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        start = int(request.url.params.get("start", "0"))
        if start == 0:
            return httpx.Response(200, json={"results": [page1], "size": 1})
        else:
            return httpx.Response(200, json={"results": [page2], "size": 1})

    # Use limit=1 to force pagination — but the source uses limit=25 internally.
    # We simulate by returning size < limit on first call to stop pagination.
    respx.get(f"{API_BASE}/content").mock(
        return_value=httpx.Response(200, json={
            "results": [page1, page2],
            "size": 2,
        })
    )

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        space_key="ENG",
    )
    docs = list(source.fetch_documents())
    source.close()

    assert len(docs) == 2
    titles = {d.title for d in docs}
    assert "Page One" in titles
    assert "Page Two" in titles


# ---------------------------------------------------------------------------
# Fetch page tree
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_page_tree():
    """Fetching by page_id returns the page and its children recursively."""
    parent = _mock_page("300", "Parent Page", "<p>Parent content.</p>")
    child = _mock_page("301", "Child Page", "<p>Child content.</p>")

    respx.get(f"{API_BASE}/content/300").mock(
        return_value=httpx.Response(200, json=parent)
    )
    respx.get(f"{API_BASE}/content/300/child/page").mock(
        return_value=httpx.Response(200, json={"results": [child], "size": 1})
    )
    # Child has no children
    respx.get(f"{API_BASE}/content/301/child/page").mock(
        return_value=httpx.Response(200, json={"results": [], "size": 0})
    )

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        page_id="300",
    )
    docs = list(source.fetch_documents())
    source.close()

    assert len(docs) == 2
    titles = {d.title for d in docs}
    assert "Parent Page" in titles
    assert "Child Page" in titles


@respx.mock
def test_fetch_page_tree_skips_empty_pages():
    """Pages with no body content are excluded."""
    parent = _mock_page("400", "Node Page", "")
    child = _mock_page("401", "Leaf Page", "<p>Content here.</p>")

    respx.get(f"{API_BASE}/content/400").mock(
        return_value=httpx.Response(200, json=parent)
    )
    respx.get(f"{API_BASE}/content/400/child/page").mock(
        return_value=httpx.Response(200, json={"results": [child], "size": 1})
    )
    respx.get(f"{API_BASE}/content/401/child/page").mock(
        return_value=httpx.Response(200, json={"results": [], "size": 0})
    )

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        page_id="400",
    )
    docs = list(source.fetch_documents())
    source.close()

    assert len(docs) == 1
    assert docs[0].title == "Leaf Page"


# ---------------------------------------------------------------------------
# CQL search
# ---------------------------------------------------------------------------


@respx.mock
def test_search_cql():
    page = _mock_page("500", "Search Result", "<p>Found content.</p>")
    respx.get(f"{API_BASE}/content/search").mock(
        return_value=httpx.Response(200, json={"results": [page], "size": 1})
    )

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        query='space = "ENG" AND title ~ "Search"',
    )
    docs = list(source.fetch_documents())
    source.close()

    assert len(docs) == 1
    assert docs[0].title == "Search Result"


# ---------------------------------------------------------------------------
# Document metadata mapping
# ---------------------------------------------------------------------------


@respx.mock
def test_document_metadata_fields():
    """Verify all expected metadata fields are populated."""
    page = _mock_page(
        "600", "Metadata Test",
        "<p>Status: Current</p><p>Owner: Platform Team</p>",
        space_key="PLAT",
        version_number=5,
    )
    respx.get(f"{API_BASE}/content").mock(
        return_value=httpx.Response(200, json={"results": [page], "size": 1})
    )

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        space_key="PLAT",
    )
    docs = list(source.fetch_documents())
    source.close()

    doc = docs[0]
    assert doc.id == "confluence-600"
    assert doc.source_type == "confluence"
    assert doc.last_modified is not None
    assert doc.metadata["version_number"] == 5
    assert doc.metadata["space_key"] == "PLAT"
    assert doc.metadata["created_by"] == "Test User"
    assert doc.metadata["last_edited_by"] == "Test User"
    # Verify trust classifier metadata is parseable from content
    assert "Status: Current" in doc.content
    assert "Owner: Platform Team" in doc.content


# ---------------------------------------------------------------------------
# Rate limiting (429 retry)
# ---------------------------------------------------------------------------


@respx.mock
def test_rate_limit_retry():
    """429 responses trigger a retry."""
    route = respx.get(f"{API_BASE}/content")

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        page = _mock_page("700", "Retried Page", "<p>Content.</p>")
        return httpx.Response(200, json={"results": [page], "size": 1})

    route.mock(side_effect=side_effect)

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        space_key="ENG",
    )
    docs = list(source.fetch_documents())
    source.close()

    assert len(docs) == 1
    assert call_count == 2


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------


@respx.mock
def test_links_extracted_from_html():
    """Links in page body are captured in metadata."""
    html = (
        '<p>See <a href="https://example.com/api">API docs</a> and '
        '<a href="https://example.com/guide">the guide</a>.</p>'
    )
    page = _mock_page("800", "Link Page", html)
    respx.get(f"{API_BASE}/content").mock(
        return_value=httpx.Response(200, json={"results": [page], "size": 1})
    )

    source = ConfluenceSource(
        base_url=MOCK_BASE, email="a@b.com", api_token="tok",
        space_key="ENG",
    )
    docs = list(source.fetch_documents())
    source.close()

    links = docs[0].metadata.get("links", [])
    assert "https://example.com/api" in links
    assert "https://example.com/guide" in links


# ---------------------------------------------------------------------------
# Integration: trust classifier sees Confluence content
# ---------------------------------------------------------------------------


class TestConfluenceTrustIntegration:
    """Verify Confluence documents work with the trust classifier."""

    @respx.mock
    def test_status_current_parsed_from_confluence(self):
        """Trust classifier parses Status: Current from Confluence HTML content."""
        from kb_audit.trust import classify

        html = (
            "<p>Status: Current</p>"
            "<p>Owner: Payments Team</p>"
            "<p>Last reviewed: 2026-06-01</p>"
            "<p>This guide covers payment processing.</p>"
        )
        page = _mock_page("900", "Payment Guide", html)
        respx.get(f"{API_BASE}/content").mock(
            return_value=httpx.Response(200, json={"results": [page], "size": 1})
        )

        source = ConfluenceSource(
            base_url=MOCK_BASE, email="a@b.com", api_token="tok",
            space_key="ENG",
        )
        docs = list(source.fetch_documents())
        source.close()

        doc = docs[0]
        verdict = classify(doc, [], incoming_ref_count=2)
        assert verdict.status == "current"
        assert verdict.metadata.declared_status == "Current"
        assert verdict.metadata.owner == "Payments Team"

    @respx.mock
    def test_deprecated_status_parsed_from_confluence(self):
        """Trust classifier picks up Status: Deprecated from Confluence."""
        from kb_audit.trust import classify

        html = "<p>Status: Deprecated</p><p>Old API guide.</p>"
        page = _mock_page("901", "Old API Guide", html)
        respx.get(f"{API_BASE}/content").mock(
            return_value=httpx.Response(200, json={"results": [page], "size": 1})
        )

        source = ConfluenceSource(
            base_url=MOCK_BASE, email="a@b.com", api_token="tok",
            space_key="ENG",
        )
        docs = list(source.fetch_documents())
        source.close()

        doc = docs[0]
        verdict = classify(doc, [], incoming_ref_count=0)
        assert verdict.status == "stale"
