"""Detect references to outdated software versions."""

from __future__ import annotations

import re

from kb_audit.analyzers.base import Analyzer
from kb_audit.models import Document, Severity, StalenessSignal

# Phrases that indicate a version is being mentioned in historical/comparative
# context rather than as the document's actual version.
_HISTORICAL_PREFIXES = re.compile(
    r"(?:migrat\w+\s+from|upgrad\w+\s+from|previously|formerly|deprecated|"
    r"replaced\s+by|instead\s+of|no\s+longer|was\s+in|changed\s+from|"
    r"prior\s+to|before)\s+",
    re.IGNORECASE,
)


class VersionRefsAnalyzer(Analyzer):
    def __init__(
        self,
        current_versions: dict[str, str] | None = None,
        patterns: list[str] | None = None,
    ) -> None:
        self._current_versions = current_versions or {}
        self._patterns = [re.compile(p) for p in (patterns or [r"v\d+(?:\.\d+)*"])]

    @classmethod
    def name(cls) -> str:
        return "version_refs"

    def analyze(self, documents: list[Document]) -> dict[str, list[StalenessSignal]]:
        if not self._current_versions:
            return {}

        results: dict[str, list[StalenessSignal]] = {}

        for doc in documents:
            signals: list[StalenessSignal] = []
            content_lower = doc.content.lower()

            for label, current_ver in self._current_versions.items():
                label_lower = label.lower()
                for pattern in self._patterns:
                    for match in pattern.finditer(doc.content):
                        found_ver = match.group(0)
                        if found_ver == current_ver:
                            continue

                        # Check if the label appears near the version reference
                        start = max(0, match.start() - 100)
                        context = content_lower[start : match.end() + 50]

                        if label_lower not in context:
                            continue

                        # Check if this version mention is in a historical context
                        # (e.g., "migration from v1" in a v2 doc)
                        pre_start = max(0, match.start() - 60)
                        preceding = doc.content[pre_start : match.start()].strip()
                        if _HISTORICAL_PREFIXES.search(preceding):
                            continue

                        signals.append(
                            StalenessSignal(
                                signal_type="version_ref",
                                severity=Severity.WARNING,
                                message=(
                                    f"References '{label} {found_ver}' "
                                    f"but current version is '{current_ver}'"
                                ),
                                details={
                                    "label": label,
                                    "found_version": found_ver,
                                    "current_version": current_ver,
                                },
                            )
                        )

            if signals:
                results[doc.id] = signals

        return results
