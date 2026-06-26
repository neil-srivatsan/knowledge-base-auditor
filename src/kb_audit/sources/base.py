"""Abstract base class for document sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from kb_audit.models import Document


class DocumentSource(ABC):
    """Base class that all document sources must implement."""

    @abstractmethod
    def fetch_documents(self) -> Iterator[Document]:
        """Yield documents from this source."""

    @classmethod
    @abstractmethod
    def source_type(cls) -> str:
        """Return a short identifier for this source type."""
