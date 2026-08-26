"""Live terminal tracker.

The point of this module is that a long unattended run stays legible, and that
the moment the tool needs a human it says so loudly instead of sitting there.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import Dict, List, Optional

from rich.console import Console, Group
from rich.markup import escape
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from pipeline.models import Barrier, BlockedItem

STAGE_STYLE = {
    "scraped": "dim",
    "researched": "cyan",
    "matched": "blue",
    "generated": "magenta",
    "applying": "yellow",
    "blocked": "bold yellow",
    "submitted": "bold green",
    "skipped": "dim",
    "failed": "bold red",
}

STATUS_ICON = {
    "submitted": "[green]✓[/green]",
    "blocked": "[yellow]⏸[/yellow]",
    "failed": "[red]✗[/red]",
    "skipped": "[dim]–[/dim]",
}


@dataclass
class Row:
    external_id: str
    company: str
    title: str
    source: str
    stage: str = "scraped"
    status: str = ""


class Tracker:
    """Rich Live wrapper. Safe to use when stdout is not a terminal."""

    def __init__(self, console: Optional[Console] = None, quiet: bool = False) -> None:
        self.console = console or Console()
        self.quiet = quiet or not self.console.is_terminal
        self._rows: Dict[str, Row] = {}
        self._order: List[str] = []
        self._blocked: List[BlockedItem] = []
        self._activity = "starting"
        self._spinner = Spinner("dots", text="")
        self._live: Optional[Live] = None
        self._max_rows = 14

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> "Tracker":
        if not self.quiet:
            self._live = Live(
                self._render(), console=self.console, refresh_per_second=8, transient=False
            )
            self._live.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._live is not None:
            self._live.update(self._render())
            self._live.__exit__(*exc_info)
            self._live = None

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    # -- updates --------------------------------------------------------------

    def activity(self, text: str) -> None:
        # Stored raw: it is rendered through Text(), which does not parse markup.
        self._activity = text
        if self.quiet:
            self.console.print(text, style="dim", markup=False, highlight=False)
        self._refresh()

    def track(self, external_id: str, company: str, title: str, source: str) -> None:
        if external_id not in self._rows:
            self._order.append(external_id)
        self._rows[external_id] = Row(external_id, company, title, source)
        self._refresh()

    def update(self, external_id: str, stage: str, status: str = "") -> None:
        row = self._rows.get(external_id)
        if row is None:
            return
        self._rows[external_id] = replace(row, stage=stage, status=status)
        self._refresh()

    def block(self, item: BlockedItem) -> None:
        """Record a barrier and get the user's attention."""
        if any(b.job_external_id == item.job_external_id and b.barrier == item.barrier for b in self._blocked):
            return
        self._blocked.append(item)
        self.update(item.job_external_id, "blocked", item.barrier.value.replace("_", " "))
        self._alert("{0} · {1}".format(item.company, item.barrier.value.replace("_", " ")))
        self._refresh()

    def unblock(self, external_id: str) -> None:
        self._blocked = [b for b in self._blocked if b.job_external_id != external_id]
        self._refresh()

    def _alert(self, message: str) -> None:
        """Terminal bell plus a desktop notification where one is available."""
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

        if sys.platform == "darwin" and shutil.which("osascript"):
            safe = message.replace('"', "'")
            try:
                subprocess.run(
                    [
                        "osascript", "-e",
                        'display notification "{0}" with title "Pipeline needs you"'.format(safe),
                    ],
                    check=False, capture_output=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass

    # -- rendering ------------------------------------------------------------

    def _render(self) -> Panel:
        table = Table(box=None, expand=True, pad_edge=False, show_edge=False)
        table.add_column("JOB", ratio=5, no_wrap=True, overflow="ellipsis")
        table.add_column("COMPANY", ratio=3, no_wrap=True, overflow="ellipsis")
        table.add_column("SOURCE", ratio=2, no_wrap=True)
        table.add_column("STAGE", ratio=2, no_wrap=True)
        table.add_column("STATUS", ratio=3, no_wrap=True, overflow="ellipsis")

        # Blocked rows always stay visible; the rest is a tail of recent activity.
        blocked_ids = {b.job_external_id for b in self._blocked}
        visible = [i for i in self._order if i in blocked_ids]
        for external_id in reversed(self._order):
            if len(visible) >= self._max_rows:
                break
            if external_id not in blocked_ids:
                visible.append(external_id)

        for external_id in visible:
            row = self._rows.get(external_id)
            if row is None:
                continue
            icon = STATUS_ICON.get(row.stage, "")
            # Job titles and companies come off third-party boards. A title
            # containing something that reads as a closing tag ("[/x]") raises
            # MarkupError and would take down an otherwise fine long run.
            table.add_row(
                escape(row.title),
                escape(row.company),
                Text(row.source, style="dim"),
                Text(row.stage, style=STAGE_STYLE.get(row.stage, "")),
                "{0} {1}".format(icon, row.status).strip(),  # status is ours, markup intended
            )

        self._spinner.text = Text(" {0}".format(self._activity), style="dim")
        parts: List[object] = [self._spinner, Text(""), table]

        if self._blocked:
            lines: List[Text] = []
            for item in self._blocked:
                lines.append(
                    Text.assemble(
                        ("  {0}".format(item.company), "bold"),
                        (" · {0}".format(item.barrier.value.replace("_", " ")), "yellow"),
                        (" · {0}".format(item.help_text), "dim"),
                    )
                )
            parts += [
                Text(""),
                Text("⚠  {0} blocked, needs you".format(len(self._blocked)), style="bold yellow"),
                *lines,
            ]

        counts = self._counts()
        subtitle = "  ".join("{0} {1}".format(v, k) for k, v in counts.items()) or "no jobs yet"
        return Panel(
            Group(*parts),
            title="[bold]Pipeline[/bold]",
            subtitle="[dim]{0}[/dim]".format(subtitle),
            border_style="blue" if not self._blocked else "yellow",
        )

    def _counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self._rows.values():
            counts[row.stage] = counts.get(row.stage, 0) + 1
        return counts


def print_blocked(console: Console, items: List[BlockedItem]) -> None:
    """Static rendering of the barrier queue, used by `pipeline unblock`."""
    if not items:
        console.print("[green]Nothing blocked.[/green]")
        return

    table = Table(title="Blocked, waiting on you", box=None, pad_edge=False)
    table.add_column("#", style="dim", no_wrap=True)
    table.add_column("COMPANY", style="bold")
    table.add_column("ROLE", overflow="ellipsis")
    table.add_column("BARRIER", style="yellow")
    table.add_column("WHAT TO DO", style="dim", overflow="fold")

    for index, item in enumerate(items, start=1):
        table.add_row(
            str(index), escape(item.company), escape(item.title),
            item.barrier.value.replace("_", " "), item.help_text,
        )
    console.print(table)
    console.print()
    for index, item in enumerate(items, start=1):
        if item.url:
            console.print("[dim]{0}.[/dim] {1}".format(index, item.url))
