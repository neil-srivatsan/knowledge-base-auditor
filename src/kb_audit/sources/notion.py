"""Notion document source — fetches pages via the Notion API."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime

import httpx

from kb_audit.models import Document
from kb_audit.sources.base import DocumentSource

logger = logging.getLogger(__name__)

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Matches Notion page URLs and extracts the 32-hex-char page ID
# Supported formats:
#   https://www.notion.so/workspace/Page-Title-abc123def456...
#   https://notion.so/abc123def456...
#   https://app.notion.com/p/Page-Title-abc123def456...
#   https://notion.com/workspace/Page-Title-abc123def456...
_NOTION_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:notion\.so|(?:app\.)?notion\.com)/(?:\S+/)?(?:\S+-)?([0-9a-f]{32})(?:\?.*)?$",
    re.IGNORECASE,
)


def extract_page_id_from_url(url: str) -> str | None:
    """Extract a Notion page ID from a Notion URL, or return None."""
    m = _NOTION_URL_RE.match(url.strip())
    if m:
        raw = m.group(1)
        # Format as UUID: 8-4-4-4-12
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return None


def _normalize_title(title: str) -> str:
    """Strip version markers and whitespace to get the base document name.

    'KB Test Page v3' → 'kb test page'
    'API Guide (old)' → 'api guide'
    """
    t = title.lower().strip()
    # Remove trailing version markers: v1, v2.3, (old), (copy), (archived)
    t = re.sub(r"\s*\bv\d+(?:\.\d+)*\s*$", "", t)
    t = re.sub(r"\s*\((?:old|copy|archived|draft|backup)\)\s*$", "", t, flags=re.IGNORECASE)
    # Remove trailing "version N"
    t = re.sub(r"\s*\bversion\s+\d+(?:\.\d+)*\s*$", "", t)
    return t.strip()


def find_notion_page_by_title(api_key: str, title: str) -> str:
    """Search Notion for a page with an exact title and return its UUID.

    Used to resolve plain-text page-tree targets before starting a scan.

    Raises ``ValueError`` with a user-friendly message when:
    - no page matches (user can try a different spelling or use a URL/ID), or
    - more than one page matches (title is ambiguous; user should use URL/ID).

    The ``api_key`` is never included in log messages or exception text.
    """
    client = httpx.Client(
        base_url=NOTION_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    title_lower = title.lower()
    matches: list[str] = []
    has_more = True
    start_cursor: str | None = None

    try:
        while has_more:
            body: dict = {
                "query": title,
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor
            resp = client.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                # Extract plain-text title from the page properties
                props = page.get("properties", {})
                page_title = "Untitled"
                for prop in props.values():
                    if prop.get("type") == "title":
                        page_title = "".join(
                            t.get("plain_text", "") for t in prop.get("title", [])
                        )
                        break
                else:
                    child = page.get("child_page")
                    if isinstance(child, dict) and isinstance(child.get("title"), str):
                        page_title = child["title"]

                if page_title.lower() == title_lower:
                    matches.append(page["id"])

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
    finally:
        client.close()

    if not matches:
        raise ValueError(
            f"No Notion page found with title \u2018{title}\u2019. "
            "Check the spelling or use a Notion page URL or page ID instead."
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple Notion pages match \u2018{title}\u2019 \u2014 the title is ambiguous. "
            "Use the Notion page URL or page ID to identify the exact page."
        )
    return matches[0]


class NotionSource(DocumentSource):
    """Fetch documents from a Notion workspace."""

    def __init__(
        self,
        api_key: str,
        root_page_id: str | None = None,
        database_id: str | None = None,
        query: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("NOTION_API_KEY is required")
        self._api_key = api_key
        self._root_page_id = root_page_id
        self._database_id = database_id
        self._query = query
        self._client = httpx.Client(
            base_url=NOTION_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    @classmethod
    def source_type(cls) -> str:
        return "notion"

    def fetch_documents(self) -> Iterator[Document]:
        if self._query:
            # Check if the query is a Notion URL
            page_id = extract_page_id_from_url(self._query)
            if page_id:
                yield from self._search_by_url(page_id)
            else:
                yield from self._search_by_title(self._query)
        elif self._database_id:
            yield from self._fetch_from_database(self._database_id)
        elif self._root_page_id:
            yield from self._fetch_page_tree(self._root_page_id)
        else:
            yield from self._fetch_all_accessible()

    def _search_by_url(self, page_id: str) -> Iterator[Document]:
        """Fetch the page at the given ID, then find all related pages by title."""
        page = self._get_page(page_id)
        if not page:
            logger.warning("Could not fetch page %s from URL", page_id)
            return

        seed_title = self._extract_title(page)
        if not seed_title or seed_title == "Untitled":
            logger.warning("Page %s has no title — fetching only this page", page_id)
            doc, _ = self._page_to_document(page)
            if doc:
                yield doc
            return

        # Check if this is a content-less node page — if so, traverse children
        content, _, _ = self._fetch_page_content(page_id)
        if not content.strip():
            logger.info(
                "URL page '%s' has no content — treating as node, fetching children",
                seed_title,
            )
            yield from self._search_by_parent_id(page_id)
            return

        logger.info("URL resolved to '%s' — searching for related pages", seed_title)
        base_title = _normalize_title(seed_title)

        # Search Notion using the base title as query, then filter by normalized match
        seen_ids: set[str] = set()
        has_more = True
        start_cursor: str | None = None

        while has_more:
            body: dict = {
                "query": base_title,
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = self._client.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()

            for result_page in data.get("results", []):
                rid = result_page["id"]
                if rid in seen_ids:
                    continue
                result_title = self._extract_title(result_page)
                if _normalize_title(result_title) == base_title:
                    seen_ids.add(rid)
                    doc, _ = self._page_to_document(result_page)
                    if doc:
                        yield doc

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        # Ensure the seed page itself is included even if search didn't return it
        if page_id not in seen_ids and page["id"] not in seen_ids:
            doc, _ = self._page_to_document(page)
            if doc:
                yield doc

    def _search_by_parent_name(self, query: str) -> Iterator[Document]:
        """Find a parent page by name, fetch its child tree, then expand related pages."""
        # Search for the parent page
        parent_page: dict | None = None
        has_more = True
        start_cursor: str | None = None
        query_lower = query.lower()

        while has_more:
            body: dict = {
                "query": query,
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = self._client.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                title = self._extract_title(page)
                if title.lower() == query_lower:
                    parent_page = page
                    break

            if parent_page:
                break
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        if not parent_page:
            logger.warning("No parent page found matching '%s'", query)
            return

        logger.info("Found parent page '%s' — fetching child tree", self._extract_title(parent_page))
        yield from self._search_by_parent_id(parent_page["id"])

    def _search_by_parent_id(self, parent_id: str) -> Iterator[Document]:
        """Fetch the full child tree under a parent page, then expand related pages.

        Uses the search API to discover the tree structure via each page's
        ``parent`` field, avoiding rate-limit issues with the blocks API.
        Content is fetched separately per page.
        """
        # Step 1: Fetch all accessible pages (lightweight — no content yet)
        all_pages = self._fetch_all_page_objects()

        # Step 2: Build parent → children mapping from the parent field
        pages_by_id: dict[str, dict] = {p["id"]: p for p in all_pages}
        children_of: dict[str, list[dict]] = defaultdict(list)
        for page in all_pages:
            parent_info = page.get("parent", {})
            parent_type = parent_info.get("type")
            if parent_type == "page_id":
                children_of[parent_info["page_id"]].append(page)
            elif parent_type == "database_id":
                # Database entries — link to the database's parent page
                children_of[parent_info["database_id"]].append(page)

        # Step 3: Walk the tree from parent_id to collect all descendants
        subtree_pages: list[dict] = []
        if parent_id in pages_by_id:
            subtree_pages.append(pages_by_id[parent_id])

        def _collect_descendants(pid: str) -> None:
            for child in children_of.get(pid, []):
                subtree_pages.append(child)
                _collect_descendants(child["id"])

        _collect_descendants(parent_id)

        logger.info(
            "Found %d pages under parent tree (via search API)",
            len(subtree_pages),
        )

        # Step 4: Convert to Documents (fetches content per page).
        # Also collect child_database refs discovered during content fetching
        # so we can include database entries that the search tree walk misses.
        tree_docs: list[Document] = []
        seen_ids: set[str] = set()
        db_ids_to_fetch: list[str] = []
        for page in subtree_pages:
            doc, child_refs = self._page_to_document(page)
            if doc:
                tree_docs.append(doc)
                seen_ids.add(doc.id)
            for ref_id, ref_type in child_refs:
                if ref_type == "child_database":
                    db_ids_to_fetch.append(ref_id)

        # Fetch entries from any inline databases found in the subtree
        for db_id in db_ids_to_fetch:
            for doc in self._fetch_from_database(db_id):
                if doc.id not in seen_ids:
                    tree_docs.append(doc)
                    seen_ids.add(doc.id)

        # Step 5: Find related pages outside the tree by normalized title
        base_titles: set[str] = set()
        for doc in tree_docs:
            base_titles.add(_normalize_title(doc.title))

        related_docs: list[Document] = []
        for base_title in base_titles:
            for doc in self._find_related_by_title(base_title, seen_ids):
                seen_ids.add(doc.id)
                related_docs.append(doc)

        if related_docs:
            logger.info(
                "Found %d related page(s) outside the parent tree",
                len(related_docs),
            )

        yield from tree_docs
        yield from related_docs

    def _fetch_all_page_objects(self) -> list[dict]:
        """Fetch all page objects the integration can access (no content)."""
        all_pages: list[dict] = []
        has_more = True
        start_cursor: str | None = None

        while has_more:
            body: dict = {
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = self._client.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()

            all_pages.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        logger.info("Fetched %d page objects from workspace", len(all_pages))
        return all_pages

    def _find_related_by_title(
        self, base_title: str, exclude_ids: set[str],
    ) -> Iterator[Document]:
        """Search the workspace for pages whose normalized title matches base_title."""
        has_more = True
        start_cursor: str | None = None

        while has_more:
            body: dict = {
                "query": base_title,
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = self._client.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                rid = page["id"]
                if rid in exclude_ids:
                    continue
                title = self._extract_title(page)
                if _normalize_title(title) == base_title:
                    doc, _ = self._page_to_document(page)
                    if doc:
                        yield doc

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

    def _search_by_title(self, query: str) -> Iterator[Document]:
        """Search for pages matching a title query.

        If an exact title match is found but the page has no content (a node
        page), automatically traverse its children instead.
        """
        has_more = True
        start_cursor: str | None = None
        query_lower = query.lower()

        # First pass: look for an exact title match that might be a node page
        exact_match: dict | None = None
        all_matches: list[dict] = []

        while has_more:
            body: dict = {
                "query": query,
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = self._client.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                title = self._extract_title(page)
                if query_lower in title.lower():
                    all_matches.append(page)
                    if title.lower() == query_lower and exact_match is None:
                        exact_match = page

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        # If we found an exact match, check if it's a content-less node page
        if exact_match is not None:
            content, _, _ = self._fetch_page_content(exact_match["id"])
            if not content.strip():
                logger.info(
                    "Page '%s' has no content — treating as node, fetching children",
                    self._extract_title(exact_match),
                )
                yield from self._search_by_parent_id(exact_match["id"])
                return

        # Normal case: yield all matching pages that have content
        for page in all_matches:
            doc, _ = self._page_to_document(page)
            if doc:
                yield doc

    def _fetch_all_accessible(self) -> Iterator[Document]:
        """Search for all pages the integration can access."""
        has_more = True
        start_cursor: str | None = None

        while has_more:
            body: dict = {"filter": {"property": "object", "value": "page"}, "page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = self._client.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                doc, _ = self._page_to_document(page)
                if doc:
                    yield doc

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

    def _fetch_page_tree(self, page_id: str) -> Iterator[Document]:
        """Fetch a page and all its child pages/databases recursively.

        Content fetching and child discovery happen in a single API
        traversal — no duplicate calls to /blocks/{id}/children.
        """
        page = self._get_page(page_id)
        if not page:
            return

        doc, child_refs = self._page_to_document(page)
        if doc:
            yield doc

        for child_id, child_type in child_refs:
            if child_type == "child_page":
                yield from self._fetch_page_tree(child_id)
            elif child_type == "child_database":
                yield from self._fetch_from_database(child_id)

    def _fetch_from_database(self, database_id: str) -> Iterator[Document]:
        """Query a Notion database and yield its pages."""
        has_more = True
        start_cursor: str | None = None

        while has_more:
            body: dict = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = self._client.post(f"/databases/{database_id}/query", json=body)
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                doc, _ = self._page_to_document(page)
                if doc:
                    yield doc

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

    def _request_with_retry(
        self, method: str, url: str, max_retries: int = 3, **kwargs: object,
    ) -> dict:
        """Make an API request with retry on 429 rate-limit responses."""
        for attempt in range(max_retries):
            resp = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                logger.info(
                    "Rate limited on %s %s — retrying in %.1fs (attempt %d/%d)",
                    method, url, retry_after, attempt + 1, max_retries,
                )
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        # Final attempt — let the error propagate
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def _get_page(self, page_id: str) -> dict[str, object] | None:
        try:
            return self._request_with_retry("GET", f"/pages/{page_id}")
        except httpx.HTTPStatusError as e:
            logger.warning("Failed to fetch page %s: %s", page_id, e)
            return None

    def _page_to_document(
        self, page: dict,
    ) -> tuple[Document | None, list[tuple[str, str]]]:
        """Convert a Notion page to a Document.

        Returns (document, child_refs) where child_refs is a list of
        (block_id, block_type) for child_page/child_database blocks found
        during content traversal.
        """
        page_id = page["id"]
        title = self._extract_title(page)
        content, links, child_refs = self._fetch_page_content(page_id)
        url = page.get("url")
        last_edited = page.get("last_edited_time")

        last_modified = None
        if last_edited:
            last_modified = datetime.fromisoformat(last_edited.replace("Z", "+00:00"))

        # Skip pages with no body content (title-only node pages)
        if not content.strip():
            return None, child_refs

        doc = Document(
            id=page_id,
            title=title,
            content=content,
            source_type="notion",
            url=url,
            last_modified=last_modified,
            metadata={
                "created_time": page.get("created_time"),
                "created_by": page.get("created_by", {}).get("id"),
                "last_edited_by": page.get("last_edited_by", {}).get("id"),
                "parent": page.get("parent", {}),
                "archived": page.get("archived", False),
                "links": links,
            },
        )
        return doc, child_refs

    def _extract_title(self, page: dict) -> str:
        props = page.get("properties", {})
        # Try "title" type property (standard for pages)
        for prop in props.values():
            if prop.get("type") == "title":
                title_parts = prop.get("title", [])
                return "".join(t.get("plain_text", "") for t in title_parts)
        # Fallback: child_page title stored in the page object
        child = page.get("child_page")
        if isinstance(child, dict):
            title = child.get("title")
            if isinstance(title, str):
                return title
        return "Untitled"

    def _fetch_page_content(
        self, page_id: str, max_depth: int = 5,
    ) -> tuple[str, list[str], list[tuple[str, str]]]:
        """Fetch all blocks of a page and flatten to plain text.

        Recursively traverses nested blocks (toggles, callouts, columns, etc.)
        up to max_depth levels.

        Returns:
            (content_text, urls_found, child_refs) where child_refs is a list
            of (block_id, block_type) for child_page and child_database blocks
            discovered during traversal.
        """
        blocks: list[str] = []
        links: list[str] = []
        child_refs: list[tuple[str, str]] = []
        self._fetch_blocks_recursive(
            page_id, blocks, links, child_refs, depth=0, max_depth=max_depth,
        )
        return "\n".join(blocks), links, child_refs

    def _fetch_blocks_recursive(
        self, block_id: str, blocks: list[str], links: list[str],
        child_refs: list[tuple[str, str]],
        depth: int, max_depth: int,
    ) -> None:
        """Fetch children of a block and recurse into nested blocks."""
        if depth >= max_depth:
            return

        has_more = True
        start_cursor: str | None = None

        while has_more:
            params: dict = {"page_size": 100}
            if start_cursor:
                params["start_cursor"] = start_cursor

            try:
                data = self._request_with_retry(
                    "GET", f"/blocks/{block_id}/children", params=params,
                )
            except httpx.HTTPStatusError as e:
                logger.warning("Failed to fetch blocks for %s: %s", block_id, e)
                break

            for block in data.get("results", []):
                block_type = block.get("type")

                # Record child pages and databases for tree traversal
                if block_type in ("child_page", "child_database"):
                    child_refs.append((block["id"], block_type))
                    continue

                text = self._extract_block_text(block)
                if text:
                    blocks.append(text)
                self._extract_block_links(block, links)

                # Recurse into blocks with children (toggles, callouts, columns, etc.)
                if block.get("has_children"):
                    self._fetch_blocks_recursive(
                        block["id"], blocks, links, child_refs,
                        depth=depth + 1, max_depth=max_depth,
                    )

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

    def _extract_block_text(self, block: dict) -> str:
        """Extract plain text from a Notion block."""
        block_type = block.get("type", "")
        block_data = block.get(block_type, {})

        # Most block types store text in "rich_text"
        rich_text = block_data.get("rich_text", [])
        if rich_text:
            return "".join(rt.get("plain_text", "") for rt in rich_text)

        # Table rows
        if block_type == "table_row":
            cells = block_data.get("cells", [])
            row_parts = []
            for cell in cells:
                cell_text = "".join(rt.get("plain_text", "") for rt in cell)
                row_parts.append(cell_text)
            return " | ".join(row_parts)

        return ""

    def _extract_block_links(self, block: dict, links: list[str]) -> None:
        """Extract URLs from rich_text href fields and bookmark/embed blocks."""
        block_type = block.get("type", "")
        block_data = block.get(block_type, {})

        # Links in rich_text spans
        for rt in block_data.get("rich_text", []):
            href = rt.get("href") or (rt.get("text", {}).get("link") or {}).get("url")
            if href:
                links.append(href)

        # Bookmark blocks
        if block_type == "bookmark":
            url = block_data.get("url")
            if url:
                links.append(url)

        # Embed blocks
        if block_type == "embed":
            url = block_data.get("url")
            if url:
                links.append(url)

        # Link preview blocks
        if block_type == "link_preview":
            url = block_data.get("url")
            if url:
                links.append(url)

    def close(self) -> None:
        self._client.close()
