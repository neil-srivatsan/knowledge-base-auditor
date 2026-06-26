"""Rich terminal output reporter."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from kb_audit.models import AuditResult
from kb_audit.reporters.base import Reporter

STATUS_STYLES = {
    "current": "green",
    "stale": "red",
    "needs_review": "yellow",
    "unknown": "dim",
}

STATUS_LABELS = {
    "current": "Current",
    "stale": "Stale",
    "needs_review": "Needs Review",
    "unknown": "Unknown",
}


class ConsoleReporter(Reporter):
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def report(self, results: list[AuditResult]) -> None:
        if not results:
            self._console.print("[dim]No documents found.[/dim]")
            return

        table = Table(title="Knowledge Base Audit Report", show_lines=True)
        table.add_column("Title", style="bold", max_width=40)
        table.add_column("Status", justify="center", width=10)
        table.add_column("Confidence", justify="center", width=12)
        table.add_column("Reason", max_width=50)
        table.add_column("Signals", max_width=50)
        table.add_column("Last Modified", width=12)

        sort_order = {"stale": 0, "needs_review": 1, "unknown": 2, "current": 3}
        results_sorted = sorted(results, key=lambda r: sort_order.get(r.overall_status, 4))

        for result in results_sorted:
            status = result.overall_status
            style = STATUS_STYLES[status]
            label = STATUS_LABELS[status]

            conf_pct = f"{result.confidence:.0%}"
            signals_text = "\n".join(
                f"- {s.message}" for s in result.signals
            ) or "[green]No issues[/green]"

            last_mod = ""
            if result.document.last_modified:
                last_mod = result.document.last_modified.strftime("%Y-%m-%d")

            table.add_row(
                result.document.title,
                f"[{style}]{label}[/{style}]",
                f"[{style}]{conf_pct}[/{style}]",
                result.confidence_reason,
                signals_text,
                last_mod,
            )

        self._console.print(table)

        total = len(results)
        stale = sum(1 for r in results if r.overall_status == "stale")
        needs_review = sum(1 for r in results if r.overall_status == "needs_review")
        unknown = sum(1 for r in results if r.overall_status == "unknown")
        current = total - stale - needs_review - unknown

        self._console.print(
            f"\n[bold]Summary:[/bold] {total} documents scanned — "
            f"[green]{current} current[/green], "
            f"[red]{stale} stale[/red], "
            f"[yellow]{needs_review} needs review[/yellow], "
            f"[dim]{unknown} unknown[/dim]"
        )
