"""Shared title normalization for scan-local matching.

Used by the reference analyzer, similarity analyzer, and trust classifier
to compare document titles after stripping version/year/stale suffixes.
"""

from __future__ import annotations

import re

# Trailing year: restricted to plausible documentation years (1900-2099).
# A year is only stripped when the preceding text is long enough to be a
# documentation title (see _year_qualifies).
_YEAR_SUFFIX_RE = re.compile(r"\s+(?:19|20)\d{2}\s*$")

# Trailing version: "Guide v1", "Guide v2.1", "Guide version 3"
_VERSION_SUFFIX_RE = re.compile(
    r"\s+(?:v|version\s*)\d+(?:\.\d+)*\s*$", re.IGNORECASE,
)

# Parenthetical stale/status suffix: "(old)", "(deprecated)", "(archived)", etc.
_STALE_SUFFIX_RE = re.compile(
    r"\s*\(\s*(?:old|deprecated|archived|legacy|obsolete|copy|draft|backup)\s*\)\s*$",
    re.IGNORECASE,
)


def _year_qualifies(remainder: str) -> bool:
    """Return True when stripping a trailing year from *remainder* is warranted.

    Requires at least two space-separated tokens **or** an existing trailing
    version marker.  This prevents short label-like titles such as "HTTP 2000"
    from having their trailing number treated as a documentation year, while
    still stripping the year from "Guide v1 2021" (version marker present) or
    "Migration Guide 2021" (two words).
    """
    return len(remainder.split()) >= 2 or bool(_VERSION_SUFFIX_RE.search(remainder))


def normalize_base_title(title: str) -> str:
    """Return the lowered base title with trailing year/version/stale suffixes removed.

    Examples::

        >>> normalize_base_title("Payment Platform Migration Guide 2021")
        'payment platform migration guide'
        >>> normalize_base_title("API Guide v1")
        'api guide'
        >>> normalize_base_title("API Guide (old)")
        'api guide'
        >>> normalize_base_title("HTTP 2000")
        'http 2000'
    """
    t = title.strip()

    # Strip stale parenthetical suffix first
    t = _STALE_SUFFIX_RE.sub("", t).strip()

    # Strip trailing year — only when the remaining text qualifies
    m = _YEAR_SUFFIX_RE.search(t)
    if m and _year_qualifies(t[: m.start()].strip()):
        t = t[: m.start()].strip()

    # Strip trailing version
    t = _VERSION_SUFFIX_RE.sub("", t).strip()

    return t.lower()


def normalize_title(title: str) -> tuple[str, str | None, str | None]:
    """Normalize a document title, returning structured components.

    Returns ``(base_title, year_or_version_suffix, stale_suffix)`` where
    *base_title* is the lowered, stripped title with trailing year/version/
    stale-suffix removed.  Both ``normalize_base_title`` and this function
    use the same stripping logic and always agree on the base title.
    """
    t = title.strip()
    stale_suffix: str | None = None
    year_or_version: str | None = None

    # Check for stale parenthetical suffix first
    m = _STALE_SUFFIX_RE.search(t)
    if m:
        stale_suffix = m.group(0).strip()
        t = t[: m.start()].strip()

    # Check trailing year — only when qualifying context is present
    m = _YEAR_SUFFIX_RE.search(t)
    if m and _year_qualifies(t[: m.start()].strip()):
        year_or_version = m.group(0).strip()
        t = t[: m.start()].strip()
        # A version marker may precede the year (e.g. "Guide v1 2021"); strip it too.
        m2 = _VERSION_SUFFIX_RE.search(t)
        if m2:
            t = t[: m2.start()].strip()
    else:
        # No qualifying year — check for trailing version marker
        m = _VERSION_SUFFIX_RE.search(t)
        if m:
            year_or_version = m.group(0).strip()
            t = t[: m.start()].strip()

    return t.lower(), year_or_version, stale_suffix
