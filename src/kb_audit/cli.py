"""CLI entry point for kb-audit."""

from __future__ import annotations

import logging
import sys

import click

from kb_audit.analyzers.base import Analyzer
from kb_audit.analyzers.broken_links import BrokenLinkAnalyzer
from kb_audit.analyzers.references import ReferenceAnalyzer
from kb_audit.analyzers.similarity import SimilarityAnalyzer
from kb_audit.analyzers.timestamp import TimestampAnalyzer
from kb_audit.analyzers.version_refs import VersionRefsAnalyzer
from kb_audit.auditor import Auditor
from kb_audit.config import Config
from kb_audit.db import Database, LeaseLostError, ScanLeaseContext, _sanitize_error, _UNSET
from kb_audit.reporters.base import Reporter
from kb_audit.reporters.console import ConsoleReporter
from kb_audit.reporters.json_reporter import JsonReporter
from kb_audit.sources.confluence import ConfluenceSource
from kb_audit.sources.demo import DemoSource
from kb_audit.sources.notion import NotionSource


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging.")
def cli(verbose: bool) -> None:
    """Knowledge Base Auditor — detect stale and outdated documentation."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def _build_analyzers(cfg: Config) -> list[Analyzer]:
    """Construct the standard analyzer stack from config."""
    return [
        TimestampAnalyzer(
            warning_days=cfg.analyzers.timestamp.warning_days,
            critical_days=cfg.analyzers.timestamp.critical_days,
        ),
        SimilarityAnalyzer(threshold=cfg.analyzers.similarity.threshold),
        VersionRefsAnalyzer(
            current_versions=cfg.analyzers.version_refs.current_versions,
            patterns=[p for p in cfg.analyzers.version_refs.patterns],
        ),
        BrokenLinkAnalyzer(),
        ReferenceAnalyzer(),
    ]


def _build_reporters(output_format: str, output_path: str | None) -> list[Reporter]:
    """Select reporters based on format and output path.

    - json, no output  → JsonReporter writing to stdout only
    - json, with output → JsonReporter writing to file only
    - table, no output → ConsoleReporter only
    - table, with output → ConsoleReporter + JsonReporter writing to file
    """
    if output_format == "json":
        return [JsonReporter(output_path=output_path)]
    reporters: list[Reporter] = [ConsoleReporter()]
    if output_path:
        reporters.append(JsonReporter(output_path=output_path))
    return reporters


@cli.command()
@click.option("--config", "config_path", type=click.Path(), default=None, help="Path to config YAML file.")
@click.option("--source", "source_type", type=click.Choice(["notion", "confluence"]), default=None, help="Document source type. Auto-detected from env vars if omitted.")
@click.option("--root-page", type=str, default=None, help="Notion root page ID to scan recursively.")
@click.option("--database-id", type=str, default=None, help="Notion database ID to scan.")
@click.option("--query", "-q", type=str, default=None, help="Page title, Notion URL, or Confluence CQL to scan.")
@click.option("--confluence-space", type=str, default=None, help="Confluence space key to scan.")
@click.option("--confluence-page-id", type=str, default=None, help="Confluence page ID to scan (with children).")
@click.option("--format", "output_format", type=click.Choice(["table", "json"]), default="table", help="Output format.")
@click.option("--output", "output_path", type=click.Path(), default=None, help="Write JSON output to file.")
@click.option("--no-db", is_flag=True, help="Skip persisting results to SQLite.")
def scan(
    config_path: str | None,
    source_type: str | None,
    root_page: str | None,
    database_id: str | None,
    query: str | None,
    confluence_space: str | None,
    confluence_page_id: str | None,
    output_format: str,
    output_path: str | None,
    no_db: bool,
) -> None:
    """Scan a knowledge base for stale documentation."""
    cfg = Config.load(config_path)

    # Auto-detect source type from env vars / config if not specified
    if source_type is None:
        if cfg.confluence.base_url and cfg.confluence.api_token:
            source_type = "confluence"
        else:
            source_type = "notion"

    # Build source
    source: NotionSource | ConfluenceSource
    if source_type == "confluence":
        if not cfg.confluence.base_url or not cfg.confluence.api_token:
            click.echo(
                "Error: Confluence credentials not set. "
                "Add CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, and CONFLUENCE_API_TOKEN "
                "to .env or set as environment variables.",
                err=True,
            )
            sys.exit(1)

        space = confluence_space or cfg.confluence.space_key
        page_id = confluence_page_id or cfg.confluence.page_id

        source = ConfluenceSource(
            base_url=cfg.confluence.base_url,
            email=cfg.confluence.email,
            api_token=cfg.confluence.api_token,
            space_key=space,
            page_id=page_id,
            query=query,
        )
    else:
        if not cfg.notion_api_key:
            click.echo(
                "Error: NOTION_API_KEY not set. "
                "Add it to .env or set as environment variable.",
                err=True,
            )
            sys.exit(1)

        notion_page_id = root_page or cfg.notion.root_page_id
        db_id = database_id or cfg.notion.database_id

        source = NotionSource(
            api_key=cfg.notion_api_key,
            root_page_id=notion_page_id,
            database_id=db_id,
            query=query,
        )

    # Build analyzers
    analyzers = _build_analyzers(cfg)

    reporters = _build_reporters(output_format, output_path)

    # Database + lease lifecycle
    if no_db:
        try:
            auditor = Auditor(
                sources=[source],
                analyzers=analyzers,
                reporters=reporters,
                db=None,
            )
            auditor.run()
        finally:
            source.close()
        return

    db = Database(cfg.database_url)
    db.connect()
    owner_token = db.try_start_scan()
    if owner_token is None:
        db.close()
        click.echo(
            "Error: Another scan is already in progress. Try again later.",
            err=True,
        )
        sys.exit(1)

    try:
        with ScanLeaseContext(db, owner_token) as ctx:
            try:
                auditor = Auditor(
                    sources=[source],
                    analyzers=analyzers,
                    reporters=reporters,
                    db=db,
                )
                auditor.run(lease_check=ctx.check, owner_token=owner_token)
                history = db.get_scan_history(limit=1)
                if history:
                    ctx.last_scan_id = history[0]["scan_id"]
            except LeaseLostError:
                click.echo(
                    "Error: Scan lease was lost (another process took over).",
                    err=True,
                )
                sys.exit(1)
            except Exception as exc:
                ctx.error = _sanitize_error(str(exc))
                raise
            finally:
                source.close()
    finally:
        db.close()


@cli.command()
@click.option("--format", "output_format", type=click.Choice(["table", "json"]), default="table",
              help="Output format.")
@click.option("--output", "output_path", type=click.Path(), default=None,
              help="Write JSON output to file.")
@click.option("--database", "database_path", type=click.Path(), default="kbaudit-demo.db",
              show_default=True, help="Demo database path.")
def demo(output_format: str, output_path: str | None, database_path: str) -> None:
    """Run the credential-free demo workspace using built-in sample pages."""
    json_to_stdout = output_format == "json" and output_path is None
    click.echo("Demo workspace", err=json_to_stdout)

    cfg = Config.load()
    db = Database(database_path)
    db.connect()

    if not db.clear_all_if_idle():
        db.close()
        click.echo(
            "Error: A demo scan is already in progress. Try again later.",
            err=True,
        )
        sys.exit(1)

    owner_token = db.try_start_scan()
    if owner_token is None:
        db.close()
        click.echo(
            "Error: Could not acquire scan lease. Try again later.",
            err=True,
        )
        sys.exit(1)

    source = DemoSource()
    try:
        analyzers = _build_analyzers(cfg)
        reporters = _build_reporters(output_format, output_path)

        with ScanLeaseContext(db, owner_token) as ctx:
            try:
                auditor = Auditor(
                    sources=[source],
                    analyzers=analyzers,
                    reporters=reporters,
                    db=db,
                )
                auditor.run(lease_check=ctx.check, owner_token=owner_token)
                history = db.get_scan_history(limit=1)
                if history:
                    ctx.last_scan_id = history[0]["scan_id"]
            except LeaseLostError:
                click.echo(
                    "Error: Demo scan lease was lost (another process took over).",
                    err=True,
                )
                sys.exit(1)
            except Exception as exc:
                ctx.error = _sanitize_error(str(exc))
                raise
            finally:
                source.close()
    finally:
        db.close()


@cli.command()
@click.option("--limit", type=int, default=10, help="Number of recent scans to show.")
@click.option("--database", "database_path", type=click.Path(), default=None,
              help="Database path. Defaults to configured database.")
def history(limit: int, database_path: str | None) -> None:
    """Show scan history from the local database."""
    cfg = Config.load()
    db = Database(database_path if database_path is not None else cfg.database_url)
    db.connect()

    try:
        scans = db.get_scan_history(limit=limit)
        if not scans:
            click.echo("No scans recorded yet. Run 'kb-audit scan' first.")
            return

        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="Scan History")
        table.add_column("Scan ID", justify="right")
        table.add_column("Started")
        table.add_column("Documents")
        table.add_column("Stale", style="red")
        table.add_column("Needs Review", style="yellow")
        table.add_column("Unknown", style="dim")

        for s in scans:
            table.add_row(
                str(s["scan_id"]),
                s["started_at"][:19],
                str(s["document_count"]),
                str(s["stale_count"]),
                str(s["needs_review_count"]),
                str(s["unknown_count"]),
            )

        console.print(table)
    finally:
        db.close()


_VALID_STATES = ("open", "acknowledged", "dismissed", "fixed", "snoozed", "accepted_risk")

_STATE_STYLES: dict[str, str] = {
    "open": "bold red",
    "acknowledged": "yellow",
    "dismissed": "dim",
    "fixed": "green",
    "snoozed": "cyan",
    "accepted_risk": "magenta",
}


@cli.command()
@click.option("--state", type=str, default=None, help="Filter by workflow state (comma-separated).")
@click.option("--scan-id", type=int, default=None, help="Filter by scan ID.")
@click.option("--include-all", is_flag=True, help="Include fixed, dismissed, accepted_risk, and snoozed findings.")
@click.option("--database", "database_path", type=click.Path(), default=None,
              help="Database path. Defaults to configured database.")
def findings(state: str | None, scan_id: int | None, include_all: bool, database_path: str | None) -> None:
    """List workflow findings from the review queue."""
    cfg = Config.load()
    db = Database(database_path if database_path is not None else cfg.database_url)
    db.connect()

    try:
        states = [s.strip() for s in state.split(",")] if state else None
        items = db.get_findings(
            scan_id=scan_id, states=states, include_all=include_all,
        )
        if not items:
            click.echo("No findings found.")
            return

        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="Review Queue")
        table.add_column("Key", max_width=12)
        table.add_column("State")
        table.add_column("Title", max_width=50)
        table.add_column("Owner")
        table.add_column("Note", max_width=30)
        table.add_column("Updated")

        for f in items:
            ws = f["workflow_state"]
            style = _STATE_STYLES.get(ws, "")
            table.add_row(
                f["finding_key"][:12],
                f"[{style}]{ws}[/{style}]" if style else ws,
                f["title"],
                f.get("assigned_owner") or "",
                f.get("note") or "",
                (f["updated_at"] or "")[:16],
            )

        console.print(table)

        # Summary — computed from the rows displayed so it always matches
        from collections import Counter
        counts: Counter[str] = Counter(f["workflow_state"] for f in items)
        parts = [f"{v} {k}" for k, v in sorted(counts.items())]
        click.echo(f"Total: {sum(counts.values())} ({', '.join(parts)})")
    finally:
        db.close()


@cli.command()
@click.argument("finding_key")
@click.argument("new_state", type=click.Choice(_VALID_STATES))
@click.option("--note", type=str, default=None, help="Add a note.")
@click.option("--owner", type=str, default=None, help="Assign an owner.")
@click.option("--due-date", type=str, default=None, help="Set a due date (YYYY-MM-DD).")
@click.option("--snooze-until", type=str, default=None, help="Snooze until date (YYYY-MM-DD).")
@click.option("--reason", type=str, default=None, help="Dismissal/acceptance reason.")
@click.option("--database", "database_path", type=click.Path(), default=None,
              help="Database path. Defaults to configured database.")
def triage(
    finding_key: str,
    new_state: str,
    note: str | None,
    owner: str | None,
    due_date: str | None,
    snooze_until: str | None,
    reason: str | None,
    database_path: str | None,
) -> None:
    """Update the workflow state of a finding.

    FINDING_KEY is the finding key (or prefix). NEW_STATE is one of:
    open, acknowledged, dismissed, fixed, snoozed, accepted_risk.
    """
    cfg = Config.load()
    db = Database(database_path if database_path is not None else cfg.database_url)
    db.connect()

    try:
        # Support prefix matching for convenience
        all_findings = db.get_findings(include_all=True)
        matches = [f for f in all_findings if f["finding_key"].startswith(finding_key)]
        if not matches:
            click.echo(f"No finding matches key prefix '{finding_key}'.", err=True)
            sys.exit(1)
        if len(matches) > 1:
            click.echo(f"Ambiguous key prefix '{finding_key}' — matches {len(matches)} findings.", err=True)
            for m in matches:
                click.echo(f"  {m['finding_key'][:12]}  {m['title']}")
            sys.exit(1)

        full_key = matches[0]["finding_key"]
        # CLI options not provided by the user arrive as None (Click default).
        # Map None → _UNSET so omitted options leave existing field values
        # unchanged. Explicit clear support is not exposed via CLI flags.
        try:
            found = db.update_workflow(
                full_key,
                state=new_state,  # type: ignore[arg-type]
                note=note if note is not None else _UNSET,  # type: ignore[arg-type]
                assigned_owner=owner if owner is not None else _UNSET,  # type: ignore[arg-type]
                due_date=due_date if due_date is not None else _UNSET,  # type: ignore[arg-type]
                snoozed_until=snooze_until if snooze_until is not None else _UNSET,  # type: ignore[arg-type]
                dismissal_reason=reason if reason is not None else _UNSET,  # type: ignore[arg-type]
            )
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        if found:
            click.echo(f"Updated {full_key[:12]} → {new_state}")
        else:
            click.echo(f"Finding not found: {full_key}", err=True)
            sys.exit(1)
    finally:
        db.close()
