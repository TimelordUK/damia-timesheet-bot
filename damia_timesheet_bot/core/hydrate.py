"""Hydration orchestrator + projection.

hydrate(): walk the timesheet driver backwards from the current week to the job's first
week, building one WeekRecord per week (portal truth) and archiving PDFs + attachments.

build_view(): pure projection over the records + config → a single render-state dict
(written as cache/view.json). This is the ONLY thing the future Textual TUI reads — the
TUI stays a dumb renderer with no knowledge of Damia, Outlook, or the cache layout.
Revenue stats and action items are computed here, not in the UI.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import Config
from .models import WeekRecord
from .paths import DataPaths
from .state import derive_events, reconcile_week_state, state_label, state_tone, WeekState

if TYPE_CHECKING:
    from .ports import TimesheetDriver


def hydrate(
    driver: "TimesheetDriver",
    paths: DataPaths,
    *,
    download_pdf: bool = True,
    pull_attachments: bool = True,
    max_weeks: int = 60,
    log: Callable[[str], None] = print,
) -> list[WeekRecord]:
    """Walk current → first week, recording each. Self-terminates when the driver refuses
    to step before the job's contract start (step_to_prev_week returns False). Read-only:
    navigates and downloads only."""
    paths.ensure_cache()
    driver.navigate_to_current_week()
    now = datetime.now()
    records: list[WeekRecord] = []

    for _ in range(max_weeks):
        start, end = driver.current_week_range()
        status = driver.status_word()
        week = driver.read_week()
        downloadable = driver.has_download_button()

        pdf_path: Path | None = None
        atts: list = []
        if downloadable:
            if download_pdf:
                try:
                    pdf_path = Path(driver.download_week_pdf(paths.pdf_dir / f"{start.isoformat()}.pdf"))
                except Exception as e:
                    log(f"   [warn] PDF download failed for {start}: {e}")
            if pull_attachments:
                try:
                    atts = driver.pull_attachments(paths.attachments_for(start), log=log)
                except Exception as e:
                    log(f"   [warn] attachment pull failed for {start}: {e}")

        records.append(WeekRecord(
            week_start=start,
            week_end=end,
            status=status,
            total_units=week.total_units,
            worked_days=week.worked_days,
            day_units=tuple(d.units for d in week.days),
            portal_timesheet_id=getattr(driver, "timesheet_id", None),
            pdf_path=pdf_path,
            attachment_paths=list(atts),
            hydrated_at=now,
        ))
        log(f"  {start} → {end}  {status:10} {week.total_units:>4g}d  "
            f"pdf={'y' if pdf_path else '-'}  att={len(atts)}")

        if not driver.step_to_prev_week():
            log(f">>> Reached the job start ({start}); walk complete.")
            break
    else:
        log(f"[warn] hit max_weeks={max_weeks} without reaching the job start.")

    records.sort(key=lambda r: r.week_start)
    return records


def _derive_actions(records: list[WeekRecord]) -> list[dict]:
    """The 'what do I still owe?' list. Pure function over the records."""
    actions: list[dict] = []
    if not records:
        return actions
    latest = records[-1]

    for r in records:
        if r.status.lower() in ("draft", "rejected") and r.total_units > 0 and r is not latest:
            actions.append({
                "kind": "unsubmitted_filled_week",
                "week": r.week_start.isoformat(),
                "message": f"Week {r.week_start} is filled ({r.total_units:g}d) but still "
                           f"{r.status} — not submitted.",
            })

    if latest.status.lower() in ("draft", "rejected"):
        if latest.total_units == 0:
            actions.append({
                "kind": "current_week_empty",
                "week": latest.week_start.isoformat(),
                "message": f"This week {latest.week_start} is empty — needs filling.",
            })
        else:
            actions.append({
                "kind": "current_week_ready",
                "week": latest.week_start.isoformat(),
                "message": f"This week {latest.week_start} is filled "
                           f"({latest.total_units:g}d, {latest.status}) — ready to submit.",
            })

    for r in records:
        if r.status.lower() == "approved" and not r.attachment_paths:
            actions.append({
                "kind": "approved_no_attachment",
                "week": r.week_start.isoformat(),
                "message": f"Approved week {r.week_start} has no approval screenshot archived.",
            })
    return actions


def build_view(
    records: list[WeekRecord],
    config: Config,
    paths: DataPaths,
    *,
    submissions: dict | None = None,
    billable_by_week: dict | None = None,
    now: datetime | None = None,
) -> dict:
    rate = config.day_rate
    submissions = submissions or {}
    billable_by_week = billable_by_week or {}
    now = now or datetime.now()
    weeks: list[dict] = []
    total_units = 0.0
    total_rev = 0.0
    approved_rev = 0.0
    pending_rev = 0.0
    by_status: dict[str, int] = {}

    for r in records:
        rev = round(r.total_units * rate, 2)
        total_units += r.total_units
        total_rev += rev
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.status.lower() == "approved":
            approved_rev += rev
        else:
            pending_rev += rev

        sub = submissions.get(r.week_start)
        state = reconcile_week_state(
            portal=r, submission=sub, now=now,
            billable_days=billable_by_week.get(r.week_start),
        )
        events = derive_events(r, sub)
        weeks.append({
            "week_start": r.week_start.isoformat(),
            "week_end": r.week_end.isoformat(),
            "status": r.status,
            "worked_days": r.worked_days,
            "units": r.total_units,
            "revenue": rev,
            "pdf": r.pdf_path.name if r.pdf_path else None,
            "attachments": [p.name for p in r.attachment_paths],
            "state": state.value,
            "state_label": state_label(state),
            "state_tone": state_tone(state),
            "tracking_id": sub.tracking_id if sub else None,
            "events": [{"when": e.when.isoformat(timespec="minutes") if e.when else None,
                        "text": e.text} for e in events],
        })

    # 'focus' = the most recent week still needing a human (else the latest week) — the "Now" tab.
    focus = None
    for w in reversed(weeks):
        if WeekState(w["state"]).needs_human:
            focus = w
            break
    if focus is None and weeks:
        focus = weeks[-1]

    generated = records[0].hydrated_at if records and records[0].hydrated_at else datetime.now()
    return {
        "generated_at": generated.isoformat(timespec="seconds"),
        "data_root": str(paths.root),
        "contractor": {"name": config.name, "day_rate": rate, "currency": config.currency},
        "job": {
            "first_week": records[0].week_start.isoformat() if records else None,
            "last_week": records[-1].week_start.isoformat() if records else None,
            "num_weeks": len(records),
        },
        "stats": {
            "total_units": round(total_units, 2),
            "total_revenue": round(total_rev, 2),
            "approved_revenue": round(approved_rev, 2),
            "pending_revenue": round(pending_rev, 2),
            "currency": config.currency,
            "weeks_by_status": by_status,
        },
        "weeks": weeks,
        "focus": focus["week_start"] if focus else None,
        "actions": _derive_actions(records),
    }


def write_view(view: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(view, indent=2, default=str), encoding="utf-8")
