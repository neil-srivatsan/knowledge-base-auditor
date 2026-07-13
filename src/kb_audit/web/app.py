"""FastAPI web harness for the Knowledge Base Auditor."""

from __future__ import annotations

import logging
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from kb_audit.analyzers.base import Analyzer
from kb_audit.analyzers.broken_links import BrokenLinkAnalyzer
from kb_audit.analyzers.internal_links import InternalLinkAnalyzer
from kb_audit.analyzers.references import ReferenceAnalyzer
from kb_audit.analyzers.similarity import SimilarityAnalyzer
from kb_audit.analyzers.timestamp import TimestampAnalyzer
from kb_audit.analyzers.version_refs import VersionRefsAnalyzer
from kb_audit.auditor import Auditor
from kb_audit.config import Config
from kb_audit.db import LeaseLostError, ScanLeaseContext, _UNSET
from kb_audit.storage import AuditStorage, create_storage
from kb_audit.models import build_finding_key
from kb_audit.sources.confluence import ConfluenceSource
from kb_audit.sources.demo import DemoSource
from kb_audit.sources.notion import NotionSource, extract_page_id_from_url, find_notion_page_by_title


def _workflow_is_actionable(wf: dict) -> bool:
    """Return True if this finding currently requires human action.

    open and acknowledged are always actionable.  snoozed is actionable only
    once the snooze date has passed (expired snooze).  All terminal states
    (fixed, dismissed, accepted_risk) and future snoozes are not actionable.
    """
    ws = wf["workflow_state"]
    if ws in ("open", "acknowledged"):
        return True
    if ws == "snoozed":
        snoozed_until = wf.get("snoozed_until")
        if not snoozed_until:
            return True  # no expiry set → treat as actionable
        now = datetime.now(timezone.utc).isoformat()
        return bool(snoozed_until <= now)
    return False

logger = logging.getLogger(__name__)

app = FastAPI(title="Knowledge Base Auditor")


@dataclass
class _AppConfig:
    """Runtime configuration set once at startup or in tests."""
    demo_mode: bool = False
    database_path: str | None = None
    host: str = "127.0.0.1"
    port: int = 8080

_app_config = _AppConfig()


def configure_app(
    *,
    demo_mode: bool = False,
    database_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> None:
    """Configure application runtime mode. Call before serving or in tests."""
    _app_config.demo_mode = demo_mode
    _app_config.database_path = database_path
    _app_config.host = host
    _app_config.port = port

TEMPLATES_DIR = Path(__file__).parent / "templates"

# UUID format: 8-4-4-4-12 hex chars
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Confluence page IDs are positive integers
_CONFLUENCE_PAGE_ID_RE = re.compile(r"^\d+$")


def _notion_page_id(value: str) -> str | None:
    """Return a Notion page UUID from a URL or UUID string, or None for plain text."""
    extracted = extract_page_id_from_url(value)
    if extracted:
        return extracted
    if _UUID_RE.match(value.strip()):
        return value.strip()
    return None


def _resolve_page_tree_target(
    cfg: Config,
    source: str | None,
    root_page: str | None,
    confluence_page_id: str | None,
) -> tuple[str | None, str | None]:
    """Validate and resolve page-tree targets before the background scan starts.

    Returns ``(resolved_root_page, resolved_confluence_page_id)``.
    Raises ``ValueError`` with a user-friendly message for invalid inputs.
    The API key is never included in raised messages or log output.
    """
    if source == "notion" and root_page:
        page_id = _notion_page_id(root_page)
        if page_id:
            return (page_id, confluence_page_id)
        # Plain text — resolve via title search
        logger.info("Notion page-tree target is plain text; resolving by title")
        resolved = find_notion_page_by_title(cfg.notion_api_key, root_page)
        logger.info("Notion title resolved to a page ID successfully")
        return (resolved, confluence_page_id)

    if source == "confluence" and confluence_page_id:
        if not _CONFLUENCE_PAGE_ID_RE.match(confluence_page_id.strip()):
            raise ValueError(
                f"Confluence page tree requires a numeric page ID (e.g. 12345678), "
                f"not \u2018{confluence_page_id}\u2019. "
                "To search by content, use the CQL scope instead."
            )
        return (root_page, confluence_page_id)

    return (root_page, confluence_page_id)


class ScanRequest(BaseModel):
    query: str | None = None
    scope_type: str | None = None
    root_page: str | None = None
    database_id: str | None = None
    confluence_space: str | None = None
    confluence_page_id: str | None = None


class WorkflowUpdateRequest(BaseModel):
    state: str | None = None
    note: str | None = None
    assigned_owner: str | None = None
    due_date: str | None = None
    snoozed_until: str | None = None
    dismissal_reason: str | None = None


def _build_analyzers(cfg: Config) -> list[Analyzer]:
    return [
        TimestampAnalyzer(
            warning_days=cfg.analyzers.timestamp.warning_days,
            critical_days=cfg.analyzers.timestamp.critical_days,
        ),
        SimilarityAnalyzer(threshold=cfg.analyzers.similarity.threshold),
        VersionRefsAnalyzer(
            current_versions=cfg.analyzers.version_refs.current_versions,
            patterns=list(cfg.analyzers.version_refs.patterns),
        ),
        BrokenLinkAnalyzer(),
        InternalLinkAnalyzer(),
        ReferenceAnalyzer(),
    ]


def _get_source_info(cfg: Config) -> dict:
    """Return source detection result without exposing secrets.

    Priority: Confluence (base_url + api_token) > Notion (api_key) > unconfigured.
    Matches the source-selection logic in _run_scan.
    """
    if _app_config.demo_mode:
        return {
            "source": "demo",
            "source_label": "Demo workspace",
            "configured": True,
            "configuration_error": None,
            "target": None,
        }
    if cfg.confluence.base_url and cfg.confluence.api_token:
        return {
            "source": "confluence",
            "source_label": "Confluence Cloud",
            "configured": True,
            "configuration_error": None,
            "target": {
                "base_url": cfg.confluence.base_url,
                "space_key": cfg.confluence.space_key,
                "page_id": cfg.confluence.page_id,
            },
        }
    if cfg.notion_api_key:
        return {
            "source": "notion",
            "source_label": "Notion",
            "configured": True,
            "configuration_error": None,
            "target": {
                "root_page_id": cfg.notion.root_page_id,
                "database_id": cfg.notion.database_id,
            },
        }
    return {
        "source": None,
        "source_label": None,
        "configured": False,
        "configuration_error": (
            "No source configured. "
            "Set CONFLUENCE_BASE_URL + CONFLUENCE_API_TOKEN, or NOTION_API_KEY."
        ),
        "target": None,
    }


def _get_db() -> AuditStorage:
    if _app_config.database_path is not None:
        db = create_storage(_app_config.database_path)
    else:
        cfg = Config.load()
        db = create_storage(cfg.database_url)
    db.connect()
    return db


def _run_scan(
    owner_token: str,
    query: str | None,
    scope_type: str | None = None,
    root_page: str | None = None,
    database_id: str | None = None,
    confluence_space: str | None = None,
    confluence_page_id: str | None = None,
    demo_mode: bool = False,
) -> None:
    db: AuditStorage | None = None
    source: NotionSource | ConfluenceSource | DemoSource | None = None
    try:
        cfg = Config.load()

        if demo_mode:
            source = DemoSource()
        elif cfg.confluence.base_url and cfg.confluence.api_token:
            # Route Confluence scope types
            if scope_type == "space":
                source = ConfluenceSource(
                    base_url=cfg.confluence.base_url,
                    email=cfg.confluence.email,
                    api_token=cfg.confluence.api_token,
                    space_key=confluence_space,
                )
            elif scope_type == "page_tree":
                source = ConfluenceSource(
                    base_url=cfg.confluence.base_url,
                    email=cfg.confluence.email,
                    api_token=cfg.confluence.api_token,
                    page_id=confluence_page_id,
                )
            elif scope_type == "cql":
                source = ConfluenceSource(
                    base_url=cfg.confluence.base_url,
                    email=cfg.confluence.email,
                    api_token=cfg.confluence.api_token,
                    query=query or None,
                )
            else:
                source = ConfluenceSource(
                    base_url=cfg.confluence.base_url,
                    email=cfg.confluence.email,
                    api_token=cfg.confluence.api_token,
                    space_key=cfg.confluence.space_key,
                    page_id=cfg.confluence.page_id,
                    query=query or None,
                )
        else:
            if scope_type == "page_tree":
                source = NotionSource(api_key=cfg.notion_api_key, root_page_id=root_page)
            elif scope_type == "database":
                source = NotionSource(
                    api_key=cfg.notion_api_key,
                    database_id=database_id,
                )
            elif scope_type == "query":
                source = NotionSource(
                    api_key=cfg.notion_api_key,
                    query=query or None,
                )
            else:
                source = NotionSource(
                    api_key=cfg.notion_api_key,
                    root_page_id=cfg.notion.root_page_id,
                    database_id=cfg.notion.database_id,
                    query=query or None,
                )

        analyzers = _build_analyzers(cfg)

        if _app_config.database_path is not None:
            db = create_storage(_app_config.database_path)
        else:
            db = create_storage(cfg.database_url)
        db.connect()

        with ScanLeaseContext(db, owner_token) as ctx:
            try:
                auditor = Auditor(
                    sources=[source],
                    analyzers=analyzers,
                    reporters=[],
                    db=db,
                )
                auditor.run(lease_check=ctx.check, owner_token=owner_token)
                history = db.get_scan_history(limit=1)
                if history:
                    ctx.last_scan_id = history[0]["scan_id"]
            except LeaseLostError:
                logger.warning(
                    "Scan aborted: lease ownership lost for token %s", owner_token
                )
            except Exception as e:
                ctx.error = f"{type(e).__name__}: {e}"
                logger.error("Scan failed: %s\n%s", e, traceback.format_exc())
            finally:
                source.close()
    except Exception:
        logger.exception("Scan setup failed for token %s", owner_token)
        if db is None:
            try:
                if _app_config.database_path is not None:
                    db_end = create_storage(_app_config.database_path)
                else:
                    cfg_end = Config.load()
                    db_end = create_storage(cfg_end.database_url)
                db_end.connect()
                db_end.end_scan(owner_token, None, "Scan setup failed")
            except Exception:
                logger.exception(
                    "Could not release lease after setup failure for token %s", owner_token
                )
    finally:
        if db is not None:
            db.close()


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        (TEMPLATES_DIR / "index.html").read_text(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/status")
async def status():
    cfg = Config.load()
    info = _get_source_info(cfg)
    db = _get_db()
    try:
        state = db.get_scan_state()
    finally:
        db.close()
    return {
        "scan_in_progress": state["in_progress"],
        "last_scan_id": state["last_scan_id"],
        "scan_error": state["scan_error"],
        "demo_mode": _app_config.demo_mode,
        **info,
    }


@app.post("/api/scans")
async def start_scan(request: ScanRequest, bg: BackgroundTasks):
    cfg = Config.load()
    info = _get_source_info(cfg)

    if not _app_config.demo_mode:
        if not info["configured"]:
            return JSONResponse({"error": info["configuration_error"]}, status_code=422)

        resolved_root_page = request.root_page
        resolved_conf_page_id = request.confluence_page_id
        if request.scope_type == "page_tree":
            try:
                resolved_root_page, resolved_conf_page_id = _resolve_page_tree_target(
                    cfg, info["source"], request.root_page, request.confluence_page_id
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=422)
    else:
        resolved_root_page = None
        resolved_conf_page_id = None

    db = _get_db()
    try:
        owner_token = db.try_start_scan()
    finally:
        db.close()

    if owner_token is None:
        return JSONResponse(
            {"error": "A scan is already in progress", "scan_in_progress": True},
            status_code=409,
        )

    bg.add_task(
        _run_scan,
        owner_token,
        request.query,
        request.scope_type,
        resolved_root_page,
        request.database_id,
        request.confluence_space,
        resolved_conf_page_id,
        demo_mode=_app_config.demo_mode,
    )
    return {"status": "started", "scan_in_progress": True}


@app.get("/api/scans")
async def list_scans():
    db = _get_db()
    try:
        return db.get_scan_history(limit=20)
    finally:
        db.close()


@app.delete("/api/scans")
async def clear_scans():
    db = _get_db()
    try:
        cleared = db.clear_all_if_idle()
        if not cleared:
            return JSONResponse(
                {"error": "Cannot clear data while a scan is in progress"},
                status_code=409,
            )
        return {"status": "cleared"}
    finally:
        db.close()


@app.get("/api/scans/{scan_id}")
async def get_scan(scan_id: int):
    db = _get_db()
    try:
        results = db.get_scan_results(scan_id)
        history = db.get_scan_history(limit=100)
        scan_meta = next((s for s in history if s["scan_id"] == scan_id), None)
        # Find the previous scan for diff
        changes: list[dict] = []
        prev_scan = None
        found = False
        for s in history:
            if found:
                prev_scan = s
                break
            if s["scan_id"] == scan_id:
                found = True
        if prev_scan:
            changes = db.get_scan_diff(scan_id, prev_scan["scan_id"])

        # Enrich results with workflow state (need all states for badges).
        # Index by finding_key (not document_id) so that when a document's
        # classification changes — e.g. stale → needs_review — the older
        # finding for the previous status does not overwrite the current one.
        findings = db.get_findings(scan_id=scan_id, include_all=True)
        workflow_by_key: dict[str, dict] = {f["finding_key"]: f for f in findings}
        for r in results:
            key = build_finding_key(r["source_type"], r["id"], r["overall_status"])
            wf = workflow_by_key.get(key)
            r["workflow"] = {
                "finding_key": wf["finding_key"],
                "state": wf["workflow_state"],
                "note": wf.get("note", ""),
                "assigned_owner": wf.get("assigned_owner", ""),
                "due_date": wf.get("due_date"),
                "snoozed_until": wf.get("snoozed_until"),
                "is_actionable": _workflow_is_actionable(wf),
            } if wf else None

        return {
            "scan": scan_meta,
            "results": results,
            "changes": changes,
            "has_previous": prev_scan is not None,
            "workflow_summary": db.get_workflow_summary(scan_id=scan_id),
            "workflow_summary_all": db.get_workflow_summary(scan_id=scan_id, include_all=True),
        }
    finally:
        db.close()


def _requires_human_audit(r: dict) -> bool:
    """Return True if this scan result requires a human audit.

    Mirrors frontend getReviewRequirement() and DB _actionable_results() semantics:
      - current => never
      - explicit requires_human_audit=True => yes
      - explicit requires_human_audit=False => no
      - flag absent (legacy row) => status-based fallback
    """
    if r.get("overall_status") == "current":
        return False
    flag = (r.get("trust_metadata") or {}).get("requires_human_audit")
    if flag is True:
        return True
    if flag is False:
        return False
    return r.get("overall_status") in ("stale", "needs_review", "unknown")


@app.get("/api/scans/{scan_id}/report")
async def get_stale_report(scan_id: int, format: str = "json"):
    db = _get_db()
    try:
        results = db.get_scan_results(scan_id)
        history = db.get_scan_history(limit=100)
        scan_meta = next((s for s in history if s["scan_id"] == scan_id), None)
        stale = [r for r in results if r["overall_status"] == "stale"]
        needs_review = [r for r in results if r["overall_status"] == "needs_review"]
        unknown = [r for r in results if r["overall_status"] == "unknown"]
        flagged = stale + needs_review + unknown
        human_audit_docs = [r for r in flagged if _requires_human_audit(r)]

        if format == "text":
            lines = [
                f"Flagged Content Report — Scan #{scan_id}",
                f"Date: {scan_meta['started_at'][:16].replace('T', ' ') if scan_meta else 'N/A'}",
                f"Total documents scanned: {len(results)}",
                f"Stale documents: {len(stale)}",
                f"Documents needing review: {len(needs_review)}",
                f"Unknown documents: {len(unknown)}",
                f"Status-flagged documents: {len(flagged)}",
                f"Human audits required: {len(human_audit_docs)}",
                "",
            ]
            if not flagged:
                lines.append("No flagged content detected.")
            else:
                for i, r in enumerate(flagged, 1):
                    conf_pct = round(r["confidence"] * 100) if r["confidence"] else 0
                    status_label = r["overall_status"].replace("_", " ").upper()
                    audit_line = "Human audit: required" if _requires_human_audit(r) else "Human audit: not required"
                    lines.append(f"{i}. [{status_label}] {r['title']}")
                    if r.get("url"):
                        lines.append(f"   URL: {r['url']}")
                    lines.append(f"   Confidence: {conf_pct}%")
                    lines.append(f"   {audit_line}")
                    if r.get("confidence_reason"):
                        lines.append(f"   Reason: {r['confidence_reason']}")
                    if r.get("signals"):
                        for s in r["signals"]:
                            lines.append(f"   - [{s['severity'].upper()}] {s['message']}")
                    # Use structured trust metadata for last reviewed date
                    trust_meta = r.get("trust_metadata", {})
                    if trust_meta.get("last_reviewed"):
                        lines.append(f"   Last reviewed: {trust_meta['last_reviewed']}")
                    elif r.get("last_modified"):
                        lines.append(f"   Last modified: {r['last_modified'][:10]}")
                    # Include recommended action if available
                    trust_ev = r.get("trust_evidence", {})
                    if trust_ev.get("recommended_action"):
                        lines.append(f"   Recommended action: {trust_ev['recommended_action']}")
                    lines.append("")
            return PlainTextResponse("\n".join(lines))

        return {
            "scan_id": scan_id,
            "scan": scan_meta,
            "total_documents": len(results),
            "stale_count": len(stale),
            "needs_review_count": len(needs_review),
            "unknown_count": len(unknown),
            "stale_documents": stale,
            "needs_review_documents": needs_review,
            "unknown_documents": unknown,
            "status_flagged_count": len(flagged),
            "human_audit_required_count": len(human_audit_docs),
            "human_audit_required_documents": human_audit_docs,
        }
    finally:
        db.close()


@app.get("/api/references/summary")
async def references_summary(scan_id: int | None = None):
    """Return a per-document reference summary from the most recent (or given) scan."""
    db = _get_db()
    try:
        if scan_id is None:
            history = db.get_scan_history(limit=1)
            if not history:
                return {"error": "No scans found", "documents": []}
            scan_id = history[0]["scan_id"]

        results = db.get_scan_results(scan_id)

        # Build per-document outgoing refs from signals
        doc_outgoing: dict[str, list[dict]] = {}
        for r in results:
            outgoing: list[dict] = []
            for s in r.get("signals", []):
                if s["signal_type"] == "resolved_reference":
                    outgoing.append({
                        "referenced_text": s["details"]["referenced_title"],
                        "resolved_title": s["details"]["resolved_title"],
                        "resolved_doc_id": s["details"]["resolved_doc_id"],
                        "status": "resolved",
                    })
                elif s["signal_type"] == "unresolved_reference":
                    outgoing.append({
                        "referenced_text": s["details"]["referenced_title"],
                        "resolved_title": None,
                        "resolved_doc_id": None,
                        "status": "unresolved",
                    })
                elif s["signal_type"] == "ambiguous_reference":
                    outgoing.append({
                        "referenced_text": s["details"]["referenced_title"],
                        "resolved_title": None,
                        "resolved_doc_id": None,
                        "status": "ambiguous",
                        "candidates": s["details"].get("matching_titles", []),
                    })
            doc_outgoing[r["id"]] = outgoing

        # Build incoming refs: which docs reference each doc
        doc_incoming: dict[str, list[str]] = {r["id"]: [] for r in results}
        for r in results:
            for s in r.get("signals", []):
                if s["signal_type"] == "resolved_reference":
                    target_id = s["details"]["resolved_doc_id"]
                    if target_id in doc_incoming:
                        doc_incoming[target_id].append(r["title"])
                elif s["signal_type"] == "ambiguous_reference":
                    for mid in s["details"].get("matching_doc_ids", []):
                        if mid in doc_incoming:
                            doc_incoming[mid].append(r["title"])

        documents = []
        for r in results:
            did = r["id"]
            outgoing = doc_outgoing.get(did, [])
            incoming = doc_incoming.get(did, [])
            documents.append({
                "document_id": did,
                "title": r["title"],
                "outgoing_reference_count": len(outgoing),
                "incoming_reference_count": len(incoming),
                "outgoing_references": outgoing,
                "incoming_references": incoming,
            })

        return {"scan_id": scan_id, "documents": documents}
    finally:
        db.close()


_VALID_WORKFLOW_STATES = {"open", "acknowledged", "dismissed", "fixed", "snoozed", "accepted_risk"}


@app.get("/api/findings")
async def list_findings(
    scan_id: int | None = None,
    state: str | None = None,
    include_all: bool = False,
):
    """List workflow findings, optionally filtered by scan and/or state.

    Default returns actionable findings only (open, acknowledged,
    expired-snoozed).  Pass include_all=true for the full history
    including fixed, dismissed, accepted_risk, and future-snoozed.
    """
    db = _get_db()
    try:
        states = [s.strip() for s in state.split(",")] if state else None
        return db.get_findings(
            scan_id=scan_id,
            states=states,
            include_all=include_all,
        )
    finally:
        db.close()


@app.get("/api/findings/summary")
async def findings_summary(
    scan_id: int | None = None,
    include_all: bool = False,
):
    """Return counts by workflow state.

    Default returns actionable counts only.  Pass include_all=true
    for counts across all states including terminal ones.
    """
    db = _get_db()
    try:
        return db.get_workflow_summary(scan_id=scan_id, include_all=include_all)
    finally:
        db.close()


@app.patch("/api/findings/{finding_key}")
async def update_finding(finding_key: str, request: WorkflowUpdateRequest):
    """Update the workflow state of a finding."""
    if "state" in request.model_fields_set and request.state is None:
        return JSONResponse({"error": "state cannot be null"}, status_code=422)
    if request.state and request.state not in _VALID_WORKFLOW_STATES:
        return JSONResponse(
            {"error": f"Invalid state: {request.state}. Valid: {sorted(_VALID_WORKFLOW_STATES)}"},
            status_code=422,
        )
    db = _get_db()
    try:
        # Use model_fields_set to distinguish fields the client explicitly
        # sent (including as null to clear) from fields it omitted entirely.
        # Omitted fields are passed as _UNSET so update_workflow() leaves
        # existing values unchanged; included null values clear the column.
        fs = request.model_fields_set

        def _field(name: str) -> object:
            return getattr(request, name) if name in fs else _UNSET

        try:
            found = db.update_workflow(
                finding_key,
                state=_field("state"),  # type: ignore[arg-type]
                note=_field("note"),  # type: ignore[arg-type]
                assigned_owner=_field("assigned_owner"),  # type: ignore[arg-type]
                due_date=_field("due_date"),  # type: ignore[arg-type]
                snoozed_until=_field("snoozed_until"),  # type: ignore[arg-type]
                dismissal_reason=_field("dismissal_reason"),  # type: ignore[arg-type]
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not found:
            return JSONResponse({"error": "Finding not found"}, status_code=404)
        finding = db.get_finding(finding_key)
        return finding
    finally:
        db.close()


@app.get("/api/findings/{finding_key}")
async def get_finding(finding_key: str):
    """Get a single finding by key."""
    db = _get_db()
    try:
        finding = db.get_finding(finding_key)
        if not finding:
            return JSONResponse({"error": "Finding not found"}, status_code=404)
        return finding
    finally:
        db.close()


def main() -> None:
    import click
    import uvicorn

    @click.command()
    @click.option("--demo", is_flag=True, help="Enable the credential-free demo workspace.")
    @click.option(
        "--database", "database_path", type=click.Path(), default=None,
        help="Override the database path.",
    )
    @click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
    @click.option("--port", default=8080, show_default=True, type=int, help="Bind port.")
    def _main(demo: bool, database_path: str | None, host: str, port: int) -> None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

        actual_db_path = database_path
        if demo and actual_db_path is None:
            actual_db_path = "kbaudit-demo.db"

        configure_app(demo_mode=demo, database_path=actual_db_path, host=host, port=port)

        if demo:
            db = create_storage(actual_db_path)  # type: ignore[arg-type]
            db.connect()
            try:
                if not db.clear_all_if_idle():
                    logger.error(
                        "Demo startup failed: a live scan is in progress. "
                        "Stop the other scan first."
                    )
                    sys.exit(1)
            finally:
                db.close()
            logger.info("Demo workspace started — database: %s", actual_db_path)

        uvicorn.run(app, host=host, port=port)

    _main()


if __name__ == "__main__":
    main()
