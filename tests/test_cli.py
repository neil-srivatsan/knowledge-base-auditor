"""Tests for the CLI commands, focused on the `findings` command."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from kb_audit.cli import cli, _build_reporters, _build_analyzers
from kb_audit.db import Database
from kb_audit.models import AuditResult, Document
from kb_audit.reporters.console import ConsoleReporter
from kb_audit.reporters.json_reporter import JsonReporter
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(id: str = "doc-1", title: str = "Test Doc") -> Document:
    return Document(
        id=id, title=title, content="Legacy content.",
        source_type="test",
        last_modified=datetime.now(timezone.utc),
    )


def _result(doc: Document | None = None, status: str = "stale") -> AuditResult:
    if doc is None:
        doc = _doc()
    return AuditResult(
        document=doc,
        signals=[],
        status=status,
        confidence=0.8,
        confidence_reason="Status field is Legacy",
        trust_evidence={
            "summary": "Stale.",
            "positive_evidence": [],
            "review_risks": ["Status field indicates 'Legacy'"],
            "missing_evidence": [],
            "recommended_action": "Update",
        },
    )


@pytest.fixture
def db_path(tmp_path):
    """Return a path to a fresh database and seed it with test findings."""
    path = tmp_path / "test.db"
    db = Database(str(path))
    db.connect()

    scan_id = db.start_scan()

    # Three findings: two stale (open), one needs_review (open)
    r1 = _result(doc=_doc("doc-1", "Alpha Guide"), status="stale")
    r2 = _result(doc=_doc("doc-2", "Beta Guide"), status="stale")
    r3 = _result(doc=_doc("doc-3", "Gamma Guide"), status="needs_review")
    db.sync_findings(scan_id, [r1, r2, r3])
    db.finish_scan(scan_id, 3)

    # Transition one finding to 'fixed' and one to 'dismissed'
    all_findings = db.get_findings(include_all=True)
    keys = {f["document_id"]: f["finding_key"] for f in all_findings}
    db.update_workflow(keys["doc-2"], state="fixed")  # type: ignore[arg-type]
    db.update_workflow(keys["doc-3"], state="dismissed", dismissal_reason="out of scope")  # type: ignore[arg-type]

    db.close()
    return path


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# `findings` command — summary line always matches displayed rows
# ---------------------------------------------------------------------------


class TestFindingsCommand:
    def _invoke(self, runner, db_path, args: list[str]):
        env = {"DATABASE_URL": str(db_path)}
        return runner.invoke(cli, ["findings", *args], env=env, catch_exceptions=False)

    def test_default_actionable_mode(self, runner, db_path):
        """Default mode shows only open (non-terminal) findings; summary matches."""
        result = self._invoke(runner, db_path, [])
        assert result.exit_code == 0

        # Only doc-1 is open (stale); doc-2 is fixed, doc-3 is dismissed
        assert "Alpha Guide" in result.output
        assert "Beta Guide" not in result.output
        assert "Gamma Guide" not in result.output

        # Summary line
        assert "Total: 1" in result.output
        assert "open" in result.output

    def test_include_all_shows_terminal_findings(self, runner, db_path):
        """--include-all returns all findings; summary total equals row count."""
        result = self._invoke(runner, db_path, ["--include-all"])
        assert result.exit_code == 0

        assert "Alpha Guide" in result.output
        assert "Beta Guide" in result.output
        assert "Gamma Guide" in result.output

        # Summary: 3 rows total (1 open, 1 fixed, 1 dismissed)
        assert "Total: 3" in result.output
        assert "fixed" in result.output
        assert "dismissed" in result.output

    def test_state_filter_fixed_include_all(self, runner, db_path):
        """--state fixed --include-all shows only fixed findings; total is 1."""
        result = self._invoke(runner, db_path, ["--state", "fixed", "--include-all"])
        assert result.exit_code == 0

        assert "Beta Guide" in result.output
        assert "Alpha Guide" not in result.output
        assert "Gamma Guide" not in result.output

        assert "Total: 1" in result.output
        assert "1 fixed" in result.output

    def test_state_filter_open(self, runner, db_path):
        """--state open shows open findings; total matches rows displayed."""
        result = self._invoke(runner, db_path, ["--state", "open"])
        assert result.exit_code == 0

        assert "Alpha Guide" in result.output
        assert "Beta Guide" not in result.output

        assert "Total: 1" in result.output
        assert "1 open" in result.output

    def test_summary_total_equals_row_count(self, runner, db_path):
        """Total in summary equals count of table rows for --include-all."""
        result = self._invoke(runner, db_path, ["--include-all"])
        assert result.exit_code == 0

        # Parse 'Total: N' from output
        total_line = next(
            line for line in result.output.splitlines() if line.startswith("Total:")
        )
        total = int(total_line.split("Total:")[1].strip().split()[0])

        # Count how many guide titles appear (one per row)
        row_count = sum(
            1 for title in ["Alpha Guide", "Beta Guide", "Gamma Guide"]
            if title in result.output
        )
        assert total == row_count == 3

    def test_empty_findings(self, runner, tmp_path):
        """No findings prints a 'no findings' message without crashing."""
        path = tmp_path / "empty.db"
        db = Database(str(path))
        db.connect()
        db.close()

        result = self._invoke(runner, path, [])
        assert result.exit_code == 0
        assert "No findings" in result.output


# ---------------------------------------------------------------------------
# `demo` command tests
# ---------------------------------------------------------------------------


class TestDemoCommand:
    """Tests 1-21: kb-audit demo command behavior."""

    def _invoke(self, runner, args: list[str], db_path=None, env: dict | None = None):
        base_env = env or {}
        return runner.invoke(cli, ["demo", *args], env=base_env, catch_exceptions=False)

    # Test 1: exit code 0 on success
    def test_exits_zero(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        result = self._invoke(runner, ["--database", db])
        assert result.exit_code == 0

    # Test 2: banner printed
    def test_banner_printed(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        result = self._invoke(runner, ["--database", db])
        assert "Demo workspace" in result.output

    # Test 3: default database name is kbaudit-demo.db
    def test_default_database_name(self, runner, tmp_path):
        import os
        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(cli, ["demo"], catch_exceptions=False)
            assert result.exit_code == 0
            assert (tmp_path / "kbaudit-demo.db").exists()
        finally:
            os.chdir(orig)

    # Test 4: produces output for 10 pages
    def test_produces_output_for_ten_pages(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        result = self._invoke(runner, ["--database", db])
        assert result.exit_code == 0
        # The summary line confirms 10 documents were processed
        assert "10" in result.output

    # Test 5: table format by default shows summary
    def test_table_format_by_default(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        result = self._invoke(runner, ["--database", db])
        # Table output contains the report summary line
        assert "current" in result.output
        assert "stale" in result.output

    # Test 6: JSON to file produces valid JSON with 10 documents
    def test_json_format_produces_json(self, runner, tmp_path):
        import json
        db = str(tmp_path / "demo.db")
        out_file = str(tmp_path / "results.json")
        result = self._invoke(runner, ["--format", "json", "--output", out_file, "--database", db])
        assert result.exit_code == 0
        with open(out_file) as f:
            parsed = json.load(f)
        assert parsed["total"] == 10
        assert len(parsed["documents"]) == 10

    # Test 7: JSON output structure has expected status counts
    def test_json_output_to_file(self, runner, tmp_path):
        import json
        db = str(tmp_path / "demo.db")
        out_file = str(tmp_path / "results.json")
        result = self._invoke(runner, ["--format", "json", "--output", out_file, "--database", db])
        assert result.exit_code == 0
        with open(out_file) as f:
            data = json.load(f)
        assert data["total"] == 10
        assert data["current"] == 3
        assert data["stale"] == 3

    # Test 8: requires no environment variables
    def test_requires_no_env_vars(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        env = {k: "" for k in (
            "NOTION_API_KEY", "CONFLUENCE_BASE_URL",
            "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN", "DATABASE_URL",
        )}
        result = runner.invoke(cli, ["demo", "--database", db], env=env, catch_exceptions=False)
        assert result.exit_code == 0

    # Test 9: resets database on each run (second run succeeds and scans again)
    def test_resets_database_on_each_run(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        result1 = self._invoke(runner, ["--database", db])
        assert result1.exit_code == 0
        result2 = self._invoke(runner, ["--database", db])
        assert result2.exit_code == 0

    # Test 10: history shows 1 scan after second run (reset cleared prior)
    def test_history_shows_one_scan_after_each_run(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        self._invoke(runner, ["--database", db_str])
        # After two runs with resets, only 1 scan should remain in DB
        from kb_audit.db import Database
        db = Database(db_str)
        db.connect()
        try:
            history = db.get_scan_history(limit=10)
        finally:
            db.close()
        assert len(history) == 1

    # Test 11: banner always present in output for JSON format
    def test_banner_present_for_json_format(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        out_file = str(tmp_path / "results.json")
        result = self._invoke(runner, ["--format", "json", "--output", out_file, "--database", db])
        assert result.exit_code == 0
        assert "Demo workspace" in result.output

    # Test 12: banner present in output for table format
    def test_banner_on_stdout_for_table_format(self, runner, tmp_path):
        db = str(tmp_path / "demo.db")
        result = self._invoke(runner, ["--database", db])
        assert result.exit_code == 0
        assert "Demo workspace" in result.output

    # Test 13: --database PATH uses specified path, not default
    def test_database_path_option_used(self, runner, tmp_path):
        custom_db = str(tmp_path / "custom_demo.db")
        result = self._invoke(runner, ["--database", custom_db])
        assert result.exit_code == 0
        import os
        assert os.path.exists(custom_db)

    # Test 14: findings accessible via --database after demo run
    def test_findings_accessible_after_demo_run(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        findings_result = runner.invoke(
            cli, ["findings", "--include-all", "--database", db_str],
            catch_exceptions=False,
        )
        assert findings_result.exit_code == 0
        # 6 findings: 3 stale + 3 needs_review (unknown payments-team-notes suppressed as low importance)
        assert "Total: 6" in findings_result.output

    # Test 15: exactly 7 non-current documents appear in findings after demo run
    def test_seven_noncurrent_documents_in_findings(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        from kb_audit.db import Database
        db = Database(db_str)
        db.connect()
        try:
            items = db.get_findings(include_all=True)
        finally:
            db.close()
        # 3 stale + 3 needs_review = 6 (unknown payments-team-notes suppressed; current pages excluded)
        assert len(items) == 6

    # Test 16: scan history document_count is 10
    def test_scan_document_count_is_ten(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        from kb_audit.db import Database
        db = Database(db_str)
        db.connect()
        try:
            history = db.get_scan_history(limit=1)
        finally:
            db.close()
        assert history
        assert history[0]["document_count"] == 10

    # Test 17: scan history recorded with document_count=10
    def test_scan_history_recorded(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        from kb_audit.db import Database
        db = Database(db_str)
        db.connect()
        try:
            history = db.get_scan_history(limit=1)
        finally:
            db.close()
        assert history
        assert history[0]["document_count"] == 10

    # Test 18: clear_all_if_idle blocks when lease is active
    def test_blocks_when_active_lease(self, runner, tmp_path):
        """If a live lease exists, clear_all_if_idle returns False and demo exits non-zero."""
        db_str = str(tmp_path / "demo.db")
        from kb_audit.db import Database
        db = Database(db_str)
        db.connect()
        # Start a scan to hold the lease
        token = db.try_start_scan()
        assert token is not None
        # Now invoke demo — it should fail because clear_all_if_idle returns False
        result = runner.invoke(cli, ["demo", "--database", db_str], catch_exceptions=False)
        db.close()
        assert result.exit_code != 0
        assert "already in progress" in result.output

    # Test 19: history command accepts --database option
    def test_history_accepts_database_option(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        result = runner.invoke(
            cli, ["history", "--database", db_str], catch_exceptions=False
        )
        assert result.exit_code == 0

    # Test 20: findings command accepts --database option
    def test_findings_accepts_database_option(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        result = runner.invoke(
            cli, ["findings", "--include-all", "--database", db_str],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    # Test 21: triage command accepts --database option
    def test_triage_accepts_database_option(self, runner, tmp_path):
        db_str = str(tmp_path / "demo.db")
        self._invoke(runner, ["--database", db_str])
        from kb_audit.db import Database
        db = Database(db_str)
        db.connect()
        try:
            findings_list = db.get_findings(include_all=True)
        finally:
            db.close()
        assert findings_list
        key = findings_list[0]["finding_key"]
        result = runner.invoke(
            cli,
            ["triage", key, "acknowledged", "--database", db_str],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "acknowledged" in result.output


# ---------------------------------------------------------------------------
# _build_reporters unit tests
# ---------------------------------------------------------------------------


class TestBuildReporters:
    """Verify reporter selection matrix for all four format/output combinations."""

    def test_json_no_output_gives_json_reporter_only(self):
        reporters = _build_reporters("json", None)
        assert len(reporters) == 1
        assert isinstance(reporters[0], JsonReporter)

    def test_json_with_output_gives_json_reporter_only(self, tmp_path):
        reporters = _build_reporters("json", str(tmp_path / "out.json"))
        assert len(reporters) == 1
        assert isinstance(reporters[0], JsonReporter)

    def test_table_no_output_gives_console_reporter_only(self):
        reporters = _build_reporters("table", None)
        assert len(reporters) == 1
        assert isinstance(reporters[0], ConsoleReporter)

    def test_table_with_output_gives_both_reporters(self, tmp_path):
        reporters = _build_reporters("table", str(tmp_path / "out.json"))
        assert len(reporters) == 2
        types = {type(r) for r in reporters}
        assert ConsoleReporter in types
        assert JsonReporter in types


# ---------------------------------------------------------------------------
# Analyzer stack wiring
# ---------------------------------------------------------------------------


class TestBuildAnalyzers:
    """Verify InternalLinkAnalyzer is wired into the CLI analyzer stack."""

    def test_internal_links_analyzer_present(self):
        from kb_audit.config import Config
        from kb_audit.analyzers.internal_links import InternalLinkAnalyzer
        analyzers = _build_analyzers(Config())
        names = [a.name() for a in analyzers]
        assert "internal_links" in names

    def test_internal_links_after_broken_links_before_references(self):
        from kb_audit.config import Config
        analyzers = _build_analyzers(Config())
        names = [a.name() for a in analyzers]
        assert names.index("internal_links") > names.index("broken_links")
        assert names.index("internal_links") < names.index("references")


# ---------------------------------------------------------------------------
# Demo command — regression / strengthened assertions
# ---------------------------------------------------------------------------


class TestDemoCommandStronger:
    """Additional regression tests for the demo command."""

    def _run_demo(self, tmp_path, extra_args: list[str] | None = None) -> str:
        """Run demo, return the db path. Raises on non-zero exit."""
        db_str = str(tmp_path / "demo.db")
        runner = CliRunner()
        result = runner.invoke(
            cli, ["demo", "--database", db_str, *(extra_args or [])],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        return db_str

    def _scan_results(self, db_str: str) -> list[dict]:
        db = Database(db_str)
        db.connect()
        try:
            history = db.get_scan_history(limit=1)
            assert history, "No scan history found"
            return db.get_scan_results(history[0]["scan_id"])
        finally:
            db.close()

    # --- reporter selection regression ---

    def test_json_stdout_is_pure_json(self, tmp_path):
        """stdout must be valid JSON with no banner or table noise mixed in."""
        import json
        import subprocess
        import sys
        from pathlib import Path

        bin_dir = Path(sys.executable).parent
        kb_audit_bin = bin_dir / "kb-audit"
        db_str = str(tmp_path / "demo.db")
        proc = subprocess.run(
            [str(kb_audit_bin), "demo", "--format", "json", "--database", db_str],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        # stdout must parse cleanly — raises JSONDecodeError if mixed with table
        data = json.loads(proc.stdout)
        assert data["total"] == 10
        assert data["current"] == 3
        assert data["stale"] == 3
        assert data["needs_review"] == 3
        assert data["unknown"] == 1
        assert "Demo workspace" not in proc.stdout  # banner went to stderr
        assert "Demo workspace" in proc.stderr

    def test_scan_json_stdout_reporter_selection(self):
        """scan --format json with no --output must produce JsonReporter only."""
        reporters = _build_reporters("json", None)
        assert len(reporters) == 1
        assert isinstance(reporters[0], JsonReporter)
        # JsonReporter with no output_path writes to stdout
        assert reporters[0]._output_path is None

    # --- no-network assertion ---

    def test_no_http_requests_during_demo(self, tmp_path, monkeypatch):
        """BrokenLinkAnalyzer must never call _check_url: demo pages have no HTTP URLs."""
        calls: list[str] = []

        def _fake_check_url(url: str, timeout: float = 10.0):
            calls.append(url)
            return (url, 200, None)

        monkeypatch.setattr("kb_audit.analyzers.broken_links._check_url", _fake_check_url)
        self._run_demo(tmp_path)
        assert calls == [], f"_check_url was called for: {calls}"

    # --- DB result counts ---

    def test_ten_results_persisted_in_db(self, tmp_path):
        db_str = self._run_demo(tmp_path)
        results = self._scan_results(db_str)
        assert len(results) == 10

    def test_status_totals_are_exact(self, tmp_path):
        db_str = self._run_demo(tmp_path)
        results = self._scan_results(db_str)
        counts: dict[str, int] = {}
        for r in results:
            counts[r["overall_status"]] = counts.get(r["overall_status"], 0) + 1
        assert counts == {"current": 3, "stale": 3, "needs_review": 3, "unknown": 1}

    def test_actionable_queue_has_six_findings(self, tmp_path):
        db_str = self._run_demo(tmp_path)
        db = Database(db_str)
        db.connect()
        try:
            findings = db.get_findings(include_all=True)
        finally:
            db.close()
        assert len(findings) == 6

    def test_no_current_page_in_findings_queue(self, tmp_path):
        """The three current pages must not appear in the findings queue."""
        db_str = self._run_demo(tmp_path)
        results = self._scan_results(db_str)
        current_ids = {r["id"] for r in results if r["overall_status"] == "current"}
        assert current_ids == {
            "payment-processing-guide",
            "payment-api-guide-v3",
            "merchant-onboarding-checklist",
        }
        db = Database(db_str)
        db.connect()
        try:
            findings = db.get_findings(include_all=True)
        finally:
            db.close()
        finding_doc_ids = {f["document_id"] for f in findings}
        assert current_ids.isdisjoint(finding_doc_ids), (
            f"Current pages found in queue: {current_ids & finding_doc_ids}"
        )

    def test_three_suggested_replacements_are_correct(self, tmp_path):
        db_str = self._run_demo(tmp_path)
        results = self._scan_results(db_str)
        replacements = {
            r["id"]: r["suggested_replacement_id"]
            for r in results
            if r["suggested_replacement_id"]
        }
        assert replacements == {
            "payment-api-guide-v1": "payment-api-guide-v3",
            "payment-api-guide-v2": "payment-api-guide-v3",
            "merchant-launch-checklist-draft": "merchant-onboarding-checklist",
        }


# ---------------------------------------------------------------------------
# .gitignore regression
# ---------------------------------------------------------------------------


def test_gitignore_excludes_demo_db_files():
    """git check-ignore must match all three demo database file patterns."""
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        [
            "git", "check-ignore", "-v",
            "kbaudit-demo.db",
            "kbaudit-demo.db-wal",
            "kbaudit-demo.db-shm",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    # git check-ignore exits 0 only when every listed path is ignored
    assert result.returncode == 0, (
        f"Not all demo db files are gitignored.\ngit output:\n{result.stdout}\n{result.stderr}"
    )
    assert "kbaudit-demo.db" in result.stdout
    assert "kbaudit-demo.db-wal" in result.stdout
    assert "kbaudit-demo.db-shm" in result.stdout
