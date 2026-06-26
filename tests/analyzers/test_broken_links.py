"""Tests for broken link analyzer."""

from datetime import datetime, timezone

import httpx
import respx

from kb_audit.analyzers.broken_links import BrokenLinkAnalyzer, _extract_urls
from kb_audit.models import Document


def _make_doc(content: str = "", links: list[str] | None = None, **kw) -> Document:
    doc = Document(
        id=kw.get("id", "1"),
        title=kw.get("title", "Test"),
        content=content,
        source_type="test",
        last_modified=datetime(2025, 6, 1, tzinfo=timezone.utc),
        metadata={"links": links or []},
    )
    return doc


def test_extract_urls_from_metadata_links():
    doc = _make_doc(links=["https://example.com/page", "https://example.com/other"])
    urls = _extract_urls(doc)
    assert "https://example.com/page" in urls
    assert "https://example.com/other" in urls


def test_extract_urls_from_content_text():
    doc = _make_doc(content="Check out https://example.com/docs for more info.")
    urls = _extract_urls(doc)
    assert "https://example.com/docs" in urls


def test_extract_urls_deduplicates():
    doc = _make_doc(
        content="Visit https://example.com/page today.",
        links=["https://example.com/page"],
    )
    urls = _extract_urls(doc)
    assert urls.count("https://example.com/page") == 1


def test_extract_urls_skips_notion_internal():
    doc = _make_doc(links=[
        "https://notion.so/workspace/some-page-abc123",
        "https://app.notion.com/p/some-page-abc123",
        "https://example.com/valid",
    ])
    urls = _extract_urls(doc)
    assert len(urls) == 1
    assert "https://example.com/valid" in urls


@respx.mock
def test_broken_link_detected():
    respx.head("https://example.com/deleted").mock(
        return_value=httpx.Response(404)
    )
    doc = _make_doc(links=["https://example.com/deleted"])
    analyzer = BrokenLinkAnalyzer()
    results = analyzer.analyze([doc])
    assert "1" in results
    assert results["1"][0].signal_type == "broken_link"
    assert "404" in results["1"][0].message


@respx.mock
def test_healthy_link_no_signal():
    respx.head("https://example.com/ok").mock(
        return_value=httpx.Response(200)
    )
    doc = _make_doc(links=["https://example.com/ok"])
    analyzer = BrokenLinkAnalyzer()
    results = analyzer.analyze([doc])
    assert not results


@respx.mock
def test_multiple_broken_links():
    respx.head("https://example.com/gone1").mock(return_value=httpx.Response(404))
    respx.head("https://example.com/gone2").mock(return_value=httpx.Response(410))
    respx.head("https://example.com/ok").mock(return_value=httpx.Response(200))

    doc = _make_doc(links=[
        "https://example.com/gone1",
        "https://example.com/gone2",
        "https://example.com/ok",
    ])
    analyzer = BrokenLinkAnalyzer()
    results = analyzer.analyze([doc])
    assert "1" in results
    assert len(results["1"]) == 2


def test_no_urls_no_signals():
    doc = _make_doc(content="Plain text with no links.")
    analyzer = BrokenLinkAnalyzer()
    results = analyzer.analyze([doc])
    assert not results


@respx.mock
def test_head_405_falls_back_to_get():
    respx.head("https://example.com/no-head").mock(return_value=httpx.Response(405))
    respx.get("https://example.com/no-head").mock(return_value=httpx.Response(200))

    doc = _make_doc(links=["https://example.com/no-head"])
    analyzer = BrokenLinkAnalyzer()
    results = analyzer.analyze([doc])
    assert not results
