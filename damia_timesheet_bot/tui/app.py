"""Damia timesheet bot — Textual TUI: the at-a-glance state board.

Three tabs:
  Now      — the focus week (most recent one needing attention): derived state + event timeline.
  History  — every week in the cache with its derived state (search to come).
  Money    — accumulated fee / revenue. HIDDEN by default (safe mode) so it's not on show at a
             desk; press 'm' to reveal.

It recomputes state live from the portal cache + the email submission overlay each refresh
(falling back to cache/view.json), so it reflects external events (your sends, the manager's
reply, the agency's decision) as the cache + submissions are updated by the other commands.
The bot is passive: it reports state, it never sends and never submits.
"""
from __future__ import annotations

import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from ..adapters.state.csv_cache import CsvWeekCache
from ..adapters.state.submission_store import JsonSubmissionStore
from ..core.config import Config
from ..core.hydrate import build_view
from ..core.paths import DataPaths

_TONE_COLOUR = {"ok": "green", "wait": "cyan", "act": "yellow", "warn": "red", "idle": "grey50"}


def _money(currency: str, value: float) -> str:
    return f"{currency} {value:,.0f}"


def _load_view_from_json(paths: DataPaths) -> dict | None:
    if not paths.view_json.exists():
        return None
    try:
        return json.loads(paths.view_json.read_text(encoding="utf-8"))
    except Exception:
        return None


def compute_view(paths: DataPaths) -> dict | None:
    """Recompute the view live from cache + submissions; fall back to a written view.json."""
    try:
        config = Config.load(paths.config_file)
        records = CsvWeekCache(paths.csv_path).read()
        if records:
            subs = JsonSubmissionStore(paths.submissions_json).all_by_week()
            return build_view(records, config, paths, submissions=subs)
    except Exception:
        pass
    return _load_view_from_json(paths)


class DamiaTUI(App):
    TITLE = "Damia timesheet bot"
    SUB_TITLE = "state board · never sends · never submits"

    CSS = """
    #now-headline { height: auto; padding: 1 2; border: round $primary; }
    #now-events { height: 1fr; padding: 0 2; }
    #weeks { height: 1fr; }
    #money { height: 1fr; padding: 1 2; border: round $secondary; }
    """

    BINDINGS = [
        ("r", "refresh", "Reload"),
        ("m", "toggle_money", "Reveal/hide money"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, data_dir: str | None = None) -> None:
        super().__init__()
        self.paths = DataPaths.resolve(data_dir)
        self.reveal_money = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="now"):
            with TabPane("Now", id="now"):
                yield Static(id="now-headline")
                yield Static(id="now-events")
            with TabPane("History", id="history"):
                yield DataTable(id="weeks", zebra_stripes=True, cursor_type="row")
            with TabPane("Money", id="money-tab"):
                yield Static(id="money")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#weeks", DataTable)
        table.add_columns("Week", "State", "Portal", "Days")
        self.refresh_view()

    # --- actions ---------------------------------------------------------------

    def action_refresh(self) -> None:
        self.refresh_view()
        self.notify("Reloaded", timeout=2)

    def action_toggle_money(self) -> None:
        self.reveal_money = not self.reveal_money
        self._render_money(compute_view(self.paths) or {})
        self.notify("Money revealed" if self.reveal_money else "Money hidden", timeout=2)

    # --- rendering -------------------------------------------------------------

    def refresh_view(self) -> None:
        view = compute_view(self.paths)
        if view is None:
            self.query_one("#now-headline", Static).update(
                "[b]No data yet.[/b]  Run [b]damia-bot hydrate[/b], then press [b]r[/b]."
            )
            self.query_one("#now-events", Static).update("")
            self.query_one("#weeks", DataTable).clear()
            self.query_one("#money", Static).update("")
            return
        self._render_now(view)
        self._render_history(view)
        self._render_money(view)

    def _render_now(self, view: dict) -> None:
        weeks = view.get("weeks", [])
        focus_id = view.get("focus")
        fw = next((w for w in weeks if w["week_start"] == focus_id), weeks[-1] if weeks else None)
        if not fw:
            self.query_one("#now-headline", Static).update("[dim]No weeks.[/dim]")
            self.query_one("#now-events", Static).update("")
            return
        colour = _TONE_COLOUR.get(fw.get("state_tone", "idle"), "white")
        head = (
            f"[b]Week {fw['week_start']} → {fw['week_end']}[/b]\n"
            f"State: [{colour} b]{fw.get('state_label', fw.get('state'))}[/{colour} b]\n"
            f"[dim]portal {fw.get('status')} · {fw.get('units', 0):g} day(s)"
            f"{' · ' + fw['tracking_id'] if fw.get('tracking_id') else ''}[/dim]"
        )
        self.query_one("#now-headline", Static).update(head)

        events = fw.get("events", [])
        if events:
            lines = ["[b]Timeline[/b]"]
            for e in events:
                lines.append(f"  [dim]{(e.get('when') or ''):16}[/dim] {e['text']}")
            self.query_one("#now-events", Static).update("\n".join(lines))
        else:
            self.query_one("#now-events", Static).update("[dim]No events yet.[/dim]")

    def _render_history(self, view: dict) -> None:
        table = self.query_one("#weeks", DataTable)
        table.clear()
        for w in view.get("weeks", []):
            colour = _TONE_COLOUR.get(w.get("state_tone", "idle"), "white")
            table.add_row(
                f"{w.get('week_start')} → {w.get('week_end')}",
                f"[{colour}]{w.get('state_label', w.get('state'))}[/{colour}]",
                w.get("status", ""),
                f"{w.get('units', 0):g}",
            )

    def _render_money(self, view: dict) -> None:
        widget = self.query_one("#money", Static)
        if not self.reveal_money:
            widget.update("[dim]Revenue hidden (safe mode). Press [b]m[/b] to reveal.[/dim]")
            return
        s = view.get("stats", {})
        c = view.get("contractor", {})
        cur = s.get("currency", "GBP")
        widget.update(
            f"[b]{c.get('name', '?')}[/b]   rate {_money(cur, c.get('day_rate', 0))}/day\n\n"
            f"Days worked: [b]{s.get('total_units', 0):g}[/b]\n"
            f"Total:    [b]{_money(cur, s.get('total_revenue', 0))}[/b]\n"
            f"  [green]approved {_money(cur, s.get('approved_revenue', 0))}[/green]\n"
            f"  [yellow]pending  {_money(cur, s.get('pending_revenue', 0))}[/yellow]"
        )


def run_app(data_dir: str | None = None) -> None:
    DamiaTUI(data_dir=data_dir).run()


def main() -> None:
    run_app()


if __name__ == "__main__":
    main()
