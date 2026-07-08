"""Tests for InternalLinkAnalyzer and trust integration."""

from __future__ import annotations

import pytest

from kb_audit.analyzers.internal_links import InternalLinkAnalyzer
from kb_audit.models import Document, DocumentLink, Severity, StalenessSignal
from kb_audit.trust import classify, compute_incoming_ref_counts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(
    doc_id: str,
    title: str,
    url: str | None = None,
    links: list[DocumentLink] | None = None,
    content: str = "Some content.",
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        content=content,
        source_type="demo",
        url=url or f"https://demo.example/pages/{doc_id}",
        links=links or [],
    )


# ---------------------------------------------------------------------------
# 1. resolved_internal_link — link by URL resolves to exactly one doc
# ---------------------------------------------------------------------------

def test_resolved_internal_link_by_url() -> None:
    target = _doc("target-doc", "Target Doc", url="https://demo.example/pages/target-doc")
    source = _doc(
        "source-doc",
        "Source Doc",
        links=[DocumentLink(url="https://demo.example/pages/target-doc", source="demo")],
    )
    analyzer = InternalLinkAnalyzer()
    results = analyzer.analyze([source, target])

    assert "source-doc" in results
    signals = results["source-doc"]
    assert len(signals) == 1
    assert signals[0].signal_type == "resolved_internal_link"
    assert signals[0].severity == Severity.INFO
    assert signals[0].details["target_id"] == "target-doc"
    assert signals[0].details["target_title"] == "Target Doc"


# ---------------------------------------------------------------------------
# 2. resolved_internal_link — link by title resolves to exactly one doc
# ---------------------------------------------------------------------------

def test_resolved_internal_link_by_title() -> None:
    target = _doc("guide-doc", "Integration Guide")
    source = _doc(
        "linking-doc",
        "Linking Doc",
        links=[DocumentLink(url="https://other.example/whatever", target_title="Integration Guide", source="demo")],
    )
    analyzer = InternalLinkAnalyzer()
    results = analyzer.analyze([source, target])

    assert "linking-doc" in results
    signals = results["linking-doc"]
    assert len(signals) == 1
    assert signals[0].signal_type == "resolved_internal_link"
    assert signals[0].details["target_title"] == "Integration Guide"


# ---------------------------------------------------------------------------
# 3. replacement_link — resolved link with replacement phrase in text
# ---------------------------------------------------------------------------

def test_replacement_link_by_text() -> None:
    new_doc = _doc("new-guide", "New Guide")
    old_doc = _doc(
        "old-guide",
        "Old Guide",
        links=[
            DocumentLink(
                url="https://demo.example/pages/new-guide",
                target_title="New Guide",
                text="replaced by New Guide",
                source="demo",
            )
        ],
    )
    analyzer = InternalLinkAnalyzer()
    results = analyzer.analyze([old_doc, new_doc])

    assert "old-guide" in results
    signals = results["old-guide"]
    assert len(signals) == 1
    assert signals[0].signal_type == "replacement_link"
    assert signals[0].severity == Severity.CRITICAL
    assert signals[0].details["target_title"] == "New Guide"


# ---------------------------------------------------------------------------
# 4. backlink_from_successor — resolved link with backlink phrase in context
# ---------------------------------------------------------------------------

def test_backlink_from_successor() -> None:
    legacy_doc = _doc("legacy-api", "Legacy API")
    new_doc = _doc(
        "new-api",
        "New API",
        links=[
            DocumentLink(
                url="https://demo.example/pages/legacy-api",
                target_title="Legacy API",
                context="Migrated from the legacy API integration.",
                source="demo",
            )
        ],
    )
    analyzer = InternalLinkAnalyzer()
    results = analyzer.analyze([new_doc, legacy_doc])

    assert "new-api" in results
    signals = results["new-api"]
    assert len(signals) == 1
    assert signals[0].signal_type == "backlink_from_successor"
    assert signals[0].severity == Severity.INFO
    assert signals[0].details["target_title"] == "Legacy API"


# ---------------------------------------------------------------------------
# 5. broken_internal_link — same domain but no match
# ---------------------------------------------------------------------------

def test_broken_internal_link_same_domain() -> None:
    source = _doc(
        "source-doc",
        "Source Doc",
        url="https://demo.example/pages/source-doc",
        links=[
            DocumentLink(
                url="https://demo.example/pages/nonexistent-page",
                source="demo",
            )
        ],
    )
    other = _doc("other-doc", "Other Doc")
    analyzer = InternalLinkAnalyzer()
    results = analyzer.analyze([source, other])

    assert "source-doc" in results
    signals = results["source-doc"]
    assert len(signals) == 1
    assert signals[0].signal_type == "broken_internal_link"
    assert signals[0].severity == Severity.WARNING
    assert "nonexistent-page" in signals[0].details["url"]


# ---------------------------------------------------------------------------
# 6. broken_internal_link — link has target_title but no match
# ---------------------------------------------------------------------------

def test_broken_internal_link_with_target_title() -> None:
    source = _doc(
        "source-doc",
        "Source Doc",
        links=[
            DocumentLink(
                url="https://external.example/something",
                target_title="Missing Document",
                source="demo",
            )
        ],
    )
    analyzer = InternalLinkAnalyzer()
    results = analyzer.analyze([source])

    assert "source-doc" in results
    signals = results["source-doc"]
    assert len(signals) == 1
    assert signals[0].signal_type == "broken_internal_link"
    assert signals[0].details["target_title"] == "Missing Document"


# ---------------------------------------------------------------------------
# 7. ambiguous_internal_link — link by title resolves to multiple docs
# ---------------------------------------------------------------------------

def test_ambiguous_internal_link() -> None:
    target_a = _doc("guide-v1", "Integration Guide")
    target_b = _doc("guide-v2", "Integration Guide")
    source = _doc(
        "linking-doc",
        "Linking Doc",
        links=[
            DocumentLink(
                url="https://demo.example/pages/guide",
                target_title="Integration Guide",
                source="demo",
            )
        ],
    )
    analyzer = InternalLinkAnalyzer()
    results = analyzer.analyze([source, target_a, target_b])

    assert "linking-doc" in results
    signals = results["linking-doc"]
    assert len(signals) == 1
    assert signals[0].signal_type == "ambiguous_internal_link"
    assert signals[0].severity == Severity.WARNING
    assert len(signals[0].details["matching_doc_ids"]) == 2


# ---------------------------------------------------------------------------
# 8. Trust: replacement_link appears in stale evidence
# ---------------------------------------------------------------------------

def test_trust_replacement_link_stale() -> None:
    new_doc = _doc("new-guide", "New Guide")
    old_doc = _doc("old-guide", "Old Guide")

    replacement_signal = StalenessSignal(
        signal_type="replacement_link",
        severity=Severity.CRITICAL,
        message="Link to 'New Guide' uses replacement language: this document is being replaced",
        details={
            "url": "https://demo.example/pages/new-guide",
            "target_id": "new-guide",
            "target_title": "New Guide",
            "link_text": "replaced by New Guide",
        },
    )

    result = classify(old_doc, [replacement_signal])
    assert result.status == "stale"
    # Verify the evidence mentions the replacement target
    combined = " ".join(result.evidence.review_risks + [result.reason])
    assert "New Guide" in combined


# ---------------------------------------------------------------------------
# 9. Trust: broken_internal_link appears in hard risk
# ---------------------------------------------------------------------------

def test_trust_broken_internal_link_needs_review() -> None:
    doc = _doc("some-doc", "Some Doc")

    broken_signal = StalenessSignal(
        signal_type="broken_internal_link",
        severity=Severity.WARNING,
        message="Internal link 'https://demo.example/pages/missing' cannot be resolved to any document in this scan",
        details={
            "url": "https://demo.example/pages/missing",
            "target_title": "",
        },
    )

    result = classify(doc, [broken_signal])
    assert result.status == "needs_review"
    # Verify the broken link URL appears in evidence
    combined = " ".join(result.evidence.review_risks + [result.reason])
    assert "demo.example/pages/missing" in combined


# ---------------------------------------------------------------------------
# 10. Trust: resolved_internal_link counted in compute_incoming_ref_counts
# ---------------------------------------------------------------------------

def test_compute_incoming_ref_counts_resolved_internal_link() -> None:
    signal_a = StalenessSignal(
        signal_type="resolved_internal_link",
        severity=Severity.INFO,
        message="Internal link → 'Target Doc'",
        details={
            "url": "https://demo.example/pages/target-doc",
            "target_id": "target-doc",
            "target_title": "Target Doc",
        },
    )
    signal_b = StalenessSignal(
        signal_type="resolved_internal_link",
        severity=Severity.INFO,
        message="Internal link → 'Target Doc'",
        details={
            "url": "https://demo.example/pages/target-doc",
            "target_id": "target-doc",
            "target_title": "Target Doc",
        },
    )

    all_signals: dict[str, list[StalenessSignal]] = {
        "source-doc-1": [signal_a],
        "source-doc-2": [signal_b],
    }

    counts = compute_incoming_ref_counts(all_signals)
    assert counts.get("target-doc", 0) == 2


# ---------------------------------------------------------------------------
# Analyzer integration: real source-style DocumentLinks
# ---------------------------------------------------------------------------


class TestSourceStyleDocumentLinks:
    """Verify DocumentLinks with text/context (as produced by Notion/Confluence)
    can trigger replacement_link and resolved_internal_link signals."""

    def test_notion_style_replacement_link_emits_replacement_signal(self):
        """A DocumentLink with rich-text replacement phrase → replacement_link."""
        replacement_url = "https://notion.so/new-api-guide"
        new_guide = _doc("new-api-guide", "New API Guide", url=replacement_url)
        old_guide = _doc(
            "old-api-guide",
            "Old API Guide",
            url="https://notion.so/old-api-guide",
            links=[
                DocumentLink(
                    url=replacement_url,
                    text="replaced by New API Guide",
                    context="This page has been replaced by New API Guide",
                    source="notion",
                )
            ],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([old_guide, new_guide])
        signals = results.get("old-api-guide", [])
        types = [s.signal_type for s in signals]
        assert "replacement_link" in types

    def test_confluence_style_anchor_text_emits_replacement_signal(self):
        """A DocumentLink extracted from Confluence anchor text → replacement_link."""
        target_url = "https://wiki.example.com/new-guide"
        new_guide = _doc("new-guide", "New Guide", url=target_url)
        old_page = _doc(
            "old-page",
            "Old Page",
            url="https://wiki.example.com/old-page",
            links=[
                DocumentLink(
                    url=target_url,
                    text="replaced by New Guide",
                    source="confluence",
                )
            ],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([old_page, new_guide])
        signals = results.get("old-page", [])
        types = [s.signal_type for s in signals]
        assert "replacement_link" in types

    def test_plain_url_link_without_text_emits_resolved_internal_link(self):
        """A DocumentLink with only a URL (no text/context) resolves cleanly."""
        target_url = "https://wiki.example.com/reference"
        reference = _doc("reference", "Reference Guide", url=target_url)
        source_page = _doc(
            "source-page",
            "Source Page",
            url="https://wiki.example.com/source-page",
            links=[DocumentLink(url=target_url, source="confluence")],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([source_page, reference])
        signals = results.get("source-page", [])
        types = [s.signal_type for s in signals]
        assert "resolved_internal_link" in types
        assert "replacement_link" not in types

    def test_context_outside_anchor_triggers_replacement_link(self):
        """Replacement phrase in context (not anchor text) still emits replacement_link."""
        target_url = "https://wiki.example.com/new-guide"
        new_guide = _doc("new-guide", "New Guide", url=target_url)
        old_page = _doc(
            "old-page",
            "Old Page",
            url="https://wiki.example.com/old-page",
            links=[
                # text is just "New Guide" (anchor text); replacement phrase is in context
                DocumentLink(
                    url=target_url,
                    text="New Guide",
                    context="This page has been replaced by New Guide.",
                    source="confluence",
                )
            ],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([old_page, new_guide])
        signals = results.get("old-page", [])
        types = [s.signal_type for s in signals]
        assert "replacement_link" in types

    def test_backlink_context_emits_backlink_from_successor(self):
        """Backlink phrase in context emits backlink_from_successor, not replacement_link."""
        legacy_url = "https://wiki.example.com/legacy-api"
        legacy = _doc("legacy-api", "Legacy API", url=legacy_url)
        new_guide = _doc(
            "new-guide",
            "New Guide",
            url="https://wiki.example.com/new-guide",
            links=[
                DocumentLink(
                    url=legacy_url,
                    text="Legacy API",
                    context="For migration from Legacy API, see the upgrade notes.",
                    source="confluence",
                )
            ],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([new_guide, legacy])
        signals = results.get("new-guide", [])
        types = [s.signal_type for s in signals]
        assert "backlink_from_successor" in types
        assert "replacement_link" not in types

    def test_normalized_relative_url_resolves_by_url(self):
        """A relative Confluence URL, pre-normalized to absolute, resolves by URL match."""
        base = "https://wiki.example.com"
        target_url = f"{base}/wiki/spaces/ENG/pages/42/Guide"
        guide = _doc("guide", "Guide", url=target_url)
        old_page = _doc(
            "old-page",
            "Old Page",
            url=f"{base}/old-page",
            links=[
                # URL already normalized to absolute (as done in _page_to_document)
                DocumentLink(url=target_url, text="Guide", source="confluence")
            ],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([old_page, guide])
        signals = results.get("old-page", [])
        types = [s.signal_type for s in signals]
        assert "resolved_internal_link" in types


# ---------------------------------------------------------------------------
# URL-less DocumentLink resolution
# ---------------------------------------------------------------------------


class TestUrlLessDocumentLink:
    """DocumentLink.url=None must not crash and must resolve via target_id / target_title."""

    def test_resolve_by_target_id_no_url(self):
        target = _doc("target-doc", "Target Doc", url="https://demo.example/target")
        source = _doc(
            "source-doc",
            "Source Doc",
            url="https://demo.example/source",
            links=[DocumentLink(target_id="target-doc")],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([source, target])
        types = [s.signal_type for s in results.get("source-doc", [])]
        assert "resolved_internal_link" in types

    def test_resolve_by_target_title_no_url(self):
        target = _doc("target-doc", "Target Doc", url="https://demo.example/target")
        source = _doc(
            "source-doc",
            "Source Doc",
            url="https://demo.example/source",
            links=[DocumentLink(target_title="Target Doc")],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([source, target])
        types = [s.signal_type for s in results.get("source-doc", [])]
        assert "resolved_internal_link" in types

    def test_broken_internal_link_no_url_no_crash(self):
        source = _doc(
            "source-doc",
            "Source Doc",
            url="https://demo.example/source",
            links=[DocumentLink(target_title="Missing Doc")],
        )
        analyzer = InternalLinkAnalyzer()
        results = analyzer.analyze([source])
        types = [s.signal_type for s in results.get("source-doc", [])]
        assert "broken_internal_link" in types

    def test_metadata_links_excludes_none_url(self):
        """metadata['links'] built from DocumentLinks with url=None stays list[str]."""
        # Simulate what a source builds when a DocumentLink has no URL
        doc_links = [
            DocumentLink(url="https://example.com/a"),
            DocumentLink(target_title="Some Doc"),   # url is None
            DocumentLink(url="https://example.com/b"),
        ]
        metadata_links = [dl.url for dl in doc_links if dl.url is not None]
        assert metadata_links == ["https://example.com/a", "https://example.com/b"]
        assert all(isinstance(u, str) for u in metadata_links)
