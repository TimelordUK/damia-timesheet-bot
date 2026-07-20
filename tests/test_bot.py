"""Pure unit tests for the bot orchestration brains (no I/O, no clock)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from damia_timesheet_bot.core.bot import (
    HealthReport,
    Notification,
    SubsystemHealth,
    TickAction,
    WeekSnapshot,
    derive_nudge,
    diff_notifications,
    due_renudges,
    next_signal_from,
    pending_human,
    plan_tick,
)
from pathlib import Path

from damia_timesheet_bot.core.config import Config
from damia_timesheet_bot.core.hydrate import _derive_actions, build_view
from damia_timesheet_bot.core.paths import DataPaths
from damia_timesheet_bot.core.models import WeekRecord
from damia_timesheet_bot.core.state import WeekState

NOW = datetime(2026, 7, 6, 9, 0, 0)  # a Monday morning
WK = date(2026, 6, 29)


def _ok(x: bool) -> SubsystemHealth:
    return SubsystemHealth(ok=x)


# --- health ----------------------------------------------------------------

def test_can_work_fully_needs_both():
    assert HealthReport(_ok(True), _ok(True), _ok(True)).can_work_fully
    assert not HealthReport(_ok(True), _ok(False), _ok(True)).can_work_fully
    assert not HealthReport(_ok(False), _ok(True), _ok(True)).can_work_fully


def test_health_partial_capabilities():
    h = HealthReport(_ok(True), _ok(False), _ok(False))
    assert h.can_sense_email and not h.can_drive_portal


# --- plan_tick -------------------------------------------------------------

def test_needs_filling_and_ready_both_draft():
    for st in (WeekState.NEEDS_FILLING, WeekState.READY_TO_DRAFT):
        plan = plan_tick([WeekSnapshot(WK, st)], now=NOW)
        assert [i.action for i in plan.intents] == [TickAction.DRAFT]
        assert plan.needs_portal


def test_mgr_approved_attaches():
    plan = plan_tick([WeekSnapshot(WK, WeekState.MGR_APPROVED)], now=NOW)
    assert [i.action for i in plan.intents] == [TickAction.ATTACH]
    assert plan.needs_portal


def test_waiting_states_do_nothing():
    for st in (WeekState.SENT_FOR_APPROVAL, WeekState.NO_RESPONSE, WeekState.MGR_QUERY,
               WeekState.DRAFTED_IN_OUTLOOK, WeekState.DAMIA_APPROVED, WeekState.NOTHING_TO_DO):
        plan = plan_tick([WeekSnapshot(WK, st)], now=NOW)
        assert plan.intents == ()
        # none of these need Chrome (no draft/attach, not a submitted re-check)
        assert not plan.needs_portal


def test_submitted_rechecks_on_interval_only():
    snap = [WeekSnapshot(WK, WeekState.SUBMITTED)]
    # never polled → due
    assert plan_tick(snap, now=NOW, last_portal_poll={}).needs_portal
    # polled 1h ago, 6h interval → not due
    recent = {WK: NOW - timedelta(hours=1)}
    assert not plan_tick(snap, now=NOW, last_portal_poll=recent).needs_portal
    # polled 7h ago → due again
    stale = {WK: NOW - timedelta(hours=7)}
    assert plan_tick(snap, now=NOW, last_portal_poll=stale).needs_portal


def test_force_full_sweep_forces_portal():
    plan = plan_tick([WeekSnapshot(WK, WeekState.DAMIA_APPROVED)], now=NOW, force_full_sweep=True)
    assert plan.needs_portal
    assert "full reverse walk" in plan.portal_reason


# --- human gates & nudges --------------------------------------------------

def test_pending_human_gates():
    assert pending_human(WeekState.DRAFTED_IN_OUTLOOK) == "send"
    assert pending_human(WeekState.ATTACHED) == "submit"
    assert pending_human(WeekState.SENT_FOR_APPROVAL) is None


def test_nudge_escalates_with_age():
    fresh = derive_nudge(WeekState.DRAFTED_IN_OUTLOOK, NOW - timedelta(hours=2), NOW)
    assert fresh is not None and fresh.level == "act" and fresh.gate == "send"
    old = derive_nudge(WeekState.DRAFTED_IN_OUTLOOK, NOW - timedelta(hours=30), NOW)
    assert old is not None and old.level == "warn" and old.hours == 30.0


def test_nudge_none_when_no_gate():
    assert derive_nudge(WeekState.SUBMITTED, NOW, NOW) is None


def test_next_signal_from():
    assert next_signal_from(WeekState.READY_TO_DRAFT) == "bot"
    assert next_signal_from(WeekState.DRAFTED_IN_OUTLOOK) == "you"
    assert next_signal_from(WeekState.ATTACHED) == "you"
    assert next_signal_from(WeekState.SENT_FOR_APPROVAL) == "outlook"
    assert next_signal_from(WeekState.SUBMITTED) == "portal"
    assert next_signal_from(WeekState.DAMIA_APPROVED) is None


# --- notifications ---------------------------------------------------------

def test_transition_toast_fires_once():
    snap = [WeekSnapshot(WK, WeekState.DRAFTED_IN_OUTLOOK)]
    notes, m = diff_notifications({}, snap)
    assert len(notes) == 1 and notes[0].kind == "transition"
    # same state next tick → no repeat
    notes2, _ = diff_notifications(m, snap)
    assert notes2 == []


def test_transition_toast_only_for_toastworthy_states():
    # SENT_FOR_APPROVAL is not a toast-on-enter state
    notes, m = diff_notifications({}, [WeekSnapshot(WK, WeekState.SENT_FOR_APPROVAL)])
    assert notes == []
    assert m[WK] == WeekState.SENT_FOR_APPROVAL.value  # still recorded so a later change diffs


def test_transition_fires_on_change_into_new_state():
    snap1 = [WeekSnapshot(WK, WeekState.DRAFTED_IN_OUTLOOK)]
    _, m = diff_notifications({}, snap1)
    snap2 = [WeekSnapshot(WK, WeekState.ATTACHED)]  # advanced
    notes, _ = diff_notifications(m, snap2)
    assert len(notes) == 1 and notes[0].state is WeekState.ATTACHED


def test_renudge_respects_cooldown():
    snap = [WeekSnapshot(WK, WeekState.ATTACHED)]
    # never nudged → due
    assert len(due_renudges(snap, {}, NOW)) == 1
    # nudged 1h ago, 4h cooldown → not due
    assert due_renudges(snap, {WK: NOW - timedelta(hours=1)}, NOW) == []
    # nudged 5h ago → due
    assert len(due_renudges(snap, {WK: NOW - timedelta(hours=5)}, NOW)) == 1


def test_renudge_ignores_non_gate_states():
    assert due_renudges([WeekSnapshot(WK, WeekState.SENT_FOR_APPROVAL)], {}, NOW) == []


# --------------------------------------------------------------- hydrate action targeting

def _rec(week_start: str, units: float, status: str) -> WeekRecord:
    s = date.fromisoformat(week_start)
    return WeekRecord(week_start=s, week_end=s + timedelta(days=6), status=status,
                      total_units=units, worked_days=int(units), day_units=(0.0,) * 7)


# Monday 20 Jul 2026 -> Damia week-start Sun 19 Jul (in progress), target Sun 12 Jul.
_TODAY = date(2026, 7, 20)


def test_actions_target_last_completed_week_not_the_in_progress_one():
    """Regression: the banner used to name records[-1] (the in-progress week), so from Sunday
    onwards it nagged 'this week is empty' about days that had not happened yet, hiding the
    previous week that actually needed work."""
    actions = _derive_actions(
        [_rec("2026-07-12", 0, "Draft"), _rec("2026-07-19", 0, "Draft")], today=_TODAY)
    weeks = {a["week"] for a in actions}
    assert "2026-07-19" not in weeks          # in-progress week stays silent
    assert weeks == {"2026-07-12"}
    assert actions[0]["kind"] == "current_week_empty"


def test_in_progress_week_never_raises_an_action_even_when_filled():
    actions = _derive_actions(
        [_rec("2026-07-12", 5, "Submitted"), _rec("2026-07-19", 3, "Draft")], today=_TODAY)
    assert [a for a in actions if a["week"] == "2026-07-19"] == []


def test_filled_target_week_is_ready_to_submit():
    actions = _derive_actions(
        [_rec("2026-07-12", 5, "Draft"), _rec("2026-07-19", 0, "Draft")], today=_TODAY)
    assert [a["kind"] for a in actions] == ["current_week_ready"]
    assert actions[0]["week"] == "2026-07-12"


def test_older_unsubmitted_filled_weeks_still_flagged():
    actions = _derive_actions(
        [_rec("2026-07-05", 5, "Draft"), _rec("2026-07-12", 0, "Draft"),
         _rec("2026-07-19", 0, "Draft")], today=_TODAY)
    kinds = {a["kind"]: a["week"] for a in actions}
    assert kinds["unsubmitted_filled_week"] == "2026-07-05"
    assert kinds["current_week_empty"] == "2026-07-12"


def test_focus_never_lands_on_the_in_progress_week():
    """Regression: `focus` (the TUI 'Now' tab) scanned reversed(weeks) for the first
    needs_human state. The in-progress week is always empty -> always NEEDS_FILLING, so it won
    every time and pinned the Now tab to a week whose days had not happened yet."""
    cfg = Config(name="T", day_rate=500.0)
    paths = DataPaths(root=Path("unused"))
    recs = [_rec("2026-07-05", 5, "Approved"), _rec("2026-07-12", 0, "Draft"),
            _rec("2026-07-19", 0, "Draft")]
    view = build_view(recs, cfg, paths,
                      billable_by_week={r.week_start: 5 for r in recs},
                      now=datetime(2026, 7, 20, 9, 0), today=_TODAY)
    assert view["focus"] == "2026-07-12"


def test_focus_falls_back_to_latest_settled_week_when_nothing_needs_a_human():
    cfg = Config(name="T", day_rate=500.0)
    paths = DataPaths(root=Path("unused"))
    recs = [_rec("2026-07-05", 5, "Approved"), _rec("2026-07-12", 5, "Approved"),
            _rec("2026-07-19", 0, "Draft")]
    view = build_view(recs, cfg, paths,
                      billable_by_week={r.week_start: 5 for r in recs},
                      now=datetime(2026, 7, 20, 9, 0), today=_TODAY)
    assert view["focus"] == "2026-07-12"      # not the in-progress 19th


# --------------------------------------------------- ATTACHED -> SUBMITTED portal detection

_WK = date(2026, 7, 12)
_NOW = datetime(2026, 7, 20, 9, 0)


def test_attached_week_triggers_a_portal_recheck():
    """Regression: only SUBMITTED re-read the portal, so after the human pressed Submit in Damia
    the week sat on 'Proof attached' until the next daily full sweep."""
    plan = plan_tick([WeekSnapshot(_WK, WeekState.ATTACHED)], now=_NOW)
    assert plan.needs_portal
    assert "submitted on the portal" in plan.portal_reason


def test_attached_rechecks_on_the_fast_cadence():
    # 1h default for ATTACHED: not due at 30m, due at 90m (the 6h SUBMITTED cadence would not be).
    for age_h, expected in ((0.5, False), (1.5, True)):
        plan = plan_tick([WeekSnapshot(_WK, WeekState.ATTACHED)], now=_NOW,
                         last_portal_poll={_WK: _NOW - timedelta(hours=age_h)})
        assert plan.needs_portal is expected, f"age={age_h}h"


def test_submitted_stays_on_the_slow_cadence():
    plan = plan_tick([WeekSnapshot(_WK, WeekState.SUBMITTED)], now=_NOW,
                     last_portal_poll={_WK: _NOW - timedelta(hours=1.5)})
    assert not plan.needs_portal          # agency decides on its own schedule
    plan = plan_tick([WeekSnapshot(_WK, WeekState.SUBMITTED)], now=_NOW,
                     last_portal_poll={_WK: _NOW - timedelta(hours=7)})
    assert plan.needs_portal


def test_states_waiting_on_a_human_or_manager_do_not_drive_the_portal():
    for st in (WeekState.SENT_FOR_APPROVAL, WeekState.DRAFTED_IN_OUTLOOK, WeekState.MGR_QUERY):
        plan = plan_tick([WeekSnapshot(_WK, st)], now=_NOW)
        assert not plan.needs_portal, st
