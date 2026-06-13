from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    ApprovalRecord,
    Holiday,
    LeaveEntry,
    Submission,
    SubmissionStatus,
    Week,
)


@runtime_checkable
class HolidayProvider(Protocol):
    def is_holiday(self, day: date) -> bool: ...
    def holidays_in_range(self, start: date, end: date) -> list[Holiday]: ...


@runtime_checkable
class LeaveProvider(Protocol):
    """The contractor's personal days-off ledger. `ConfigLeaveProvider` reads it from
    config.yml today; an Outlook-calendar adapter can swap in later behind the same port."""

    def leave_on(self, day: date) -> LeaveEntry | None: ...
    def leave_in_range(self, start: date, end: date) -> list[LeaveEntry]: ...


@runtime_checkable
class TimesheetDriver(Protocol):
    """Drives the timesheet portal. Read-and-fill, never submits.

    Beyond fill, the driver exposes a read-only history surface used by the hydrator to
    back-walk the job and archive each week. A different timesheet system plugs in by
    implementing the same methods."""

    # fill surface
    def navigate_to_week(self, week_start: date) -> None: ...
    def read_week(self) -> Week: ...
    def fill_week(self, week: Week) -> None: ...
    def screenshot_week(self) -> bytes: ...

    # history / back-walk surface (used by hydrate())
    def navigate_to_current_week(self) -> None: ...
    def current_week_range(self) -> tuple[date, date]: ...
    def status_word(self) -> str: ...
    def has_download_button(self) -> bool: ...
    def step_to_prev_week(self) -> bool:
        """Step back one week. Return False if refused (before the job's first week)."""
        ...

    def download_week_pdf(self, save_to: "Path") -> "Path": ...
    def pull_attachments(self, save_dir: "Path") -> list["Path"]: ...


@runtime_checkable
class EmailDriver(Protocol):
    """Drafts approval-request emails and locates approval replies. Never sends."""

    def draft_submission_email(
        self,
        *,
        to: list[str],
        subject: str,
        body_html: str,
        attachment_png: bytes,
        tracking_id: str,
    ) -> str:
        """Save the draft to the user's Drafts folder. Returns the draft message id."""
        ...

    def find_by_tracking_id(self, tracking_id: str) -> list[str]:
        """Return message ids that reference this tracking id (in subject or body)."""
        ...

    def extract_approval(self, message_id: str) -> ApprovalRecord | None:
        """If the message is an approval, extract the embedded image to disk and return the record."""
        ...

    def scan_recent(self, weeks: int) -> list[tuple[str, str]]:
        """Best-effort recovery scan. Returns [(tracking_id, observed_state_hint), ...]."""
        ...


@runtime_checkable
class StateStore(Protocol):
    def get(self, tracking_id: str) -> Submission | None: ...
    def put(self, submission: Submission) -> None: ...
    def list_recent(self, weeks: int) -> list[Submission]: ...
    def mark_status(self, tracking_id: str, status: SubmissionStatus) -> None: ...
