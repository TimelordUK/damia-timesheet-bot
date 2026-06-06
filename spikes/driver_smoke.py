"""End-to-end smoke test for DamiaTimesheetDriver.

Attaches to the CDP-launched Chrome, reads the current (or a specified past) week,
prints a summary, and snaps a screenshot. With --autofill / --save-draft it also
exercises the destructive happy path (Autofill → confirm → Save draft).

Class-diff analysis automatically surfaces CSS classes that appear on SOME days but
not all — that's where Damia's bank-holiday yellow marker will show up.

Usage:
    uv run python -m spikes.driver_smoke
    uv run python -m spikes.driver_smoke --past-week 24/05/26
    uv run python -m spikes.driver_smoke --autofill --save-draft
    uv run python -m spikes.driver_smoke --past-week 24/05/26                    # find holiday class
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from damia_timesheet_bot.adapters.timesheet.damia_playwright import DamiaTimesheetDriver
from damia_timesheet_bot.core.models import Week

OUTPUT_DIR = Path(__file__).parent / "output"
DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def print_week(week: Week) -> None:
    print(f"\nTotal units:   {week.total_units}")
    print(f"Worked days:   {week.worked_days}")
    print()
    print(f"  {'Date':12} {'Day':5} {'Units':>6}  {'Kind':14}  {'BH?':3}  td.classList")
    for i, d in enumerate(week.days):
        classes = " ".join(d.damia_classes) if d.damia_classes else "-"
        bh = "yes" if d.is_damia_holiday else "-"
        print(f"  {str(d.date):12} {DAY_NAMES[i]:5} {d.units:>6.2f}  {d.kind.value:14}  {bh:3}  {classes}")


def print_class_diff(week: Week) -> None:
    """Show which classes appear on which days. Universal classes are summarised on one line;
    non-universal classes are listed per-day — those are where bank-holiday markers live."""
    class_to_days: dict[str, list[int]] = {}
    for i, d in enumerate(week.days):
        for c in d.damia_classes:
            class_to_days.setdefault(c, []).append(i)

    if not class_to_days:
        print("\n(no CSS classes captured)")
        return

    universal = sorted(c for c, idxs in class_to_days.items() if len(idxs) == 7)
    weekend_only = sorted(c for c, idxs in class_to_days.items() if set(idxs) == {0, 6})
    weekday_only = sorted(c for c, idxs in class_to_days.items() if set(idxs) == {1, 2, 3, 4, 5})
    other = {
        c: idxs for c, idxs in class_to_days.items()
        if c not in set(universal + weekend_only + weekday_only)
    }

    print("\nCSS class distribution:")
    if universal:
        print(f"  universal (all 7 days):       {' '.join(universal)}")
    if weekend_only:
        print(f"  weekend only (Sun, Sat):      {' '.join(weekend_only)}")
    if weekday_only:
        print(f"  weekday only (Mon-Fri):       {' '.join(weekday_only)}")
    if other:
        print(f"  OUTLIERS (likely holiday/special markers):")
        for c, idxs in sorted(other.items()):
            days = ", ".join(DAY_NAMES[i] for i in idxs)
            print(f"    {c:30s} on {days}")
    else:
        print("  (no outlier classes — no bank holidays visible on this week)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--past-week",
        help="Navigate to a past week (dd/mm/yy of Sunday) before reading. e.g. 24/05/26.",
    )
    parser.add_argument(
        "--autofill",
        action="store_true",
        help="Click Autofill timesheet and confirm. DESTRUCTIVE: overwrites existing entries.",
    )
    parser.add_argument(
        "--save-draft",
        action="store_true",
        help="Click Save draft after autofill so the populated state persists server-side.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Click the Download button and save the PDF to spikes/output/.",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with DamiaTimesheetDriver().attached() as drv:
        if args.past_week:
            target = datetime.strptime(args.past_week, "%d/%m/%y").date()
            print(f"Navigating to week starting {target}...")
            drv.navigate_to_week(target)

        start, end = drv.current_week_range()
        print(f"Week:          {start} → {end}")
        print(f"Status:        {drv.status_word()}")
        print(f"Timesheet ID:  {drv.timesheet_id}  (Damia per-week record id)")
        print(f"Editable:      {drv.is_editable()}")
        print(f"Download btn:  {'yes' if drv.has_download_button() else 'no'}")

        # First read — what's currently in the form
        week = drv.read_week()
        print_week(week)
        print_class_diff(week)

        if args.autofill:
            print("\n>>> Clicking Autofill timesheet and confirming...")
            drv.autofill_week()
            print("    done.")

            if args.save_draft:
                print(">>> Clicking Save draft...")
                drv.save_draft()
                print("    done.")

            # Re-read after the mutation
            print("\nAfter autofill:")
            week_after = drv.read_week()
            print_week(week_after)
            print(f"Status:        {drv.status_word()}")

        if args.download:
            pdf_target = OUTPUT_DIR / f"driver_smoke_{start.isoformat()}.pdf"
            print(f"\n>>> Clicking Download to save PDF...")
            saved = drv.download_week_pdf(pdf_target)
            print(f"    PDF saved: {saved}")

        # Screenshot the final state, named after the week so multiple runs don't overwrite
        png = drv.screenshot_week()
        out = OUTPUT_DIR / f"driver_smoke_{start.isoformat()}.png"
        out.write_bytes(png)
        print(f"\nScreenshot:    {out} ({len(png):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
