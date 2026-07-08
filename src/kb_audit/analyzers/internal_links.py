"""Analyze structured links extracted from document source representations.

Each :class:`DocumentLink` on a :class:`Document` is resolved against all
documents in the current scan.  Five signal types may result:

- ``resolved_internal_link`` (INFO): link resolves to exactly one other doc
- ``replacement_link`` (CRITICAL): resolved link uses replacement language
- ``backlink_from_successor`` (INFO): resolved link references a previous/legacy version
- ``broken_internal_link`` (WARNING): link appears internal but cannot be resolved
- ``ambiguous_internal_link`` (WARNING): link resolves to more than one doc
"""

from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlparse

from kb_audit.analyzers.base import Analyzer
from kb_audit.models import Document, DocumentLink, Severity, StalenessSignal
from kb_audit.titles import normalize_base_title

# Phrases in a link's text/context indicating the source document is obsolete
# and this link points to its replacement.
_REPLACEMENT_PHRASES = re.compile(
    r"\b(?:replaced?\s+by|superseded?\s+by|use\s+instead|moved?\s+to|"
    r"see\s+(?:the\s+)?(?:new|current)\s+version|new\s+version(?:\s+is)?)\b",
    re.IGNORECASE,
)

# Phrases indicating the source document is a successor referencing old content
# (backlink context: the target is the *older* document).
_BACKLINK_PHRASES = re.compile(
    r"\b(?:migration\s+from|migrat(?:e[ds]?|ing)\s+from|upgrade\s+from|"
    r"previous\s+version|legacy\s+version|from\s+the\s+old)\b",
    re.IGNORECASE,
)


class InternalLinkAnalyzer(Analyzer):
    """Emit signals for structured links in each document's ``links`` list."""

    @classmethod
    def name(cls) -> str:
        return "internal_links"

    def analyze(self, documents: list[Document]) -> dict[str, list[StalenessSignal]]:
        # Build lookup indexes
        docs_by_id: dict[str, Document] = {}
        docs_by_url: dict[str, list[Document]] = defaultdict(list)
        docs_by_exact_title: dict[str, list[Document]] = defaultdict(list)
        docs_by_norm_title: dict[str, list[Document]] = defaultdict(list)

        for doc in documents:
            docs_by_id[doc.id] = doc
            if doc.url:
                docs_by_url[doc.url.rstrip("/")].append(doc)
            docs_by_exact_title[doc.title.lower()].append(doc)
            docs_by_norm_title[normalize_base_title(doc.title)].append(doc)

        results: dict[str, list[StalenessSignal]] = {}

        for doc in documents:
            if not doc.links:
                continue
            for link in doc.links:
                signal = self._analyze_link(
                    link, doc,
                    docs_by_id, docs_by_url,
                    docs_by_exact_title, docs_by_norm_title,
                )
                if signal is not None:
                    results.setdefault(doc.id, []).append(signal)

        return results

    # ------------------------------------------------------------------
    # Link resolution
    # ------------------------------------------------------------------

    def _analyze_link(
        self,
        link: DocumentLink,
        source_doc: Document,
        docs_by_id: dict[str, Document],
        docs_by_url: dict[str, list[Document]],
        docs_by_exact_title: dict[str, list[Document]],
        docs_by_norm_title: dict[str, list[Document]],
    ) -> StalenessSignal | None:
        candidates = self._resolve_candidates(
            link, source_doc, docs_by_id, docs_by_url,
            docs_by_exact_title, docs_by_norm_title,
        )

        if not candidates:
            if self._is_internal(link, source_doc):
                ref = link.url or link.target_title or link.target_id or ""
                return StalenessSignal(
                    signal_type="broken_internal_link",
                    severity=Severity.WARNING,
                    message=(
                        f"Internal link '{ref}' cannot be resolved "
                        f"to any document in this scan"
                    ),
                    details={
                        "url": link.url or "",
                        "target_title": link.target_title or "",
                    },
                )
            return None

        if len(candidates) > 1:
            ref = link.url or link.target_title or link.target_id or ""
            return StalenessSignal(
                signal_type="ambiguous_internal_link",
                severity=Severity.WARNING,
                message=(
                    f"Internal link '{ref}' matches "
                    f"{len(candidates)} documents"
                ),
                details={
                    "url": link.url or "",
                    "matching_doc_ids": [d.id for d in candidates],
                    "matching_titles": [d.title for d in candidates],
                },
            )

        target = candidates[0]
        combined_text = " ".join(filter(None, [link.text, link.context]))

        if _REPLACEMENT_PHRASES.search(combined_text):
            return StalenessSignal(
                signal_type="replacement_link",
                severity=Severity.CRITICAL,
                message=(
                    f"Link to '{target.title}' uses replacement language: "
                    f"this document is being replaced"
                ),
                details={
                    "url": link.url or "",
                    "target_id": target.id,
                    "target_title": target.title,
                    "link_text": link.text or "",
                },
            )

        if _BACKLINK_PHRASES.search(combined_text):
            return StalenessSignal(
                signal_type="backlink_from_successor",
                severity=Severity.INFO,
                message=(
                    f"Link to '{target.title}' references a previous/legacy version"
                ),
                details={
                    "url": link.url or "",
                    "target_id": target.id,
                    "target_title": target.title,
                    "link_text": link.text or "",
                },
            )

        return StalenessSignal(
            signal_type="resolved_internal_link",
            severity=Severity.INFO,
            message=f"Internal link \u2192 '{target.title}'",
            details={
                "url": link.url or "",
                "target_id": target.id,
                "target_title": target.title,
            },
        )

    def _resolve_candidates(
        self,
        link: DocumentLink,
        source_doc: Document,
        docs_by_id: dict[str, Document],
        docs_by_url: dict[str, list[Document]],
        docs_by_exact_title: dict[str, list[Document]],
        docs_by_norm_title: dict[str, list[Document]],
    ) -> list[Document]:
        # 1. Exact ID match
        if link.target_id and link.target_id in docs_by_id:
            target = docs_by_id[link.target_id]
            if target.id != source_doc.id:
                return [target]

        # 2. URL match
        if link.url:
            url_key = link.url.rstrip("/")
            url_matches = [
                d for d in docs_by_url.get(url_key, [])
                if d.id != source_doc.id
            ]
            if url_matches:
                return url_matches

        # 3. Title match (exact then normalized)
        if link.target_title:
            title_lower = link.target_title.lower()
            exact = [
                d for d in docs_by_exact_title.get(title_lower, [])
                if d.id != source_doc.id
            ]
            if exact:
                return exact
            norm = normalize_base_title(link.target_title)
            norm_matches = [
                d for d in docs_by_norm_title.get(norm, [])
                if d.id != source_doc.id
            ]
            if norm_matches:
                return norm_matches

        return []

    def _is_internal(self, link: DocumentLink, source_doc: Document) -> bool:
        """True if this link plausibly points to a document in this knowledge base."""
        if link.target_id or link.target_title:
            return True
        if link.url and source_doc.url:
            try:
                link_netloc = urlparse(link.url).netloc
                doc_netloc = urlparse(source_doc.url).netloc
                if link_netloc and doc_netloc and link_netloc == doc_netloc:
                    return True
            except Exception:
                pass
        return False
