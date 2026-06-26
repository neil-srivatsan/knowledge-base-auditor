"""JSON output reporter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kb_audit.models import AuditResult
from kb_audit.reporters.base import Reporter


class JsonReporter(Reporter):
    def __init__(self, output_path: str | Path | None = None) -> None:
        self._output_path = Path(output_path) if output_path else None

    def report(self, results: list[AuditResult]) -> None:
        data = {
            "total": len(results),
            "stale": sum(1 for r in results if r.overall_status == "stale"),
            "needs_review": sum(1 for r in results if r.overall_status == "needs_review"),
            "unknown": sum(1 for r in results if r.overall_status == "unknown"),
            "current": sum(1 for r in results if r.overall_status == "current"),
            "documents": [self._serialize_result(r) for r in results],
        }

        output = json.dumps(data, indent=2, default=str)

        if self._output_path:
            self._output_path.write_text(output)
        else:
            sys.stdout.write(output + "\n")

    def _serialize_result(self, result: AuditResult) -> dict:
        doc = result.document
        return {
            "id": doc.id,
            "title": doc.title,
            "source_type": doc.source_type,
            "url": doc.url,
            "last_modified": doc.last_modified.isoformat() if doc.last_modified else None,
            "content_hash": doc.content_hash,
            "overall_status": result.overall_status,
            "confidence": result.confidence,
            "confidence_reason": result.confidence_reason,
            "signals": [
                {
                    "type": s.signal_type,
                    "severity": s.severity.value,
                    "message": s.message,
                    "details": s.details,
                }
                for s in result.signals
            ],
            "suggested_replacement": (
                {
                    "id": result.suggested_replacement.id,
                    "title": result.suggested_replacement.title,
                    "url": result.suggested_replacement.url,
                }
                if result.suggested_replacement
                else None
            ),
        }
