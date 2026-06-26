"""Tests for the finding review workflow."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kb_audit.db import Database
from kb_audit.models import AuditResult, Document, Severity, StalenessSignal, build_finding_key


def _doc(
    id: str = "doc-1",
    title: str = "Test Doc",
    content: str = "Some content.",
    source_type: str = "test",
) -> Document:
    return Document(
        id=id, title=title, content=content, source_type=source_type,
        last_modified=datetime.now(timezone.utc),
    )


def _result(
    doc: Document | None = None,
    status: str = "stale",
    confidence: float = 0.7,
    reason: str = "Status field indicates 'Legacy'",
    signals: list[StalenessSignal] | None = None,
    trust_evidence: dict | None = None,
) -> AuditResult:
    if doc is None:
        doc = _doc()
    return AuditResult(
        document=doc,
        signals=signals or [],
        status=status,
        confidence=confidence,
        confidence_reason=reason,
        trust_evidence=trust_evidence or {
            "summary": "Stale because status field indicates 'Legacy'.",
            "positive_evidence": [],
            "review_risks": ["Status field indicates 'Legacy'"],
            "missing_evidence": [],
            "recommended_action": "Do not use as authoritative guidance",
        },
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.connect()
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Finding key and evidence hash
# ---------------------------------------------------------------------------


class TestFindingKey:
    def test_deterministic(self):
        r1 = _result()
        r2 = _result()
        assert r1.finding_key == r2.finding_key

    def test_changes_with_status(self):
        r_stale = _result(status="stale")
        r_review = _result(status="needs_review")
        assert r_stale.finding_key != r_review.finding_key

    def test_changes_with_doc_id(self):
        r1 = _result(doc=_doc(id="doc-1"))
        r2 = _result(doc=_doc(id="doc-2"))
        assert r1.finding_key != r2.finding_key

    def test_changes_with_source_type(self):
        r1 = _result(doc=_doc(source_type="notion"))
        r2 = _result(doc=_doc(source_type="confluence"))
        assert r1.finding_key != r2.finding_key


class TestEvidenceHash:
    def test_deterministic(self):
        r1 = _result()
        r2 = _result()
        assert r1.evidence_hash == r2.evidence_hash

    def test_changes_with_signals(self):
        sig = StalenessSignal("broken_link", Severity.WARNING, "Broken", details={})
        r1 = _result()
        r2 = _result(signals=[sig])
        assert r1.evidence_hash != r2.evidence_hash

    def test_changes_with_risks(self):
        r1 = _result(trust_evidence={
            "summary": "S", "review_risks": ["risk A"],
            "positive_evidence": [], "missing_evidence": [],
        })
        r2 = _result(trust_evidence={
            "summary": "S", "review_risks": ["risk B"],
            "positive_evidence": [], "missing_evidence": [],
        })
        assert r1.evidence_hash != r2.evidence_hash


# ---------------------------------------------------------------------------
# Actionable filtering
# ---------------------------------------------------------------------------


class TestActionableResults:
    def test_stale_is_actionable(self):
        results = Database._actionable_results([_result(status="stale")])
        assert len(results) == 1

    def test_needs_review_is_actionable(self):
        results = Database._actionable_results([_result(status="needs_review")])
        assert len(results) == 1

    def test_current_without_risks_not_actionable(self):
        r = _result(status="current", trust_evidence={
            "summary": "OK", "review_risks": [],
            "positive_evidence": ["Strong"], "missing_evidence": [],
        })
        results = Database._actionable_results([r])
        assert len(results) == 0

    def test_current_with_risks_not_actionable(self):
        """Under new semantics, a current doc should never have review_risks.
        If it somehow does, _actionable_results only checks status, so it
        won't be tracked.  The trust classifier prevents this case."""
        r = _result(status="current", trust_evidence={
            "summary": "OK", "review_risks": ["Old review date"],
            "positive_evidence": ["Strong"], "missing_evidence": [],
        })
        results = Database._actionable_results([r])
        assert len(results) == 0

    def test_unknown_is_actionable(self):
        r = _result(status="unknown", trust_evidence={
            "summary": "Unknown", "review_risks": [],
            "positive_evidence": [], "missing_evidence": ["No status"],
        })
        results = Database._actionable_results([r])
        assert len(results) == 1

    def test_current_is_not_actionable(self):
        results = Database._actionable_results([_result(status="current")])
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Sync findings
# ---------------------------------------------------------------------------


class TestSyncFindings:
    def test_new_findings_created_as_open(self, db):
        scan_id = db.start_scan()
        results = [_result()]
        stats = db.sync_findings(scan_id, results)
        assert stats["new"] == 1
        assert stats["updated"] == 0
        assert stats["reopened"] == 0

        findings = db.get_findings()
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "open"
        assert findings[0]["document_id"] == "doc-1"

    def test_rescan_same_evidence_updates_not_creates(self, db):
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, results)
        assert stats["new"] == 0
        assert stats["updated"] == 1

        findings = db.get_findings()
        assert len(findings) == 1
        assert findings[0]["last_seen_scan_id"] == scan2

    def test_dismissed_stays_dismissed_when_evidence_unchanged(self, db):
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        key = results[0].finding_key
        db.update_workflow(key, state="dismissed", dismissal_reason="False positive")

        scan2 = db.start_scan()
        db.sync_findings(scan2, results)

        findings = db.get_findings(include_all=True)
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "dismissed"

    def test_dismissed_reopened_when_evidence_changes(self, db):
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        key = results[0].finding_key
        db.update_workflow(key, state="dismissed")

        # Change the evidence
        new_sig = StalenessSignal("broken_link", Severity.WARNING, "Broken", details={})
        changed = [_result(signals=[new_sig])]
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, changed)
        assert stats["reopened"] == 1

        findings = db.get_findings()
        assert findings[0]["workflow_state"] == "open"

    def test_fixed_reopened_when_evidence_changes(self, db):
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        key = results[0].finding_key
        db.update_workflow(key, state="fixed")

        # Different trust evidence summary
        changed = [_result(trust_evidence={
            "summary": "Different summary now.",
            "review_risks": ["New risk"],
            "positive_evidence": [], "missing_evidence": [],
        })]
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, changed)
        assert stats["reopened"] == 1

    def test_accepted_risk_reopened_when_evidence_changes(self, db):
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        key = results[0].finding_key
        db.update_workflow(key, state="accepted_risk")

        changed = [_result(signals=[
            StalenessSignal("unresolved_reference", Severity.WARNING, "Unresolved"),
        ])]
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, changed)
        assert stats["reopened"] == 1

    def test_acknowledged_not_reopened_on_evidence_change(self, db):
        """Acknowledged is not a terminal state — don't reopen, just update."""
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        key = results[0].finding_key
        db.update_workflow(key, state="acknowledged")

        changed = [_result(signals=[
            StalenessSignal("broken_link", Severity.WARNING, "Broken"),
        ])]
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, changed)
        # acknowledged is NOT terminal → just update, not reopen
        assert stats["updated"] == 1
        assert stats["reopened"] == 0

        findings = db.get_findings()
        assert findings[0]["workflow_state"] == "acknowledged"

    def test_snoozed_past_due_reopened(self, db):
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        key = results[0].finding_key
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=past)

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, results)
        assert stats["reopened"] == 1

        findings = db.get_findings()
        assert findings[0]["workflow_state"] == "open"

    def test_snoozed_future_not_reopened(self, db):
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)

        key = results[0].finding_key
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, results)
        assert stats["updated"] == 1  # not reopened

        findings = db.get_findings(include_all=True)
        assert findings[0]["workflow_state"] == "snoozed"

    def test_current_docs_not_tracked(self, db):
        scan_id = db.start_scan()
        r = _result(status="current", trust_evidence={
            "summary": "OK", "review_risks": [],
            "positive_evidence": [], "missing_evidence": [],
        })
        stats = db.sync_findings(scan_id, [r])
        assert stats["new"] == 0
        findings = db.get_findings()
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# Unknown status workflow tracking
# ---------------------------------------------------------------------------


class TestUnknownWorkflow:
    """Unknown documents require human review and must receive workflow findings."""

    def test_unknown_creates_open_finding(self, db):
        scan_id = db.start_scan()
        r = _result(status="unknown")
        stats = db.sync_findings(scan_id, [r])
        assert stats["new"] == 1
        findings = db.get_findings()
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "open"
        assert findings[0]["document_id"] == r.document.id

    def test_current_creates_no_finding(self, db):
        scan_id = db.start_scan()
        r = _result(status="current")
        stats = db.sync_findings(scan_id, [r])
        assert stats["new"] == 0
        assert db.get_findings() == []

    def test_unknown_finding_appears_in_queue(self, db):
        scan_id = db.start_scan()
        r = _result(status="unknown")
        db.sync_findings(scan_id, [r])
        findings = db.get_findings(scan_id=scan_id)
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "open"

    def test_terminal_unknown_finding_excluded_from_actionable_queue(self, db):
        scan_id = db.start_scan()
        r = _result(status="unknown")
        db.sync_findings(scan_id, [r])
        db.update_workflow(r.finding_key, state="dismissed")
        # actionable-only query should exclude it
        assert db.get_findings(scan_id=scan_id) == []
        # include_all should see it
        all_f = db.get_findings(scan_id=scan_id, include_all=True)
        assert len(all_f) == 1
        assert all_f[0]["workflow_state"] == "dismissed"

    def test_future_snoozed_unknown_excluded_from_actionable_queue(self, db):
        scan_id = db.start_scan()
        r = _result(status="unknown")
        db.sync_findings(scan_id, [r])
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.update_workflow(r.finding_key, state="snoozed", snoozed_until=future)  # type: ignore[arg-type]
        assert db.get_findings(scan_id=scan_id) == []

    def test_unknown_auto_resolved_when_document_becomes_current(self, db):
        """When an unknown doc is rescanned and classified as current, its
        finding should be auto-fixed because current is not actionable."""
        scan1 = db.start_scan()
        r_unknown = _result(doc=_doc(id="d1"), status="unknown")
        db.sync_findings(scan1, [r_unknown], scanned_doc_ids={"d1"})
        assert db.get_finding(r_unknown.finding_key)["workflow_state"] == "open"

        # Second scan: same doc now classified as current (not in actionable results)
        scan2 = db.start_scan()
        r_current = _result(doc=_doc(id="d1"), status="current")
        stats = db.sync_findings(scan2, [r_current], scanned_doc_ids={"d1"})
        assert stats["auto_fixed"] == 1
        assert db.get_finding(r_unknown.finding_key)["workflow_state"] == "fixed"

    def test_unknown_finding_stays_open_on_rescan(self, db):
        """A persistently-unknown doc should keep its finding open across scans."""
        scan1 = db.start_scan()
        r = _result(status="unknown")
        db.sync_findings(scan1, [r])

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [r])
        assert stats["new"] == 0
        assert stats["auto_fixed"] == 0
        assert db.get_finding(r.finding_key)["workflow_state"] == "open"


# ---------------------------------------------------------------------------
# Update workflow
# ---------------------------------------------------------------------------


class TestUpdateWorkflow:
    def test_update_state(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        assert db.update_workflow(key, state="acknowledged")
        f = db.get_finding(key)
        assert f is not None
        assert f["workflow_state"] == "acknowledged"

    def test_update_note_and_owner(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        db.update_workflow(key, note="Checking with author", assigned_owner="alice@co.com")
        f = db.get_finding(key)
        assert f["note"] == "Checking with author"
        assert f["assigned_owner"] == "alice@co.com"

    def test_update_nonexistent_returns_false(self, db):
        assert not db.update_workflow("nonexistent", state="fixed")

    def test_update_due_date(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        db.update_workflow(key, due_date="2026-07-15")
        f = db.get_finding(key)
        assert f["due_date"] == "2026-07-15"

    def test_update_dismissal_reason(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        db.update_workflow(key, state="dismissed", dismissal_reason="False positive")
        f = db.get_finding(key)
        assert f["dismissal_reason"] == "False positive"


# ---------------------------------------------------------------------------
# Query / filter findings
# ---------------------------------------------------------------------------


class TestQueryFindings:
    def test_filter_by_state(self, db):
        scan_id = db.start_scan()
        r1 = _result(doc=_doc(id="d1"))
        r2 = _result(doc=_doc(id="d2"))
        db.sync_findings(scan_id, [r1, r2])
        db.update_workflow(r1.finding_key, state="dismissed")

        open_findings = db.get_findings(states=["open"])
        assert len(open_findings) == 1
        assert open_findings[0]["document_id"] == "d2"

        dismissed = db.get_findings(states=["dismissed"])
        assert len(dismissed) == 1

    def test_filter_by_scan_id(self, db):
        scan1 = db.start_scan()
        db.sync_findings(scan1, [_result(doc=_doc(id="d1"))])

        scan2 = db.start_scan()
        db.sync_findings(scan2, [_result(doc=_doc(id="d2"))])

        f1 = db.get_findings(scan_id=scan1)
        f2 = db.get_findings(scan_id=scan2)
        assert len(f1) == 1
        assert f1[0]["document_id"] == "d1"
        assert len(f2) == 1
        assert f2[0]["document_id"] == "d2"

    def test_snoozed_hidden_by_default(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)

        assert len(db.get_findings()) == 0
        assert len(db.get_findings(include_all=True)) == 1

    def test_expired_snooze_shown(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=past)

        # Expired snooze is shown even without include_snoozed
        assert len(db.get_findings()) == 1

    def test_workflow_summary_default_actionable_only(self, db):
        scan_id = db.start_scan()
        r1 = _result(doc=_doc(id="d1"))
        r2 = _result(doc=_doc(id="d2"))
        r3 = _result(doc=_doc(id="d3"))
        db.sync_findings(scan_id, [r1, r2, r3])
        db.update_workflow(r1.finding_key, state="dismissed")
        db.update_workflow(r2.finding_key, state="fixed")

        summary = db.get_workflow_summary()
        assert summary.get("open") == 1
        # Terminal states excluded from default
        assert "dismissed" not in summary
        assert "fixed" not in summary

    def test_workflow_summary_include_all(self, db):
        scan_id = db.start_scan()
        r1 = _result(doc=_doc(id="d1"))
        r2 = _result(doc=_doc(id="d2"))
        r3 = _result(doc=_doc(id="d3"))
        db.sync_findings(scan_id, [r1, r2, r3])
        db.update_workflow(r1.finding_key, state="dismissed")
        db.update_workflow(r2.finding_key, state="fixed")

        summary = db.get_workflow_summary(include_all=True)
        assert summary.get("open") == 1
        assert summary.get("dismissed") == 1
        assert summary.get("fixed") == 1


# ---------------------------------------------------------------------------
# Actionable queue defaults
# ---------------------------------------------------------------------------


class TestActionableQueueDefaults:
    """Default get_findings() and get_workflow_summary() return only
    actionable items: open, acknowledged, and expired-snoozed."""

    def test_default_excludes_fixed(self, db):
        scan_id = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan_id, [r])
        db.update_workflow(r.finding_key, state="fixed")

        assert len(db.get_findings()) == 0

    def test_default_excludes_dismissed(self, db):
        scan_id = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan_id, [r])
        db.update_workflow(r.finding_key, state="dismissed")

        assert len(db.get_findings()) == 0

    def test_default_excludes_accepted_risk(self, db):
        scan_id = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan_id, [r])
        db.update_workflow(r.finding_key, state="accepted_risk")

        assert len(db.get_findings()) == 0

    def test_default_excludes_future_snoozed(self, db):
        scan_id = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan_id, [r])
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(r.finding_key, state="snoozed", snoozed_until=future)

        assert len(db.get_findings()) == 0

    def test_default_includes_open(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result(doc=_doc(id="d1"))])

        assert len(db.get_findings()) == 1

    def test_default_includes_acknowledged(self, db):
        scan_id = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan_id, [r])
        db.update_workflow(r.finding_key, state="acknowledged")

        findings = db.get_findings()
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "acknowledged"

    def test_default_includes_expired_snoozed(self, db):
        scan_id = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan_id, [r])
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db.update_workflow(r.finding_key, state="snoozed", snoozed_until=past)

        findings = db.get_findings()
        assert len(findings) == 1

    def test_include_all_returns_everything(self, db):
        scan_id = db.start_scan()
        r1 = _result(doc=_doc(id="d1"))
        r2 = _result(doc=_doc(id="d2"))
        r3 = _result(doc=_doc(id="d3"))
        r4 = _result(doc=_doc(id="d4"))
        db.sync_findings(scan_id, [r1, r2, r3, r4])
        db.update_workflow(r1.finding_key, state="fixed")
        db.update_workflow(r2.finding_key, state="dismissed")
        db.update_workflow(r3.finding_key, state="accepted_risk")
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(r4.finding_key, state="snoozed", snoozed_until=future)

        # Default: nothing actionable
        assert len(db.get_findings()) == 0
        # All: everything returned
        assert len(db.get_findings(include_all=True)) == 4

    def test_explicit_states_bypasses_terminal_filter(self, db):
        """Explicit states= filter returns those states even if terminal."""
        scan_id = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan_id, [r])
        db.update_workflow(r.finding_key, state="fixed")

        findings = db.get_findings(states=["fixed"])
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "fixed"

    def test_scan_scoped_actionable_count(self, db):
        """Scan-scoped summary should match scan-scoped actionable findings."""
        scan1 = db.start_scan()
        r1 = _result(doc=_doc(id="d1"))
        r2 = _result(doc=_doc(id="d2"))
        db.sync_findings(scan1, [r1, r2], scanned_doc_ids={"d1", "d2"})
        db.update_workflow(r1.finding_key, state="fixed")

        # Actionable summary for scan1: only open d2
        summary = db.get_workflow_summary(scan_id=scan1)
        total = sum(summary.values())
        assert total == 1

        # Actionable findings for scan1: only d2
        findings = db.get_findings(scan_id=scan1)
        assert len(findings) == 1
        assert len(findings) == total


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


class TestWorkflowAPI:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from kb_audit.web.app import app
        return TestClient(app)

    def test_list_findings_empty(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_findings.return_value = []
            resp = client.get("/api/findings")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_findings_with_state_filter(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_findings.return_value = [{"finding_key": "abc", "workflow_state": "open"}]
            resp = client.get("/api/findings?state=open")
        db.get_findings.assert_called_once_with(
            scan_id=None, states=["open"], include_all=False,
        )
        assert resp.status_code == 200

    def test_findings_summary(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_workflow_summary.return_value = {"open": 3, "dismissed": 1}
            resp = client.get("/api/findings/summary")
        assert resp.status_code == 200
        assert resp.json() == {"open": 3, "dismissed": 1}

    def test_default_queue_count_matches_list(self, client):
        """Default /api/findings count matches /api/findings/summary total."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_findings.return_value = [
                {"finding_key": "a", "workflow_state": "open"},
                {"finding_key": "b", "workflow_state": "acknowledged"},
            ]
            db.get_workflow_summary.return_value = {"open": 1, "acknowledged": 1}
            findings_resp = client.get("/api/findings")
            summary_resp = client.get("/api/findings/summary")
        findings = findings_resp.json()
        summary = summary_resp.json()
        assert len(findings) == sum(summary.values())

    def test_include_all_returns_terminal(self, client):
        """include_all=true returns terminal states from /api/findings."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_findings.return_value = [
                {"finding_key": "a", "workflow_state": "fixed"},
            ]
            resp = client.get("/api/findings?include_all=true")
        db.get_findings.assert_called_once_with(
            scan_id=None, states=None, include_all=True,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_update_finding(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.update_workflow.return_value = True
            db.get_finding.return_value = {
                "finding_key": "abc123", "workflow_state": "dismissed",
            }
            resp = client.patch("/api/findings/abc123", json={"state": "dismissed"})
        assert resp.status_code == 200
        assert resp.json()["workflow_state"] == "dismissed"

    def test_update_finding_invalid_state(self, client):
        resp = client.patch("/api/findings/abc123", json={"state": "invalid"})
        assert resp.status_code == 422

    def test_update_finding_not_found(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.update_workflow.return_value = False
            resp = client.patch("/api/findings/abc123", json={"state": "fixed"})
        assert resp.status_code == 404

    def test_get_finding(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_finding.return_value = {
                "finding_key": "abc123", "workflow_state": "open",
            }
            resp = client.get("/api/findings/abc123")
        assert resp.status_code == 200
        assert resp.json()["finding_key"] == "abc123"

    def test_get_finding_not_found(self, client):
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_finding.return_value = None
            resp = client.get("/api/findings/nonexistent")
        assert resp.status_code == 404

    def test_scan_results_include_workflow_summary(self, client):
        """GET /api/scans/{id} response includes workflow_summary."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_scan_results.return_value = []
            db.get_scan_history.return_value = [
                {"scan_id": 1, "started_at": "2025-01-01T00:00:00"},
            ]
            db.get_findings.return_value = []
            db.get_scan_diff.return_value = []
            db.get_workflow_summary.return_value = {"open": 2, "dismissed": 1}
            resp = client.get("/api/scans/1")
        assert resp.status_code == 200
        data = resp.json()
        assert "workflow_summary" in data
        assert data["workflow_summary"]["open"] == 2
        # workflow_summary uses actionable-only by default; the mock
        # returns the same value for both calls so "dismissed" appears here
        assert "workflow_summary_all" in data

    def test_scan_results_include_workflow_per_result(self, client):
        """GET /api/scans/{id} enriches each result with a workflow field."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            expected_key = build_finding_key("notion", "doc-1", "stale")
            db.get_scan_results.return_value = [
                {"id": "doc-1", "title": "Test", "overall_status": "stale",
                 "source_type": "notion", "confidence": 0.8, "signals": []},
            ]
            db.get_scan_history.return_value = [
                {"scan_id": 5, "started_at": "2025-01-01T00:00:00"},
            ]
            db.get_findings.return_value = [
                {"document_id": "doc-1", "finding_key": expected_key,
                 "workflow_state": "acknowledged", "note": "reviewing",
                 "assigned_owner": "alice", "due_date": "2025-02-01",
                 "snoozed_until": None},
            ]
            db.get_scan_diff.return_value = []
            db.get_workflow_summary.return_value = {"acknowledged": 1}
            resp = client.get("/api/scans/5")
        assert resp.status_code == 200
        data = resp.json()
        results = data["results"]
        assert len(results) == 1
        wf = results[0]["workflow"]
        assert wf is not None
        assert wf["finding_key"] == expected_key
        assert wf["state"] == "acknowledged"
        assert wf["assigned_owner"] == "alice"
        assert wf["note"] == "reviewing"

    def test_findings_summary_with_scan_id(self, client):
        """GET /api/findings/summary?scan_id= passes scan_id through."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_workflow_summary.return_value = {"open": 1}
            resp = client.get("/api/findings/summary?scan_id=3")
        db.get_workflow_summary.assert_called_once_with(scan_id=3, include_all=False)
        assert resp.status_code == 200

    def test_list_findings_with_scan_id_and_include_all(self, client):
        """GET /api/findings with scan_id and include_all params."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_findings.return_value = []
            resp = client.get("/api/findings?scan_id=2&include_all=true&state=open,snoozed")
        db.get_findings.assert_called_once_with(
            scan_id=2, states=["open", "snoozed"], include_all=True,
        )
        assert resp.status_code == 200

    def test_update_finding_with_all_fields(self, client):
        """PATCH /api/findings/{key} accepts all workflow fields."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.update_workflow.return_value = True
            db.get_finding.return_value = {
                "finding_key": "abc123", "workflow_state": "snoozed",
                "note": "will check later", "assigned_owner": "bob",
                "due_date": "2025-03-01", "snoozed_until": "2025-02-15",
                "dismissal_reason": "",
            }
            resp = client.patch("/api/findings/abc123", json={
                "state": "snoozed",
                "note": "will check later",
                "assigned_owner": "bob",
                "due_date": "2025-03-01",
                "snoozed_until": "2025-02-15",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["workflow_state"] == "snoozed"
        assert data["assigned_owner"] == "bob"
        assert data["snoozed_until"] == "2025-02-15"


# ---------------------------------------------------------------------------
# Integration: auditor → workflow sync
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fix 1: clear_all() clears finding_workflow
# ---------------------------------------------------------------------------


class TestClearAll:
    def test_clear_all_removes_findings(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        assert len(db.get_findings()) == 1

        db.clear_all()
        assert len(db.get_findings()) == 0

    def test_clear_all_on_db_without_workflow_table(self, tmp_path):
        """clear_all() handles databases created before finding_workflow existed."""
        import sqlite3
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, document_count INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS documents (id TEXT, scan_id INTEGER, title TEXT, content_hash TEXT, source_type TEXT, url TEXT, last_modified TEXT, metadata TEXT DEFAULT '{}', PRIMARY KEY(id, scan_id));
            CREATE TABLE IF NOT EXISTS audit_results (document_id TEXT, scan_id INTEGER, overall_status TEXT, signals TEXT DEFAULT '[]', suggested_replacement_id TEXT, confidence REAL DEFAULT 0.0, confidence_reason TEXT DEFAULT '', trust_data TEXT DEFAULT '{}', PRIMARY KEY(document_id, scan_id));
            INSERT INTO sqlite_sequence (name, seq) VALUES ('scans', 0);
        """)
        conn.close()
        database = Database(db_path)
        database.connect()
        # Should not raise even though finding_workflow doesn't exist yet
        database.clear_all()
        database.close()


# ---------------------------------------------------------------------------
# Fix 2: get_finding() retrieves snoozed findings
# ---------------------------------------------------------------------------


class TestGetFindingSnoozed:
    def test_snoozed_finding_hidden_from_list(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)
        # get_findings() hides it by default
        assert len(db.get_findings()) == 0

    def test_snoozed_finding_returned_by_get_finding(self, db):
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        key = _result().finding_key
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)
        # get_finding() still returns it
        f = db.get_finding(key)
        assert f is not None
        assert f["workflow_state"] == "snoozed"


class TestGetFindingSnoozedAPI:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from kb_audit.web.app import app
        return TestClient(app)

    def test_api_get_snoozed_finding(self, client):
        """GET /api/findings/{key} returns a future-snoozed finding."""
        with patch("kb_audit.web.app._get_db") as mock_db:
            db = mock_db.return_value
            db.get_finding.return_value = {
                "finding_key": "abc123",
                "workflow_state": "snoozed",
                "snoozed_until": "2099-01-01T00:00:00",
                "note": "",
                "assigned_owner": "",
                "audit_context": None,
            }
            resp = client.get("/api/findings/abc123")
        assert resp.status_code == 200
        assert resp.json()["workflow_state"] == "snoozed"


# ---------------------------------------------------------------------------
# Fix 3: Enriched findings with audit context
# ---------------------------------------------------------------------------


class TestAuditContextEnrichment:
    def test_get_finding_has_audit_context(self, db):
        """get_finding() enriches with audit_context from audit_results."""
        scan_id = db.start_scan()
        doc = _doc(id="ctx-1", title="Context Test")
        result = _result(doc=doc)
        db.store_document(scan_id, doc)
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)

        f = db.get_finding(result.finding_key)
        assert f is not None
        ctx = f["audit_context"]
        assert ctx is not None
        assert ctx["overall_status"] == "stale"
        assert ctx["confidence"] == 0.7

    def test_get_findings_has_audit_context(self, db):
        """get_findings() enriches each finding with audit_context."""
        scan_id = db.start_scan()
        doc = _doc(id="ctx-2", title="Context Test 2")
        result = _result(doc=doc)
        db.store_document(scan_id, doc)
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)

        findings = db.get_findings()
        assert len(findings) == 1
        ctx = findings[0]["audit_context"]
        assert ctx is not None
        assert ctx["overall_status"] == "stale"
        assert "trust_evidence" in ctx

    def test_audit_context_null_when_result_pruned(self, db):
        """audit_context is None when audit result no longer exists."""
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        # We didn't store the document/result in the DB, so no join possible
        f = db.get_finding(_result().finding_key)
        assert f is not None
        assert f["audit_context"] is None

    def test_audit_context_includes_url(self, db):
        scan_id = db.start_scan()
        doc = _doc(id="url-1")
        doc.url = "https://example.com/doc"
        result = _result(doc=doc)
        db.store_document(scan_id, doc)
        db.store_result(scan_id, result)
        db.sync_findings(scan_id, [result])
        db.finish_scan(scan_id, 1)

        f = db.get_finding(result.finding_key)
        assert f["audit_context"]["url"] == "https://example.com/doc"


# ---------------------------------------------------------------------------
# Fix 4: Stronger evidence_hash
# ---------------------------------------------------------------------------


class TestEvidenceHashStrengthened:
    def test_broken_url_detail_changes_hash(self):
        """Different URLs in broken_link signal details produce different hashes."""
        sig_a = StalenessSignal(
            "broken_link", Severity.WARNING, "Broken link",
            details={"url": "https://old.example.com/page"},
        )
        sig_b = StalenessSignal(
            "broken_link", Severity.WARNING, "Broken link",
            details={"url": "https://new.example.com/page"},
        )
        r1 = _result(signals=[sig_a])
        r2 = _result(signals=[sig_b])
        assert r1.evidence_hash != r2.evidence_hash

    def test_same_signals_different_order_same_hash(self):
        """Signals in different order produce the same hash."""
        sig_a = StalenessSignal("broken_link", Severity.WARNING, "Link A", details={"url": "a"})
        sig_b = StalenessSignal("version_ref", Severity.CRITICAL, "Version B", details={"version": "1"})
        r1 = _result(signals=[sig_a, sig_b])
        r2 = _result(signals=[sig_b, sig_a])
        assert r1.evidence_hash == r2.evidence_hash

    def test_positive_evidence_changes_hash(self):
        ev1 = {
            "summary": "S", "positive_evidence": ["Has owner"],
            "review_risks": [], "missing_evidence": [],
            "recommended_action": "",
        }
        ev2 = {
            "summary": "S", "positive_evidence": ["Has owner", "Recently reviewed"],
            "review_risks": [], "missing_evidence": [],
            "recommended_action": "",
        }
        r1 = _result(trust_evidence=ev1)
        r2 = _result(trust_evidence=ev2)
        assert r1.evidence_hash != r2.evidence_hash

    def test_missing_evidence_changes_hash(self):
        ev1 = {
            "summary": "S", "positive_evidence": [],
            "review_risks": [], "missing_evidence": ["No status"],
            "recommended_action": "",
        }
        ev2 = {
            "summary": "S", "positive_evidence": [],
            "review_risks": [], "missing_evidence": ["No status", "No owner"],
            "recommended_action": "",
        }
        r1 = _result(trust_evidence=ev1)
        r2 = _result(trust_evidence=ev2)
        assert r1.evidence_hash != r2.evidence_hash

    def test_recommended_action_changes_hash(self):
        ev1 = {
            "summary": "S", "positive_evidence": [],
            "review_risks": [], "missing_evidence": [],
            "recommended_action": "Do not use",
        }
        ev2 = {
            "summary": "S", "positive_evidence": [],
            "review_risks": [], "missing_evidence": [],
            "recommended_action": "Use with caution",
        }
        r1 = _result(trust_evidence=ev1)
        r2 = _result(trust_evidence=ev2)
        assert r1.evidence_hash != r2.evidence_hash

    def test_unchanged_evidence_preserves_terminal_state(self, db):
        """Same evidence across rescans keeps dismissed/fixed intact."""
        scan1 = db.start_scan()
        results = [_result()]
        db.sync_findings(scan1, results)
        key = results[0].finding_key
        db.update_workflow(key, state="dismissed")

        # Rescan with identical results
        scan2 = db.start_scan()
        db.sync_findings(scan2, results)
        f = db.get_finding(key)
        assert f["workflow_state"] == "dismissed"

    def test_changed_signal_details_reopen_terminal(self, db):
        """Changed signal details (same type/severity) reopens dismissed finding."""
        sig_v1 = StalenessSignal(
            "broken_link", Severity.WARNING, "Broken",
            details={"url": "https://example.com/old"},
        )
        scan1 = db.start_scan()
        results = [_result(signals=[sig_v1])]
        db.sync_findings(scan1, results)
        key = results[0].finding_key
        db.update_workflow(key, state="fixed")

        # Rescan with different URL detail
        sig_v2 = StalenessSignal(
            "broken_link", Severity.WARNING, "Broken",
            details={"url": "https://example.com/new"},
        )
        changed = [_result(signals=[sig_v2])]
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, changed)
        assert stats["reopened"] == 1

        f = db.get_finding(key)
        assert f["workflow_state"] == "open"


# ---------------------------------------------------------------------------
# Auto-resolution: findings disappear from scan
# ---------------------------------------------------------------------------


class TestAutoResolution:
    """When a document is rescanned and its finding disappears, the finding
    should be auto-resolved to 'fixed'."""

    def test_open_finding_auto_fixed_when_issue_disappears(self, db):
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        # Rescan same doc but it's no longer actionable
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids={"d1"})
        assert stats["auto_fixed"] == 1

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "fixed"
        assert "No longer detected" in f["note"]

    def test_acknowledged_finding_auto_fixed(self, db):
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})
        db.update_workflow(r.finding_key, state="acknowledged")

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids={"d1"})
        assert stats["auto_fixed"] == 1

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "fixed"

    def test_snoozed_finding_auto_fixed_when_issue_disappears(self, db):
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(r.finding_key, state="snoozed", snoozed_until=future)

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids={"d1"})
        assert stats["auto_fixed"] == 1

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "fixed"

    def test_accepted_risk_auto_fixed_when_issue_disappears(self, db):
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})
        db.update_workflow(r.finding_key, state="accepted_risk")

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids={"d1"})
        assert stats["auto_fixed"] == 1

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "fixed"

    def test_dismissed_auto_fixed_when_issue_disappears(self, db):
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})
        db.update_workflow(r.finding_key, state="dismissed")

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids={"d1"})
        assert stats["auto_fixed"] == 1

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "fixed"

    def test_finding_not_fixed_when_doc_absent_from_scan(self, db):
        """Partial scan: doc not scanned → finding left alone."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        # Partial scan that doesn't include d1
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids={"d2"})
        assert stats["auto_fixed"] == 0

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "open"

    def test_last_seen_scan_id_unchanged_on_auto_fix(self, db):
        """last_seen_scan_id stays at the scan where finding was last detected."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        scan2 = db.start_scan()
        db.sync_findings(scan2, [], scanned_doc_ids={"d1"})

        f = db.get_finding(r.finding_key)
        assert f["last_seen_scan_id"] == scan1

    def test_last_checked_scan_id_updates_on_scan(self, db):
        """last_checked_scan_id tracks the most recent scan of the document."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})
        f = db.get_finding(r.finding_key)
        assert f["last_checked_scan_id"] == scan1

        # Rescan with finding still present
        scan2 = db.start_scan()
        db.sync_findings(scan2, [r], scanned_doc_ids={"d1"})
        f = db.get_finding(r.finding_key)
        assert f["last_checked_scan_id"] == scan2

    def test_last_checked_scan_id_updates_on_auto_fix(self, db):
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        scan2 = db.start_scan()
        db.sync_findings(scan2, [], scanned_doc_ids={"d1"})

        f = db.get_finding(r.finding_key)
        assert f["last_checked_scan_id"] == scan2

    def test_workflow_summary_includes_auto_fixed(self, db):
        scan1 = db.start_scan()
        r1 = _result(doc=_doc(id="d1"))
        r2 = _result(doc=_doc(id="d2"))
        db.sync_findings(scan1, [r1, r2], scanned_doc_ids={"d1", "d2"})

        # Rescan: d1 is clean, d2 still actionable
        scan2 = db.start_scan()
        db.sync_findings(scan2, [r2], scanned_doc_ids={"d1", "d2"})

        summary = db.get_workflow_summary(include_all=True)
        assert summary.get("fixed", 0) == 1

    def test_already_fixed_not_double_counted(self, db):
        """A finding already in 'fixed' state doesn't get auto-fixed again."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})
        db.update_workflow(r.finding_key, state="fixed")

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids={"d1"})
        # Already fixed — not in auto_fixable states, so no action
        assert stats["auto_fixed"] == 0

    def test_reopen_still_works_after_auto_fix(self, db):
        """If a finding was auto-fixed then reappears with new evidence, reopen."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        # Auto-fix
        scan2 = db.start_scan()
        db.sync_findings(scan2, [], scanned_doc_ids={"d1"})
        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "fixed"

        # Reappears with changed evidence
        new_sig = StalenessSignal("broken_link", Severity.WARNING, "Broken", details={})
        changed = [_result(doc=_doc(id="d1"), signals=[new_sig])]
        scan3 = db.start_scan()
        stats = db.sync_findings(scan3, changed, scanned_doc_ids={"d1"})
        assert stats["reopened"] == 1

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "open"

    def test_no_scanned_doc_ids_skips_auto_resolve(self, db):
        """Backward compat: if scanned_doc_ids is None, no auto-resolution."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r])

        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [])
        assert stats["auto_fixed"] == 0

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "open"

    def test_auto_fixed_appears_in_scan_scoped_findings(self, db):
        """Auto-fixed findings appear in get_findings(scan_id=scan2, include_all=True)."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        scan2 = db.start_scan()
        db.sync_findings(scan2, [], scanned_doc_ids={"d1"})

        # Default (actionable) excludes fixed
        findings = db.get_findings(scan_id=scan2)
        assert len(findings) == 0

        # include_all shows the auto-fixed finding
        findings = db.get_findings(scan_id=scan2, include_all=True)
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "fixed"
        assert findings[0]["last_seen_scan_id"] == scan1
        assert findings[0]["last_checked_scan_id"] == scan2

    def test_auto_fixed_counted_in_scan_scoped_summary(self, db):
        """get_workflow_summary(scan_id=scan2, include_all=True) counts the auto-fixed finding."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        scan2 = db.start_scan()
        db.sync_findings(scan2, [], scanned_doc_ids={"d1"})

        # Default (actionable) — fixed not counted
        summary = db.get_workflow_summary(scan_id=scan2)
        assert summary.get("fixed", 0) == 0

        # include_all — fixed is counted
        summary = db.get_workflow_summary(scan_id=scan2, include_all=True)
        assert summary.get("fixed", 0) == 1

    def test_skipped_unchanged_doc_not_auto_fixed(self, db):
        """A document excluded from scanned_doc_ids must not be auto-fixed,
        even if its results were carried forward in the DB."""
        scan1 = db.start_scan()
        r = _result(doc=_doc(id="d1"))
        db.sync_findings(scan1, [r], scanned_doc_ids={"d1"})

        # Simulate a scan where d1 is unchanged (skipped) — its doc ID
        # is NOT in scanned_doc_ids passed to sync_findings.
        scan2 = db.start_scan()
        stats = db.sync_findings(scan2, [], scanned_doc_ids=set())
        assert stats["auto_fixed"] == 0

        f = db.get_finding(r.finding_key)
        assert f["workflow_state"] == "open"


# ---------------------------------------------------------------------------
# Integration: auditor → workflow sync
# ---------------------------------------------------------------------------


class TestAuditorWorkflowIntegration:
    def test_scan_creates_findings(self, db):
        """A full auditor run should create workflow findings for stale docs."""
        from kb_audit.auditor import Auditor
        from kb_audit.analyzers.timestamp import TimestampAnalyzer

        doc = _doc(id="int-1", title="Old Guide", content="Status: Legacy\nOld content.")

        class FakeSource:
            def fetch_documents(self):
                return iter([doc])
            def close(self):
                pass

        auditor = Auditor(
            sources=[FakeSource()],
            analyzers=[TimestampAnalyzer()],
            reporters=[],
            db=db,
        )
        auditor.run()

        findings = db.get_findings()
        assert len(findings) >= 1
        assert any(f["document_id"] == "int-1" for f in findings)
        assert all(f["workflow_state"] == "open" for f in findings)

    def test_unchanged_doc_finding_not_auto_fixed(self, db):
        """When a stale doc is unchanged on rescan, its finding must survive."""
        from kb_audit.auditor import Auditor
        from kb_audit.analyzers.timestamp import TimestampAnalyzer

        doc = _doc(id="int-2", title="Old Guide", content="Status: Legacy\nOld content.")

        class FakeSource:
            def fetch_documents(self):
                return iter([doc])
            def close(self):
                pass

        auditor = Auditor(
            sources=[FakeSource()],
            analyzers=[TimestampAnalyzer()],
            reporters=[],
            db=db,
        )

        # First scan: creates the finding
        auditor.run()
        findings = db.get_findings()
        assert any(f["document_id"] == "int-2" for f in findings)
        assert all(f["workflow_state"] == "open" for f in findings)

        # Second scan: doc unchanged → skipped, result carried forward
        auditor.run()
        findings = db.get_findings()
        stale_findings = [f for f in findings if f["document_id"] == "int-2"]
        assert len(stale_findings) == 1
        # Must NOT be auto-fixed — the doc was skipped, not re-analyzed
        assert stale_findings[0]["workflow_state"] == "open"

    def test_carried_forward_finding_appears_in_scan2_queue(self, db):
        """Scan 2 queue includes carried-forward stale finding from scan 1."""
        from kb_audit.auditor import Auditor
        from kb_audit.analyzers.timestamp import TimestampAnalyzer

        doc = _doc(id="cf-1", title="Old Guide", content="Status: Legacy\nOld content.")

        class FakeSource:
            def fetch_documents(self):
                return iter([doc])
            def close(self):
                pass

        auditor = Auditor(
            sources=[FakeSource()],
            analyzers=[TimestampAnalyzer()],
            reporters=[],
            db=db,
        )

        # Scan 1: creates finding
        auditor.run()
        scan1 = db.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

        # Scan 2: doc unchanged
        auditor.run()
        scan2 = db.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert scan2 > scan1

        # Finding should appear in scan 2 queue (last_checked_scan_id = scan2)
        findings = db.get_findings(scan_id=scan2)
        stale = [f for f in findings if f["document_id"] == "cf-1"]
        assert len(stale) == 1
        assert stale[0]["workflow_state"] == "open"

    def test_all_unchanged_still_syncs_workflow(self, db):
        """When every document is unchanged, workflow last_checked_scan_id still advances."""
        from kb_audit.auditor import Auditor
        from kb_audit.analyzers.timestamp import TimestampAnalyzer

        doc = _doc(id="cf-2", title="Old Guide", content="Status: Legacy\nOld content.")

        class FakeSource:
            def fetch_documents(self):
                return iter([doc])
            def close(self):
                pass

        auditor = Auditor(
            sources=[FakeSource()],
            analyzers=[TimestampAnalyzer()],
            reporters=[],
            db=db,
        )

        # Scan 1
        auditor.run()
        scan1 = db.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        f1 = [f for f in db.get_findings() if f["document_id"] == "cf-2"]
        assert f1 and f1[0]["last_checked_scan_id"] == scan1

        # Scan 2: all docs unchanged
        auditor.run()
        scan2 = db.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert scan2 > scan1

        # last_checked_scan_id must have advanced to scan2
        f2 = [f for f in db.get_findings() if f["document_id"] == "cf-2"]
        assert f2 and f2[0]["last_checked_scan_id"] == scan2
        # last_seen_scan_id stays at scan1 (issue still present)
        assert f2[0]["last_seen_scan_id"] == scan1

    def test_summary_includes_carried_forward_for_current_scan(self, db):
        """get_workflow_summary(scan_id=scan2) counts carried-forward findings."""
        from kb_audit.auditor import Auditor
        from kb_audit.analyzers.timestamp import TimestampAnalyzer

        doc = _doc(id="cf-3", title="Old Guide", content="Status: Legacy\nOld content.")

        class FakeSource:
            def fetch_documents(self):
                return iter([doc])
            def close(self):
                pass

        auditor = Auditor(
            sources=[FakeSource()],
            analyzers=[TimestampAnalyzer()],
            reporters=[],
            db=db,
        )
        auditor.run()
        auditor.run()
        scan2 = db.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

        summary = db.get_workflow_summary(scan_id=scan2)
        assert sum(summary.values()) >= 1

    def test_mixed_scan_unchanged_stale_visible(self, db):
        """Mixed scan: changed clean doc + unchanged stale doc → stale still in queue."""
        from datetime import datetime, timezone
        from kb_audit.auditor import Auditor
        from kb_audit.analyzers.timestamp import TimestampAnalyzer
        from kb_audit.models import Document

        stale_doc = Document(
            id="cf-4", title="Old Guide",
            content="Status: Legacy\nOld content.",
            source_type="test",
            last_modified=datetime.now(timezone.utc),
        )
        # Current doc will be different between scan 1 and scan 2 (content changes)
        current_docs = [
            Document(
                id="cf-5", title="Good Doc v1",
                content="Status: Current\nOwner: Team\nContent v1.",
                source_type="test",
                last_modified=datetime.now(timezone.utc),
            ),
            Document(
                id="cf-5", title="Good Doc v2",
                content="Status: Current\nOwner: Team\nContent v2.",
                source_type="test",
                last_modified=datetime.now(timezone.utc),
            ),
        ]
        call_count = [0]

        class FakeSource:
            def fetch_documents(self):
                idx = call_count[0]
                call_count[0] += 1
                if idx == 0:
                    return iter([stale_doc, current_docs[0]])
                return iter([stale_doc, current_docs[1]])
            def close(self):
                pass

        auditor = Auditor(
            sources=[FakeSource()],
            analyzers=[TimestampAnalyzer()],
            reporters=[],
            db=db,
        )

        # Scan 1
        auditor.run()

        # Scan 2: stale_doc unchanged, current_doc changed
        auditor.run()
        scan2 = db.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

        # Stale finding must still be in the scan 2 queue
        findings = db.get_findings(scan_id=scan2)
        stale_findings = [f for f in findings if f["document_id"] == "cf-4"]
        assert len(stale_findings) == 1
        assert stale_findings[0]["workflow_state"] == "open"

    def test_auto_resolution_still_works_after_reanalysis(self, db):
        """A changed doc whose issue disappears is still auto-fixed correctly."""
        from datetime import datetime, timezone
        from kb_audit.auditor import Auditor
        from kb_audit.analyzers.timestamp import TimestampAnalyzer
        from kb_audit.models import Document

        # Scan 1: stale content
        # Scan 2: content updated to current
        docs_by_scan: list[Document] = [
            Document(
                id="cf-6", title="Guide",
                content="Status: Legacy\nOld content.",
                source_type="test",
                last_modified=datetime.now(timezone.utc),
            ),
            Document(
                id="cf-6", title="Guide",
                content="Status: Current\nOwner: Team\nNew content.",
                source_type="test",
                last_modified=datetime.now(timezone.utc),
            ),
        ]
        call_count = [0]

        class FakeSource:
            def fetch_documents(self):
                idx = call_count[0]
                call_count[0] += 1
                return iter([docs_by_scan[idx]])
            def close(self):
                pass

        auditor = Auditor(
            sources=[FakeSource()],
            analyzers=[TimestampAnalyzer()],
            reporters=[],
            db=db,
        )

        # Scan 1 — finding created
        auditor.run()
        findings = db.get_findings()
        assert any(f["document_id"] == "cf-6" for f in findings)

        # Scan 2 — doc changed, now current — stale finding auto-fixed
        auditor.run()
        scan2 = db.conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        all_findings = db.get_findings(scan_id=scan2, include_all=True)
        cf6_findings = [f for f in all_findings if f["document_id"] == "cf-6"]
        # The old stale finding should be fixed
        assert all(f["workflow_state"] == "fixed" for f in cf6_findings)

# ---------------------------------------------------------------------------
# Workflow transition hygiene
# ---------------------------------------------------------------------------


class TestWorkflowTransitionHygiene:
    """State-transition cleanup: stale metadata is cleared on state change."""

    def _seed(self, db) -> str:
        """Create one open finding and return its key."""
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        return _result().finding_key

    # -- open clears snoozed_until and dismissal_reason ----------------------

    def test_snoozed_to_open_clears_snoozed_until(self, db):
        key = self._seed(db)
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)  # type: ignore[arg-type]
        assert db.get_finding(key)["snoozed_until"] is not None

        db.update_workflow(key, state="open")  # type: ignore[arg-type]
        f = db.get_finding(key)
        assert f["snoozed_until"] is None

    def test_dismissed_to_open_clears_dismissal_reason(self, db):
        key = self._seed(db)
        db.update_workflow(key, state="dismissed", dismissal_reason="false positive")  # type: ignore[arg-type]
        assert db.get_finding(key)["dismissal_reason"] == "false positive"

        db.update_workflow(key, state="open")  # type: ignore[arg-type]
        f = db.get_finding(key)
        assert not f["dismissal_reason"]  # cleared to NULL/empty

    # -- acknowledged clears snoozed_until and dismissal_reason --------------

    def test_accepted_risk_to_acknowledged_clears_dismissal_reason(self, db):
        key = self._seed(db)
        db.update_workflow(key, state="accepted_risk", dismissal_reason="low impact")  # type: ignore[arg-type]
        assert db.get_finding(key)["dismissal_reason"] == "low impact"

        db.update_workflow(key, state="acknowledged")  # type: ignore[arg-type]
        f = db.get_finding(key)
        assert not f["dismissal_reason"]

    # -- fixed clears snoozed_until ------------------------------------------

    def test_snoozed_to_fixed_clears_snoozed_until(self, db):
        key = self._seed(db)
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)  # type: ignore[arg-type]

        db.update_workflow(key, state="fixed")  # type: ignore[arg-type]
        f = db.get_finding(key)
        assert f["workflow_state"] == "fixed"
        assert f["snoozed_until"] is None

    # -- caller-supplied value overrides cleanup default ---------------------

    def test_caller_supplied_dismissal_reason_preserved_on_dismissed(self, db):
        """Moving to 'dismissed' clears snoozed_until but NOT dismissal_reason
        because that field is appropriate for the dismissed state."""
        key = self._seed(db)
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)  # type: ignore[arg-type]

        db.update_workflow(key, state="dismissed", dismissal_reason="won't fix")  # type: ignore[arg-type]
        f = db.get_finding(key)
        assert f["snoozed_until"] is None          # cleared by transition
        assert f["dismissal_reason"] == "won't fix"  # kept from call


# ---------------------------------------------------------------------------
# Snooze validation
# ---------------------------------------------------------------------------


class TestSnoozeValidation:
    def _seed(self, db) -> str:
        scan_id = db.start_scan()
        db.sync_findings(scan_id, [_result()])
        return _result().finding_key

    def test_snooze_without_snoozed_until_raises(self, db):
        key = self._seed(db)
        with pytest.raises(ValueError, match="snoozed_until"):
            db.update_workflow(key, state="snoozed")  # type: ignore[arg-type]

    def test_snooze_with_future_date_hides_finding(self, db):
        key = self._seed(db)
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=future)  # type: ignore[arg-type]

        assert len(db.get_findings()) == 0
        assert len(db.get_findings(include_all=True)) == 1

    def test_snooze_with_past_date_makes_finding_visible(self, db):
        key = self._seed(db)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db.update_workflow(key, state="snoozed", snoozed_until=past)  # type: ignore[arg-type]

        findings = db.get_findings()
        assert len(findings) == 1
        assert findings[0]["workflow_state"] == "snoozed"

    def test_api_snooze_without_snoozed_until_returns_400(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        from kb_audit.web.app import app

        client = TestClient(app)
        with patch("kb_audit.web.app._get_db") as mock_db:
            db_inst = mock_db.return_value
            db_inst.update_workflow.side_effect = ValueError(
                "Transitioning to 'snoozed' requires snoozed_until to be specified."
            )
            resp = client.patch("/api/findings/abc123", json={"state": "snoozed"})
        assert resp.status_code == 400
        assert "snoozed_until" in resp.json()["error"]
