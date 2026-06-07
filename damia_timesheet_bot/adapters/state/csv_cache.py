"""CsvWeekCache — the portal-truth cache as a flat CSV, keyed by week_start.

This is a pure cache: delete the file and re-hydrating from the portal rebuilds it 100%.
It's deliberately a human-readable, hand-editable CSV (per the project's "can always be
edited manually if needs be" stance). One row per week.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from ...core.models import WeekRecord

FIELDS = [
    "week_start",
    "week_end",
    "status",
    "total_units",
    "worked_days",
    "day_units",            # Sun..Sat, comma-joined e.g. "0,1,1,1,1,1,0"
    "portal_timesheet_id",
    "pdf_path",
    "attachment_paths",     # pipe-joined
    "hydrated_at",
]


def _fmt_units(units: tuple[float, ...]) -> str:
    return ",".join(f"{u:g}" for u in units)


def _parse_units(s: str) -> tuple[float, ...]:
    if not s:
        return ()
    return tuple(float(x) for x in s.split(","))


def _record_to_row(r: WeekRecord) -> dict:
    return {
        "week_start": r.week_start.isoformat(),
        "week_end": r.week_end.isoformat(),
        "status": r.status,
        "total_units": f"{r.total_units:g}",
        "worked_days": r.worked_days,
        "day_units": _fmt_units(r.day_units),
        "portal_timesheet_id": "" if r.portal_timesheet_id is None else r.portal_timesheet_id,
        "pdf_path": str(r.pdf_path) if r.pdf_path else "",
        "attachment_paths": "|".join(str(p) for p in r.attachment_paths),
        "hydrated_at": r.hydrated_at.isoformat(timespec="seconds") if r.hydrated_at else "",
    }


def _row_to_record(row: dict) -> WeekRecord:
    atts = [Path(p) for p in (row.get("attachment_paths") or "").split("|") if p]
    tid = row.get("portal_timesheet_id") or ""
    hyd = row.get("hydrated_at") or ""
    return WeekRecord(
        week_start=date.fromisoformat(row["week_start"]),
        week_end=date.fromisoformat(row["week_end"]),
        status=row["status"],
        total_units=float(row["total_units"]),
        worked_days=int(row["worked_days"]),
        day_units=_parse_units(row.get("day_units", "")),
        portal_timesheet_id=int(tid) if str(tid).strip() else None,
        pdf_path=Path(row["pdf_path"]) if row.get("pdf_path") else None,
        attachment_paths=atts,
        hydrated_at=datetime.fromisoformat(hyd) if hyd else None,
    )


class CsvWeekCache:
    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, records: list[WeekRecord]) -> None:
        """Rewrite the whole cache, sorted oldest-first."""
        rows = sorted(records, key=lambda r: r.week_start)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            for r in rows:
                writer.writerow(_record_to_row(r))

    def read(self) -> list[WeekRecord]:
        if not self.path.exists():
            return []
        with self.path.open("r", newline="", encoding="utf-8") as f:
            return [_row_to_record(row) for row in csv.DictReader(f)]
