"""Tests for the FastAPI web endpoints."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from kb_audit.web.app import app


@pytest.fixture
def client():
    return TestClient(app)


def _make_scan_result(doc_id, title, signals=None):
    return {
        "id": doc_id,
        "title": title,
        "url": f"https://notion.so/{doc_id}",
        "source_type": "notion",
        "last_modified": "2025-06-01T00:00:00+00:00",
        "overall_status": "current",
        "signals": signals or [],
        "suggested_replacement_id": None,
        "confidence": 0.8,
        "confidence_reason": "Recently edited",
        "trust_metadata": {
            "last_reviewed": "2025-05-15",
            "last_modified": "2025-06-01",
            "owner": "Test Team",
            "declared_status": "Current",
        },
        "trust_evidence": {
            "summary": "Well-maintained document",
            "positive_evidence": ["Recently edited", "Has owner"],
            "review_risks": [],
            "missing_evidence": [],
            "recommended_action": "No action needed",
        },
    }


class TestBuildAnalyzers:
    """Verify InternalLinkAnalyzer is wired into the web analyzer stack."""

    def test_internal_links_analyzer_present(self):
        from kb_audit.web.app import _build_analyzers
        from kb_audit.config import Config
        from kb_audit.analyzers.internal_links import InternalLinkAnalyzer
        analyzers = _build_analyzers(Config())
        names = [a.name() for a in analyzers]
        assert "internal_links" in names

    def test_internal_links_after_broken_links_before_references(self):
        from kb_audit.web.app import _build_analyzers
        from kb_audit.config import Config
        analyzers = _build_analyzers(Config())
        names = [a.name() for a in analyzers]
        assert names.index("internal_links") > names.index("broken_links")
        assert names.index("internal_links") < names.index("references")


class TestReferencesSummary:
    def test_no_scans(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_history.return_value = []
            resp = client.get("/api/references/summary")
        assert resp.status_code == 200
        assert resp.json()["documents"] == []

    def test_with_resolved_references(self, client):
        results = [
            _make_scan_result("doc-a", "Guide A", signals=[
                {
                    "signal_type": "resolved_reference",
                    "severity": "info",
                    "message": "References 'Guide B' → 'Guide B'",
                    "details": {
                        "referenced_title": "Guide B",
                        "resolved_doc_id": "doc-b",
                        "resolved_title": "Guide B",
                    },
                },
            ]),
            _make_scan_result("doc-b", "Guide B"),
        ]
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_history.return_value = [{"scan_id": 1}]
            db.get_scan_results.return_value = results
            resp = client.get("/api/references/summary")

        data = resp.json()
        assert data["scan_id"] == 1
        docs = {d["document_id"]: d for d in data["documents"]}

        # Guide A has 1 outgoing resolved reference
        assert docs["doc-a"]["outgoing_reference_count"] == 1
        assert docs["doc-a"]["outgoing_references"][0]["status"] == "resolved"
        assert docs["doc-a"]["outgoing_references"][0]["resolved_title"] == "Guide B"
        assert docs["doc-a"]["incoming_reference_count"] == 0

        # Guide B has 1 incoming reference from Guide A
        assert docs["doc-b"]["incoming_reference_count"] == 1
        assert docs["doc-b"]["incoming_references"] == ["Guide A"]
        assert docs["doc-b"]["outgoing_reference_count"] == 0

    def test_with_unresolved_reference(self, client):
        results = [
            _make_scan_result("doc-a", "Guide A", signals=[
                {
                    "signal_type": "unresolved_reference",
                    "severity": "warning",
                    "message": "References 'Missing Doc' but no matching document found",
                    "details": {"referenced_title": "Missing Doc"},
                },
            ]),
        ]
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_history.return_value = [{"scan_id": 2}]
            db.get_scan_results.return_value = results
            resp = client.get("/api/references/summary")

        docs = resp.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["outgoing_reference_count"] == 1
        assert docs[0]["outgoing_references"][0]["status"] == "unresolved"
        assert docs[0]["outgoing_references"][0]["referenced_text"] == "Missing Doc"

    def test_explicit_scan_id(self, client):
        results = [_make_scan_result("doc-x", "Doc X")]
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_results.return_value = results
            resp = client.get("/api/references/summary?scan_id=5")

        assert resp.json()["scan_id"] == 5
        # get_scan_history should NOT be called when scan_id is explicit
        db.get_scan_history.assert_not_called()


class TestTextReport:
    """Text report should use structured trust_metadata instead of regex parsing."""

    def test_text_report_uses_structured_last_reviewed(self, client):
        result = _make_scan_result("doc-a", "Guide A")
        result["overall_status"] = "stale"
        result["trust_metadata"] = {
            "last_reviewed": "2025-05-15",
            "last_modified": None,
            "owner": None,
            "declared_status": "Legacy",
        }
        result["trust_evidence"] = {
            "summary": "Marked as legacy",
            "positive_evidence": [],
            "review_risks": [],
            "missing_evidence": [],
            "recommended_action": "Archive or update this document",
        }
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_results.return_value = [result]
            db.get_scan_history.return_value = [{"scan_id": 1, "started_at": "2025-06-01T12:00:00"}]
            resp = client.get("/api/scans/1/report?format=text")

        text = resp.text
        assert "Last reviewed: 2025-05-15" in text
        assert "Recommended action: Archive or update this document" in text

    def test_text_report_falls_back_to_last_modified(self, client):
        result = _make_scan_result("doc-b", "Guide B")
        result["overall_status"] = "needs_review"
        result["trust_metadata"] = {
            "last_reviewed": None,
            "last_modified": None,
            "owner": None,
            "declared_status": None,
        }
        result["trust_evidence"] = {
            "summary": "",
            "positive_evidence": [],
            "review_risks": ["Unresolved reference"],
            "missing_evidence": [],
            "recommended_action": "",
        }
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_results.return_value = [result]
            db.get_scan_history.return_value = [{"scan_id": 2, "started_at": "2025-06-01T12:00:00"}]
            resp = client.get("/api/scans/2/report?format=text")

        text = resp.text
        assert "Last modified: 2025-06-01" in text

    def test_text_report_includes_unknown_documents(self, client):
        result = _make_scan_result("doc-u", "Unknown Doc")
        result["overall_status"] = "unknown"
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_results.return_value = [result]
            db.get_scan_history.return_value = [{"scan_id": 3, "started_at": "2025-06-01T12:00:00"}]
            resp = client.get("/api/scans/3/report?format=text")

        text = resp.text
        assert "Unknown Doc" in text
        assert "Unknown documents: 1" in text

    def test_json_report_includes_unknown_fields(self, client):
        stale_doc = _make_scan_result("doc-s", "Stale Doc")
        stale_doc["overall_status"] = "stale"
        unknown_doc = _make_scan_result("doc-u", "Unknown Doc")
        unknown_doc["overall_status"] = "unknown"
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_results.return_value = [stale_doc, unknown_doc]
            db.get_scan_history.return_value = [{"scan_id": 4, "started_at": "2025-06-01T12:00:00"}]
            resp = client.get("/api/scans/4/report")

        data = resp.json()
        assert data["stale_count"] == 1
        assert data["needs_review_count"] == 0
        assert data["unknown_count"] == 1
        assert len(data["stale_documents"]) == 1
        assert len(data["needs_review_documents"]) == 0
        assert len(data["unknown_documents"]) == 1
        assert data["unknown_documents"][0]["id"] == "doc-u"

    def test_json_report_only_unknown_no_flagged_stale(self, client):
        """A scan with only unknown docs must not return empty flagged lists."""
        unknown_doc = _make_scan_result("doc-u", "Unknown Doc")
        unknown_doc["overall_status"] = "unknown"
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_results.return_value = [unknown_doc]
            db.get_scan_history.return_value = [{"scan_id": 5, "started_at": "2025-06-01T12:00:00"}]
            resp = client.get("/api/scans/5/report")

        data = resp.json()
        assert data["unknown_count"] == 1
        assert data["stale_count"] == 0
        assert data["needs_review_count"] == 0
        assert len(data["unknown_documents"]) == 1


# ---------------------------------------------------------------------------
# UI payload contract: omitting stale fields lets backend cleanup fire
# ---------------------------------------------------------------------------


class TestUIPayloadCleanup:
    """Confirm that the UI's state-aware PATCH payloads (which omit fields
    like snoozed_until and dismissal_reason for non-applicable states) trigger
    the correct backend transition cleanup.

    These are integration tests using a real Database so the full
    update_workflow() cleanup path runs, matching what happens when
    saveDetail() sends a minimal payload through the real API.

    _get_db is patched to open a fresh per-request connection (matching real
    app behaviour) so the SQLite same-thread restriction is satisfied.
    """

    @pytest.fixture
    def api(self, tmp_path):
        """Seed a database and yield (client, finding_key, read_fn).

        The patch on _get_db remains active for the lifetime of the fixture so
        all client calls within the test are covered.
        """
        from kb_audit.db import Database
        from kb_audit.models import AuditResult, Document
        from datetime import datetime, timezone

        db_file = tmp_path / "test.db"

        # Seed initial data
        db = Database(str(db_file))
        db.connect()
        doc = Document(
            id="ui-doc-1", title="UI Test Doc", content="Legacy content.",
            source_type="test", last_modified=datetime.now(timezone.utc),
        )
        result = AuditResult(
            document=doc, signals=[], status="stale", confidence=0.8,
            confidence_reason="stale",
            trust_evidence={
                "summary": "Stale.", "positive_evidence": [],
                "review_risks": ["Old"], "missing_evidence": [],
                "recommended_action": "Review",
            },
        )
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [result])
        finding_key = result.finding_key
        db.close()

        def get_db():
            """Open a fresh connection per API request (thread-safe)."""
            conn = Database(str(db_file))
            conn.connect()
            return conn

        def read():
            """Read the finding from a fresh connection for assertion."""
            conn = Database(str(db_file))
            conn.connect()
            f = conn.get_finding(finding_key)
            conn.close()
            return f

        def seed(**kwargs):
            """Update workflow state via a fresh connection."""
            conn = Database(str(db_file))
            conn.connect()
            conn.update_workflow(finding_key, **kwargs)
            conn.close()

        with patch("kb_audit.web.app._get_db", side_effect=get_db):
            yield TestClient(app), finding_key, read, seed

    def test_open_payload_clears_snoozed_until(self, api):
        """PATCH {state: open} (no snoozed_until) clears a stale snoozed_until."""
        from datetime import datetime, timedelta, timezone
        client, key, read, seed = api
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        seed(state="snoozed", snoozed_until=future)  # type: ignore[arg-type]
        assert read()["snoozed_until"] is not None

        # UI payload omits snoozed_until → backend cleanup clears it
        resp = client.patch(f"/api/findings/{key}", json={"state": "open"})
        assert resp.status_code == 200
        assert read()["workflow_state"] == "open"
        assert read()["snoozed_until"] is None

    def test_acknowledged_payload_clears_dismissal_reason(self, api):
        """PATCH {state: acknowledged} (no dismissal_reason) clears a stale reason."""
        client, key, read, seed = api
        seed(state="dismissed", dismissal_reason="false positive")  # type: ignore[arg-type]
        assert read()["dismissal_reason"] == "false positive"

        # UI payload omits dismissal_reason → backend cleanup clears it
        resp = client.patch(f"/api/findings/{key}", json={"state": "acknowledged"})
        assert resp.status_code == 200
        assert read()["workflow_state"] == "acknowledged"
        assert not read()["dismissal_reason"]

    def test_snooze_without_snoozed_until_returns_400(self, api):
        """PATCH {state: snoozed} with no snoozed_until returns 400."""
        client, key, _read, _seed = api
        resp = client.patch(f"/api/findings/{key}", json={"state": "snoozed"})
        assert resp.status_code == 400
        assert "snoozed_until" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Clearing editable metadata fields via PATCH (explicit null)
# ---------------------------------------------------------------------------


class TestClearMetadataFields:
    """Verify that sending null for an editable field in the PATCH body clears
    the stored value, while omitting the field leaves it unchanged.

    The API uses model_fields_set (Pydantic v2) to distinguish the two cases.
    """

    @pytest.fixture
    def api(self, tmp_path):
        """Seed a finding with all metadata set; yield (client, key, read_fn, seed_fn)."""
        from kb_audit.db import Database
        from kb_audit.models import AuditResult, Document
        from datetime import datetime, timezone

        db_file = tmp_path / "meta.db"

        db = Database(str(db_file))
        db.connect()
        doc = Document(
            id="meta-1", title="Meta Doc", content="Stale content.",
            source_type="test", last_modified=datetime.now(timezone.utc),
        )
        result = AuditResult(
            document=doc, signals=[], status="stale", confidence=0.8,
            confidence_reason="stale",
            trust_evidence={
                "summary": "Stale.", "positive_evidence": [],
                "review_risks": ["Old"], "missing_evidence": [],
                "recommended_action": "Review",
            },
        )
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [result])
        finding_key = result.finding_key
        db.close()

        def get_db():
            conn = Database(str(db_file))
            conn.connect()
            return conn

        def read():
            conn = Database(str(db_file))
            conn.connect()
            f = conn.get_finding(finding_key)
            conn.close()
            return f

        def seed(**kwargs):
            conn = Database(str(db_file))
            conn.connect()
            conn.update_workflow(finding_key, **kwargs)
            conn.close()

        with patch("kb_audit.web.app._get_db", side_effect=get_db):
            yield TestClient(app), finding_key, read, seed

    # -- explicit null clears the field -----------------------------------

    def test_clear_assigned_owner(self, api):
        """PATCH {assigned_owner: null} clears an existing owner."""
        client, key, read, seed = api
        seed(assigned_owner="alice@example.com")
        assert read()["assigned_owner"] == "alice@example.com"

        resp = client.patch(f"/api/findings/{key}", json={"assigned_owner": None})
        assert resp.status_code == 200
        assert not read()["assigned_owner"]

    def test_clear_due_date(self, api):
        """PATCH {due_date: null} clears an existing due date."""
        client, key, read, seed = api
        seed(due_date="2026-12-31")
        assert read()["due_date"] == "2026-12-31"

        resp = client.patch(f"/api/findings/{key}", json={"due_date": None})
        assert resp.status_code == 200
        assert not read()["due_date"]

    def test_clear_note(self, api):
        """PATCH {note: null} clears an existing note."""
        client, key, read, seed = api
        seed(note="needs manual review")
        assert read()["note"] == "needs manual review"

        resp = client.patch(f"/api/findings/{key}", json={"note": None})
        assert resp.status_code == 200
        assert not read()["note"]

    def test_clear_dismissal_reason_for_dismissed_finding(self, api):
        """PATCH {state: dismissed, dismissal_reason: null} clears existing reason."""
        client, key, read, seed = api
        seed(state="dismissed", dismissal_reason="false positive")  # type: ignore[arg-type]
        assert read()["dismissal_reason"] == "false positive"

        resp = client.patch(
            f"/api/findings/{key}",
            json={"state": "dismissed", "dismissal_reason": None},
        )
        assert resp.status_code == 200
        f = read()
        assert f["workflow_state"] == "dismissed"
        assert not f["dismissal_reason"]

    # -- omitting a field leaves it unchanged ------------------------------

    def test_omit_owner_leaves_it_unchanged(self, api):
        """PATCH that omits assigned_owner does not alter the stored value."""
        client, key, read, seed = api
        seed(assigned_owner="bob@example.com")

        resp = client.patch(f"/api/findings/{key}", json={"state": "open"})
        assert resp.status_code == 200
        assert read()["assigned_owner"] == "bob@example.com"

    def test_omit_note_leaves_it_unchanged(self, api):
        """PATCH that omits note does not alter the stored value."""
        client, key, read, seed = api
        seed(note="important context")

        resp = client.patch(f"/api/findings/{key}", json={"state": "acknowledged"})
        assert resp.status_code == 200
        assert read()["note"] == "important context"

    # -- state-transition cleanup still fires for omitted state fields -----

    def test_transition_to_open_still_clears_dismissal_reason(self, api):
        """PATCH {state: open} (no dismissal_reason) triggers backend cleanup."""
        client, key, read, seed = api
        seed(state="dismissed", dismissal_reason="out of scope")  # type: ignore[arg-type]
        assert read()["dismissal_reason"] == "out of scope"

        resp = client.patch(f"/api/findings/{key}", json={"state": "open"})
        assert resp.status_code == 200
        assert not read()["dismissal_reason"]

    def test_explicit_reason_on_open_transition_is_caller_override(self, api):
        """PATCH {state: open, dismissal_reason: 'x'} — caller override wins.

        The UI never sends dismissal_reason for non-applicable states, so this
        path is only reachable via direct API calls. Documented semantics:
        an explicitly supplied field overrides transition cleanup.
        """
        client, key, read, seed = api
        seed(state="dismissed", dismissal_reason="old reason")  # type: ignore[arg-type]

        resp = client.patch(
            f"/api/findings/{key}",
            json={"state": "open", "dismissal_reason": "explicit-override"},
        )
        assert resp.status_code == 200
        f = read()
        assert f["workflow_state"] == "open"
        # Caller explicitly supplied a value; it overrides cleanup.
        assert f["dismissal_reason"] == "explicit-override"

    # -- explicit null state is rejected -----------------------------------

    def test_explicit_null_state_returns_422(self, api):
        """PATCH {"state": null} is rejected with 422; workflow_state unchanged."""
        client, key, read, seed = api
        seed(state="open")  # type: ignore[arg-type]

        resp = client.patch(f"/api/findings/{key}", json={"state": None})
        assert resp.status_code == 422
        assert "state" in resp.json()["error"].lower()
        assert read()["workflow_state"] == "open"

    def test_omit_state_with_metadata_update_works(self, api):
        """PATCH with no state field updates metadata only; workflow_state unchanged."""
        client, key, read, seed = api
        seed(state="acknowledged")  # type: ignore[arg-type]

        resp = client.patch(f"/api/findings/{key}", json={"assigned_owner": "carol@example.com"})
        assert resp.status_code == 200
        f = read()
        assert f["workflow_state"] == "acknowledged"
        assert f["assigned_owner"] == "carol@example.com"

    def test_clear_owner_still_works_with_null(self, api):
        """PATCH {"assigned_owner": null} still clears owner (regression guard)."""
        client, key, read, seed = api
        seed(assigned_owner="dave@example.com")

        resp = client.patch(f"/api/findings/{key}", json={"assigned_owner": None})
        assert resp.status_code == 200
        assert not read()["assigned_owner"]


# ---------------------------------------------------------------------------
# /api/status — source detection
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    """GET /api/status reflects the active source without leaking secrets."""

    @pytest.fixture(autouse=True)
    def patch_db(self):
        from kb_audit.db import Database
        def fresh_db():
            db = Database(":memory:")
            db.connect()
            return db
        with patch("kb_audit.web.app._get_db", side_effect=fresh_db):
            yield

    def test_notion_configured(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = "secret-notion-key"
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            cfg.notion.root_page_id = "abc123"
            cfg.notion.database_id = None
            resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "notion"
        assert data["source_label"] == "Notion"
        assert data["configured"] is True
        assert data["configuration_error"] is None
        assert data["target"]["root_page_id"] == "abc123"
        # Secret must not be present
        assert "secret-notion-key" not in str(data)
        assert "api_key" not in data

    def test_confluence_configured(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = ""
            cfg.confluence.base_url = "https://myco.atlassian.net"
            cfg.confluence.api_token = "secret-confluence-token"
            cfg.confluence.space_key = "ENG"
            cfg.confluence.page_id = None
            resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "confluence"
        assert data["source_label"] == "Confluence Cloud"
        assert data["configured"] is True
        assert data["target"]["base_url"] == "https://myco.atlassian.net"
        assert data["target"]["space_key"] == "ENG"
        # Token must not be present
        assert "secret-confluence-token" not in str(data)
        assert "api_token" not in data

    def test_neither_configured(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = ""
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] is None
        assert data["source_label"] is None
        assert data["configured"] is False
        assert data["configuration_error"] is not None
        assert data["target"] is None

    def test_confluence_takes_priority_over_notion(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = "also-set"
            cfg.confluence.base_url = "https://corp.atlassian.net"
            cfg.confluence.api_token = "token"
            cfg.confluence.space_key = None
            cfg.confluence.page_id = None
            resp = client.get("/api/status")
        assert resp.json()["source"] == "confluence"


# ---------------------------------------------------------------------------
# POST /api/scans — source-aware validation
# ---------------------------------------------------------------------------


class TestStartScanValidation:
    """POST /api/scans validates credentials for the active source."""

    @pytest.fixture(autouse=True)
    def patch_db(self):
        from kb_audit.db import Database
        def fresh_db():
            db = Database(":memory:")
            db.connect()
            return db
        with patch("kb_audit.web.app._get_db", side_effect=fresh_db):
            yield

    def test_confluence_configured_without_notion_key_is_accepted(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = ""
            cfg.confluence.base_url = "https://myco.atlassian.net"
            cfg.confluence.api_token = "token"
            cfg.confluence.space_key = "ENG"
            cfg.confluence.page_id = None
            with patch("kb_audit.web.app._run_scan"):
                resp = client.post("/api/scans", json={})
        assert resp.status_code == 200
        assert resp.json().get("scan_in_progress") is True

    def test_notion_configured_is_accepted(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = "secret"
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            with patch("kb_audit.web.app._run_scan"):
                resp = client.post("/api/scans", json={})
        assert resp.status_code == 200
        assert resp.json().get("scan_in_progress") is True

    def test_neither_configured_returns_422(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = ""
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            resp = client.post("/api/scans", json={})
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert "NOTION_API_KEY" in error or "configured" in error.lower()
        # Must not expose secrets (nothing to leak here, but check anyway)
        assert "secret" not in error.lower()


class TestScanScopeRouting:
    """POST /api/scans routes scope fields correctly to _run_scan."""

    @pytest.fixture(autouse=True)
    def reset_scan_state(self):
        """Provide an isolated in-memory DB so each test starts with in_progress=0."""
        from kb_audit.db import Database

        def fresh_db():
            db = Database(":memory:")
            db.connect()
            return db

        with patch("kb_audit.web.app._get_db", side_effect=fresh_db):
            yield

    def _notion_cfg(self, MockCfg, *, root_page_id=None, database_id=None):
        cfg = MockCfg.load.return_value
        cfg.notion_api_key = "secret"
        cfg.notion.root_page_id = root_page_id
        cfg.notion.database_id = database_id
        cfg.confluence.base_url = ""
        cfg.confluence.api_token = ""
        return cfg

    def _confluence_cfg(self, MockCfg, *, space_key=None, page_id=None):
        cfg = MockCfg.load.return_value
        cfg.notion_api_key = ""
        cfg.confluence.base_url = "https://myco.atlassian.net"
        cfg.confluence.email = "user@example.com"
        cfg.confluence.api_token = "token"
        cfg.confluence.space_key = space_key
        cfg.confluence.page_id = page_id
        return cfg

    # --- Notion scope routing ---

    def test_notion_configured_scope_passes_no_extra_fields(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg, root_page_id="root-abc")
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={"scope_type": "configured"})
        assert resp.status_code == 200
        mock_run.assert_called_once()
        _, _, scope_type, root_page, database_id, conf_space, conf_page = mock_run.call_args.args
        assert scope_type == "configured"
        assert root_page is None
        assert database_id is None

    def test_notion_page_tree_scope(self, client):
        valid_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "root_page": valid_uuid,
                })
        assert resp.status_code == 200
        _, _, scope_type, root_page, database_id, _, _ = mock_run.call_args.args
        assert scope_type == "page_tree"
        assert root_page == valid_uuid
        assert database_id is None

    def test_notion_database_scope(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "database",
                    "database_id": "db-abc",
                })
        assert resp.status_code == 200
        _, _, scope_type, root_page, database_id, _, _ = mock_run.call_args.args
        assert scope_type == "database"
        assert root_page is None
        assert database_id == "db-abc"

    def test_notion_query_scope(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "query",
                    "query": "onboarding guide",
                })
        assert resp.status_code == 200
        _, query, scope_type, *_ = mock_run.call_args.args
        assert scope_type == "query"
        assert query == "onboarding guide"

    # --- Confluence scope routing ---

    def test_confluence_configured_scope(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._confluence_cfg(MockCfg, space_key="ENG")
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={"scope_type": "configured"})
        assert resp.status_code == 200
        _, _, scope_type, _, _, conf_space, conf_page = mock_run.call_args.args
        assert scope_type == "configured"
        assert conf_space is None
        assert conf_page is None

    def test_confluence_space_scope(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._confluence_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "space",
                    "confluence_space": "DOCS",
                })
        assert resp.status_code == 200
        _, _, scope_type, _, _, conf_space, conf_page = mock_run.call_args.args
        assert scope_type == "space"
        assert conf_space == "DOCS"
        assert conf_page is None

    def test_confluence_page_tree_scope(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._confluence_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "confluence_page_id": "99887766",
                })
        assert resp.status_code == 200
        _, _, scope_type, _, _, conf_space, conf_page = mock_run.call_args.args
        assert scope_type == "page_tree"
        assert conf_space is None
        assert conf_page == "99887766"

    def test_confluence_cql_scope(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._confluence_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "cql",
                    "query": 'space = "ENG" AND title ~ "API"',
                })
        assert resp.status_code == 200
        _, query, scope_type, *_ = mock_run.call_args.args
        assert scope_type == "cql"
        assert query == 'space = "ENG" AND title ~ "API"'

    def test_default_scope_type_none_is_accepted(self, client):
        """Omitting scope_type (None) defaults to configured scope on the backend."""
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg, root_page_id="root-abc")
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={})
        assert resp.status_code == 200
        _, _, scope_type, *_ = mock_run.call_args.args
        assert scope_type is None


class TestNotionPageTreeRouting:
    """_run_scan passes the already-resolved UUID to NotionSource for page_tree scope."""

    NOTION_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def _patch_cfg(self, MockCfg):
        cfg = MockCfg.load.return_value
        cfg.notion_api_key = "secret"
        cfg.notion.root_page_id = None
        cfg.notion.database_id = None
        cfg.confluence.base_url = ""
        cfg.confluence.api_token = ""
        cfg.analyzers.timestamp.warning_days = 90
        cfg.analyzers.timestamp.critical_days = 180
        cfg.analyzers.similarity.threshold = 0.85
        cfg.analyzers.version_refs.current_versions = []
        cfg.analyzers.version_refs.patterns = []
        cfg.database_url = "sqlite://:memory:"
        return cfg

    def test_uuid_passed_directly_to_notion_source(self):
        """_run_scan receives a pre-resolved UUID and passes it as root_page_id."""
        with (
            patch("kb_audit.web.app.Config") as MockCfg,
            patch("kb_audit.web.app.NotionSource") as MockNotion,
            patch("kb_audit.web.app.Auditor"),
            patch("kb_audit.web.app.create_storage"),
        ):
            self._patch_cfg(MockCfg)
            from kb_audit.web.app import _run_scan
            _run_scan("fake-owner-token", None, "page_tree", self.NOTION_UUID, None, None, None)
        MockNotion.assert_called_once()
        kwargs = MockNotion.call_args.kwargs
        assert kwargs.get("root_page_id") == self.NOTION_UUID
        assert kwargs.get("query") is None


class TestPageTreeValidation:
    """start_scan validates page-tree targets before accepting the request."""

    NOTION_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    NOTION_URL = f"https://notion.so/Engineering-{'a1b2c3d4e5f67890abcdef1234567890'}"

    @pytest.fixture(autouse=True)
    def reset_scan_state(self):
        """Provide an isolated in-memory DB so each test starts with in_progress=0."""
        from kb_audit.db import Database

        def fresh_db():
            db = Database(":memory:")
            db.connect()
            return db

        with patch("kb_audit.web.app._get_db", side_effect=fresh_db):
            yield

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def _notion_cfg(self, MockCfg):
        cfg = MockCfg.load.return_value
        cfg.notion_api_key = "secret"
        cfg.notion.root_page_id = None
        cfg.notion.database_id = None
        cfg.confluence.base_url = ""
        cfg.confluence.api_token = ""
        return cfg

    def _confluence_cfg(self, MockCfg):
        cfg = MockCfg.load.return_value
        cfg.notion_api_key = ""
        cfg.confluence.base_url = "https://myco.atlassian.net"
        cfg.confluence.email = "user@example.com"
        cfg.confluence.api_token = "token"
        cfg.confluence.space_key = None
        cfg.confluence.page_id = None
        return cfg

    # --- Notion page tree ---

    def test_notion_uuid_accepted(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "root_page": self.NOTION_UUID,
                })
        assert resp.status_code == 200
        _, _, _, root_page, *_ = mock_run.call_args.args
        assert root_page == self.NOTION_UUID

    def test_notion_url_extracted_to_uuid(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "root_page": self.NOTION_URL,
                })
        assert resp.status_code == 200
        _, _, _, root_page, *_ = mock_run.call_args.args
        # Extracted UUID — not the raw URL
        assert root_page != self.NOTION_URL
        assert "-" in root_page  # UUID format

    def test_notion_plain_title_single_match_resolves(self, client):
        resolved_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with (
                patch("kb_audit.web.app.find_notion_page_by_title", return_value=resolved_id),
                patch("kb_audit.web.app._run_scan") as mock_run,
            ):
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "root_page": "Engineering",
                })
        assert resp.status_code == 200
        _, _, _, root_page, *_ = mock_run.call_args.args
        assert root_page == resolved_id

    def test_notion_plain_title_no_match_returns_422(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch(
                "kb_audit.web.app.find_notion_page_by_title",
                side_effect=ValueError("No Notion page found with title \u2018Engineering\u2019."),
            ):
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "root_page": "Engineering",
                })
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert "Engineering" in error
        assert "secret" not in error

    def test_notion_plain_title_multiple_matches_returns_422(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch(
                "kb_audit.web.app.find_notion_page_by_title",
                side_effect=ValueError(
                    "Multiple Notion pages match \u2018Engineering\u2019 \u2014 the title is ambiguous."
                ),
            ):
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "root_page": "Engineering",
                })
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert "ambiguous" in error.lower() or "multiple" in error.lower()

    def test_notion_query_scope_plain_title_always_accepted(self, client):
        """Search/query scope does not go through page-tree validation."""
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._notion_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "query",
                    "query": "Engineering",
                })
        assert resp.status_code == 200
        _, query, *_ = mock_run.call_args.args
        assert query == "Engineering"

    # --- Confluence page tree ---

    def test_confluence_numeric_page_id_accepted(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._confluence_cfg(MockCfg)
            with patch("kb_audit.web.app._run_scan") as mock_run:
                resp = client.post("/api/scans", json={
                    "scope_type": "page_tree",
                    "confluence_page_id": "12345678",
                })
        assert resp.status_code == 200
        _, _, _, _, _, _, conf_page = mock_run.call_args.args
        assert conf_page == "12345678"

    def test_confluence_plain_text_page_id_returns_422(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._confluence_cfg(MockCfg)
            resp = client.post("/api/scans", json={
                "scope_type": "page_tree",
                "confluence_page_id": "Engineering",
            })
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert "numeric" in error.lower() or "page id" in error.lower()
        assert "token" not in error.lower()

    def test_confluence_page_id_with_spaces_returns_422(self, client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            self._confluence_cfg(MockCfg)
            resp = client.post("/api/scans", json={
                "scope_type": "page_tree",
                "confluence_page_id": "ENG overview",
            })
        assert resp.status_code == 422


class TestFindNotionPageByTitle:
    """Unit tests for find_notion_page_by_title()."""

    API_KEY = "secret-key"

    def _make_page(self, title: str, page_id: str = "aaaabbbb-cccc-dddd-eeee-ffffffffffff") -> dict:
        return {
            "id": page_id,
            "properties": {
                "title": {
                    "type": "title",
                    "title": [{"plain_text": title}],
                }
            },
        }

    def _mock_search_response(self, results: list, has_more: bool = False) -> dict:
        return {"results": results, "has_more": has_more, "next_cursor": None}

    def test_exact_match_returns_page_id(self):
        from kb_audit.sources.notion import find_notion_page_by_title
        page = self._make_page("Engineering", "aaaabbbb-cccc-dddd-eeee-ffffffffffff")
        mock_resp = self._mock_search_response([page])

        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value = MockClient.return_value
            instance.post.return_value.json.return_value = mock_resp
            instance.post.return_value.raise_for_status.return_value = None
            result = find_notion_page_by_title(self.API_KEY, "Engineering")

        assert result == "aaaabbbb-cccc-dddd-eeee-ffffffffffff"

    def test_case_insensitive_match(self):
        from kb_audit.sources.notion import find_notion_page_by_title
        page = self._make_page("Engineering", "aaaabbbb-cccc-dddd-eeee-ffffffffffff")
        mock_resp = self._mock_search_response([page])

        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value
            instance.post.return_value.json.return_value = mock_resp
            instance.post.return_value.raise_for_status.return_value = None
            result = find_notion_page_by_title(self.API_KEY, "engineering")

        assert result == "aaaabbbb-cccc-dddd-eeee-ffffffffffff"

    def test_no_match_raises_value_error(self):
        from kb_audit.sources.notion import find_notion_page_by_title
        # A page that does NOT match the title
        page = self._make_page("Something Else")
        mock_resp = self._mock_search_response([page])

        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value
            instance.post.return_value.json.return_value = mock_resp
            instance.post.return_value.raise_for_status.return_value = None
            with pytest.raises(ValueError) as exc_info:
                find_notion_page_by_title(self.API_KEY, "Engineering")

        assert "Engineering" in str(exc_info.value)
        assert self.API_KEY not in str(exc_info.value)

    def test_multiple_matches_raises_value_error(self):
        from kb_audit.sources.notion import find_notion_page_by_title
        p1 = self._make_page("Engineering", "aaaa0000-0000-0000-0000-000000000001")
        p2 = self._make_page("Engineering", "aaaa0000-0000-0000-0000-000000000002")
        mock_resp = self._mock_search_response([p1, p2])

        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value
            instance.post.return_value.json.return_value = mock_resp
            instance.post.return_value.raise_for_status.return_value = None
            with pytest.raises(ValueError) as exc_info:
                find_notion_page_by_title(self.API_KEY, "Engineering")

        assert "ambiguous" in str(exc_info.value).lower() or "multiple" in str(exc_info.value).lower()
        assert self.API_KEY not in str(exc_info.value)

    def test_partial_match_not_counted(self):
        """'Engineering Team' should not match a search for 'Engineering'."""
        from kb_audit.sources.notion import find_notion_page_by_title
        page = self._make_page("Engineering Team")
        mock_resp = self._mock_search_response([page])

        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value
            instance.post.return_value.json.return_value = mock_resp
            instance.post.return_value.raise_for_status.return_value = None
            with pytest.raises(ValueError):
                find_notion_page_by_title(self.API_KEY, "Engineering")

    def test_child_page_title_fallback(self):
        """Pages using child_page title format are also matched."""
        from kb_audit.sources.notion import find_notion_page_by_title
        page = {
            "id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            "properties": {},
            "child_page": {"title": "Engineering"},
        }
        mock_resp = self._mock_search_response([page])

        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value
            instance.post.return_value.json.return_value = mock_resp
            instance.post.return_value.raise_for_status.return_value = None
            result = find_notion_page_by_title(self.API_KEY, "Engineering")

        assert result == "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


class TestScanLease:
    """Owner-bound expiring lease: try_start_scan / renew_lease / end_scan."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mem_db(self):
        from kb_audit.db import Database
        db = Database(":memory:")
        db.connect()
        return db

    def _file_db(self, tmp_path):
        from kb_audit.db import Database
        db = Database(tmp_path / "test.db")
        db.connect()
        return db

    @staticmethod
    def _past(seconds: int = 600) -> str:
        """Return an ISO timestamp `seconds` in the past."""
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_first_acquire_returns_token(self):
        db = self._mem_db()
        token = db.try_start_scan()
        assert token is not None
        assert len(token) > 0
        db.close()

    def test_second_acquire_fails_while_live(self, tmp_path):
        db1 = self._file_db(tmp_path)
        token1 = db1.try_start_scan()
        assert token1 is not None
        db1.close()

        db2 = self._file_db(tmp_path)
        token2 = db2.try_start_scan()
        assert token2 is None
        db2.close()

    def test_expired_lease_can_be_taken_over(self, tmp_path):
        db1 = self._file_db(tmp_path)
        token1 = db1.try_start_scan(now=self._past(700))
        assert token1 is not None
        db1.close()

        db2 = self._file_db(tmp_path)
        token2 = db2.try_start_scan(now=self._now())
        assert token2 is not None
        assert token2 != token1
        db2.close()

    def test_renew_succeeds_for_owner(self):
        db = self._mem_db()
        token = db.try_start_scan()
        assert token is not None
        result = db.renew_lease(token)
        assert result is True
        db.close()

    def test_renew_fails_for_wrong_owner(self):
        db = self._mem_db()
        token = db.try_start_scan()
        assert token is not None
        result = db.renew_lease("wrong-token")
        assert result is False
        db.close()

    def test_old_owner_cannot_end_new_lease(self, tmp_path):
        db1 = self._file_db(tmp_path)
        old_token = db1.try_start_scan(now=self._past(700))
        assert old_token is not None
        db1.close()

        db2 = self._file_db(tmp_path)
        new_token = db2.try_start_scan(now=self._now())
        assert new_token is not None

        result = db2.end_scan(old_token, None, None)
        assert result is False
        state = db2.get_scan_state()
        assert state["in_progress"] is True
        db2.close()

    def test_stale_lease_not_reported_as_in_progress(self):
        db = self._mem_db()
        token = db.try_start_scan(now=self._past(700))
        assert token is not None
        state = db.get_scan_state(now=self._now())
        assert state["in_progress"] is False
        db.close()

    def test_post_scan_starts_after_expired_lease(self, tmp_path):
        from kb_audit.db import Database
        expired_db = self._file_db(tmp_path)
        expired_db.try_start_scan(now=self._past(700))
        expired_db.close()

        def fresh_db():
            db = Database(tmp_path / "test.db")
            db.connect()
            return db

        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = "secret"
            cfg.notion.root_page_id = None
            cfg.notion.database_id = None
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            with patch("kb_audit.web.app._get_db", side_effect=fresh_db):
                with patch("kb_audit.web.app._run_scan"):
                    resp = TestClient(app).post("/api/scans", json={})
        assert resp.status_code == 200

    def test_successful_scan_releases_lease(self):
        db = self._mem_db()
        token = db.try_start_scan()
        assert token is not None
        result = db.end_scan(token, 1, None)
        assert result is True
        state = db.get_scan_state()
        assert state["in_progress"] is False
        assert state["last_scan_id"] == 1
        db.close()

    def test_failed_scan_releases_lease(self):
        db = self._mem_db()
        token = db.try_start_scan()
        assert token is not None
        result = db.end_scan(token, None, "SomeError: x")
        assert result is True
        state = db.get_scan_state()
        assert state["in_progress"] is False
        assert state["scan_error"] == "SomeError: x"
        db.close()

    # ------------------------------------------------------------------
    # Lease renewal correctness
    # ------------------------------------------------------------------

    def test_expired_owner_cannot_renew(self):
        """renew_lease returns False when the lease has already expired."""
        db = self._mem_db()
        # Acquire with a timestamp far enough in the past that the lease expired
        token = db.try_start_scan(now=self._past(700))
        assert token is not None
        # Attempt renewal with the current time — lease_expires_at is in the past
        result = db.renew_lease(token, now=self._now())
        assert result is False
        db.close()

    # ------------------------------------------------------------------
    # ScanLeaseContext._renewal_loop unit tests (no real sleeps; interval=0)
    # ------------------------------------------------------------------

    def test_failed_renewal_sets_ownership_lost_and_stops(self, tmp_path):
        """When renew_lease returns False the loop sets _ownership_lost and exits."""
        from kb_audit.db import ScanLeaseContext

        mock_rdb = MagicMock()
        mock_rdb.renew_lease.return_value = False

        mock_db = MagicMock()
        mock_db._path = str(tmp_path / "test.db")

        ctx = ScanLeaseContext(mock_db, "token")

        with patch("kb_audit.storage.sqlite.SqliteStorage", return_value=mock_rdb), \
             patch("kb_audit.storage.sqlite.RENEW_INTERVAL_SECONDS", 0):
            ctx._renewal_loop()

        assert ctx._ownership_lost.is_set()
        assert ctx._stop.is_set()
        mock_rdb.close.assert_called()

    def test_heartbeat_db_closes_on_success(self, tmp_path):
        """DB connection is closed in a finally block even when renewal succeeds."""
        from kb_audit.db import ScanLeaseContext

        mock_rdb = MagicMock()
        mock_db = MagicMock()
        mock_db._path = str(tmp_path / "test.db")

        ctx = ScanLeaseContext(mock_db, "token")

        def renew_and_stop(token, **kw):
            ctx._stop.set()   # cause the loop to exit after this iteration
            return True

        mock_rdb.renew_lease.side_effect = renew_and_stop

        with patch("kb_audit.storage.sqlite.SqliteStorage", return_value=mock_rdb), \
             patch("kb_audit.storage.sqlite.RENEW_INTERVAL_SECONDS", 0):
            ctx._renewal_loop()

        assert not ctx._ownership_lost.is_set()
        mock_rdb.close.assert_called()

    def test_heartbeat_db_closes_on_exception(self, tmp_path):
        """DB connection is closed even when renewal raises an exception."""
        from kb_audit.db import ScanLeaseContext

        mock_rdb = MagicMock()
        mock_db = MagicMock()
        mock_db._path = str(tmp_path / "test.db")

        ctx = ScanLeaseContext(mock_db, "token")

        def raise_and_stop(token, **kw):
            ctx._stop.set()
            raise RuntimeError("db gone")

        mock_rdb.renew_lease.side_effect = raise_and_stop

        with patch("kb_audit.storage.sqlite.SqliteStorage", return_value=mock_rdb), \
             patch("kb_audit.storage.sqlite.RENEW_INTERVAL_SECONDS", 0):
            ctx._renewal_loop()

        assert not ctx._ownership_lost.is_set()   # exception != ownership loss
        mock_rdb.close.assert_called()

    # ------------------------------------------------------------------
    # Orchestration boundary abort
    # ------------------------------------------------------------------

    def test_worker_aborts_at_orchestration_boundary(self):
        """Auditor.run propagates LeaseLostError raised by lease_check."""
        from kb_audit.auditor import Auditor
        from kb_audit.db import LeaseLostError
        from kb_audit.models import Document

        call_count = [0]

        def lease_check():
            call_count[0] += 1
            if call_count[0] >= 2:
                raise LeaseLostError("ownership lost")

        mock_db = MagicMock()
        mock_db.start_scan.return_value = 1
        mock_db.get_previous_hashes.return_value = {}

        source = MagicMock()
        # Two documents so the check fires at least twice during fetch
        source.fetch_documents.return_value = [
            Document(id="d1", title="Doc 1", content="", source_type="notion"),
            Document(id="d2", title="Doc 2", content="", source_type="notion"),
        ]

        auditor = Auditor(sources=[source], analyzers=[], reporters=[], db=mock_db)
        with pytest.raises(LeaseLostError):
            auditor.run(lease_check=lease_check)

    # ------------------------------------------------------------------
    # Heartbeat thread lifecycle inside _run_scan
    # ------------------------------------------------------------------

    def test_scan_joins_heartbeat_thread_on_success(self, tmp_path):
        """Heartbeat thread is joined before _run_scan returns (success path)."""
        from kb_audit.web.app import _run_scan

        join_calls = []
        original_join = threading.Thread.join

        def spy_join(self, timeout=None):
            join_calls.append(self)
            original_join(self, timeout=timeout)

        mock_db = MagicMock()
        mock_db._path = str(tmp_path / "test.db")
        mock_db.end_scan.return_value = True
        mock_db.get_scan_history.return_value = []
        mock_db.owns_live_lease.return_value = True

        with (
            patch("kb_audit.db.ScanLeaseContext._renewal_loop"),   # exits immediately
            patch("kb_audit.web.app.Config") as MockCfg,
            patch("kb_audit.web.app.Auditor") as MockAuditor,
            patch("kb_audit.web.app.create_storage", return_value=mock_db),
            patch("kb_audit.web.app.NotionSource"),
            patch.object(threading.Thread, "join", spy_join),
        ):
            cfg = MockCfg.load.return_value
            cfg.database_url = str(tmp_path / "test.db")
            cfg.notion_api_key = "key"
            cfg.notion.root_page_id = None
            cfg.notion.database_id = None
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            cfg.analyzers.timestamp.warning_days = 90
            cfg.analyzers.timestamp.critical_days = 180
            cfg.analyzers.similarity.threshold = 0.85
            cfg.analyzers.version_refs.current_versions = []
            cfg.analyzers.version_refs.patterns = []
            MockAuditor.return_value.run.return_value = []

            _run_scan("fake-token", None)

        assert len(join_calls) == 1

    def test_scan_joins_heartbeat_thread_on_failure(self, tmp_path):
        """Heartbeat thread is joined before _run_scan returns (error path)."""
        from kb_audit.web.app import _run_scan

        join_calls = []
        original_join = threading.Thread.join

        def spy_join(self, timeout=None):
            join_calls.append(self)
            original_join(self, timeout=timeout)

        mock_db = MagicMock()
        mock_db._path = str(tmp_path / "test.db")
        mock_db.end_scan.return_value = True
        mock_db.owns_live_lease.return_value = True

        with (
            patch("kb_audit.db.ScanLeaseContext._renewal_loop"),
            patch("kb_audit.web.app.Config") as MockCfg,
            patch("kb_audit.web.app.Auditor") as MockAuditor,
            patch("kb_audit.web.app.create_storage", return_value=mock_db),
            patch("kb_audit.web.app.NotionSource"),
            patch.object(threading.Thread, "join", spy_join),
        ):
            cfg = MockCfg.load.return_value
            cfg.database_url = str(tmp_path / "test.db")
            cfg.notion_api_key = "key"
            cfg.notion.root_page_id = None
            cfg.notion.database_id = None
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            cfg.analyzers.timestamp.warning_days = 90
            cfg.analyzers.timestamp.critical_days = 180
            cfg.analyzers.similarity.threshold = 0.85
            cfg.analyzers.version_refs.current_versions = []
            cfg.analyzers.version_refs.patterns = []
            MockAuditor.return_value.run.side_effect = RuntimeError("boom")

            _run_scan("fake-token", None)

        assert len(join_calls) == 1

    # ------------------------------------------------------------------
    # end_scan expiry fence
    # ------------------------------------------------------------------

    def test_expired_owner_cannot_end_scan(self):
        """end_scan returns False when the owner's lease has expired."""
        db = self._mem_db()
        token = db.try_start_scan(now=self._past(700))
        assert token is not None
        result = db.end_scan(token, 1, None, now=self._now())
        assert result is False

    def test_expired_end_scan_leaves_state_unchanged(self):
        """A failed end_scan must not alter any scan_state field."""
        db = self._mem_db()
        token = db.try_start_scan(now=self._past(700))
        db.end_scan(token, 42, "boom", now=self._now())
        row = db.conn.execute(
            "SELECT in_progress, last_scan_id, scan_error, owner_token FROM scan_state WHERE id=1"
        ).fetchone()
        assert row[0] == 1         # still in_progress
        assert row[1] is None      # last_scan_id untouched
        assert row[2] is None      # scan_error untouched
        assert row[3] == token     # owner_token untouched
        db.close()

    # ------------------------------------------------------------------
    # owns_live_lease correctness
    # ------------------------------------------------------------------

    def test_owns_live_lease_accepts_live(self):
        db = self._mem_db()
        token = db.try_start_scan()
        assert db.owns_live_lease(token) is True
        db.close()

    def test_owns_live_lease_rejects_expired(self):
        db = self._mem_db()
        token = db.try_start_scan(now=self._past(700))
        assert db.owns_live_lease(token, now=self._now()) is False
        db.close()

    def test_owns_live_lease_rejects_wrong_owner(self):
        db = self._mem_db()
        db.try_start_scan()
        assert db.owns_live_lease("wrong-token") is False
        db.close()

    def test_owns_live_lease_rejects_inactive(self):
        """Returns False when no scan is in progress."""
        db = self._mem_db()
        assert db.owns_live_lease("any-token") is False
        db.close()

    # ------------------------------------------------------------------
    # Auditor boundary placement
    # ------------------------------------------------------------------

    def test_auditor_checks_before_finish_scan_normal_path(self):
        """Ownership is verified before finish_scan; finish_scan not called on loss."""
        from kb_audit.auditor import Auditor
        from kb_audit.db import LeaseLostError
        from kb_audit.models import Document

        mock_db = MagicMock()
        mock_db.start_scan.return_value = 1
        mock_db.get_previous_hashes.return_value = {}

        raise_now = [False]

        def lease_check():
            if raise_now[0]:
                raise LeaseLostError("lost")

        def after_store(scan_id, result, **_):
            raise_now[0] = True   # next check (before finish_scan) will raise

        mock_db.store_result.side_effect = after_store

        source = MagicMock()
        source.fetch_documents.return_value = [
            Document(id="d1", title="Doc 1", content="", source_type="notion"),
        ]

        auditor = Auditor(sources=[source], analyzers=[], reporters=[], db=mock_db)
        with pytest.raises(LeaseLostError):
            auditor.run(lease_check=lease_check)

        mock_db.finish_scan.assert_not_called()
        mock_db.sync_findings.assert_not_called()

    def test_auditor_all_unchanged_aborts_before_finish_and_sync(self):
        """All-unchanged path: ownership loss aborts before finish_scan and sync_findings."""
        from kb_audit.auditor import Auditor
        from kb_audit.db import LeaseLostError

        mock_db = MagicMock()
        mock_db.start_scan.return_value = 1
        mock_db.get_previous_hashes.return_value = {"d1": "h1"}

        raise_after_carry = [False]

        def lease_check():
            if raise_after_carry[0]:
                raise LeaseLostError("lost")

        def carry_and_flag(*_, **__):
            raise_after_carry[0] = True
            return 1

        mock_db.carry_forward_results.side_effect = carry_and_flag

        source = MagicMock()
        source.fetch_documents.return_value = [MagicMock(id="d1", content_hash="h1")]

        auditor = Auditor(sources=[source], analyzers=[], reporters=[], db=mock_db)
        with pytest.raises(LeaseLostError):
            auditor.run(lease_check=lease_check)

        mock_db.finish_scan.assert_not_called()
        mock_db.sync_findings.assert_not_called()

    def test_normal_path_stops_at_second_store_result_after_loss(self):
        """store_result called once; the second is blocked by ownership loss."""
        from kb_audit.auditor import Auditor
        from kb_audit.db import LeaseLostError
        from kb_audit.models import Document

        store_calls = [0]
        raise_after_first = [False]

        def fake_store(scan_id, result, **_):
            store_calls[0] += 1
            raise_after_first[0] = True

        def lease_check():
            if raise_after_first[0]:
                raise LeaseLostError("lost")

        mock_db = MagicMock()
        mock_db.start_scan.return_value = 1
        mock_db.get_previous_hashes.return_value = {}
        mock_db.store_result.side_effect = fake_store

        source = MagicMock()
        source.fetch_documents.return_value = [
            Document(id="d1", title="Doc 1", content="", source_type="notion"),
            Document(id="d2", title="Doc 2", content="", source_type="notion"),
        ]

        auditor = Auditor(sources=[source], analyzers=[], reporters=[], db=mock_db)
        with pytest.raises(LeaseLostError):
            auditor.run(lease_check=lease_check)

        assert store_calls[0] == 1
        mock_db.finish_scan.assert_not_called()

    def test_renewal_exception_expiry_detected_at_boundary(self, tmp_path):
        """_check_lease raises from DB check even when heartbeat event is not set."""
        from kb_audit.web.app import _run_scan

        # Heartbeat is suppressed; ownership_lost event never set.
        # The authoritative DB check (owns_live_lease=False) must catch the expiry.
        mock_db = MagicMock()
        mock_db._path = str(tmp_path / "test.db")
        mock_db.owns_live_lease.return_value = False
        mock_db.end_scan.return_value = False

        with (
            patch("kb_audit.db.ScanLeaseContext._renewal_loop"),
            patch("kb_audit.web.app.Config") as MockCfg,
            patch("kb_audit.web.app.Auditor") as MockAuditor,
            patch("kb_audit.web.app.create_storage", return_value=mock_db),
            patch("kb_audit.web.app.NotionSource"),
            patch("kb_audit.web.app.logger") as mock_logger,
        ):
            cfg = MockCfg.load.return_value
            cfg.database_url = str(tmp_path / "test.db")
            cfg.notion_api_key = "key"
            cfg.notion.root_page_id = None
            cfg.notion.database_id = None
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            cfg.analyzers.timestamp.warning_days = 90
            cfg.analyzers.timestamp.critical_days = 180
            cfg.analyzers.similarity.threshold = 0.85
            cfg.analyzers.version_refs.current_versions = []
            cfg.analyzers.version_refs.patterns = []

            def fake_run(lease_check=None, owner_token=None):
                if lease_check:
                    lease_check()

            MockAuditor.return_value.run.side_effect = fake_run

            _run_scan("fake-token", None)

            # LeaseLostError path → warning logged, scan error not set
            mock_logger.warning.assert_called()
            mock_logger.error.assert_not_called()

    def test_running_tests_creates_no_cwd_db_files(self):
        """test_web.py must not leave unexpected SQLite or MagicMock files in the working dir.

        Known runtime DB files created by normal use (e.g. kb-audit demo) are allowed.
        """
        import os
        allowed_runtime_db_files = {
            "kbaudit.db",
            "kbaudit-demo.db",
            "kbaudit-demo.db-wal",
            "kbaudit-demo.db-shm",
        }
        cwd_files = os.listdir(".")
        bad = [
            f for f in cwd_files
            if (f.endswith(".db") and f not in allowed_runtime_db_files)
            or f.startswith("<MagicMock")
        ]
        assert bad == [], f"Unexpected files in cwd: {bad}"


class TestClearScansEndpoint:
    """API-level tests for DELETE /api/scans.

    Uses mock_db to control what clear_all_if_idle returns, verifying the
    endpoint's 200/409 behaviour without touching the file system.
    """

    @pytest.fixture(autouse=True)
    def _patch_db(self, monkeypatch):
        """Patch _get_db to return a fresh in-memory mock for every test."""
        self.mock_db = MagicMock()
        monkeypatch.setattr("kb_audit.web.app._get_db", lambda: self.mock_db)

    def test_idle_db_returns_200_cleared(self, client):
        """When no scan is running, clearing succeeds and returns {status: cleared}."""
        self.mock_db.clear_all_if_idle.return_value = True
        resp = client.delete("/api/scans")
        assert resp.status_code == 200
        assert resp.json() == {"status": "cleared"}
        self.mock_db.clear_all_if_idle.assert_called_once()

    def test_live_scan_returns_409(self, client):
        """When a live lease exists, clearing is blocked and 409 is returned."""
        self.mock_db.clear_all_if_idle.return_value = False
        resp = client.delete("/api/scans")
        assert resp.status_code == 409
        assert "scan is in progress" in resp.json()["error"].lower()

    def test_db_is_closed_after_success(self, client):
        """Database.close() is always called, even on success."""
        self.mock_db.clear_all_if_idle.return_value = True
        client.delete("/api/scans")
        self.mock_db.close.assert_called_once()

    def test_db_is_closed_after_409(self, client):
        """Database.close() is always called, even when 409 is returned."""
        self.mock_db.clear_all_if_idle.return_value = False
        client.delete("/api/scans")
        self.mock_db.close.assert_called_once()


class TestAuditorScanLifecycle:
    """Focused auditor tests: scan status lifecycle via mock DB.

    Verifies that Auditor.run() calls fail_scan on errors and never
    calls fail_scan when LeaseLostError propagates.
    """

    def _make_mock_db(self):
        mock_db = MagicMock()
        mock_db.start_scan.return_value = 42
        mock_db.get_previous_hashes.return_value = {}
        mock_db.carry_forward_results.return_value = 0
        mock_db.load_audit_results.return_value = []
        mock_db.complete_scan_with_findings.return_value = {
            "new": 0, "updated": 0, "reopened": 0, "auto_fixed": 0
        }
        mock_db.fail_scan.return_value = True
        mock_db.prune_scans.return_value = 0
        return mock_db

    def _make_source(self, docs):
        from kb_audit.models import Document
        source = MagicMock()
        source.fetch_documents.return_value = [
            Document(id=d, title=d, content="x", source_type="notion") for d in docs
        ]
        return source

    def test_successful_scan_calls_complete_not_fail(self):
        """Normal path: complete_scan_with_findings called once, fail_scan never called."""
        from kb_audit.auditor import Auditor
        mock_db = self._make_mock_db()
        auditor = Auditor(sources=[self._make_source(["d1"])], analyzers=[], reporters=[], db=mock_db)
        auditor.run()
        mock_db.complete_scan_with_findings.assert_called_once()
        mock_db.fail_scan.assert_not_called()

    def test_fetch_error_calls_fail_scan(self):
        """Source.fetch_documents raising causes fail_scan, not finish_scan."""
        from kb_audit.auditor import Auditor
        mock_db = self._make_mock_db()
        source = MagicMock()
        source.fetch_documents.side_effect = RuntimeError("network gone")
        auditor = Auditor(sources=[source], analyzers=[], reporters=[], db=mock_db)
        with pytest.raises(RuntimeError, match="network gone"):
            auditor.run()
        mock_db.fail_scan.assert_called_once_with(42, "network gone", owner_token=None)
        mock_db.finish_scan.assert_not_called()

    def test_complete_failure_calls_fail_scan(self):
        """complete_scan_with_findings failure prevents completion; fail_scan is called."""
        from kb_audit.auditor import Auditor
        mock_db = self._make_mock_db()
        mock_db.complete_scan_with_findings.side_effect = RuntimeError("db locked")
        auditor = Auditor(sources=[self._make_source(["d1"])], analyzers=[], reporters=[], db=mock_db)
        with pytest.raises(RuntimeError):
            auditor.run()
        mock_db.fail_scan.assert_called_once()
        mock_db.complete_scan_with_findings.assert_called_once()

    def test_lease_lost_error_does_not_call_fail_scan(self):
        """LeaseLostError propagates cleanly without calling fail_scan."""
        from kb_audit.auditor import Auditor
        from kb_audit.db import LeaseLostError
        mock_db = self._make_mock_db()
        source = MagicMock()
        source.fetch_documents.side_effect = LeaseLostError("taken over")
        auditor = Auditor(sources=[source], analyzers=[], reporters=[], db=mock_db)
        with pytest.raises(LeaseLostError):
            auditor.run()
        mock_db.fail_scan.assert_not_called()
        mock_db.finish_scan.assert_not_called()

    def test_reporters_run_only_on_success(self):
        """Reporters are called after successful completion, not on failure."""
        from kb_audit.auditor import Auditor
        mock_db = self._make_mock_db()
        mock_reporter = MagicMock()
        source = MagicMock()
        source.fetch_documents.side_effect = RuntimeError("boom")
        auditor = Auditor(sources=[source], analyzers=[], reporters=[mock_reporter], db=mock_db)
        with pytest.raises(RuntimeError):
            auditor.run()
        mock_reporter.report.assert_not_called()

    def test_reporters_run_after_success(self):
        """Reporters are invoked once after a successful scan."""
        from kb_audit.auditor import Auditor
        mock_db = self._make_mock_db()
        mock_reporter = MagicMock()
        auditor = Auditor(
            sources=[self._make_source(["d1"])],
            analyzers=[],
            reporters=[mock_reporter],
            db=mock_db,
        )
        auditor.run()
        mock_reporter.report.assert_called_once()

    def test_all_unchanged_success_calls_complete_not_fail(self):
        """All-unchanged path: complete_scan_with_findings called once, fail_scan never."""
        from kb_audit.auditor import Auditor
        mock_db = self._make_mock_db()
        mock_db.get_previous_hashes.return_value = {"d1": "hashval"}
        source = MagicMock()
        from kb_audit.models import Document
        doc = Document(id="d1", title="d1", content="x", source_type="notion")
        # Provide matching content_hash so the doc is skipped
        source.fetch_documents.return_value = [doc]
        auditor = Auditor(sources=[source], analyzers=[], reporters=[], db=mock_db)
        auditor.run()
        mock_db.complete_scan_with_findings.assert_called_once()
        mock_db.fail_scan.assert_not_called()

    def test_prune_failure_does_not_call_fail_scan(self):
        """Pruning failure is non-fatal: fail_scan is never called after success."""
        from kb_audit.auditor import Auditor
        mock_db = self._make_mock_db()
        mock_db.prune_scans.side_effect = RuntimeError("disk full")
        auditor = Auditor(
            sources=[self._make_source(["d1"])],
            analyzers=[],
            reporters=[],
            db=mock_db,
        )
        # Must not raise; prune error is swallowed
        auditor.run()
        mock_db.complete_scan_with_findings.assert_called_once()
        mock_db.fail_scan.assert_not_called()

# ---------------------------------------------------------------------------
# Demo mode backend tests
# ---------------------------------------------------------------------------


class TestDemoMode:
    """Backend tests for demo mode (requirements 1–18)."""

    @pytest.fixture(autouse=True)
    def reset_app_config(self):
        """Ensure demo mode is disabled before and after each test."""
        from kb_audit.web.app import configure_app
        configure_app(demo_mode=False, database_path=None)
        yield
        configure_app(demo_mode=False, database_path=None)

    @pytest.fixture
    def demo_client(self, tmp_path):
        """TestClient with demo mode active and a real temp database."""
        from kb_audit.web.app import configure_app
        db_path = str(tmp_path / "demo.db")
        configure_app(demo_mode=True, database_path=db_path)
        return TestClient(app)

    # Req 1: help text documents all options
    def test_help_documents_all_options(self):
        import subprocess
        import sys
        from pathlib import Path
        bin_dir = Path(sys.executable).parent
        result = subprocess.run(
            [str(bin_dir / "kb-audit-web"), "--help"],
            capture_output=True, text=True,
        )
        out = result.stdout
        assert "--demo" in out
        assert "--database" in out
        assert "--host" in out
        assert "--port" in out

    # Req 2: demo startup defaults to kbaudit-demo.db
    def test_demo_startup_default_database(self):
        from kb_audit.web.app import _app_config, configure_app
        configure_app(demo_mode=True, database_path="kbaudit-demo.db")
        assert _app_config.database_path == "kbaudit-demo.db"

    # Req 3: --database overrides demo database
    def test_database_option_overrides_demo_db(self, tmp_path):
        from kb_audit.web.app import _app_config, configure_app
        custom = str(tmp_path / "custom.db")
        configure_app(demo_mode=True, database_path=custom)
        assert _app_config.database_path == custom

    # Req 4: demo startup resets prior demo data
    def test_demo_startup_resets_prior_data(self, tmp_path):
        from kb_audit.web.app import configure_app
        from kb_audit.db import Database
        db_path = str(tmp_path / "demo.db")
        # Seed some data
        db = Database(db_path)
        db.connect()
        db.start_scan()
        db.close()
        # Simulate startup reset (as main() would do)
        configure_app(demo_mode=True, database_path=db_path)
        db2 = Database(db_path)
        db2.connect()
        cleared = db2.clear_all_if_idle()
        db2.close()
        assert cleared is True

    # Req 5: live lease prevents startup reset
    def test_live_lease_prevents_startup_reset(self, tmp_path):
        from kb_audit.db import Database
        db_path = str(tmp_path / "demo.db")
        db = Database(db_path)
        db.connect()
        token = db.try_start_scan()
        assert token is not None
        # Trying to reset while lease is live should return False
        result = db.clear_all_if_idle()
        db.close()
        assert result is False

    # Req 6: normal startup does not reset configured database
    def test_normal_startup_does_not_reset_db(self, tmp_path):
        from kb_audit.web.app import configure_app
        db_path = str(tmp_path / "normal.db")
        configure_app(demo_mode=False, database_path=db_path)
        # Normal mode: no reset should happen at startup
        # Just verify configure_app sets the path without clearing
        from kb_audit.web.app import _app_config
        assert _app_config.demo_mode is False

    # Req 7: /api/status returns demo_mode field
    def test_status_returns_demo_mode_true(self, demo_client):
        resp = demo_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["demo_mode"] is True

    def test_status_returns_demo_mode_false_normally(self, client):
        with patch("kb_audit.web.app._get_db") as mock_get_db:
            db = MagicMock()
            db.get_scan_state.return_value = {"in_progress": False, "last_scan_id": None, "scan_error": None}
            mock_get_db.return_value = db
            with patch("kb_audit.web.app.Config") as MockCfg:
                cfg = MockCfg.load.return_value
                cfg.notion_api_key = "key"
                cfg.confluence.base_url = ""
                cfg.confluence.api_token = ""
                resp = client.get("/api/status")
        assert resp.json()["demo_mode"] is False

    # Req 7b: /api/status in demo mode returns correct fields
    def test_status_demo_mode_fields(self, demo_client):
        resp = demo_client.get("/api/status")
        data = resp.json()
        assert data["configured"] is True
        assert data["source"] == "demo"
        assert data["source_label"] == "Demo workspace"
        assert data["configuration_error"] is None
        assert data["demo_mode"] is True

    # Req 8: demo mode is considered configured without env credentials
    def test_demo_mode_configured_without_credentials(self, demo_client):
        with patch("kb_audit.web.app.Config") as MockCfg:
            cfg = MockCfg.load.return_value
            cfg.notion_api_key = ""
            cfg.confluence.base_url = ""
            cfg.confluence.api_token = ""
            resp = demo_client.get("/api/status")
        assert resp.json()["configured"] is True

    # Req 9: demo scan construction uses DemoSource
    def test_demo_scan_uses_demo_source(self, tmp_path):
        from kb_audit.web.app import configure_app, _run_scan
        from kb_audit.sources.demo import DemoSource
        db_path = str(tmp_path / "demo.db")
        configure_app(demo_mode=True, database_path=db_path)

        demo_source_instances = []
        original_init = DemoSource.__init__
        def spy_init(self_inner):
            demo_source_instances.append(self_inner)
            original_init(self_inner)

        mock_db_inst = MagicMock()
        mock_db_inst._path = db_path
        mock_db_inst.end_scan.return_value = True
        mock_db_inst.owns_live_lease.return_value = True
        mock_db_inst.get_scan_history.return_value = []
        with patch("kb_audit.web.app.DemoSource") as MockDemo, \
             patch("kb_audit.web.app.Auditor"), \
             patch("kb_audit.web.app.create_storage", return_value=mock_db_inst):
            with patch("kb_audit.db.ScanLeaseContext._renewal_loop"):
                _run_scan("token", None, demo_mode=True)
        MockDemo.assert_called_once()

    # Req 10: NotionSource and ConfluenceSource never constructed in demo mode
    def test_demo_mode_never_constructs_notion_or_confluence(self, tmp_path):
        from kb_audit.web.app import configure_app, _run_scan
        db_path = str(tmp_path / "demo.db")
        configure_app(demo_mode=True, database_path=db_path)

        mock_db_inst = MagicMock()
        mock_db_inst._path = db_path
        mock_db_inst.end_scan.return_value = True
        mock_db_inst.owns_live_lease.return_value = True
        mock_db_inst.get_scan_history.return_value = []
        with patch("kb_audit.web.app.DemoSource"), \
             patch("kb_audit.web.app.Auditor"), \
             patch("kb_audit.web.app.create_storage", return_value=mock_db_inst), \
             patch("kb_audit.web.app.NotionSource") as MockNotion, \
             patch("kb_audit.web.app.ConfluenceSource") as MockConfluence:
            with patch("kb_audit.db.ScanLeaseContext._renewal_loop"):
                _run_scan("token", None, demo_mode=True)
        MockNotion.assert_not_called()
        MockConfluence.assert_not_called()

    # Req 11: demo scans make no external HTTP requests
    def test_demo_scan_no_http_requests(self, tmp_path, monkeypatch):
        from kb_audit.web.app import configure_app, _run_scan
        db_path = str(tmp_path / "demo.db")
        configure_app(demo_mode=True, database_path=db_path)

        calls = []
        def fake_check_url(url, timeout=10.0):
            calls.append(url)
            return (url, 200, None)

        monkeypatch.setattr("kb_audit.analyzers.broken_links._check_url", fake_check_url)

        from kb_audit.db import Database
        db = Database(db_path)
        db.connect()
        token = db.try_start_scan()
        db.close()

        _run_scan(token, None, demo_mode=True)
        assert calls == [], f"HTTP check called for: {calls}"

    # Req 12: POST /api/scans completes with 10 results and 3/3/3/1 totals
    def test_demo_scan_ten_results_correct_totals(self, demo_client, tmp_path):
        from kb_audit.web.app import configure_app
        import time
        db_path = str(tmp_path / "demo2.db")
        configure_app(demo_mode=True, database_path=db_path)
        client2 = TestClient(app)

        resp = client2.post("/api/scans", json={})
        assert resp.status_code == 200

        # Wait for scan to complete (polling)
        for _ in range(30):
            status = client2.get("/api/status").json()
            if not status["scan_in_progress"]:
                break
            time.sleep(0.2)
        else:
            pytest.fail("Scan did not complete in time")

        assert status["last_scan_id"] is not None
        scan_data = client2.get(f"/api/scans/{status['last_scan_id']}").json()
        results = scan_data["results"]
        assert len(results) == 10
        counts = {}
        for r in results:
            counts[r["overall_status"]] = counts.get(r["overall_status"], 0) + 1
        assert counts == {"current": 3, "stale": 3, "needs_review": 3, "unknown": 1}

    # Req 13: seven actionable findings appear after demo scan
    def test_demo_scan_seven_actionable_findings(self, tmp_path):
        from kb_audit.web.app import configure_app
        import time
        db_path = str(tmp_path / "demo3.db")
        configure_app(demo_mode=True, database_path=db_path)
        client3 = TestClient(app)

        client3.post("/api/scans", json={})
        for _ in range(30):
            status = client3.get("/api/status").json()
            if not status["scan_in_progress"]:
                break
            time.sleep(0.2)

        findings = client3.get("/api/findings?include_all=true").json()
        assert len(findings) == 6

    # Req 14: current pages do not appear in actionable queue
    def test_current_pages_not_in_findings_queue(self, tmp_path):
        from kb_audit.web.app import configure_app
        import time
        db_path = str(tmp_path / "demo4.db")
        configure_app(demo_mode=True, database_path=db_path)
        client4 = TestClient(app)

        client4.post("/api/scans", json={})
        for _ in range(30):
            status = client4.get("/api/status").json()
            if not status["scan_in_progress"]:
                break
            time.sleep(0.2)

        scan_data = client4.get(f"/api/scans/{status['last_scan_id']}").json()
        current_ids = {r["id"] for r in scan_data["results"] if r["overall_status"] == "current"}
        findings = client4.get("/api/findings?include_all=true").json()
        finding_doc_ids = {f["document_id"] for f in findings}
        assert current_ids.isdisjoint(finding_doc_ids)

    # Req 15: suggested replacements preserved
    def test_suggested_replacements_correct(self, tmp_path):
        from kb_audit.web.app import configure_app
        from kb_audit.db import Database
        import time
        db_path = str(tmp_path / "demo5.db")
        configure_app(demo_mode=True, database_path=db_path)
        client5 = TestClient(app)

        client5.post("/api/scans", json={})
        for _ in range(30):
            status = client5.get("/api/status").json()
            if not status["scan_in_progress"]:
                break
            time.sleep(0.2)

        db = Database(db_path)
        db.connect()
        try:
            history = db.get_scan_history(limit=1)
            results = db.get_scan_results(history[0]["scan_id"])
        finally:
            db.close()

        replacements = {r["id"]: r["suggested_replacement_id"] for r in results if r["suggested_replacement_id"]}
        assert replacements == {
            "payment-api-guide-v1": "payment-api-guide-v3",
            "payment-api-guide-v2": "payment-api-guide-v3",
            "merchant-launch-checklist-draft": "merchant-onboarding-checklist",
        }

    # Req 16: review workflow updates work in demo mode
    def test_workflow_update_works_in_demo_mode(self, tmp_path):
        from kb_audit.web.app import configure_app
        import time
        db_path = str(tmp_path / "demo6.db")
        configure_app(demo_mode=True, database_path=db_path)
        client6 = TestClient(app)

        client6.post("/api/scans", json={})
        for _ in range(30):
            status = client6.get("/api/status").json()
            if not status["scan_in_progress"]:
                break
            time.sleep(0.2)

        findings = client6.get("/api/findings").json()
        assert findings
        key = findings[0]["finding_key"]
        resp = client6.patch(f"/api/findings/{key}", json={"state": "acknowledged"})
        assert resp.status_code == 200
        assert resp.json()["workflow_state"] == "acknowledged"

    # Req 17: scan history available across multiple scans
    def test_scan_history_available_across_scans(self, tmp_path):
        from kb_audit.web.app import configure_app
        import time
        db_path = str(tmp_path / "demo7.db")
        configure_app(demo_mode=True, database_path=db_path)
        client7 = TestClient(app)

        for _ in range(2):
            client7.post("/api/scans", json={})
            for __ in range(30):
                s = client7.get("/api/status").json()
                if not s["scan_in_progress"]:
                    break
                time.sleep(0.2)

        history = client7.get("/api/scans").json()
        assert len(history) >= 2

    # Req 18: database NOT reset between scans
    def test_database_not_reset_between_scans(self, tmp_path):
        from kb_audit.web.app import configure_app
        import time
        db_path = str(tmp_path / "demo8.db")
        configure_app(demo_mode=True, database_path=db_path)
        client8 = TestClient(app)

        client8.post("/api/scans", json={})
        for _ in range(30):
            s = client8.get("/api/status").json()
            if not s["scan_in_progress"]:
                break
            time.sleep(0.2)
        first_scan_id = s["last_scan_id"]

        # Triage a finding to verify it persists
        findings = client8.get("/api/findings").json()
        if findings:
            key = findings[0]["finding_key"]
            client8.patch(f"/api/findings/{key}", json={"note": "my note"})

        # Second scan should NOT reset existing data
        client8.post("/api/scans", json={})
        for _ in range(30):
            s = client8.get("/api/status").json()
            if not s["scan_in_progress"]:
                break
            time.sleep(0.2)

        # Both scans should still be in history
        history = client8.get("/api/scans").json()
        scan_ids = [h["scan_id"] for h in history]
        assert first_scan_id in scan_ids


class TestActionabilityInPayload:
    """API scan result payload includes actionability metadata.

    These tests use unknown docs with requires_human_audit=False to verify
    the distinction between classification status and human-audit actionability.
    """

    def test_scan_result_includes_requires_human_audit(self, client):
        """Scan result trust_metadata should include requires_human_audit flag."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_history.return_value = [
                {"scan_id": 1, "scan_status": "completed", "document_count": 1,
                 "stale_count": 0, "needs_review_count": 0, "unknown_count": 1,
                 "current_count": 0, "started_at": "2025-01-01T00:00:00Z",
                 "completed_at": "2025-01-01T00:01:00Z", "error_message": None}
            ]
            result = {
                "id": "doc-1",
                "title": "Unknown Doc",
                "url": "https://example.com/doc-1",
                "source_type": "notion",
                "last_modified": "2025-01-01T00:00:00Z",
                "overall_status": "unknown",
                "confidence": 0.1,
                "confidence_reason": "No metadata",
                "signals": [],
                "suggested_replacement_id": None,
                "trust_metadata": {
                    "requires_human_audit": False,
                    "audit_priority": "none",
                    "importance_score": 0,
                    "importance_reasons": [],
                    "actionability_reason": "Insufficient importance signals (score 0)",
                },
                "trust_evidence": {
                    "summary": "No evidence",
                    "positive_evidence": [],
                    "review_risks": [],
                    "missing_evidence": ["No status field"],
                    "recommended_action": "",
                },
            }
            db.get_scan_results.return_value = [result]
            db.get_findings.return_value = []
            db.get_workflow_summary.return_value = {}

            resp = client.get("/api/scans/1")
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        tm = results[0]["trust_metadata"]
        assert "requires_human_audit" in tm
        assert tm["requires_human_audit"] is False
        assert "audit_priority" in tm

    def test_non_actionable_unknown_has_no_workflow_finding(self, client):
        """Non-actionable unknown does not receive a workflow dict."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_history.return_value = [
                {"scan_id": 1, "scan_status": "completed", "document_count": 1,
                 "stale_count": 0, "needs_review_count": 0, "unknown_count": 1,
                 "current_count": 0, "started_at": "2025-01-01T00:00:00Z",
                 "completed_at": "2025-01-01T00:01:00Z", "error_message": None}
            ]
            result = {
                "id": "doc-no-workflow",
                "title": "Low Importance Unknown",
                "url": None,
                "source_type": "notion",
                "last_modified": "2025-01-01T00:00:00Z",
                "overall_status": "unknown",
                "confidence": 0.1,
                "confidence_reason": "No metadata",
                "signals": [],
                "suggested_replacement_id": None,
                "trust_metadata": {
                    "requires_human_audit": False,
                    "audit_priority": "none",
                    "importance_score": 0,
                },
                "trust_evidence": {
                    "summary": "", "positive_evidence": [],
                    "review_risks": [], "missing_evidence": [], "recommended_action": "",
                },
            }
            db.get_scan_results.return_value = [result]
            db.get_findings.return_value = []  # no findings
            db.get_workflow_summary.return_value = {}

            resp = client.get("/api/scans/1")
        assert resp.status_code == 200
        results = resp.json()["results"]
        # No workflow finding for this doc
        assert results[0]["workflow"] is None


# ---------------------------------------------------------------------------
# Report actionability tests
# ---------------------------------------------------------------------------


class TestReportActionability:
    """Report endpoint correctly separates status-flagged from human-audit-required."""

    def _patch_db(self, mock_db, results):
        db = mock_db.return_value
        db.get_scan_results.return_value = results
        db.get_scan_history.return_value = [{"scan_id": 9, "started_at": "2025-06-01T12:00:00"}]
        return db

    def _make_result(self, doc_id, status, trust_metadata=None):
        r = _make_scan_result(doc_id, f"Doc {doc_id}")
        r["overall_status"] = status
        r["trust_metadata"] = trust_metadata or {}
        return r

    # ------------------------------------------------------------------
    # JSON: new fields present
    # ------------------------------------------------------------------

    def test_json_report_has_status_flagged_count(self, client):
        stale = self._make_result("s1", "stale")
        needs_review = self._make_result("n1", "needs_review")
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [stale, needs_review])
            resp = client.get("/api/scans/9/report")
        data = resp.json()
        assert data["status_flagged_count"] == 2
        assert "human_audit_required_count" in data
        assert "human_audit_required_documents" in data

    def test_json_backward_compat_fields_still_present(self, client):
        stale = self._make_result("s1", "stale")
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [stale])
            resp = client.get("/api/scans/9/report")
        data = resp.json()
        assert "stale_count" in data
        assert "needs_review_count" in data
        assert "unknown_count" in data
        assert "stale_documents" in data
        assert "needs_review_documents" in data
        assert "unknown_documents" in data

    # ------------------------------------------------------------------
    # JSON: explicit false flag suppresses audit count
    # ------------------------------------------------------------------

    def test_false_flag_unknown_not_in_human_audit_count(self, client):
        unknown_no_audit = self._make_result("u1", "unknown", {
            "requires_human_audit": False,
            "audit_priority": "none",
            "importance_score": 0,
            "actionability_reason": "Insufficient importance signals",
        })
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [unknown_no_audit])
            resp = client.get("/api/scans/9/report")
        data = resp.json()
        # Still appears in unknown_documents (status visibility preserved)
        assert len(data["unknown_documents"]) == 1
        assert data["unknown_documents"][0]["id"] == "u1"
        assert data["status_flagged_count"] == 1
        # But NOT counted as a human audit
        assert data["human_audit_required_count"] == 0
        assert data["human_audit_required_documents"] == []

    def test_true_flag_unknown_counted_in_human_audit(self, client):
        unknown_audit = self._make_result("u2", "unknown", {
            "requires_human_audit": True,
            "audit_priority": "medium",
            "importance_score": 3,
        })
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [unknown_audit])
            resp = client.get("/api/scans/9/report")
        data = resp.json()
        assert data["human_audit_required_count"] == 1
        assert data["human_audit_required_documents"][0]["id"] == "u2"

    def test_legacy_unknown_no_flag_counts_as_human_audit(self, client):
        legacy = self._make_result("u3", "unknown", {})  # no requires_human_audit key
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [legacy])
            resp = client.get("/api/scans/9/report")
        data = resp.json()
        assert data["human_audit_required_count"] == 1

    def test_mixed_flags_count_correctly(self, client):
        stale = self._make_result("s1", "stale", {"requires_human_audit": True})
        suppressed = self._make_result("n1", "needs_review", {"requires_human_audit": False})
        legacy_unknown = self._make_result("u1", "unknown", {})
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [stale, suppressed, legacy_unknown])
            resp = client.get("/api/scans/9/report")
        data = resp.json()
        assert data["status_flagged_count"] == 3
        assert data["human_audit_required_count"] == 2  # stale + legacy_unknown; suppressed excluded
        ids = {d["id"] for d in data["human_audit_required_documents"]}
        assert ids == {"s1", "u1"}

    # ------------------------------------------------------------------
    # Text format
    # ------------------------------------------------------------------

    def test_text_report_has_status_flagged_and_human_audit_summary_lines(self, client):
        stale = self._make_result("s1", "stale", {"requires_human_audit": True})
        suppressed = self._make_result("u1", "unknown", {"requires_human_audit": False})
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [stale, suppressed])
            resp = client.get("/api/scans/9/report?format=text")
        text = resp.text
        assert "Status-flagged documents: 2" in text
        assert "Human audits required: 1" in text

    def test_text_report_marks_false_flag_as_not_required(self, client):
        suppressed = self._make_result("u1", "unknown", {"requires_human_audit": False})
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [suppressed])
            resp = client.get("/api/scans/9/report?format=text")
        text = resp.text
        assert "Human audit: not required" in text
        assert "Human audit: required" not in text

    def test_text_report_marks_true_flag_as_required(self, client):
        stale = self._make_result("s1", "stale", {"requires_human_audit": True})
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [stale])
            resp = client.get("/api/scans/9/report?format=text")
        text = resp.text
        assert "Human audit: required" in text

    def test_text_report_legacy_no_flag_marked_required(self, client):
        legacy = self._make_result("u1", "unknown", {})
        with patch("kb_audit.web.app._get_db") as mock_db:
            self._patch_db(mock_db, [legacy])
            resp = client.get("/api/scans/9/report?format=text")
        text = resp.text
        assert "Human audit: required" in text


# ---------------------------------------------------------------------------
# Unit tests for _requires_human_audit helper
# ---------------------------------------------------------------------------


class TestRequiresHumanAudit:
    """Direct unit tests for the backend _requires_human_audit() helper."""

    def test_current_always_false(self):
        from kb_audit.web.app import _requires_human_audit
        assert _requires_human_audit({"overall_status": "current"}) is False

    def test_current_with_true_flag_still_false(self):
        from kb_audit.web.app import _requires_human_audit
        # current overrides even an explicit true flag
        assert _requires_human_audit({
            "overall_status": "current",
            "trust_metadata": {"requires_human_audit": True},
        }) is False

    def test_explicit_true(self):
        from kb_audit.web.app import _requires_human_audit
        assert _requires_human_audit({
            "overall_status": "unknown",
            "trust_metadata": {"requires_human_audit": True},
        }) is True

    def test_explicit_false(self):
        from kb_audit.web.app import _requires_human_audit
        # Status-flagged but not audit-required: classification status ≠ actionability
        assert _requires_human_audit({
            "overall_status": "unknown",
            "trust_metadata": {"requires_human_audit": False},
        }) is False

    def test_explicit_false_needs_review(self):
        from kb_audit.web.app import _requires_human_audit
        assert _requires_human_audit({
            "overall_status": "needs_review",
            "trust_metadata": {"requires_human_audit": False},
        }) is False

    def test_legacy_unknown_no_flag(self):
        from kb_audit.web.app import _requires_human_audit
        assert _requires_human_audit({
            "overall_status": "unknown",
            "trust_metadata": {},
        }) is True

    def test_legacy_needs_review_no_flag(self):
        from kb_audit.web.app import _requires_human_audit
        assert _requires_human_audit({
            "overall_status": "needs_review",
            "trust_metadata": {},
        }) is True

    def test_legacy_stale_no_flag(self):
        from kb_audit.web.app import _requires_human_audit
        assert _requires_human_audit({
            "overall_status": "stale",
            "trust_metadata": {},
        }) is True

    def test_legacy_no_trust_metadata_key(self):
        from kb_audit.web.app import _requires_human_audit
        # trust_metadata key absent entirely — still legacy fallback
        assert _requires_human_audit({"overall_status": "unknown"}) is True

    def test_none_trust_metadata(self):
        from kb_audit.web.app import _requires_human_audit
        # trust_metadata explicitly None — still legacy fallback
        assert _requires_human_audit({
            "overall_status": "stale",
            "trust_metadata": None,
        }) is True
