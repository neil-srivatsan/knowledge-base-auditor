"""Confluence Cloud document source — fetches pages via the Confluence REST API."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from kb_audit.models import Document, DocumentLink
from kb_audit.sources.base import DocumentSource

logger = logging.getLogger(__name__)


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and extract plain text from Confluence storage format."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table"):
            self._parts.append("\n")
        elif tag == "td":
            self._parts.append(" | ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of blank lines into single newlines
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(html: str) -> str:
    """Convert Confluence storage-format HTML to plain text."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    return extractor.get_text()


def _extract_links(html: str) -> list[str]:
    """Extract href URLs from HTML content."""
    return re.findall(r'href="([^"]+)"', html)


class _AnchorExtractor(HTMLParser):
    """Extract anchor hrefs with display text and block context from Confluence HTML.

    For each anchor, captures:
    - url: the href attribute
    - text: the anchor's visible text
    - context: the full visible text of the containing block element
      (p, li, td, th, h1-h6), giving relationship phrases outside the <a>
      tag (e.g. "This page has been replaced by New Guide") to the analyzer.
    """

    # Block-level tags that define natural context boundaries in Confluence HTML
    _BLOCK_TAGS: frozenset[str] = frozenset(
        {"p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__()
        self._structured: list[DocumentLink] = []
        # Stack of (text_parts, [(href, anchor_text)]) for nested blocks
        self._block_stack: list[tuple[list[str], list[tuple[str, str | None]]]] = []
        self._current_href: str | None = None
        self._current_anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS:
            self._block_stack.append(([], []))
        if tag == "a":
            self._current_href = dict(attrs).get("href")
            self._current_anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href:
            anchor_text = "".join(self._current_anchor_text).strip() or None
            if self._block_stack:
                self._block_stack[-1][1].append((self._current_href, anchor_text))
            else:
                # No containing block — emit with anchor text as fallback context
                self._structured.append(
                    DocumentLink(
                        url=self._current_href,
                        text=anchor_text,
                        context=anchor_text,
                        source="confluence",
                    )
                )
            self._current_href = None
            self._current_anchor_text = []
        elif tag in self._BLOCK_TAGS and self._block_stack:
            text_parts, anchors = self._block_stack.pop()
            block_context = "".join(text_parts).strip() or None
            for href, anchor_text in anchors:
                self._structured.append(
                    DocumentLink(
                        url=href,
                        text=anchor_text,
                        context=block_context or anchor_text,
                        source="confluence",
                    )
                )
            # Propagate text upward so parent blocks see nested content
            if self._block_stack:
                self._block_stack[-1][0].extend(text_parts)

    def handle_data(self, data: str) -> None:
        if self._block_stack:
            self._block_stack[-1][0].append(data)
        if self._current_href is not None:
            self._current_anchor_text.append(data)

    def get_structured(self) -> list[DocumentLink]:
        return self._structured


def _normalize_confluence_href(href: str, base_url: str) -> str:
    """Return *href* as an absolute URL relative to *base_url*.

    Handles two documented Confluence base URL shapes:
    - https://example.atlassian.net/wiki   (includes /wiki path)
    - https://example.atlassian.net        (no extra path)

    Rules:
    - Absolute hrefs (http/https) are returned unchanged.
    - Site-root relative paths starting with "/" are joined against the
      scheme+netloc *origin* only, so /wiki/... paths are never doubled
      regardless of whether base_url itself ends with /wiki.
    - Non-slash-prefixed relative paths are joined relative to base_url.
    """
    from urllib.parse import urlparse as _urlparse

    if href.startswith(("http://", "https://")):
        return href
    parsed = _urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if href.startswith("/"):
        return urljoin(origin, href)
    # Relative without leading slash — join under base_url directory
    return urljoin(base_url.rstrip("/") + "/", href)


def _extract_structured_links(html: str, base_url: str = "") -> list[DocumentLink]:
    """Extract anchors with display text and block context from Confluence HTML.

    Relative hrefs are resolved against *base_url* when provided.
    """
    extractor = _AnchorExtractor()
    extractor.feed(html)
    links = extractor.get_structured()
    if base_url:
        for link in links:
            link.url = _normalize_confluence_href(link.url, base_url)
    return links


class ConfluenceSource(DocumentSource):
    """Fetch documents from a Confluence Cloud instance.

    Authentication uses HTTP Basic with email + API token, the standard
    method for Confluence Cloud (Atlassian Cloud).
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        space_key: str | None = None,
        page_id: str | None = None,
        query: str | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("CONFLUENCE_BASE_URL is required")
        if not email:
            raise ValueError("CONFLUENCE_EMAIL is required")
        if not api_token:
            raise ValueError("CONFLUENCE_API_TOKEN is required")

        self._base_url = base_url.rstrip("/")
        self._space_key = space_key
        self._page_id = page_id
        self._query = query
        self._client = httpx.Client(
            base_url=f"{self._base_url}/rest/api",
            auth=(email, api_token),
            headers={"Accept": "application/json"},
            timeout=30.0,
        )

    @classmethod
    def source_type(cls) -> str:
        return "confluence"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_documents(self) -> Iterator[Document]:
        if self._query:
            yield from self._search_cql(self._query)
        elif self._page_id:
            yield from self._fetch_page_tree(self._page_id)
        elif self._space_key:
            yield from self._fetch_space(self._space_key)
        else:
            raise ValueError(
                "Confluence source requires at least one of: "
                "space_key, page_id, or query"
            )

    # ------------------------------------------------------------------
    # Fetching strategies
    # ------------------------------------------------------------------

    def _fetch_space(self, space_key: str) -> Iterator[Document]:
        """Fetch all pages in a Confluence space."""
        start = 0
        limit = 25
        while True:
            data = self._get_json(
                "/content",
                params={
                    "spaceKey": space_key,
                    "type": "page",
                    "status": "current",
                    "expand": "body.storage,version,history,ancestors",
                    "start": str(start),
                    "limit": str(limit),
                },
            )
            results = data.get("results", [])
            if not results:
                break

            for page in results:
                doc = self._page_to_document(page)
                if doc:
                    yield doc

            size = data.get("size", len(results))
            if size < limit:
                break
            start += size

    def _fetch_page_tree(self, page_id: str) -> Iterator[Document]:
        """Fetch a page and all its descendants recursively."""
        page = self._get_page(page_id)
        if not page:
            return
        doc = self._page_to_document(page)
        if doc:
            yield doc
        yield from self._fetch_children(page_id)

    def _fetch_children(self, page_id: str) -> Iterator[Document]:
        """Recursively fetch child pages of a given page."""
        start = 0
        limit = 25
        while True:
            data = self._get_json(
                f"/content/{page_id}/child/page",
                params={
                    "expand": "body.storage,version,history,ancestors",
                    "start": str(start),
                    "limit": str(limit),
                },
            )
            results = data.get("results", [])
            if not results:
                break

            for child in results:
                doc = self._page_to_document(child)
                if doc:
                    yield doc
                # Recurse into children
                yield from self._fetch_children(child["id"])

            size = data.get("size", len(results))
            if size < limit:
                break
            start += size

    def _search_cql(self, cql: str) -> Iterator[Document]:
        """Search Confluence using CQL and yield matching pages."""
        start = 0
        limit = 25
        while True:
            data = self._get_json(
                "/content/search",
                params={
                    "cql": cql,
                    "expand": "body.storage,version,history,ancestors",
                    "start": str(start),
                    "limit": str(limit),
                },
            )
            results = data.get("results", [])
            if not results:
                break

            for page in results:
                doc = self._page_to_document(page)
                if doc:
                    yield doc

            size = data.get("size", len(results))
            if size < limit:
                break
            start += size

    # ------------------------------------------------------------------
    # Page → Document conversion
    # ------------------------------------------------------------------

    def _page_to_document(self, page: dict) -> Document | None:
        """Convert a Confluence page JSON object to a Document."""
        page_id = str(page["id"])
        title = page.get("title", "Untitled")

        # Extract body content (storage format HTML → plain text)
        body_storage = page.get("body", {}).get("storage", {}).get("value", "")
        content = html_to_text(body_storage)
        if not content.strip():
            return None

        doc_links = _extract_structured_links(body_storage, base_url=self._base_url)
        links = [dl.url for dl in doc_links if dl.url is not None]

        # URL
        web_link = page.get("_links", {}).get("webui", "")
        url = f"{self._base_url}{web_link}" if web_link else None

        # Timestamps
        version = page.get("version", {})
        last_modified = None
        when = version.get("when")
        if when:
            last_modified = datetime.fromisoformat(when.replace("Z", "+00:00"))

        # History / creation info
        history = page.get("history", {})
        created_by = history.get("createdBy", {}).get("displayName")
        created_date = history.get("createdDate")
        last_edited_by = version.get("by", {}).get("displayName")

        # Ancestors (for breadcrumb / hierarchy context)
        ancestors = page.get("ancestors", [])
        ancestor_titles = [a.get("title", "") for a in ancestors]

        # Confluence page "status" field: "current" means the published
        # version of the content object. Do NOT map to KB Audit trust status.
        page_status = page.get("status", "")

        doc = Document(
            id=f"confluence-{page_id}",
            title=title,
            content=content,
            source_type="confluence",
            url=url,
            last_modified=last_modified,
            metadata={
                "created_date": created_date,
                "created_by": created_by,
                "last_edited_by": last_edited_by,
                "version_number": version.get("number"),
                "page_status": page_status,
                "space_key": page.get("space", {}).get("key")
                    or (ancestors[0].get("space", {}).get("key") if ancestors else None),
                "ancestors": ancestor_titles,
                "links": links,
            },
            links=doc_links,
        )
        return doc

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_json(
        self, path: str, params: dict | None = None, max_retries: int = 3,
    ) -> dict:
        """GET a JSON endpoint with retry on 429."""
        for attempt in range(max_retries):
            resp = self._client.get(path, params=params)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                logger.info(
                    "Rate limited on GET %s — retrying in %.1fs (attempt %d/%d)",
                    path, retry_after, attempt + 1, max_retries,
                )
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def _get_page(self, page_id: str) -> dict | None:
        """Fetch a single page by ID with full body expansion."""
        try:
            return self._get_json(
                f"/content/{page_id}",
                params={
                    "expand": "body.storage,version,history,ancestors",
                },
            )
        except httpx.HTTPStatusError as e:
            logger.warning("Failed to fetch Confluence page %s: %s", page_id, e)
            return None

    def close(self) -> None:
        self._client.close()
