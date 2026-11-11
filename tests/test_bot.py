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


# --- delta walk ------------------------------------------------------------

from damia_timesheet_bot.adapters.state.csv_cache import CsvWeekCache
from damia_timesheet_bot.core.bot import SubsystemHealth as Sub
from damia_timesheet_bot.core.hydrate import hydrate, is_settled, sweep_floor
from damia_timesheet_bot.core.models import Week
from damia_timesheet_bot.poll import _portal_health, _update_record_status
from damia_timesheet_bot.tui import app as tui

_DELTA_TODAY = date(2026, 11, 2)
_NEWEST = date(2026, 10, 25)   # the current week in a fake portal


def _back(n_back: int) -> date:
    return _NEWEST - timedelta(days=7 * n_back)


def _delta_rec(n_back, status="Approved", units=5.0, pdf=None) -> WeekRecord:
    s = _back(n_back)
    return WeekRecord(week_start=s, week_end=s + timedelta(days=6), status=status,
                      total_units=units, worked_days=int(units), day_units=(), pdf_path=pdf)


class FakePortal:
    """Steps back through `statuses` (newest first), refusing past the end like the contract start."""
    timesheet_id = 1

    def __init__(self, statuses):
        self.statuses = statuses
        self.i = 0
        self.visited = []

    def navigate_to_current_week(self): self.i = 0
    def current_week_range(self): return _back(self.i), _back(self.i) + timedelta(days=6)
    def status_word(self): return self.statuses[self.i]
    def has_download_button(self): return False

    def read_week(self):
        self.visited.append(_back(self.i))
        return Week(start=_back(self.i), days=[])

    def step_to_prev_week(self):
        if self.i + 1 >= len(self.statuses):
            return False
        self.i += 1
        return True


def _paths(tmp_path):
    return DataPaths.resolve(str(tmp_path))


def _pdf(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    return pdf


def test_settled_needs_approved_and_its_pdf(tmp_path):
    pdf = _pdf(tmp_path)
    assert is_settled(_delta_rec(5, pdf=pdf), today=_DELTA_TODAY)
    assert not is_settled(_delta_rec(5), today=_DELTA_TODAY)
    assert not is_settled(_delta_rec(5, pdf=tmp_path / "gone.pdf"), today=_DELTA_TODAY)
    assert not is_settled(_delta_rec(5, "Submitted", pdf=pdf), today=_DELTA_TODAY)


def test_old_empty_week_settles_but_a_recent_one_does_not():
    assert is_settled(_delta_rec(8, "Draft", units=0), today=_DELTA_TODAY)
    assert is_settled(_delta_rec(8, "New", units=0), today=_DELTA_TODAY)
    assert not is_settled(_delta_rec(1, "Draft", units=0), today=_DELTA_TODAY)
    assert not is_settled(_delta_rec(8, "Draft", units=5), today=_DELTA_TODAY)


def test_floor_is_the_oldest_unsettled_week_even_behind_settled_ones(tmp_path):
    pdf = _pdf(tmp_path)
    known = [_delta_rec(4, pdf=pdf), _delta_rec(3, "Submitted"), _delta_rec(2, pdf=pdf)]
    assert sweep_floor(known, today=_DELTA_TODAY) == _back(3)


def test_floor_catches_a_hole_in_the_cache(tmp_path):
    pdf = _pdf(tmp_path)
    assert sweep_floor([_delta_rec(6, pdf=pdf), _delta_rec(2, pdf=pdf)], today=_DELTA_TODAY) == _back(2)


def test_floor_ignores_a_duplicate_row_keeping_the_later_one(tmp_path):
    pdf = _pdf(tmp_path)
    known = [_delta_rec(3), _delta_rec(3, pdf=pdf), _delta_rec(2, pdf=pdf)]
    assert sweep_floor(known, today=_DELTA_TODAY) == _back(2)


def test_no_cache_means_a_full_walk():
    assert sweep_floor([]) is None


def test_delta_walk_stops_at_the_floor_and_keeps_older_rows(tmp_path):
    pdf = _pdf(tmp_path)
    known = [_delta_rec(n, pdf=pdf) for n in range(40, 2, -1)]   # up to 3 weeks back, all settled
    portal = FakePortal(["Draft", "Draft", "Approved"] + ["Approved"] * 38)
    out = hydrate(portal, _paths(tmp_path), known=known, log=lambda *_: None)
    assert portal.visited == [_back(0), _back(1), _back(2), _back(3)]
    assert [r.week_start for r in out] == [_back(n) for n in range(40, -1, -1)]


def test_without_known_records_the_walk_reaches_the_job_start(tmp_path):
    portal = FakePortal(["Draft"] * 5)
    out = hydrate(portal, _paths(tmp_path), log=lambda *_: None)
    assert len(portal.visited) == len(out) == 5


def test_a_status_change_unsettles_the_week_until_it_is_rearchived(tmp_path):
    pdf = _pdf(tmp_path)
    paths = _paths(tmp_path)
    paths.ensure_cache()
    CsvWeekCache(paths.csv_path).write([_delta_rec(2, "Submitted", pdf=pdf)])
    assert _update_record_status(paths, _back(2), "Approved") is True
    assert not is_settled(CsvWeekCache(paths.csv_path).read()[0], today=_DELTA_TODAY)
    assert _update_record_status(paths, _back(2), "Approved") is False


# --- keep-alive ------------------------------------------------------------

_OK = Sub(ok=True, detail="logged in")
_T0 = datetime(2026, 11, 2, 9, 0)


def _fake_portal_probe(monkeypatch, result=_OK):
    calls = []

    def probe(url):
        calls.append(url)
        return result
    monkeypatch.setattr("damia_timesheet_bot.poll._probe_portal", probe)
    return calls


def _ph(runtime, now, *, chrome_ok=True, paused=False, force=False):
    return _portal_health("cdp", chrome_ok=chrome_ok, paused=paused, runtime=runtime, now=now,
                          force=force)


def test_keepalive_runs_on_the_five_minute_cadence(monkeypatch):
    calls = _fake_portal_probe(monkeypatch)
    rt = {}
    _ph(rt, _T0)
    _ph(rt, _T0 + timedelta(minutes=2))
    assert len(calls) == 1
    _ph(rt, _T0 + timedelta(minutes=5))
    assert len(calls) == 2


def test_a_dead_session_is_rechecked_every_tick(monkeypatch):
    calls = _fake_portal_probe(monkeypatch, Sub(ok=False, error="expired"))
    rt = {}
    _ph(rt, _T0)
    _ph(rt, _T0 + timedelta(seconds=30))
    assert len(calls) == 2


def test_no_keepalive_while_paused_or_without_chrome(monkeypatch):
    calls = _fake_portal_probe(monkeypatch)
    assert _ph({}, _T0, paused=True) is None
    assert _ph({}, _T0, chrome_ok=False) is None
    assert calls == []


def test_a_new_process_does_not_trust_the_recorded_session(monkeypatch):
    calls = _fake_portal_probe(monkeypatch)
    rt = {"portal": _OK.to_dict(), "last_keepalive": _T0.isoformat()}
    _ph(rt, _T0 + timedelta(seconds=10), force=True)
    assert len(calls) == 1


def test_logged_out_portal_stops_portal_work_but_not_email():
    h = HealthReport(Sub(ok=True), Sub(ok=True), Sub(ok=True), portal=Sub(ok=False))
    assert h.can_sense_email and not h.can_drive_portal and not h.can_work_fully
    assert HealthReport(Sub(ok=True), Sub(ok=True), Sub(ok=True)).can_work_fully   # unknown != down


# --- board health ----------------------------------------------------------

def test_a_finished_session_is_not_live():
    last = {"last_tick_at": "2026-11-02T09:00:00", "next_tick_at": "2026-11-02T09:02:00"}
    assert tui.loop_is_live(last, datetime(2026, 11, 2, 9, 2, 30))
    assert not tui.loop_is_live(last, datetime(2026, 11, 2, 17, 0))
    assert not tui.loop_is_live(None, _T0)


def test_a_long_tick_still_counts_as_live():
    mid = {"last_tick_at": "2026-11-02T09:00:00", "next_tick_at": None}
    assert tui.loop_is_live(mid, datetime(2026, 11, 2, 9, 10))
    assert not tui.loop_is_live(mid, datetime(2026, 11, 2, 9, 30))


def test_stale_down_status_is_replaced_by_the_live_check():
    bot = {"health": {"outlook": {"ok": False}, "chrome_cdp": {"ok": False},
                      "portal": {"ok": False}}}
    live = {"outlook": {"ok": True}, "chrome_cdp": {"ok": True}}
    h = tui.merged_health(bot, live, loop_live=False)
    assert h["outlook"]["ok"] and h["chrome_cdp"]["ok"] and "portal" not in h


def test_before_the_first_check_a_dead_loop_shows_unknown_not_down():
    bot = {"health": {"outlook": {"ok": False}}}
    mark = tui.status_mark(tui.merged_health(bot, None, loop_live=False).get("outlook"))
    assert mark == "[dim]?[/dim]"


def test_board_starts_and_reports_a_live_check(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr("damia_timesheet_bot.poll.probe_local",
                        lambda url: {"outlook": {"ok": True}, "chrome_cdp": {"ok": True}})

    async def go():
        app = tui.DamiaTUI(data_dir=str(tmp_path))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.live_health["outlook"]["ok"]
    asyncio.run(go())
