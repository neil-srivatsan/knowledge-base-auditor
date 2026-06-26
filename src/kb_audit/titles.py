"""Shared title normalization for scan-local matching.

Used by the reference analyzer, similarity analyzer, and trust classifier
to compare document titles after stripping version/year/stale suffixes.
"""

from __future__ import annotations

import re

# Trailing year: "Guide 2021"
_YEAR_SUFFIX_RE = re.compile(r"\s+\d{4}\s*$")

# Trailing version: "Guide v1", "Guide v2.1", "Guide version 3"
_VERSION_SUFFIX_RE = re.compile(
    r"\s+(?:v|version\s*)\d+(?:\.\d+)*\s*$", re.IGNORECASE,
)

# Parenthetical stale/status suffix: "(old)", "(deprecated)", "(archived)", etc.
_STALE_SUFFIX_RE = re.compile(
    r"\s*\(\s*(?:old|deprecated|archived|legacy|obsolete|copy|draft|backup)\s*\)\s*$",
    re.IGNORECASE,
)


def normalize_base_title(title: str) -> str:
    """Return the lowered base title with trailing year/version/stale suffixes removed.

    Examples::

        >>> normalize_base_title("Payment Platform Migration Guide 2021")
        'payment platform migration guide'
        >>> normalize_base_title("API Guide v1")
        'api guide'
        >>> normalize_base_title("API Guide (old)")
        'api guide'
    """
    t = title.strip()

    # Strip stale parenthetical suffix first
    t = _STALE_SUFFIX_RE.sub("", t).strip()

    # Strip trailing year
    t = _YEAR_SUFFIX_RE.sub("", t).strip()

    # Strip trailing version
    t = _VERSION_SUFFIX_RE.sub("", t).strip()

    return t.lower()


def normalize_title(title: str) -> tuple[str, str | None, str | None]:
    """Normalize a document title, returning structured components.

    Returns ``(base_title, year_or_version_suffix, stale_suffix)`` where
    *base_title* is the lowered, stripped title with trailing year/version/
    stale-suffix removed.
    """
    t = title.strip()
    stale_suffix: str | None = None
    year_or_version: str | None = None

    # Check for stale parenthetical suffix first
    m = _STALE_SUFFIX_RE.search(t)
    if m:
        stale_suffix = m.group(0).strip()
        t = t[: m.start()].strip()

    # Check trailing year: "Guide 2021"
    m = _YEAR_SUFFIX_RE.search(t)
    if m:
        year_or_version = m.group(0).strip()
        t = t[: m.start()].strip()
    else:
        # Check trailing version: "Guide v1", "Guide version 2.0"
        m = _VERSION_SUFFIX_RE.search(t)
        if m:
            year_or_version = m.group(0).strip()
            t = t[: m.start()].strip()

    return t.lower(), year_or_version, stale_suffix
