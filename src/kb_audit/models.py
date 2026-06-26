"""Core data models for the Knowledge Base Auditor."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


Status = Literal["current", "stale", "needs_review", "unknown"]

WorkflowState = Literal[
    "open", "acknowledged", "dismissed", "fixed", "snoozed", "accepted_risk",
]


@dataclass
class Document:
    """A normalized representation of a document from any source."""

    id: str
    title: str
    content: str
    source_type: str
    url: str | None = None
    last_modified: datetime | None = None
    metadata: dict = field(default_factory=dict)
    content_hash: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()


@dataclass
class StalenessSignal:
    """A single reason a document might be stale."""

    signal_type: str
    severity: Severity
    message: str
    details: dict = field(default_factory=dict)


def build_finding_key(source_type: str, document_id: str, status: str) -> str:
    """Compute the deterministic finding key for a source/document/status triple.

    Based on source_type, document id, and audit status.  Stable across
    rescans as long as the document identity and classification don't change.
    """
    raw = f"{source_type}:{document_id}:{status}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass
class AuditResult:
    """The full audit result for one document."""

    document: Document
    signals: list[StalenessSignal] = field(default_factory=list)
    suggested_replacement: Document | None = None
    status: Status = "unknown"
    confidence: float = 0.0
    confidence_reason: str = ""
    trust_metadata: dict = field(default_factory=dict)
    trust_evidence: dict = field(default_factory=dict)

    @property
    def overall_status(self) -> Status:
        """Backward-compatible alias for status."""
        return self.status

    @property
    def finding_key(self) -> str:
        """Deterministic key for tracking this finding across scans."""
        return build_finding_key(self.document.source_type, self.document.id, self.status)

    @property
    def evidence_hash(self) -> str:
        """Hash of the material evidence behind this finding.

        Changes when signal details, trust evidence summary, review risks,
        positive evidence, missing evidence, or recommended action change —
        triggering a reopen of dismissed/fixed findings.

        Includes stable, deterministic representations of signal details
        (sorted by type+severity+message+detail keys) so that e.g. a broken
        URL changing from one URL to another is detected.
        """
        parts: list[str] = []
        parts.append(self.status)

        # Signals: type + severity + message + sorted stable detail values
        sig_keys: list[str] = []
        for s in self.signals:
            detail_parts: list[str] = []
            for k in sorted(s.details.keys()):
                v = s.details[k]
                # Only include deterministic scalar/list values
                if isinstance(v, (str, int, float, bool)):
                    detail_parts.append(f"{k}={v}")
                elif isinstance(v, list):
                    detail_parts.append(f"{k}=[{','.join(str(x) for x in sorted(str(i) for i in v))}]")
            detail_str = ";".join(detail_parts)
            sig_keys.append(f"{s.signal_type}:{s.severity.value}:{s.message}:{detail_str}")
        parts.extend(sorted(sig_keys))

        # Trust evidence fields (all sorted for stability)
        parts.append(self.trust_evidence.get("summary", ""))
        parts.extend(sorted(self.trust_evidence.get("positive_evidence", [])))
        parts.extend(sorted(self.trust_evidence.get("review_risks", [])))
        parts.extend(sorted(self.trust_evidence.get("missing_evidence", [])))
        parts.append(self.trust_evidence.get("recommended_action", ""))

        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
