"""Damia timesheet bot — Textual TUI.

A DUMB RENDERER. It knows nothing about Damia, Outlook, or the cache layout — it reads one
file, cache/view.json, and draws it. Everything domain-specific (hydration, revenue,
action items) is computed upstream by the probes/projection and baked into that JSON.
Press 'r' to re-read the file after a probe has refreshed it.

Run standalone:   uv run damia-bot tui
Or via the Zellij launcher (main pane), with probes as floating panes.
"""
from __future__ import annotations

import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from ..core.paths import DataPaths


def load_view(paths: DataPaths) -> dict | None:
    if not paths.view_json.exists():
        return None
    try:
        return json.loads(paths.view_json.read_text(encoding="utf-8"))
    except Exception:
        return None


def _money(currency: str, value: float) -> str:
    return f"{currency} {value:,.0f}"


class DamiaTUI(App):
    TITLE = "Damia timesheet bot"
    SUB_TITLE = "draft-only · never submits"

    CSS = """
    Screen { layout: vertical; }
    #top { height: auto; }
    #stats, #agents { width: 1fr; border: round $primary; padding: 0 1; height: 100%; }
    #agents { border: round $secondary; }
    #weeks { height: 1fr; border: round $primary; }
    #actions { height: auto; max-height: 30%; border: round $warning; padding: 0 1; }
    .muted { color: $text-muted; }
    """

    BINDINGS = [
        ("r", "refresh", "Reload view.json"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, data_dir: str | None = None) -> None:
        super().__init__()
        self.paths = DataPaths.resolve(data_dir)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield Static(id="stats")
            yield Static(id="agents")
        yield DataTable(id="weeks", zebra_stripes=True, cursor_type="row")
        yield VerticalScroll(Static(id="actions"))
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#weeks", DataTable)
        table.add_columns("Week", "Status", "Days", "Revenue", "PDF", "Att")
        self.refresh_view()

    # --- actions --------------------------------------------------------------

    def action_refresh(self) -> None:
        self.refresh_view()
        self.notify("Reloaded view.json", timeout=2)

    # --- rendering ------------------------------------------------------------

    def refresh_view(self) -> None:
        view = load_view(self.paths)
        if view is None:
            self.query_one("#stats", Static).update(
                "[b]No data yet.[/b]\n\nRun [b]damia-bot hydrate[/b] to build "
                f"[i]{self.paths.view_json}[/i], then press [b]r[/b]."
            )
            self.query_one("#agents", Static).update("")
            self.query_one("#actions", Static).update("")
            self.query_one("#weeks", DataTable).clear()
            return

        self._render_stats(view)
        self._render_agents(view)
        self._render_weeks(view)
        self._render_actions(view)

    def _render_stats(self, view: dict) -> None:
        c = view.get("contractor", {})
        job = view.get("job", {})
        s = view.get("stats", {})
        cur = s.get("currency", "GBP")
        by_status = ", ".join(f"{k} {v}" for k, v in (s.get("weeks_by_status") or {}).items())
        text = (
            f"[b]{c.get('name', '?')}[/b]   [dim]{view.get('data_root', '')}[/dim]\n"
            f"Job: [b]{job.get('first_week')}[/b] → [b]{job.get('last_week')}[/b]  "
            f"({job.get('num_weeks', 0)} weeks)   rate {_money(cur, c.get('day_rate', 0))}/day\n"
            f"Days worked: [b]{s.get('total_units', 0):g}[/b]   "
            f"Revenue: [b]{_money(cur, s.get('total_revenue', 0))}[/b]\n"
            f"  [green]approved {_money(cur, s.get('approved_revenue', 0))}[/green]   "
            f"[yellow]pending {_money(cur, s.get('pending_revenue', 0))}[/yellow]\n"
            f"[dim]{by_status}[/dim]"
        )
        self.query_one("#stats", Static).update(text)

    def _render_agents(self, view: dict) -> None:
        # Placeholder orchestration status — the probes will report into view.json later.
        gen = view.get("generated_at", "—")
        text = (
            "[b]Agents[/b]\n"
            f"[green]●[/green] hydrator   [dim]last run {gen}[/dim]\n"
            "[grey50]●[/grey50] email      [dim]placeholder — poll Outlook for [TS:] replies[/dim]\n"
            "[grey50]●[/grey50] approval   [dim]placeholder — match → attach to draft[/dim]"
        )
        self.query_one("#agents", Static).update(text)

    def _render_weeks(self, view: dict) -> None:
        table = self.query_one("#weeks", DataTable)
        table.clear()
        cur = view.get("stats", {}).get("currency", "GBP")
        for w in view.get("weeks", []):
            status = w.get("status", "")
            colour = {"approved": "green", "draft": "yellow", "rejected": "red"}.get(
                status.lower(), "white"
            )
            att = w.get("attachments", []) or []
            table.add_row(
                f"{w.get('week_start')} → {w.get('week_end')}",
                f"[{colour}]{status}[/{colour}]",
                f"{w.get('units', 0):g}",
                _money(cur, w.get("revenue", 0)),
                "✓" if w.get("pdf") else "-",
                str(len(att)) if att else "-",
            )

    def _render_actions(self, view: dict) -> None:
        actions = view.get("actions", [])
        if not actions:
            self.query_one("#actions", Static).update("[green]No outstanding actions.[/green]")
            return
        icon = {
            "current_week_empty": "[yellow]○[/yellow]",
            "current_week_ready": "[green]►[/green]",
            "unsubmitted_filled_week": "[yellow]![/yellow]",
            "approved_no_attachment": "[red]⚠[/red]",
        }
        lines = ["[b]Action items[/b]"]
        for a in actions:
            lines.append(f"{icon.get(a.get('kind'), '·')} {a.get('message', '')}")
        self.query_one("#actions", Static).update("\n".join(lines))


def run_app(data_dir: str | None = None) -> None:
    DamiaTUI(data_dir=data_dir).run()


def main() -> None:
    run_app()


if __name__ == "__main__":
    main()
