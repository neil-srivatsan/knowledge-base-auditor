"""Detect references to other documents and flag unresolved or ambiguous ones."""

from __future__ import annotations

import re
from collections import defaultdict

from kb_audit.analyzers.base import Analyzer
from kb_audit.models import Document, Severity, StalenessSignal
from kb_audit.titles import normalize_base_title

# Patterns that introduce a reference to another document title.
# Capture group 1 holds the referenced title text.
# The title capture stops at newline, period, semicolon, or comma.
_REFERENCE_PATTERNS = [
    re.compile(r"(?:For (?:more )?(?:details|information),?\s+)?[Ss]ee:?\s+([^\n.,;]+)", re.IGNORECASE),
    re.compile(r"[Rr]efer\s+to:?\s+([^\n.,;]+)", re.IGNORECASE),
]


def _extract_references(content: str) -> list[str]:
    """Extract referenced titles from document content."""
    refs: list[str] = []
    for pattern in _REFERENCE_PATTERNS:
        for match in pattern.finditer(content):
            title = match.group(1).strip()
            if title:
                refs.append(title)
    return refs


class ReferenceAnalyzer(Analyzer):
    """Flag references to other documents that cannot be resolved."""

    @classmethod
    def name(cls) -> str:
        return "references"

    def analyze(self, documents: list[Document]) -> dict[str, list[StalenessSignal]]:
        # Build lookup indexes for title resolution
        exact_titles: dict[str, list[Document]] = defaultdict(list)
        normalized_titles: dict[str, list[Document]] = defaultdict(list)
        base_titles: dict[str, list[Document]] = defaultdict(list)
        for doc in documents:
            exact_titles[doc.title.lower()].append(doc)
            normalized_titles[normalize_base_title(doc.title)].append(doc)
            base_titles[normalize_base_title(doc.title)].append(doc)

        results: dict[str, list[StalenessSignal]] = {}

        for doc in documents:
            refs = _extract_references(doc.content)
            for ref in refs:
                signal = self._resolve_reference(
                    ref, doc, exact_titles, normalized_titles, base_titles,
                )
                if signal:
                    results.setdefault(doc.id, []).append(signal)

        return results

    def _resolve_reference(
        self,
        ref: str,
        source_doc: Document,
        exact_titles: dict[str, list[Document]],
        normalized_titles: dict[str, list[Document]],
        base_titles: dict[str, list[Document]],
    ) -> StalenessSignal | None:
        """Attempt to resolve a reference, returning the appropriate signal.

        Resolution order:
        1. Exact case-insensitive title match
        2. Normalized base-title match (strips year/version/stale suffixes)
        3. Base-title variant match on the *reference text* itself
        """
        ref_lower = ref.lower()
        ref_normalized = normalize_base_title(ref)

        # --- Step 1: exact case-insensitive ---
        matches = [m for m in exact_titles.get(ref_lower, []) if m.id != source_doc.id]
        if len(matches) == 1:
            return self._resolved_signal(ref, matches[0])
        if len(matches) > 1:
            return self._ambiguous_signal(ref, matches, ref_normalized)

        # --- Step 2: normalized title match ---
        matches = [m for m in normalized_titles.get(ref_normalized, []) if m.id != source_doc.id]
        if len(matches) == 1:
            return self._resolved_variant_signal(ref, matches[0], ref_normalized)
        if len(matches) > 1:
            return self._ambiguous_signal(ref, matches, ref_normalized)

        # --- Step 3: base-title variant match on the reference text ---
        # The reference itself might have a suffix that doesn't match any
        # document exactly, but its base title matches a document in the scan.
        # This is already covered by step 2 since we normalize the ref.
        # No additional step needed.

        # --- Unresolved ---
        return self._unresolved_signal(ref, ref_normalized)

    @staticmethod
    def _resolved_signal(ref: str, target: Document) -> StalenessSignal:
        return StalenessSignal(
            signal_type="resolved_reference",
            severity=Severity.INFO,
            message=f"References '{ref}' → '{target.title}'",
            details={
                "referenced_title": ref,
                "resolved_doc_id": target.id,
                "resolved_title": target.title,
            },
        )

    @staticmethod
    def _resolved_variant_signal(
        ref: str, target: Document, normalized_ref: str,
    ) -> StalenessSignal:
        return StalenessSignal(
            signal_type="resolved_reference",
            severity=Severity.INFO,
            message=(
                f"References '{ref}' → '{target.title}' "
                f"(matched by base title)"
            ),
            details={
                "referenced_title": ref,
                "resolved_doc_id": target.id,
                "resolved_title": target.title,
                "normalized_reference": normalized_ref,
                "match_type": "base_title_variant",
                "resolution_scope": "scan_scope",
            },
        )

    @staticmethod
    def _ambiguous_signal(
        ref: str,
        matches: list[Document],
        normalized_ref: str,
    ) -> StalenessSignal:
        return StalenessSignal(
            signal_type="ambiguous_reference",
            severity=Severity.WARNING,
            message=f"References '{ref}' which matches {len(matches)} documents",
            details={
                "referenced_title": ref,
                "normalized_reference": normalized_ref,
                "matching_doc_ids": [m.id for m in matches],
                "matching_titles": [m.title for m in matches],
                "resolution_scope": "scan_scope",
            },
        )

    @staticmethod
    def _unresolved_signal(ref: str, normalized_ref: str) -> StalenessSignal:
        return StalenessSignal(
            signal_type="unresolved_reference",
            severity=Severity.WARNING,
            message=f"References '{ref}' but no matching page found among the pages in this scan",
            details={
                "referenced_title": ref,
                "normalized_reference": normalized_ref,
                "resolution_scope": "scan_scope",
            },
        )
