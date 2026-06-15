"""Derived week state — the 'where are we?' the bot/TUI reports.

A pure reconciliation over the two truth sources we already keep:
  - the portal record (Damia/agency side: Draft / Submitted / Approved / Rejected), and
  - the email-side Submission (our side: drafted → sent → manager-approved → attached).

The bot's job (for now) is purely passive: gather these and compute one WeekState per week
plus a short event timeline. No automation, no sending — just an honest read of state driven
by external events (your sends, the manager's reply, the agency's decision).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .models import Submission, SubmissionStatus, WeekRecord


class WeekState(str, Enum):
    NOTHING_TO_DO = "NOTHING_TO_DO"            # full leave/holiday week — nothing owed
    NOT_STARTED = "NOT_STARTED"                # no portal record yet (not hydrated / future)
    NEEDS_FILLING = "NEEDS_FILLING"            # days owed, portal empty, no draft
    READY_TO_DRAFT = "READY_TO_DRAFT"          # portal filled, no approval email yet
    DRAFTED_IN_OUTLOOK = "DRAFTED_IN_OUTLOOK"  # draft created, not yet sent
    SENT_FOR_APPROVAL = "SENT_FOR_APPROVAL"    # sent, awaiting the manager
    NO_RESPONSE = "NO_RESPONSE"                # sent, no reply past the threshold
    MGR_APPROVED = "MGR_APPROVED"              # clean "Approved" reply; proof rendered
    MGR_QUERY = "MGR_QUERY"                    # manager replied with a question/rejection
    ATTACHED = "ATTACHED"                      # proof attached to Damia, pending your Submit
    SUBMITTED = "SUBMITTED"                    # submitted to agency, awaiting their decision
    DAMIA_APPROVED = "DAMIA_APPROVED"          # agency accepted — paid bucket
    DAMIA_REJECTED = "DAMIA_REJECTED"          # agency rejected

    @property
    def is_terminal(self) -> bool:
        return self in (WeekState.DAMIA_APPROVED, WeekState.NOTHING_TO_DO)

    @property
    def needs_human(self) -> bool:
        """States waiting on a human action or attention (drives the TUI 'focus')."""
        return self in (
            WeekState.NEEDS_FILLING, WeekState.READY_TO_DRAFT, WeekState.DRAFTED_IN_OUTLOOK,
            WeekState.NO_RESPONSE, WeekState.MGR_APPROVED, WeekState.MGR_QUERY,
            WeekState.ATTACHED, WeekState.DAMIA_REJECTED,
        )


# label + tone (tone maps to a colour in the TUI: ok / wait / act / warn / idle)
STATE_INFO: dict[WeekState, tuple[str, str]] = {
    WeekState.NOTHING_TO_DO:      ("Nothing to do (leave/holiday)", "idle"),
    WeekState.NOT_STARTED:        ("Not started", "idle"),
    WeekState.NEEDS_FILLING:      ("Needs filling", "act"),
    WeekState.READY_TO_DRAFT:     ("Ready to draft", "act"),
    WeekState.DRAFTED_IN_OUTLOOK: ("Drafted — review & send", "act"),
    WeekState.SENT_FOR_APPROVAL:  ("Sent — awaiting manager", "wait"),
    WeekState.NO_RESPONSE:        ("No response yet", "warn"),
    WeekState.MGR_APPROVED:       ("Manager approved — attach proof", "act"),
    WeekState.MGR_QUERY:          ("Manager has a query — check", "warn"),
    WeekState.ATTACHED:           ("Proof attached — submit to agency", "act"),
    WeekState.SUBMITTED:          ("Submitted — awaiting agency", "wait"),
    WeekState.DAMIA_APPROVED:     ("Agency approved", "ok"),
    WeekState.DAMIA_REJECTED:     ("Agency rejected", "warn"),
}


def state_label(state: WeekState) -> str:
    return STATE_INFO[state][0]


def state_tone(state: WeekState) -> str:
    return STATE_INFO[state][1]


def reconcile_week_state(
    *,
    portal: WeekRecord | None,
    submission: Submission | None,
    now: datetime,
    billable_days: int | None = None,
    no_response_after_hours: float = 48.0,
) -> WeekState:
    """The single derived state for a week. Portal status is authoritative for the agency
    side; the email Submission carries our side. `billable_days` (from the plan) is only used
    to tell a 0-day week from an unfilled one when nothing has been drafted yet."""
    p = portal.status.lower() if portal else None
    if p == "approved":
        return WeekState.DAMIA_APPROVED
    if p == "rejected":
        return WeekState.DAMIA_REJECTED
    if p == "submitted":
        return WeekState.SUBMITTED

    if submission is not None:
        st = submission.status
        if st is SubmissionStatus.SENT_TO_PORTAL:
            return WeekState.ATTACHED
        if st is SubmissionStatus.APPROVED:
            return WeekState.MGR_APPROVED
        if st is SubmissionStatus.NEEDS_ATTENTION:
            return WeekState.MGR_QUERY
        if st is SubmissionStatus.AWAITING_APPROVAL:
            age_h = (now - submission.updated_at).total_seconds() / 3600.0
            return (WeekState.NO_RESPONSE if age_h >= no_response_after_hours
                    else WeekState.SENT_FOR_APPROVAL)
        # EMAIL_DRAFTED (or the reserved DRAFT) — a draft exists but isn't sent.
        return WeekState.DRAFTED_IN_OUTLOOK

    # Nothing drafted yet.
    if billable_days == 0:
        return WeekState.NOTHING_TO_DO
    if portal is None:
        return WeekState.NOT_STARTED
    if round(portal.total_units, 2) == 0.0:
        return WeekState.NEEDS_FILLING
    return WeekState.READY_TO_DRAFT


@dataclass(frozen=True)
class WeekEvent:
    when: datetime | None
    text: str


def derive_events(portal: WeekRecord | None, submission: Submission | None) -> list[WeekEvent]:
    """A short human timeline of what has happened for a week, oldest first."""
    events: list[WeekEvent] = []
    if submission is not None:
        events.append(WeekEvent(submission.created_at, "Drafted in Outlook"))
        st = submission.status
        u = submission.updated_at
        if st is SubmissionStatus.AWAITING_APPROVAL:
            events.append(WeekEvent(u, "Sent for approval"))
        elif st is SubmissionStatus.APPROVED:
            events.append(WeekEvent(u, "Manager approved"))
        elif st is SubmissionStatus.NEEDS_ATTENTION:
            events.append(WeekEvent(u, "Manager replied with a query"))
        elif st is SubmissionStatus.SENT_TO_PORTAL:
            events.append(WeekEvent(u, "Proof attached to Damia"))
    if portal is not None:
        p = portal.status.lower()
        when = portal.hydrated_at
        if p == "submitted":
            events.append(WeekEvent(when, "Submitted to agency"))
        elif p == "approved":
            events.append(WeekEvent(when, "Agency approved"))
        elif p == "rejected":
            events.append(WeekEvent(when, "Agency rejected"))
    return sorted(events, key=lambda e: (e.when or datetime.min))
