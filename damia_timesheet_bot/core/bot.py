"""The bot orchestration brains — PURE, provider-free, unit-testable (like decide.py/state.py).

The `poll` loop is a thin I/O shell around these functions. Everything that decides *what the
bot should do this tick*, *what to nudge the human about*, and *what to notify* lives here with
no Outlook, no portal, no clock-of-its-own — so the whole workflow is testable without the world.

Split of responsibilities:
  - `probe_health` I/O (can we reach Outlook / Chrome?) lives in the loop; the RESULT is modelled
    here as `HealthReport` so `can_work_fully` and its JSON shape are testable.
  - `plan_tick` turns a set of per-week states into the ONE mechanical action allowed per week
    (draft / attach) plus whether this tick needs to drive Chrome (event-driven portal).
  - `pending_human` / `derive_nudge` model the two human gates (send / submit) as standing nudges.
  - `diff_notifications` / `due_renudges` decide what to toast — once per transition, and again
    on a cooldown while a human gate stays open.

The safety invariants live a layer down (`decide_week`, `run_draft`, `run_attach`); this module
only ever proposes DRAFT or ATTACH — never send, never submit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from .state import WeekState


# --------------------------------------------------------------------------- health

@dataclass(frozen=True)
class SubsystemHealth:
    ok: bool
    detail: str = ""            # account name, CDP url, …
    error: str | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail, "error": self.error}


@dataclass(frozen=True)
class HealthReport:
    """The 'are we in a state we can work?' snapshot, surfaced on the TUI header."""
    outlook: SubsystemHealth
    chrome_cdp: SubsystemHealth
    render: SubsystemHealth

    @property
    def can_sense_email(self) -> bool:
        return self.outlook.ok

    @property
    def can_drive_portal(self) -> bool:
        return self.chrome_cdp.ok

    @property
    def can_work_fully(self) -> bool:
        """Fully operational needs BOTH: Outlook to sense sends/replies, Chrome to fill/draft/
        attach/render. Either down and the loop degrades to what the other allows."""
        return self.outlook.ok and self.chrome_cdp.ok

    def to_dict(self) -> dict:
        return {
            "outlook": self.outlook.to_dict(),
            "chrome_cdp": self.chrome_cdp.to_dict(),
            "render": self.render.to_dict(),
            "can_work_fully": self.can_work_fully,
        }


# --------------------------------------------------------------------------- tick plan

class TickAction(str, Enum):
    DRAFT = "draft"      # fill portal + create the Outlook draft (never sends)
    ATTACH = "attach"    # upload the approval proof + Save draft (never submits)


@dataclass(frozen=True)
class WeekIntent:
    week_start: date
    action: TickAction
    reason: str


@dataclass(frozen=True)
class TickPlan:
    intents: tuple[WeekIntent, ...]
    needs_portal: bool          # will this tick drive the live Chrome?
    portal_reason: str          # human-readable why (for the JSON/log)

    def portal_intents(self) -> tuple[WeekIntent, ...]:
        return self.intents


# States a not-yet-in-flight week can be in that the bot advances by DRAFTING. NEEDS_FILLING
# (portal empty) and READY_TO_DRAFT (portal already matches) both resolve, via decide_week
# against the LIVE week inside run_draft, to a fill-and-draft. run_draft re-verifies, so a stale
# state here can only ever cause a no-op, never a wrong action.
_DRAFT_STATES = (WeekState.NEEDS_FILLING, WeekState.READY_TO_DRAFT)

# States whose portal status can change with NO involvement from the bot, so re-reading the
# portal is the only way to find out:
#   SUBMITTED — the agency approves/rejects on their own schedule.
#   ATTACHED  — the human presses Submit in Damia. Without this the week sat on "Proof attached"
#               until the next daily full sweep, even though the portal already said Submitted.
PORTAL_RECHECK_STATES = (WeekState.SUBMITTED, WeekState.ATTACHED)


def plan_tick(
    snapshots: "list[WeekSnapshot]",
    *,
    now: datetime,
    last_portal_poll: dict[date, datetime] | None = None,
    portal_recheck_hours: float = 6.0,
    attached_recheck_hours: float = 1.0,
    force_full_sweep: bool = False,
) -> TickPlan:
    """Given each managed week's derived state, choose ≤1 mechanical action per week and decide
    whether we must drive Chrome this tick. Terminal / waiting-on-human / waiting-on-manager
    states yield no action — they surface and the bot acts on nothing."""
    last_portal_poll = last_portal_poll or {}
    intents: list[WeekIntent] = []
    portal_reasons: list[str] = []

    for s in snapshots:
        if s.state in _DRAFT_STATES:
            intents.append(WeekIntent(s.week_start, TickAction.DRAFT,
                                      f"{s.state.value} — fill portal + draft email"))
        elif s.state is WeekState.MGR_APPROVED:
            intents.append(WeekIntent(s.week_start, TickAction.ATTACH,
                                      "manager approved — attach proof + save draft"))
        elif s.state in PORTAL_RECHECK_STATES:
            # ATTACHED re-checks more often than SUBMITTED: it is an open human gate the user has
            # just been nudged about, so the Submit can land at any moment and they expect the
            # board to notice. A SUBMITTED week is waiting on the agency's own schedule — days,
            # not minutes — so it stays on the slow cadence to avoid pointless portal navigation.
            every = (attached_recheck_hours if s.state is WeekState.ATTACHED
                     else portal_recheck_hours)
            last = last_portal_poll.get(s.week_start)
            due = last is None or (now - last).total_seconds() / 3600.0 >= every
            if due:
                why = ("submitted — re-check agency decision"
                       if s.state is WeekState.SUBMITTED
                       else "proof attached — check whether it has been submitted on the portal")
                portal_reasons.append(f"{s.week_start} {why}")

    if force_full_sweep:
        portal_reasons.insert(0, "startup/rollover — full reverse walk")
    needs_portal = bool(intents) or bool(portal_reasons) or force_full_sweep
    return TickPlan(tuple(intents), needs_portal, "; ".join(portal_reasons))


# --------------------------------------------------------------------------- snapshots

@dataclass(frozen=True)
class WeekSnapshot:
    """The minimal per-week input the brains need: its derived state + when it entered it
    (the submission's updated_at, used for nudge age). Keeps bot.py decoupled from the view dict."""
    week_start: date
    state: WeekState
    since: datetime | None = None


# --------------------------------------------------------------------------- human gates

# The two permanently-manual gates. Mapping state → which gate is open.
_PENDING: dict[WeekState, str] = {
    WeekState.DRAFTED_IN_OUTLOOK: "send",
    WeekState.ATTACHED: "submit",
}

_NUDGE_TEXT = {
    "send": "send draft to your boss",
    "submit": "review, then submit to the agency",
}


def pending_human(state: WeekState) -> str | None:
    """The open human gate for a state ('send' | 'submit'), or None. Drives the standing nudge."""
    return _PENDING.get(state)


@dataclass(frozen=True)
class Nudge:
    since: datetime | None
    hours: float
    level: str          # 'act' fresh → 'warn' once it's been waiting a while
    gate: str           # 'send' | 'submit'
    text: str

    def to_dict(self) -> dict:
        return {
            "since": self.since.isoformat(timespec="minutes") if self.since else None,
            "hours": self.hours,
            "level": self.level,
            "gate": self.gate,
            "text": self.text,
        }


def derive_nudge(state: WeekState, since: datetime | None, now: datetime,
                 *, escalate_after_hours: float = 24.0) -> Nudge | None:
    """A standing reminder while a human gate is open. Escalates tone once it's gone quiet too
    long, so a forgotten 'send' doesn't just disappear."""
    gate = pending_human(state)
    if gate is None:
        return None
    hours = (now - since).total_seconds() / 3600.0 if since else 0.0
    level = "warn" if hours >= escalate_after_hours else "act"
    return Nudge(since=since, hours=round(hours, 1), level=level, gate=gate, text=_NUDGE_TEXT[gate])


def next_signal_from(state: WeekState) -> str | None:
    """Who moves this week forward next — drives the 'waiting on…' hint in the JSON/TUI."""
    if state in _DRAFT_STATES:
        return "bot"
    if state in (WeekState.DRAFTED_IN_OUTLOOK, WeekState.ATTACHED):
        return "you"
    if state in (WeekState.SENT_FOR_APPROVAL, WeekState.NO_RESPONSE, WeekState.MGR_QUERY):
        return "outlook"
    if state is WeekState.SUBMITTED:
        return "portal"
    return None


# --------------------------------------------------------------------------- notifications

@dataclass(frozen=True)
class Notification:
    week_start: date
    state: WeekState
    kind: str           # 'transition' | 'nudge'
    title: str
    body: str


# One-shot toast the first time a week ENTERS one of these states.
_TOAST_ON_ENTER: dict[WeekState, tuple[str, str]] = {
    WeekState.DRAFTED_IN_OUTLOOK: ("Draft ready", "Review & send the timesheet email to your boss."),
    WeekState.NO_RESPONSE:        ("No response yet", "The manager hasn't replied — consider a nudge."),
    WeekState.MGR_QUERY:          ("Manager has a query", "They replied with a question — check it."),
    WeekState.ATTACHED:           ("Approved & attached", "Proof attached — review, then submit to the agency."),
    WeekState.DAMIA_APPROVED:     ("Agency approved", "The agency accepted the timesheet."),
    WeekState.DAMIA_REJECTED:     ("Agency rejected", "The agency rejected the timesheet — check."),
}


def diff_notifications(
    last_notified: dict[date, str],
    snapshots: "list[WeekSnapshot]",
) -> tuple[list[Notification], dict[date, str]]:
    """Fire a transition toast the first time a week enters a toast-worthy state. Returns the
    notifications to send and the updated last-notified map (persist it to de-dup)."""
    out: list[Notification] = []
    new_map = dict(last_notified)
    for s in snapshots:
        prev = last_notified.get(s.week_start)
        if s.state.value != prev and s.state in _TOAST_ON_ENTER:
            title, body = _TOAST_ON_ENTER[s.state]
            out.append(Notification(s.week_start, s.state, "transition",
                                    title, f"Week {s.week_start}: {body}"))
        new_map[s.week_start] = s.state.value
    return out, new_map


def due_renudges(
    snapshots: "list[WeekSnapshot]",
    last_nudged: dict[date, datetime],
    now: datetime,
    *,
    cooldown_hours: float = 4.0,
) -> list[Notification]:
    """Re-toast an OPEN human gate on a cooldown, so a forgotten send/submit resurfaces instead
    of going silent after its one transition toast. Weeks with no open gate never renudge."""
    out: list[Notification] = []
    for s in snapshots:
        gate = pending_human(s.state)
        if gate is None:
            continue
        last = last_nudged.get(s.week_start)
        if last is None or (now - last).total_seconds() / 3600.0 >= cooldown_hours:
            title, body = _TOAST_ON_ENTER[s.state]  # DRAFTED_IN_OUTLOOK / ATTACHED both present
            out.append(Notification(s.week_start, s.state, "nudge", title,
                                    f"Reminder — week {s.week_start}: {body}"))
    return out
