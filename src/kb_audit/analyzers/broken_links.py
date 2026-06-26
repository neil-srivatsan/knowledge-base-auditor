"""Staleness detection based on broken links in document content."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from kb_audit.analyzers.base import Analyzer
from kb_audit.models import Document, Severity, StalenessSignal

logger = logging.getLogger(__name__)

# Match http/https URLs in plain text content
_URL_RE = re.compile(r"https?://[^\s<>\"\')]+")

# Skip internal Notion links — these aren't checkable via HTTP
_NOTION_INTERNAL_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|(?:app\.)?notion\.com)/")


def _extract_urls(doc: Document) -> list[str]:
    """Extract unique URLs from a document's metadata links and content text."""
    urls: set[str] = set()

    # Prefer structured links extracted during fetch
    metadata_links = (doc.metadata or {}).get("links", [])
    if isinstance(metadata_links, list):
        for link in metadata_links:
            if isinstance(link, str) and link.startswith("http"):
                urls.add(link)

    # Also scan plain text content for URLs not captured via rich_text
    for match in _URL_RE.finditer(doc.content):
        url = match.group(0).rstrip(".,;:!?)")
        urls.add(url)

    # Filter out internal Notion links
    return [u for u in urls if not _NOTION_INTERNAL_RE.match(u)]


def _check_url(url: str, timeout: float = 10.0) -> tuple[str, int | None, str | None]:
    """Check a single URL. Returns (url, status_code_or_None, error_or_None)."""
    try:
        resp = httpx.head(url, timeout=timeout, follow_redirects=True)
        # Some servers reject HEAD; fall back to GET
        if resp.status_code == 405:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        return (url, resp.status_code, None)
    except httpx.TimeoutException:
        return (url, None, "timeout")
    except httpx.ConnectError:
        return (url, None, "connection_failed")
    except httpx.HTTPError as e:
        return (url, None, str(e))


class BrokenLinkAnalyzer(Analyzer):
    """Detect broken links in document content as a staleness signal."""

    def __init__(self, max_workers: int = 5, timeout: float = 10.0) -> None:
        self._max_workers = max_workers
        self._timeout = timeout

    @classmethod
    def name(cls) -> str:
        return "broken_links"

    def analyze(self, documents: list[Document]) -> dict[str, list[StalenessSignal]]:
        # Collect all URLs to check, mapped back to their documents
        url_to_docs: dict[str, list[str]] = {}
        for doc in documents:
            urls = _extract_urls(doc)
            for url in urls:
                url_to_docs.setdefault(url, []).append(doc.id)

        if not url_to_docs:
            return {}

        logger.info("Checking %d unique URLs across %d documents", len(url_to_docs), len(documents))

        # Check all URLs in parallel
        url_results: dict[str, tuple[int | None, str | None]] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(_check_url, url, self._timeout): url
                for url in url_to_docs
            }
            for future in as_completed(futures):
                url, status, error = future.result()
                url_results[url] = (status, error)

        # Build signals for documents with broken links
        results: dict[str, list[StalenessSignal]] = {}
        for url, (status, error) in url_results.items():
            is_broken = False
            if status is not None and status >= 400:
                is_broken = True
            elif error == "connection_failed":
                is_broken = True

            if not is_broken:
                continue

            for doc_id in url_to_docs[url]:
                if status is not None:
                    message = f"Broken link ({status}): {url}"
                else:
                    message = f"Broken link ({error}): {url}"

                signal = StalenessSignal(
                    signal_type="broken_link",
                    severity=Severity.WARNING,
                    message=message,
                    details={
                        "url": url,
                        "status_code": status,
                        "error": error,
                    },
                )
                results.setdefault(doc_id, []).append(signal)

        if results:
            total_broken = sum(len(sigs) for sigs in results.values())
            logger.info("Found %d broken link(s) across %d document(s)", total_broken, len(results))

        return results
