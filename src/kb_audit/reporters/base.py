"""Abstract base class for reporters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from kb_audit.models import AuditResult


class Reporter(ABC):
    @abstractmethod
    def report(self, results: list[AuditResult]) -> None:
        """Output audit results."""
