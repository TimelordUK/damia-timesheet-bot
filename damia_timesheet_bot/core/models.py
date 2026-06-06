from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path


class DayKind(str, Enum):
    WORKED = "worked"
    BANK_HOLIDAY = "bank_holiday"
    ANNUAL_LEAVE = "annual_leave"
    SICK = "sick"
    NOT_WORKED = "not_worked"


@dataclass(frozen=True)
class Holiday:
    date: date
    title: str
    region: str


@dataclass
class Day:
    """A single day entry. `units` is the timesheet-system quantity (0.0..1.0 in 0.25 steps
    for Damia). `kind` is a LOCAL concept the bot uses for awareness/email-body wording — it
    does not map 1:1 to anything Damia stores at the cell level.

    `damia_classes` captures the CSS classes Damia attached to the entry cell.
    `is_damia_holiday` is True when Damia marks the day-header with a yellow background —
    its built-in bank-holiday signal. The bot uses this to corroborate with UK gov.uk data
    and to warn the user before they claim hours on a marked day."""
    date: date
    kind: DayKind = DayKind.NOT_WORKED
    units: float = 0.0
    note: str = ""
    damia_classes: tuple[str, ...] = ()
    is_damia_holiday: bool = False


@dataclass
class Week:
    """A week's worth of timesheet entries. For Damia, weeks start Sunday and end Saturday.
    Other timesheet systems may use different conventions — adapters are responsible for
    translating to their own day-ordering."""
    start: date  # the first day of the week (Sunday for Damia)
    days: list[Day] = field(default_factory=list)

    @property
    def end(self) -> date:
        return self.days[-1].date if self.days else self.start + timedelta(days=6)

    @property
    def worked_days(self) -> int:
        return sum(1 for d in self.days if d.kind == DayKind.WORKED)

    @property
    def total_units(self) -> float:
        return round(sum(d.units for d in self.days), 2)


class SubmissionStatus(str, Enum):
    DRAFT = "draft"
    EMAIL_DRAFTED = "email_drafted"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SENT_TO_PORTAL = "sent_to_portal"


@dataclass
class Submission:
    tracking_id: str
    week_start: date
    status: SubmissionStatus
    created_at: datetime
    updated_at: datetime
    approver_emails: list[str] = field(default_factory=list)
    timesheet_screenshot_path: Path | None = None


@dataclass
class ApprovalRecord:
    tracking_id: str
    approver_email: str
    approved_at: datetime
    approval_png_path: Path
