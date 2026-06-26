"""Staleness detection based on document age."""

from __future__ import annotations

from datetime import datetime, timezone

from kb_audit.analyzers.base import Analyzer
from kb_audit.models import Document, Severity, StalenessSignal


class TimestampAnalyzer(Analyzer):
    def __init__(self, warning_days: int = 90, critical_days: int = 180) -> None:
        self._warning_days = warning_days
        self._critical_days = critical_days

    @classmethod
    def name(cls) -> str:
        return "timestamp"

    def analyze(self, documents: list[Document]) -> dict[str, list[StalenessSignal]]:
        now = datetime.now(timezone.utc)
        results: dict[str, list[StalenessSignal]] = {}

        for doc in documents:
            if doc.last_modified is None:
                results[doc.id] = [
                    StalenessSignal(
                        signal_type="age",
                        severity=Severity.WARNING,
                        message="No last-modified date available",
                        details={},
                    )
                ]
                continue

            age_days = (now - doc.last_modified).days
            signals: list[StalenessSignal] = []

            if age_days >= self._critical_days:
                signals.append(
                    StalenessSignal(
                        signal_type="age",
                        severity=Severity.CRITICAL,
                        message=f"Document is {age_days} days old (threshold: {self._critical_days})",
                        details={"age_days": age_days, "threshold": self._critical_days},
                    )
                )
            elif age_days >= self._warning_days:
                signals.append(
                    StalenessSignal(
                        signal_type="age",
                        severity=Severity.WARNING,
                        message=f"Document is {age_days} days old (threshold: {self._warning_days})",
                        details={"age_days": age_days, "threshold": self._warning_days},
                    )
                )

            if signals:
                results[doc.id] = signals

        return results
