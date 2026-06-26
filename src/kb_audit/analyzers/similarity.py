"""Near-duplicate document detection via content hashing, version markers, and fuzzy matching."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

from rapidfuzz import fuzz

from kb_audit.analyzers.base import Analyzer
from kb_audit.models import Document, Severity, StalenessSignal
from kb_audit.titles import normalize_base_title as _normalize_base_title

_WINDOW_SIZE = 1000

# Patterns to extract version markers from title and content
_VERSION_PATTERNS = [
    re.compile(r"\bv(\d+(?:\.\d+)*)\b", re.IGNORECASE),
    re.compile(r"\bversion\s+(\d+(?:\.\d+)*)\b", re.IGNORECASE),
]


def _extract_version(doc: Document) -> tuple[float, ...] | None:
    """Extract the highest version tuple from a document's title and first 500 chars."""
    text = doc.title + " " + doc.content[:500]
    versions: list[tuple[float, ...]] = []
    for pattern in _VERSION_PATTERNS:
        for match in pattern.finditer(text):
            parts = tuple(int(p) for p in match.group(1).split("."))
            versions.append(parts)
    return max(versions) if versions else None


def _multi_window_similarity(text_a: str, text_b: str) -> float:
    """Compare two texts using first/middle/last windows and return the average score."""
    len_a, len_b = len(text_a), len(text_b)

    # Short texts: compare directly
    if len_a <= _WINDOW_SIZE and len_b <= _WINDOW_SIZE:
        return fuzz.token_sort_ratio(text_a, text_b)

    # Extract windows: first, middle, last
    def windows(text: str) -> list[str]:
        n = len(text)
        first = text[:_WINDOW_SIZE]
        mid_start = max(0, (n - _WINDOW_SIZE) // 2)
        middle = text[mid_start : mid_start + _WINDOW_SIZE]
        last = text[-_WINDOW_SIZE:] if n > _WINDOW_SIZE else text
        return [first, middle, last]

    wins_a = windows(text_a)
    wins_b = windows(text_b)

    scores = [fuzz.token_sort_ratio(a, b) for a, b in zip(wins_a, wins_b)]
    return sum(scores) / len(scores)


class SimilarityAnalyzer(Analyzer):
    def __init__(self, threshold: float = 0.80) -> None:
        self._threshold = threshold * 100  # rapidfuzz uses 0-100 scale

    @classmethod
    def name(cls) -> str:
        return "similarity"

    def analyze(self, documents: list[Document]) -> dict[str, list[StalenessSignal]]:
        results: dict[str, list[StalenessSignal]] = {}

        # Phase 1: exact duplicates by content hash
        hash_groups: dict[str, list[Document]] = defaultdict(list)
        for doc in documents:
            hash_groups[doc.content_hash].append(doc)

        for group in hash_groups.values():
            if len(group) < 2:
                continue
            # Sort by last_modified descending; newest is the canonical version
            # Documents without dates sort last (treated as oldest)
            _epoch = datetime.min.replace(tzinfo=timezone.utc)
            group.sort(key=lambda d: d.last_modified or _epoch, reverse=True)
            newest = group[0]
            for older in group[1:]:
                results.setdefault(older.id, []).append(
                    StalenessSignal(
                        signal_type="duplicate",
                        severity=Severity.CRITICAL,
                        message=f"Exact duplicate of '{newest.title}'",
                        details={
                            "duplicate_of": newest.id,
                            "duplicate_title": newest.title,
                            "similarity": 100.0,
                        },
                    )
                )

        # Phase 2: version marker comparison
        # Group versioned docs by normalized base title so that only
        # documents on the same topic are compared (e.g. "API Guide v1"
        # vs "API Guide v3", not "Billing Setup v2" vs "API Guide v3").
        title_groups: dict[str, list[tuple[Document, tuple[float, ...]]]] = defaultdict(list)
        for doc in documents:
            ver = _extract_version(doc)
            if ver is not None:
                base = _normalize_base_title(doc.title)
                title_groups[base].append((doc, ver))

        version_flagged: set[str] = set()
        for ver_group in title_groups.values():
            if len(ver_group) < 2:
                continue
            ver_group.sort(key=lambda x: x[1], reverse=True)
            highest_ver = ver_group[0][1]
            newest_doc = ver_group[0][0]
            for doc, ver in ver_group[1:]:
                if ver < highest_ver and doc.id not in results:
                    ver_str = ".".join(str(p) for p in ver)
                    highest_str = ".".join(str(p) for p in highest_ver)
                    results.setdefault(doc.id, []).append(
                        StalenessSignal(
                            signal_type="version_marker",
                            severity=Severity.CRITICAL,
                            message=(
                                f"Contains version v{ver_str}, "
                                f"but v{highest_str} exists in '{newest_doc.title}'"
                            ),
                            details={
                                "found_version": f"v{ver_str}",
                                "current_version": f"v{highest_str}",
                                "current_doc_id": newest_doc.id,
                                "current_doc_title": newest_doc.title,
                                "similar_to": newest_doc.id,
                                "similar_title": newest_doc.title,
                            },
                        )
                    )
                    version_flagged.add(doc.id)

        # Phase 3: fuzzy near-duplicates (skip pairs already flagged as exact)
        exact_pairs: set[tuple[str, str]] = set()
        for group in hash_groups.values():
            if len(group) >= 2:
                for i, d1 in enumerate(group):
                    for d2 in group[i + 1 :]:
                        exact_pairs.add((d1.id, d2.id))
                        exact_pairs.add((d2.id, d1.id))

        for i, doc_a in enumerate(documents):
            for doc_b in documents[i + 1 :]:
                if (doc_a.id, doc_b.id) in exact_pairs:
                    continue

                # Compare using token sort ratio across multiple content windows
                score = _multi_window_similarity(doc_a.content, doc_b.content)
                if score >= self._threshold:
                    # Flag the older document (no date = treated as oldest)
                    _epoch = datetime.min.replace(tzinfo=timezone.utc)
                    if (doc_a.last_modified or _epoch) >= (
                        doc_b.last_modified or _epoch
                    ):
                        older, newer = doc_b, doc_a
                    else:
                        older, newer = doc_a, doc_b

                    results.setdefault(older.id, []).append(
                        StalenessSignal(
                            signal_type="near_duplicate",
                            severity=Severity.WARNING,
                            message=f"~{score:.0f}% similar to '{newer.title}'",
                            details={
                                "similar_to": newer.id,
                                "similar_title": newer.title,
                                "similarity": round(score, 1),
                            },
                        )
                    )

        return results
