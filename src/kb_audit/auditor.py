"""Core orchestration — wires sources, analyzers, and reporters together."""

from __future__ import annotations

import logging
from collections.abc import Callable

from kb_audit.analyzers.base import Analyzer
from kb_audit.db import LeaseLostError
from kb_audit.storage.contracts import AuditStorage
from kb_audit.models import AuditResult, Document, Severity, StalenessSignal
from kb_audit.reporters.base import Reporter
from kb_audit.sources.base import DocumentSource
from kb_audit.titles import normalize_title
from kb_audit.trust import classify, compute_audit_actionability, compute_incoming_ref_counts, parse_applicability_scope

logger = logging.getLogger(__name__)


class Auditor:
    def __init__(
        self,
        sources: list[DocumentSource],
        analyzers: list[Analyzer],
        reporters: list[Reporter],
        db: AuditStorage | None = None,
    ) -> None:
        self._sources = sources
        self._analyzers = analyzers
        self._reporters = reporters
        self._db = db

    def run(
        self,
        lease_check: Callable[[], None] | None = None,
        owner_token: str | None = None,
    ) -> list[AuditResult]:
        """Execute the full audit pipeline.

        *lease_check* is an optional callable that raises ``LeaseLostError``
        when the worker's scan lease is no longer valid.  It is called at key
        orchestration boundaries so that a worker whose lease has been taken
        over by another process aborts instead of overwriting the new owner's
        state.

        Scan lifecycle guarantees:
        - Every started scan reaches exactly one terminal state: completed or
          failed.
        - ``LeaseLostError`` propagates without calling ``fail_scan``; the
          replacement worker cleans up the abandoned scan during takeover.
        - Reporters run only after successful persistence and completion.
        """
        scan_id: int | None = None

        if self._db:
            if lease_check:
                lease_check()  # boundary: before scan initialisation writes
            scan_id = self._db.start_scan(owner_token=owner_token)

        # Everything from here is wrapped so that any non-LeaseLostError
        # exception marks the scan as failed and removes partial data.
        scan_completed = False
        results: list[AuditResult] = []
        try:
            previous_hashes: dict[str, str] = {}
            if self._db and scan_id is not None:
                previous_hashes = self._db.get_previous_hashes()

            # 1. Fetch documents from all sources, skipping unchanged ones
            documents: list[Document] = []
            skipped_ids: list[str] = []
            for source in self._sources:
                for doc in source.fetch_documents():
                    prev_hash = previous_hashes.get(doc.id)
                    if prev_hash and prev_hash == doc.content_hash:
                        skipped_ids.append(doc.id)
                        logger.info("Skipped (unchanged): %s", doc.title)
                        if self._db and scan_id is not None:
                            if lease_check:
                                lease_check()  # boundary: immediately before store_document
                            self._db.store_document(scan_id, doc, owner_token=owner_token)
                        continue
                    documents.append(doc)
                    logger.info("Fetched: %s (%s)", doc.title, doc.source_type)
                    if self._db and scan_id is not None:
                        if lease_check:
                            lease_check()  # boundary: immediately before store_document
                        self._db.store_document(scan_id, doc, owner_token=owner_token)

            logger.info(
                "Total documents: %d new/changed, %d unchanged (carried forward)",
                len(documents), len(skipped_ids),
            )

            # Carry forward previous audit results for unchanged documents
            if self._db and scan_id is not None and skipped_ids:
                if lease_check:
                    lease_check()  # boundary: before carry_forward_results
                carried = self._db.carry_forward_results(
                    scan_id, skipped_ids, owner_token=owner_token
                )
                logger.info("Carried forward %d previous result(s)", carried)

            if not documents:
                if self._db and scan_id is not None:
                    # All docs unchanged: sync workflow and complete atomically.
                    carried_results = self._db.load_audit_results(scan_id, skipped_ids)
                    all_doc_ids = set(skipped_ids)
                    if lease_check:
                        lease_check()  # boundary: before complete_scan_with_findings (all-unchanged path)
                    wf_stats = self._db.complete_scan_with_findings(
                        scan_id, len(skipped_ids), carried_results,
                        scanned_doc_ids=all_doc_ids,
                        reanalyzed_doc_ids=set(),  # nothing was re-analyzed
                        owner_token=owner_token,
                    )
                    if any(wf_stats.values()):
                        logger.info(
                            "Workflow sync: %d new, %d updated, %d reopened, %d auto-fixed",
                            wf_stats["new"], wf_stats["updated"], wf_stats["reopened"],
                            wf_stats["auto_fixed"],
                        )
                    scan_completed = True
                return []

            # 2. Run all analyzers
            all_signals: dict[str, list[StalenessSignal]] = {}
            for analyzer in self._analyzers:
                if lease_check:
                    lease_check()  # boundary: before each analyzer
                logger.info("Running analyzer: %s", analyzer.name())
                analyzer_signals = analyzer.analyze(documents)
                for doc_id, sigs in analyzer_signals.items():
                    all_signals.setdefault(doc_id, []).extend(sigs)

            # 3. Compute cross-document reference context
            incoming_refs = compute_incoming_ref_counts(all_signals)

            # 4. Classify each document using the trust classifier
            doc_by_id = {doc.id: doc for doc in documents}
            scan_titles = {doc.id: doc.title for doc in documents}
            # Pre-compute applicability scopes once so classify() can suppress
            # scan-local version/year supersession when sibling scopes differ.
            scan_scopes = {
                doc.id: scope
                for doc in documents
                for scope in (parse_applicability_scope(doc),)
                if scope
            }
            raw_results: dict[str, AuditResult] = {}
            for doc in documents:
                signals = all_signals.get(doc.id, [])
                verdict = classify(
                    doc, signals, incoming_refs.get(doc.id, 0),
                    scan_titles=scan_titles,
                    scan_scopes=scan_scopes if scan_scopes else None,
                )
                result = AuditResult(
                    document=doc,
                    signals=signals,
                    status=verdict.status,
                    confidence=verdict.confidence,
                    confidence_reason=verdict.reason,
                    trust_metadata={
                        "last_reviewed": verdict.metadata.last_reviewed,
                        "last_modified": verdict.metadata.last_modified,
                        "owner": verdict.metadata.owner,
                        "declared_status": verdict.metadata.declared_status,
                        "replaced_by": verdict.metadata.replaced_by,
                        "deprecated_as_of": verdict.metadata.deprecated_as_of,
                        "canonical": verdict.metadata.canonical,
                        "review_cadence": verdict.metadata.review_cadence,
                        "applies_to": verdict.metadata.applies_to,
                        "applicability_scope": verdict.metadata.applicability_scope,
                        "lifecycle": verdict.metadata.lifecycle,
                        "lifecycle_evidence": verdict.metadata.lifecycle_evidence,
                    },
                    trust_evidence={
                        "summary": verdict.evidence.summary,
                        "positive_evidence": verdict.evidence.positive_evidence,
                        "review_risks": verdict.evidence.review_risks,
                        "missing_evidence": verdict.evidence.missing_evidence,
                        "recommended_action": verdict.evidence.recommended_action,
                    },
                )
                for signal in result.signals:
                    replacement_id = signal.details.get("duplicate_of") or signal.details.get(
                        "similar_to"
                    )
                    if replacement_id and replacement_id in doc_by_id:
                        result.suggested_replacement = doc_by_id[replacement_id]
                        break
                raw_results[doc.id] = result

            # 4b. Downgrade docs whose "current" status relies solely on
            #     incoming references from stale sources.
            stale_ids = {r.document.id for r in raw_results.values() if r.status == "stale"}
            for result in raw_results.values():
                if result.status != "current":
                    continue
                # Find which docs reference this one
                referencing_ids: set[str] = set()
                for src_id, sigs in all_signals.items():
                    for s in sigs:
                        if (s.signal_type == "resolved_reference"
                                and s.details.get("resolved_doc_id") == result.document.id):
                            referencing_ids.add(src_id)
                if not referencing_ids:
                    continue
                non_stale_refs = referencing_ids - stale_ids
                if not non_stale_refs and len(referencing_ids) >= 2:
                    has_body_trust = any(
                        kw in (result.trust_metadata.get("declared_status") or "").lower()
                        for kw in ("current",)
                    ) or result.trust_metadata.get("canonical")
                    if not has_body_trust:
                        result.status = "needs_review"
                        result.confidence = 0.50
                        result.confidence_reason = (
                            f"All {len(referencing_ids)} incoming references are from stale documents"
                        )
                        risks = result.trust_evidence.get("review_risks", [])
                        risks.append(f"All {len(referencing_ids)} incoming references are from stale documents")
                        result.trust_evidence["review_risks"] = risks
                        result.trust_evidence["summary"] = (
                            "Needs review because all incoming references are from stale documents."
                        )
                        result.trust_evidence["recommended_action"] = "Review before relying on this document"

            # 5. Boost/promote: if stale docs point to a target via version_marker
            #    or duplicate signals, boost/promote the target.
            _HARD_RISK_TYPES = {
                "unresolved_reference", "ambiguous_reference", "broken_link",
                "near_duplicate", "duplicate",
            }

            ref_stale_count: dict[str, int] = {}
            for result in raw_results.values():
                if result.status != "stale":
                    continue
                result.confidence = min(result.confidence, 0.95)
                for s in result.signals:
                    ref_id = s.details.get("duplicate_of") or s.details.get(
                        "similar_to"
                    ) or s.details.get("current_doc_id")
                    if ref_id and ref_id in raw_results:
                        ref_stale_count[ref_id] = ref_stale_count.get(ref_id, 0) + 1
                        break

            for doc_id, n_stale in ref_stale_count.items():
                target = raw_results[doc_id]
                total_siblings = n_stale + 1

                if target.status == "current":
                    has_risks = bool(target.trust_evidence.get("review_risks"))
                    if has_risks:
                        target.status = "needs_review"
                    else:
                        target.confidence = 1.0
                    target.confidence_reason = (
                        f"{n_stale} other version{'s' if n_stale != 1 else ''} "
                        f"identified as stale relative to this document"
                    )
                    pos = list(target.trust_evidence.get("positive_evidence", []))
                    pos.append(f"Latest version among {total_siblings} related pages in this scan")
                    pos.append(f"Supersedes {n_stale} older version{'s' if n_stale != 1 else ''}")
                    target.trust_evidence["positive_evidence"] = pos
                    target.trust_evidence["summary"] = (
                        "Recommended because this is the latest version among related pages in this scan."
                    )

                elif target.status == "unknown":
                    has_hard_risk = any(
                        s.signal_type in _HARD_RISK_TYPES
                        or (s.signal_type == "age" and s.severity == Severity.CRITICAL)
                        for s in target.signals
                    )
                    has_review_risks = bool(target.trust_evidence.get("review_risks"))
                    if not has_hard_risk and not has_review_risks:
                        confidence = 0.75 if n_stale == 1 else min(0.85, 0.75 + n_stale * 0.05)
                        if (target.trust_metadata.get("declared_status") or "").lower() == "current":
                            confidence = min(0.95, confidence + 0.10)
                        target.status = "current"
                        target.confidence = round(confidence, 2)
                        target.confidence_reason = (
                            f"Latest version among {total_siblings} related pages in this scan; "
                            f"supersedes {n_stale} older version{'s' if n_stale != 1 else ''}"
                        )
                        target.trust_evidence = {
                            "summary": "Recommended because this is the latest version among related pages in this scan.",
                            "positive_evidence": [
                                f"Latest version among {total_siblings} related pages in this scan",
                                f"Supersedes {n_stale} older version{'s' if n_stale != 1 else ''}",
                            ],
                            "review_risks": [],
                            "missing_evidence": target.trust_evidence.get("missing_evidence", []),
                            "recommended_action": "Use as trusted reference",
                        }

            # 5b. Compute actionability for every result now that all statuses
            #     are final.  Merge actionability fields into trust_metadata so
            #     they are persisted and serialised to API payloads without any
            #     schema migration.
            for doc in documents:
                result = raw_results[doc.id]
                doc_base, _, _ = normalize_title(doc.title)
                has_siblings = any(
                    normalize_title(scan_titles[did])[0] == doc_base
                    for did in scan_titles
                    if did != doc.id
                )
                n_stale = ref_stale_count.get(doc.id, 0)
                is_replacement = n_stale > 0
                actionability = compute_audit_actionability(
                    status=result.status,
                    trust_metadata=result.trust_metadata,
                    trust_evidence=result.trust_evidence,
                    signals=result.signals,
                    incoming_ref_count=incoming_refs.get(doc.id, 0),
                    has_version_siblings=has_siblings,
                    is_suggested_replacement=is_replacement,
                    n_stale_siblings=n_stale,
                )
                result.trust_metadata.update(actionability)

            results = list(raw_results.values())

            if self._db and scan_id is not None:
                for result in results:
                    if lease_check:
                        lease_check()  # boundary: immediately before each store_result
                    self._db.store_result(scan_id, result, owner_token=owner_token)

            if self._db and scan_id is not None:
                if lease_check:
                    lease_check()  # boundary: before complete_scan_with_findings (normal path)
                carried_results = self._db.load_audit_results(scan_id, skipped_ids) if skipped_ids else []
                all_results = results + carried_results
                all_doc_ids = {doc.id for doc in documents} | set(skipped_ids)
                reanalyzed_ids = {doc.id for doc in documents}
                wf_stats = self._db.complete_scan_with_findings(
                    scan_id, len(documents) + len(skipped_ids), all_results,
                    scanned_doc_ids=all_doc_ids,
                    reanalyzed_doc_ids=reanalyzed_ids,
                    owner_token=owner_token,
                )
                if any(wf_stats.values()):
                    logger.info(
                        "Workflow sync: %d new, %d updated, %d reopened, %d auto-fixed",
                        wf_stats["new"], wf_stats["updated"], wf_stats["reopened"],
                        wf_stats["auto_fixed"],
                    )
                scan_completed = True

        except LeaseLostError:
            # Stale worker must not clean up — the replacement handles
            # abandoned scans atomically during lease takeover.
            raise
        except Exception as exc:
            if self._db and scan_id is not None and not scan_completed:
                try:
                    self._db.fail_scan(scan_id, str(exc), owner_token=owner_token)
                except LeaseLostError:
                    pass  # lease already lost; replacement handles cleanup
                except Exception:
                    logger.exception("Could not mark scan %d as failed", scan_id)
            raise

        # 5b. Prune old scans — non-fatal maintenance after successful completion.
        if self._db and scan_id is not None and scan_completed:
            if lease_check:
                lease_check()  # boundary: before prune_scans
            try:
                pruned = self._db.prune_scans(keep=20, owner_token=owner_token)
                if pruned:
                    logger.info("Pruned %d old scan(s)", pruned)
            except Exception:
                logger.exception("Pruning failed — scan is still marked completed")

        # 6. Report — runs only after successful persistence and completion.
        for reporter in self._reporters:
            if lease_check:
                lease_check()  # boundary: before each reporter
            reporter.report(results)

        return results
