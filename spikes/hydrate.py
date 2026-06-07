"""Portal-truth hydrator — walk the Damia week-navigator backwards from the current week
to the first week of the job, recording each week to a CSV cache and (on submitted/approved
weeks) downloading the server-rendered PDF archive.

Design stance (locked 2026-06-07): the Damia portal is the GOLDEN SOURCE OF TRUTH — it's
who pays. This CSV is a pure cache: delete it and re-run this to rebuild it 100% from the
portal. No email data is involved here; email is a separate metadata overlay joined on
week_start later.

The back-walk self-terminates: Damia refuses to step before the job's contract start
(popup: "...not a valid period for this job, active between <start> - <end>"). step_to_prev_week()
returns False on that refusal, so we stop without needing to know the contract dates up front.

Read-only: never autofills, saves, submits, or cancels. Only navigates and downloads.

Usage:
    uv run python -m spikes.hydrate                 # full walk, download PDFs
    uv run python -m spikes.hydrate --no-download   # fast dry run, CSV only
    uv run python -m spikes.hydrate --max-weeks 20 --out spikes/output/hydrate
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

# Windows console is cp1252; Damia/UI strings and our arrows are UTF-8. Force it.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from damia_timesheet_bot.adapters.timesheet.damia_playwright import DamiaTimesheetDriver
from damia_timesheet_bot.core.models import Week

DEFAULT_OUT = Path(__file__).parent / "output" / "hydrate"
DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

CSV_FIELDS = [
    "week_start",
    "week_end",
    "status",
    "total_units",
    "worked_days",
    "day_units",            # Sun..Sat, comma-joined e.g. "0.0,1.0,1.0,1.0,1.0,1.0,0.0"
    "portal_timesheet_id",
    "pdf_path",
    "hydrated_at",
]


def day_units_str(week: Week) -> str:
    return ",".join(f"{d.units:g}" for d in week.days)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output dir for CSV + PDFs (default {DEFAULT_OUT}).")
    parser.add_argument("--max-weeks", type=int, default=60,
                        help="Safety cap on how many weeks to walk back (default 60).")
    parser.add_argument("--no-download", action="store_true",
                        help="Skip PDF downloads; write the CSV only (fast dry run).")
    args = parser.parse_args()

    out_dir: Path = args.out
    pdf_dir = out_dir / "pdf"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    now_iso = datetime.now().isoformat(timespec="seconds")

    with DamiaTimesheetDriver().attached() as drv:
        print(">>> Navigating to current week as the walk's starting point...")
        drv.navigate_to_current_week()

        print(f"\n  {'Week':23} {'Status':12} {'Days':>5} {'DL':>3}  PDF")
        print(f"  {'-' * 23} {'-' * 12} {'-' * 5} {'-' * 3}  {'-' * 20}")

        for step in range(args.max_weeks):
            start, end = drv.current_week_range()
            status = drv.status_word()
            week = drv.read_week()
            downloadable = drv.has_download_button()

            pdf_path = ""
            if downloadable and not args.no_download:
                target = pdf_dir / f"{start.isoformat()}.pdf"
                try:
                    saved = drv.download_week_pdf(target)
                    pdf_path = str(saved)
                except Exception as e:
                    pdf_path = f"(download failed: {e})"

            rows.append({
                "week_start": start.isoformat(),
                "week_end": end.isoformat(),
                "status": status,
                "total_units": f"{week.total_units:g}",
                "worked_days": week.worked_days,
                "day_units": day_units_str(week),
                "portal_timesheet_id": drv.timesheet_id,
                "pdf_path": pdf_path,
                "hydrated_at": now_iso,
            })

            dl = "yes" if downloadable else "-"
            pdf_disp = Path(pdf_path).name if pdf_path and not pdf_path.startswith("(") else (pdf_path or "-")
            print(f"  {start.isoformat()} → {end.isoformat()} {status:12} "
                  f"{week.total_units:>5g} {dl:>3}  {pdf_disp}")

            # Step back one week. False = Damia refused (we're at the contract's first week).
            if not drv.step_to_prev_week():
                print(f"\n>>> Hit the start of the job — Damia refused to step before "
                      f"{start.isoformat()}. Walk complete.")
                break
        else:
            print(f"\n[warn] Reached --max-weeks={args.max_weeks} without hitting the job "
                  f"start. Increase --max-weeks if the job really is that long.")

    # Write the CSV cache (oldest-first reads more naturally; we walked newest-first).
    rows.sort(key=lambda r: r["week_start"])
    csv_path = out_dir / "timesheets.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nHydrated {len(rows)} week(s).")
    print(f"CSV cache: {csv_path}")
    if not args.no_download:
        print(f"PDFs:      {pdf_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
