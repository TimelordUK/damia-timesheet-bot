from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from .models import (
    ApprovalRecord,
    Holiday,
    Submission,
    SubmissionStatus,
    Week,
)


@runtime_checkable
class HolidayProvider(Protocol):
    def is_holiday(self, day: date) -> bool: ...
    def holidays_in_range(self, start: date, end: date) -> list[Holiday]: ...


@runtime_checkable
class TimesheetDriver(Protocol):
    """Drives the timesheet portal. Read-and-fill, never submits."""

    def navigate_to_week(self, week_start: date) -> None: ...
    def read_week(self) -> Week: ...
    def fill_week(self, week: Week) -> None: ...
    def screenshot_week(self) -> bytes: ...


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
