"""The circuit-breaker. Pure function: given the plan for a week, the portal-truth record,
and any existing email-side submission, decide the ONE thing the bot may do.

This is the layer that stops the tool "going nuts": the orchestrator acts only on
READY_TO_DRAFT, and even then only drafts (never sends, never submits). Every other
situation — already in flight, nothing owed, or any inconsistency — resolves to a state
that is surfaced to the human and acted on by nobody.

Pure and provider-free, so the whole decision matrix is unit-testable without a portal,
Outlook, or the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from .models import Submission, WeekPlan, WeekRecord


class DecisionKind(str, Enum):
    READY_TO_DRAFT = "ready_to_draft"            # the ONLY actionable state (draft only)
    ALREADY_IN_FLIGHT = "already_in_flight"      # draft exists / awaiting reply — never re-draft
    NOTHING_TO_DO = "nothing_to_do"              # 0 billable days, or already settled
    MANUAL_INTERVENTION = "manual_intervention"  # any inconsistency — surface, act on nothing


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    week_start: date
    reason: str

    @property
    def is_actionable(self) -> bool:
        return self.kind is DecisionKind.READY_TO_DRAFT


_EDITABLE_PORTAL = ("draft", "rejected")


def _units_match(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    if len(a) != len(b):
        return False
    return all(round(x, 2) == round(y, 2) for x, y in zip(a, b))


def _all_zero(units: tuple[float, ...]) -> bool:
    return all(round(u, 2) == 0.0 for u in units)


def decide_week(
    plan: WeekPlan,
    portal: WeekRecord | None,
    submission: Submission | None,
) -> Decision:
    """Resolve the single permitted action for one week. Order matters: an in-flight or
    settled submission short-circuits everything (idempotency / anti-spam) before we ever
    look at portal/plan consistency."""
    wk = plan.week_start

    # 1) Existing email-side state wins — this is the anti-spam / idempotency gate.
    if submission is not None:
        st = submission.status
        if st.is_in_flight:
            return Decision(DecisionKind.ALREADY_IN_FLIGHT, wk,
                            f"submission {submission.tracking_id} is {st.value}; not re-drafting.")
        if st.value == "needs_attention":
            return Decision(DecisionKind.MANUAL_INTERVENTION, wk,
                            f"submission {submission.tracking_id} was flagged needs_attention.")
        if st.value in ("approved", "sent_to_portal"):
            return Decision(DecisionKind.NOTHING_TO_DO, wk,
                            f"submission {submission.tracking_id} is {st.value}; week is settled.")

    # 2) Nothing owed this week.
    if plan.billable_days == 0:
        return Decision(DecisionKind.NOTHING_TO_DO, wk,
                        "0 billable days (full leave/holiday week) — no email to draft.")

    # 3) We need portal truth to safely proceed.
    if portal is None:
        return Decision(DecisionKind.MANUAL_INTERVENTION, wk,
                        "no portal record for this week — run `hydrate` first.")

    pstatus = portal.status.lower()
    if pstatus in ("approved", "submitted"):
        return Decision(DecisionKind.NOTHING_TO_DO, wk,
                        f"portal already {portal.status}; the email loop has nothing to add.")
    if pstatus not in _EDITABLE_PORTAL:
        return Decision(DecisionKind.MANUAL_INTERVENTION, wk,
                        f"unexpected portal status {portal.status!r}.")

    # 4) Editable portal week with days owed. Reconcile what's filled against the plan.
    if _all_zero(portal.day_units):
        return Decision(DecisionKind.READY_TO_DRAFT, wk,
                        f"{plan.billable_days}d owed; portal {portal.status} and empty — "
                        f"will fill, screenshot, and draft.")
    if _units_match(portal.day_units, plan.day_units):
        return Decision(DecisionKind.READY_TO_DRAFT, wk,
                        f"{plan.billable_days}d owed; portal {portal.status} already matches "
                        f"the plan — will screenshot and draft.")
    return Decision(DecisionKind.MANUAL_INTERVENTION, wk,
                    f"portal units {_fmt(portal.day_units)} != plan {_fmt(plan.day_units)} "
                    f"(e.g. a day off not reflected) — reconcile before drafting.")


def _fmt(units: tuple[float, ...]) -> str:
    return ",".join(f"{u:g}" for u in units)
