"""Abstract base class for analyzers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from kb_audit.models import Document, StalenessSignal


class Analyzer(ABC):
    """Base class that all analyzers must implement."""

    @abstractmethod
    def analyze(self, documents: list[Document]) -> dict[str, list[StalenessSignal]]:
        """Analyze documents and return doc_id -> list of signals."""

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Short identifier for this analyzer."""
