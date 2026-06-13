"""damia-bot CLI. Draft-only, never submits.

Commands:
  hydrate   Walk the portal back to the job start; rebuild cache/ (CSV + PDFs + attachments)
            and cache/view.json. The portal is the source of truth; the cache is disposable.
  view      Print the current cache/view.json.
"""
from __future__ import annotations

import argparse
import sys

# Windows console is cp1252; portal strings + arrows are UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from datetime import date

from .adapters.holidays.uk_govuk import UkGovUkHolidayProvider
from .adapters.leave.config_ledger import ConfigLeaveProvider
from .adapters.state.csv_cache import CsvWeekCache
from .adapters.state.submission_store import JsonSubmissionStore
from .adapters.timesheet.damia_playwright import DEFAULT_CDP_URL, DamiaTimesheetDriver
from .core.config import ConfigError, load_or_scaffold
from .core.decide import DecisionKind, decide_week
from .core.hydrate import build_view, hydrate, write_view
from .core.paths import DataPaths
from .core.tracking import new_tracking_id
from .core.weekplan import approval_subject, build_week_plan, sunday_of


def cmd_hydrate(args: argparse.Namespace) -> int:
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, scaffolded = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if scaffolded:
        print(f">>> Scaffolded a config template at {paths.config_file}")
    if config.is_placeholder:
        print(f"[note] config.yml is still the template (name={config.name!r}, "
              f"day_rate={config.day_rate}); revenue stats use the template rate for now.")

    print(f">>> Data root: {paths.root}")
    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        records = hydrate(
            drv, paths,
            download_pdf=not args.no_download,
            pull_attachments=not args.no_attachments,
            max_weeks=args.max_weeks,
        )

    CsvWeekCache(paths.csv_path).write(records)
    view = build_view(records, config, paths)
    write_view(view, paths.view_json)

    s = view["stats"]
    cur = s["currency"]
    print(f"\nHydrated {len(records)} week(s).")
    print(f"  CSV:  {paths.csv_path}")
    print(f"  view: {paths.view_json}")
    print(f"  {s['total_units']:g}d total  ~{cur} {s['total_revenue']:,.0f}  "
          f"(approved {cur} {s['approved_revenue']:,.0f}, pending {cur} {s['pending_revenue']:,.0f})")
    if view["actions"]:
        print("\n  Action items:")
        for a in view["actions"]:
            print(f"   - {a['message']}")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    paths = DataPaths.resolve(args.data_dir)
    if not paths.view_json.exists():
        print("No view.json yet — run `hydrate` first.", file=sys.stderr)
        return 2
    print(paths.view_json.read_text(encoding="utf-8"))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Read-only: show what the bot WOULD put on the timesheet for a week, and the approval
    subject it would draft. No portal, no Outlook — pure leave + bank-holiday logic."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, scaffolded = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if scaffolded:
        print(f">>> Scaffolded a config template at {paths.config_file}")

    try:
        leave = ConfigLeaveProvider.from_config(config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")

    anchor = date.fromisoformat(args.week) if args.week else date.today()
    week_start = sunday_of(anchor)
    plan = build_week_plan(week_start, holidays, leave)

    # Portal truth (cached) + email-side overlay — both read-only local files.
    portal = next((r for r in CsvWeekCache(paths.csv_path).read()
                   if r.week_start == week_start), None)
    submission = JsonSubmissionStore(paths.submissions_json).get_by_week(week_start)
    decision = decide_week(plan, portal, submission)

    print(f"Week {plan.week_start} – {plan.week_end}  (Sun..Sat)")
    print(f"  billable days : {plan.billable_days}")
    print(f"  day_units     : {','.join(f'{u:g}' for u in plan.day_units)}  (Sun..Sat)")
    print(f"  portal        : {portal.status if portal else '(not hydrated)'}"
          + (f"  units={','.join(f'{u:g}' for u in portal.day_units)}" if portal else ""))
    if plan.excluded:
        print("  excluded:")
        for e in plan.excluded:
            print(f"    {e.date} {e.date.strftime('%a')}  {e.kind.value:13} {e.label}")

    marker = {
        DecisionKind.READY_TO_DRAFT: "[READY]",
        DecisionKind.ALREADY_IN_FLIGHT: "[IN FLIGHT]",
        DecisionKind.NOTHING_TO_DO: "[NOTHING TO DO]",
        DecisionKind.MANUAL_INTERVENTION: "[MANUAL]",
    }[decision.kind]
    print(f"\n  decision: {marker} {decision.reason}")

    if decision.kind is DecisionKind.READY_TO_DRAFT:
        subj = approval_subject(plan, new_tracking_id(date.today()))
        print(f"\n  would draft subject:\n    {subj}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    # Import lazily so non-TUI commands don't pull in textual.
    from .tui.app import run_app
    run_app(args.data_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="damia-bot",
                                description="Damia timesheet bot (draft-only; never submits).")
    p.add_argument("--data-dir",
                   help=r"Override data root (default %%LOCALAPPDATA%%\damia-timesheet-bot). Dev: ./state")
    sub = p.add_subparsers(dest="command", required=True)

    h = sub.add_parser("hydrate", help="Rebuild the cache + view.json by walking the portal.")
    h.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    h.add_argument("--max-weeks", type=int, default=60)
    h.add_argument("--no-download", action="store_true", help="Skip PDF downloads.")
    h.add_argument("--no-attachments", action="store_true",
                   help="Skip pulling approval screenshots.")
    h.set_defaults(func=cmd_hydrate)

    v = sub.add_parser("view", help="Print the current view.json.")
    v.set_defaults(func=cmd_view)

    pl = sub.add_parser("plan", help="Show the planned week (leave + bank holidays); no I/O.")
    pl.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: today.")
    pl.set_defaults(func=cmd_plan)

    t = sub.add_parser("tui", help="Launch the Textual TUI (reads view.json).")
    t.set_defaults(func=cmd_tui)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
