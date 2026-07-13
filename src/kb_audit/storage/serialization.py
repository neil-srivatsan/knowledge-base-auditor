"""Storage serialization helpers for kb_audit.

All JSON encode/decode logic for the SQLite persistence layer lives here.
No domain logic (trust classification, finding key computation, evidence
hashing) belongs in this module — those contracts stay in kb_audit.models.
"""

from __future__ import annotations

import json
import re

from kb_audit.models import Severity, StalenessSignal


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------

def sanitize_error(error: str | None) -> str | None:
    """Redact likely credentials then truncate to 500 chars for DB storage."""
    if error is None:
        return None
    text = str(error)
    # Redact Authorization header values (Bearer / Basic tokens).
    text = re.sub(r"\b(Bearer|Basic)\s+\S+", r"\1 [REDACTED]", text, flags=re.IGNORECASE)
    # Redact key=value style credential assignments.
    # Stop at & or whitespace to avoid consuming adjacent URL parameters.
    text = re.sub(
        r"\b(token|api[_-]?key|password|passwd|secret|credential)\s*=\s*[^&\s]+",
        r"\1=[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # Redact key: value style (colon form — HTTP headers, YAML, log output).
    text = re.sub(
        r"\b(token|api[_-]?key|password|passwd|secret|credential)\s*:\s*[^&\s]+",
        r"\1: [REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    # Redact URL query-string credential parameters.
    # Stop at & or whitespace so adjacent params are preserved.
    text = re.sub(
        r"([?&](?:token|api[_-]?key|password|passwd|secret|credential)=)[^&\s]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text[:500]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def serialize_signals(signals: list[StalenessSignal]) -> str:
    """JSON-encode a list of StalenessSignal objects for DB storage."""
    return json.dumps([
        {
            "signal_type": s.signal_type,
            "severity": s.severity.value,
            "message": s.message,
            "details": s.details,
        }
        for s in signals
    ])


def deserialize_signals(blob: str | None) -> list[StalenessSignal]:
    """Decode a JSON blob into a list of StalenessSignal objects."""
    if not blob:
        return []
    raw: list[dict] = json.loads(blob)
    return [
        StalenessSignal(
            signal_type=s["signal_type"],
            severity=Severity(s["severity"]),
            message=s["message"],
            details=s.get("details", {}),
        )
        for s in raw
    ]


def deserialize_signal_records(blob: str | None) -> list[dict]:
    """Decode a JSON blob into a list of raw signal dicts (for API responses)."""
    if not blob:
        return []
    return json.loads(blob)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Trust data
# ---------------------------------------------------------------------------

def serialize_trust_data(trust_metadata: dict, trust_evidence: dict) -> str:
    """JSON-encode trust metadata and evidence into a single DB blob."""
    return json.dumps({"metadata": trust_metadata, "evidence": trust_evidence})


def deserialize_trust_data(blob: str | None) -> tuple[dict, dict]:
    """Decode a trust data blob into (trust_metadata, trust_evidence) tuple."""
    data = json.loads(blob) if blob else {}
    return data.get("metadata", {}), data.get("evidence", {})


def deserialize_trust_data_blob(blob: str | None) -> dict:
    """Decode a trust data blob into its raw dict form for API responses."""
    return json.loads(blob) if blob else {}


# ---------------------------------------------------------------------------
# Document metadata
# ---------------------------------------------------------------------------

def serialize_document_metadata(metadata: dict) -> str:
    """JSON-encode document metadata for DB storage."""
    return json.dumps(metadata)


def deserialize_document_metadata(blob: str | None) -> dict:
    """Decode a JSON blob into document metadata dict."""
    if not blob:
        return {}
    return json.loads(blob)  # type: ignore[no-any-return]
