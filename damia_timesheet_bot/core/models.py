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


class LeaveType(str, Enum):
    ANNUAL = "annual"
    SICK = "sick"
    UNPAID = "unpaid"

    @property
    def day_kind(self) -> "DayKind":
        return {
            LeaveType.ANNUAL: DayKind.ANNUAL_LEAVE,
            LeaveType.SICK: DayKind.SICK,
            LeaveType.UNPAID: DayKind.NOT_WORKED,
        }[self]


@dataclass(frozen=True)
class Holiday:
    date: date
    title: str
    region: str


@dataclass(frozen=True)
class LeaveEntry:
    """A day of personal leave from the contractor's ledger (config.yml `leave:`). A range
    in config is expanded into one LeaveEntry per covered weekday by the LeaveProvider."""
    date: date
    type: LeaveType
    note: str = ""


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
    DRAFT = "draft"                      # reserved / pre-draft
    EMAIL_DRAFTED = "email_drafted"      # draft sitting in Outlook Drafts, not sent
    AWAITING_APPROVAL = "awaiting_approval"  # user sent it; watching for the reply
    APPROVED = "approved"                # a clean "Approved" reply was matched
    SENT_TO_PORTAL = "sent_to_portal"    # proof uploaded to Damia (the manual last step)
    NEEDS_ATTENTION = "needs_attention"  # boss queried/rejected, or an inconsistency — manual

    @property
    def is_in_flight(self) -> bool:
        """True while a draft exists or we're awaiting a reply — the bot must NOT re-draft."""
        return self in (SubmissionStatus.EMAIL_DRAFTED, SubmissionStatus.AWAITING_APPROVAL)


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


@dataclass(frozen=True)
class ExcludedDay:
    """A weekday (Mon–Fri) that is NOT billable, with the reason. Bank-holiday exclusions
    are named in the approval-email subject per the real grammar; leave exclusions just
    reduce the day count and are noted in the body."""
    date: date
    kind: DayKind            # BANK_HOLIDAY | ANNUAL_LEAVE | SICK | NOT_WORKED (unpaid)
    label: str               # e.g. "Spring bank holiday" or "annual leave"


@dataclass(frozen=True)
class WeekPlan:
    """Pure projection of what a week SHOULD contain, computed from the working-day
    convention + bank holidays + the leave ledger. Drives both the timesheet fill and the
    approval-email subject. `billable_days` == 0 means nothing to submit (full leave /
    holiday week) — the orchestrator must NOT draft an email for it."""
    week_start: date
    week_end: date
    worked_dates: tuple[date, ...]
    excluded: tuple[ExcludedDay, ...]
    day_units: tuple[float, ...]      # Sun..Sat, 1.0 on worked days else 0.0

    @property
    def billable_days(self) -> int:
        return len(self.worked_dates)

    @property
    def bank_holidays(self) -> tuple[ExcludedDay, ...]:
        return tuple(e for e in self.excluded if e.kind == DayKind.BANK_HOLIDAY)


@dataclass
class WeekRecord:
    """One row of portal-truth cache, keyed by `week_start`. This is the authoritative
    facet rebuilt by hydrating from the Damia portal — distinct from the email-side
    `Submission`/tracking-id world, which overlays onto this on `week_start`.

    `status` is Damia's own status word ('Approved', 'Submitted', 'Draft', 'Rejected').
    `day_units` is Sun..Sat. `pdf_path` / `attachment_paths` point into the cache."""
    week_start: date
    week_end: date
    status: str
    total_units: float
    worked_days: int
    day_units: tuple[float, ...]
    portal_timesheet_id: int | None = None
    pdf_path: Path | None = None
    attachment_paths: list[Path] = field(default_factory=list)
    hydrated_at: datetime | None = None
