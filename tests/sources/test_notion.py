"""Tests for Notion source with mocked API responses."""

import pytest
import httpx
import respx

from kb_audit.sources.notion import NotionSource, extract_page_id_from_url, _normalize_title


@respx.mock
def test_fetch_all_accessible():
    respx.post("https://api.notion.com/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "page-1",
                        "object": "page",
                        "url": "https://notion.so/page-1",
                        "last_edited_time": "2025-06-01T00:00:00.000Z",
                        "created_time": "2025-01-01T00:00:00.000Z",
                        "created_by": {"id": "user-1"},
                        "last_edited_by": {"id": "user-1"},
                        "parent": {"type": "workspace"},
                        "archived": False,
                        "properties": {
                            "title": {
                                "type": "title",
                                "title": [{"plain_text": "Test Page"}],
                            }
                        },
                    }
                ],
                "has_more": False,
            },
        )
    )

    respx.get("https://api.notion.com/v1/blocks/page-1/children").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"plain_text": "Hello, world!"}]
                        },
                    }
                ],
                "has_more": False,
            },
        )
    )

    source = NotionSource(api_key="test-key")
    docs = list(source.fetch_documents())

    assert len(docs) == 1
    assert docs[0].title == "Test Page"
    assert docs[0].content == "Hello, world!"
    assert docs[0].source_type == "notion"
    source.close()


def test_missing_api_key():
    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        NotionSource(api_key="")


# --- URL parsing ---


def test_extract_page_id_standard_url():
    url = "https://www.notion.so/workspace/My-Page-Title-abc123def456abc123def456abc123de"
    page_id = extract_page_id_from_url(url)
    assert page_id == "abc123de-f456-abc1-23de-f456abc123de"


def test_extract_page_id_short_url():
    url = "https://notion.so/abc123def456abc123def456abc123de"
    page_id = extract_page_id_from_url(url)
    assert page_id == "abc123de-f456-abc1-23de-f456abc123de"


def test_extract_page_id_with_query_params():
    url = "https://www.notion.so/workspace/Page-abc123def456abc123def456abc123de?v=abc"
    page_id = extract_page_id_from_url(url)
    assert page_id == "abc123de-f456-abc1-23de-f456abc123de"


def test_extract_page_id_app_notion_com():
    url = "https://app.notion.com/p/KBA-Test-Page-3409df7c15288087a301ea6a35dd84e4"
    page_id = extract_page_id_from_url(url)
    assert page_id == "3409df7c-1528-8087-a301-ea6a35dd84e4"


def test_extract_page_id_notion_com():
    url = "https://notion.com/workspace/Page-abc123def456abc123def456abc123de"
    page_id = extract_page_id_from_url(url)
    assert page_id == "abc123de-f456-abc1-23de-f456abc123de"


def test_extract_page_id_not_a_url():
    assert extract_page_id_from_url("My Page Title") is None
    assert extract_page_id_from_url("https://google.com/page") is None


# --- Title normalization ---


def test_normalize_title_strips_version():
    assert _normalize_title("KB Test Page v3") == "kb test page"
    assert _normalize_title("API Guide v2.1") == "api guide"


def test_normalize_title_strips_old_suffix():
    assert _normalize_title("API Guide (old)") == "api guide"
    assert _normalize_title("Setup Doc (copy)") == "setup doc"
    assert _normalize_title("Notes (archived)") == "notes"


def test_normalize_title_strips_version_keyword():
    assert _normalize_title("Setup Guide Version 2") == "setup guide"


def test_normalize_title_no_change():
    assert _normalize_title("Regular Title") == "regular title"


@respx.mock
def test_search_by_url():
    """When a Notion URL is provided, fetch the page, resolve its title, and find related."""
    page_id_raw = "abc123def456abc123def456abc123de"
    page_id_uuid = "abc123de-f456-abc1-23de-f456abc123de"

    # Mock: GET /pages/{id} — the seed page
    respx.get(f"https://api.notion.com/v1/pages/{page_id_uuid}").mock(
        return_value=httpx.Response(200, json={
            "id": page_id_uuid,
            "object": "page",
            "url": f"https://notion.so/{page_id_raw}",
            "last_edited_time": "2025-06-01T00:00:00.000Z",
            "created_time": "2025-01-01T00:00:00.000Z",
            "created_by": {"id": "user-1"},
            "last_edited_by": {"id": "user-1"},
            "parent": {"type": "workspace"},
            "archived": False,
            "properties": {
                "title": {"type": "title", "title": [{"plain_text": "KB Test Page v3"}]}
            },
        })
    )

    # Mock: POST /search — returns seed + related pages
    respx.post("https://api.notion.com/v1/search").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "id": page_id_uuid,
                    "object": "page",
                    "url": f"https://notion.so/{page_id_raw}",
                    "last_edited_time": "2025-06-01T00:00:00.000Z",
                    "created_time": "2025-01-01T00:00:00.000Z",
                    "created_by": {"id": "user-1"},
                    "last_edited_by": {"id": "user-1"},
                    "parent": {"type": "workspace"},
                    "archived": False,
                    "properties": {
                        "title": {"type": "title", "title": [{"plain_text": "KB Test Page v3"}]}
                    },
                },
                {
                    "id": "page-v2",
                    "object": "page",
                    "url": "https://notion.so/page-v2",
                    "last_edited_time": "2025-03-01T00:00:00.000Z",
                    "created_time": "2025-01-01T00:00:00.000Z",
                    "created_by": {"id": "user-1"},
                    "last_edited_by": {"id": "user-1"},
                    "parent": {"type": "workspace"},
                    "archived": False,
                    "properties": {
                        "title": {"type": "title", "title": [{"plain_text": "KB Test Page v2"}]}
                    },
                },
                {
                    "id": "page-unrelated",
                    "object": "page",
                    "url": "https://notion.so/page-unrelated",
                    "last_edited_time": "2025-05-01T00:00:00.000Z",
                    "created_time": "2025-01-01T00:00:00.000Z",
                    "created_by": {"id": "user-1"},
                    "last_edited_by": {"id": "user-1"},
                    "parent": {"type": "workspace"},
                    "archived": False,
                    "properties": {
                        "title": {"type": "title", "title": [{"plain_text": "Unrelated Page"}]}
                    },
                },
            ],
            "has_more": False,
        })
    )

    # Mock: content fetches — pages have actual content
    respx.get(url__regex=r".*/blocks/.*/children").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Page content."}]}},
            ],
            "has_more": False,
        })
    )

    url = f"https://www.notion.so/workspace/KB-Test-Page-v3-{page_id_raw}"
    source = NotionSource(api_key="test-key", query=url)
    docs = list(source.fetch_documents())
    source.close()

    titles = {d.title for d in docs}
    assert "KB Test Page v3" in titles
    assert "KB Test Page v2" in titles
    assert "Unrelated Page" not in titles
    assert len(docs) == 2


def _mock_page(page_id: str, title: str) -> dict:
    """Helper to build a minimal Notion page object."""
    return {
        "id": page_id,
        "object": "page",
        "url": f"https://notion.so/{page_id}",
        "last_edited_time": "2025-06-01T00:00:00.000Z",
        "created_time": "2025-01-01T00:00:00.000Z",
        "created_by": {"id": "user-1"},
        "last_edited_by": {"id": "user-1"},
        "parent": {"type": "workspace"},
        "archived": False,
        "properties": {
            "title": {"type": "title", "title": [{"plain_text": title}]}
        },
    }


@respx.mock
def test_node_page_auto_traverses_children():
    """A query matching a content-less page auto-traverses its children."""
    # Pages have parent fields indicating the tree structure
    parent_page = _mock_page("parent-1", "Documentation")
    child_a = {**_mock_page("child-a", "API Guide v2"), "parent": {"type": "page_id", "page_id": "parent-1"}}
    child_b = {**_mock_page("child-b", "Setup Notes"), "parent": {"type": "page_id", "page_id": "parent-1"}}
    related_page = _mock_page("external-1", "API Guide v1")

    search_route = respx.post("https://api.notion.com/v1/search")
    call_count = 0

    def search_side_effect(request):
        nonlocal call_count
        call_count += 1
        import json as _json
        parsed = _json.loads(request.content)
        query = parsed.get("query", "")

        if call_count == 1:
            # First call: _search_by_title looking for "Documentation"
            return httpx.Response(200, json={
                "results": [parent_page],
                "has_more": False,
            })
        elif call_count == 2:
            # Second call: _fetch_all_page_objects — returns all workspace pages
            return httpx.Response(200, json={
                "results": [parent_page, child_a, child_b, related_page],
                "has_more": False,
            })
        elif "api guide" in query.lower():
            # Related page search for "api guide"
            return httpx.Response(200, json={
                "results": [child_a, related_page],
                "has_more": False,
            })
        else:
            return httpx.Response(200, json={"results": [], "has_more": False})

    search_route.mock(side_effect=search_side_effect)

    def blocks_side_effect(request):
        """Parent page has no content; child pages have content."""
        url = str(request.url)
        if "parent-1" in url:
            return httpx.Response(200, json={"results": [], "has_more": False})
        # Children have real content
        return httpx.Response(200, json={
            "results": [
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Some content here."}]}},
            ],
            "has_more": False,
        })

    respx.get(url__regex=r".*/blocks/.*/children").mock(side_effect=blocks_side_effect)

    source = NotionSource(api_key="test-key", query="Documentation")
    docs = list(source.fetch_documents())
    source.close()

    titles = {d.title for d in docs}
    ids = {d.id for d in docs}

    # Node page "Documentation" excluded (no content), children + related included
    assert "Documentation" not in titles
    assert "API Guide v2" in titles
    assert "Setup Notes" in titles
    assert "API Guide v1" in titles
    assert "external-1" in ids
    assert len(docs) == 3


@respx.mock
def test_parent_mode_finds_pages_in_child_database():
    """Database entries under a parent are discovered via child_refs during content fetch."""
    parent_page = _mock_page("eng-1", "Engineering")
    db_entry_1 = _mock_page("entry-1", "Payment Flow")
    db_entry_2 = _mock_page("entry-2", "Refund Flow")

    # GET /pages/eng-1 — used by _fetch_page_tree path (root_page_id)
    respx.get("https://api.notion.com/v1/pages/eng-1").mock(
        return_value=httpx.Response(200, json=parent_page)
    )

    # Engineering's blocks include a child_database (discovered during content fetch)
    respx.get("https://api.notion.com/v1/blocks/eng-1/children").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"type": "child_database", "id": "db-1", "child_database": {"title": "Docs DB"}},
            ],
            "has_more": False,
        })
    )

    # Query the database — returns two page entries
    respx.post("https://api.notion.com/v1/databases/db-1/query").mock(
        return_value=httpx.Response(200, json={
            "results": [db_entry_1, db_entry_2],
            "has_more": False,
        })
    )

    # Block content for database entries — they have actual content
    respx.get(url__regex=r".*/blocks/entry-.*/children").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Entry content."}]}},
            ],
            "has_more": False,
        })
    )

    source = NotionSource(api_key="test-key", root_page_id="eng-1")
    docs = list(source.fetch_documents())
    source.close()

    titles = {d.title for d in docs}
    # Engineering has no text content (only a child_database block), so it's excluded
    assert "Engineering" not in titles
    assert "Payment Flow" in titles
    assert "Refund Flow" in titles
    assert len(docs) == 2


@respx.mock
def test_node_page_deep_tree_via_search():
    """Content-less node auto-discovers a 3-level tree: Engineering → Payments → 3 leaf pages."""
    engineering = _mock_page("eng-1", "Engineering")
    payments = {**_mock_page("pay-1", "Payments"), "parent": {"type": "page_id", "page_id": "eng-1"}}
    leaf_1 = {**_mock_page("leaf-1", "Payment Flow"), "parent": {"type": "page_id", "page_id": "pay-1"}}
    leaf_2 = {**_mock_page("leaf-2", "Refund Process"), "parent": {"type": "page_id", "page_id": "pay-1"}}
    leaf_3 = {**_mock_page("leaf-3", "Settlement Guide"), "parent": {"type": "page_id", "page_id": "pay-1"}}

    search_route = respx.post("https://api.notion.com/v1/search")
    call_count = 0

    def search_side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # _search_by_title: find Engineering (exact match, no content → auto-traverse)
            return httpx.Response(200, json={"results": [engineering], "has_more": False})
        elif call_count == 2:
            # _fetch_all_page_objects: all workspace pages
            return httpx.Response(200, json={
                "results": [engineering, payments, leaf_1, leaf_2, leaf_3],
                "has_more": False,
            })
        else:
            # Related page searches — no matches
            return httpx.Response(200, json={"results": [], "has_more": False})

    search_route.mock(side_effect=search_side_effect)

    # Block content — Engineering and Payments have no content, leaves have content
    respx.get(url__regex=r".*/blocks/(eng-1|pay-1)/children").mock(
        return_value=httpx.Response(200, json={"results": [], "has_more": False})
    )
    respx.get(url__regex=r".*/blocks/leaf-.*/children").mock(
        return_value=httpx.Response(200, json={
            "results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Page content here."}]}}],
            "has_more": False,
        })
    )

    source = NotionSource(api_key="test-key", query="Engineering")
    docs = list(source.fetch_documents())
    source.close()

    titles = {d.title for d in docs}
    # Node pages with no content are excluded
    assert "Engineering" not in titles
    assert "Payments" not in titles
    # Leaf pages with content are included
    assert "Payment Flow" in titles
    assert "Refund Process" in titles
    assert "Settlement Guide" in titles
    assert len(docs) == 3

    # Verify leaf pages have content
    leaves = [d for d in docs if d.id.startswith("leaf-")]
    assert all(d.content == "Page content here." for d in leaves)


@respx.mock
def test_parent_mode_finds_pages_inside_toggle():
    """Child pages nested inside a toggle block are discovered via blocks API (root_page_id path)."""
    parent_page = _mock_page("parent-t", "Team Docs")
    nested_page = _mock_page("nested-1", "Onboarding Guide")

    respx.get("https://api.notion.com/v1/pages/parent-t").mock(
        return_value=httpx.Response(200, json=parent_page)
    )

    # Parent has a toggle block with has_children=True, no direct child_page blocks
    respx.get("https://api.notion.com/v1/blocks/parent-t/children").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {
                    "type": "toggle",
                    "id": "toggle-1",
                    "has_children": True,
                    "toggle": {"rich_text": [{"plain_text": "Resources"}]},
                },
            ],
            "has_more": False,
        })
    )

    # Toggle contains a child_page
    respx.get("https://api.notion.com/v1/blocks/toggle-1/children").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"type": "child_page", "id": "nested-1", "child_page": {"title": "Onboarding Guide"}},
            ],
            "has_more": False,
        })
    )

    respx.get("https://api.notion.com/v1/pages/nested-1").mock(
        return_value=httpx.Response(200, json=nested_page)
    )

    respx.get("https://api.notion.com/v1/blocks/nested-1/children").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Onboarding steps."}]}},
            ],
            "has_more": False,
        })
    )

    source = NotionSource(api_key="test-key", root_page_id="parent-t")
    docs = list(source.fetch_documents())
    source.close()

    titles = {d.title for d in docs}
    assert "Team Docs" in titles
    assert "Onboarding Guide" in titles
    assert len(docs) == 2
