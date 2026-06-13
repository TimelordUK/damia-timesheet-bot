"""Pure week planning: what SHOULD a given week contain?

build_week_plan() walks a Damia week (Sun..Sat) and, for each Mon–Fri, decides whether it
is a worked day (1.0 unit), a bank holiday (gov.uk), or personal leave (the ledger).
Weekends are never billable. The result drives both the timesheet fill (`day_units`,
Sun..Sat) and the approval-email subject.

This module is pure — it takes the holiday/leave providers as arguments and touches no I/O,
no portal, no Outlook — so the whole leave/holiday/0-day story is unit-testable in isolation.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from .models import DayKind, ExcludedDay, WeekPlan

if TYPE_CHECKING:
    from .ports import HolidayProvider, LeaveProvider

# Python date.weekday(): Mon=0 .. Sun=6. Working days are Mon–Fri.
_WORKING_WEEKDAYS = frozenset({0, 1, 2, 3, 4})


def build_week_plan(
    week_start: date,
    holidays: "HolidayProvider",
    leave: "LeaveProvider",
) -> WeekPlan:
    """Build the plan for the Damia week beginning `week_start` (a Sunday).

    Precedence on a working day: bank holiday first (it's a fact about the calendar), then
    personal leave, otherwise worked. A day that is both is reported as a bank holiday —
    you don't burn annual leave on a day that's already off."""
    week_end = week_start + timedelta(days=6)
    worked: list[date] = []
    excluded: list[ExcludedDay] = []
    units: list[float] = []

    for i in range(7):
        d = week_start + timedelta(days=i)
        if d.weekday() not in _WORKING_WEEKDAYS:
            units.append(0.0)
            continue

        hol = next((h for h in holidays.holidays_in_range(d, d)), None)
        if hol is not None:
            excluded.append(ExcludedDay(date=d, kind=DayKind.BANK_HOLIDAY, label=hol.title))
            units.append(0.0)
            continue

        lv = leave.leave_on(d)
        if lv is not None:
            excluded.append(ExcludedDay(date=d, kind=lv.type.day_kind,
                                        label=lv.note or f"{lv.type.value} leave"))
            units.append(0.0)
            continue

        worked.append(d)
        units.append(1.0)

    return WeekPlan(
        week_start=week_start,
        week_end=week_end,
        worked_dates=tuple(worked),
        excluded=tuple(excluded),
        day_units=tuple(units),
    )


def approval_subject(plan: WeekPlan, tracking_id: str) -> str:
    """Render the approval-request subject in the real Damia grammar:

        please approve timesheet DD/MM/YYYY - DD/MM/YYYY (N days)
          [ - excluding bank holiday DD/MM/YYYY (Name)]... [TS:...]

    Only bank holidays are named (matching the existing prototype mail); personal leave just
    reduces N. Caller is responsible for never invoking this on a 0-day week."""
    n = plan.billable_days
    day_word = "day" if n == 1 else "days"
    parts = [
        f"please approve timesheet {_dmy(plan.week_start)} - {_dmy(plan.week_end)} "
        f"({n} {day_word})"
    ]
    for bh in plan.bank_holidays:
        parts.append(f" - excluding bank holiday {_dmy(bh.date)} ({bh.label})")
    parts.append(f" [{tracking_id}]")
    return "".join(parts)


def sunday_of(d: date) -> date:
    """The Damia week-start (Sunday) for the week containing `d`. Python weekday(): Mon=0..Sun=6,
    so days since the most recent Sunday is (weekday()+1) % 7."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _dmy(d: date) -> str:
    return d.strftime("%d/%m/%Y")
