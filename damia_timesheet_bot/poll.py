"""The `poll` loop — the autonomous workflow manager.

A thin I/O shell around the pure brains in `core/bot.py` and the guarded actions in `runner.py`.
Each tick it re-derives state from ground truth (Outlook, portal cache, submission ledger), lets
the circuit-breaker choose ≤1 mechanical action per week, drives Chrome ONLY when a transition
needs portal truth (event-driven), fires transition/nudge toasts, and writes the whole state to
`cache/view.json` for the TUI. It NEVER sends an email and NEVER submits — those two human gates
are surfaced as standing nudges.

State lives entirely in files, so killing and restarting the loop reconstructs identical state —
you can spin it up Monday morning and it picks up exactly where things stand.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import date, datetime, timedelta

from .adapters.email.outlook_com import OutlookComEmailDriver
from .adapters.holidays.uk_govuk import UkGovUkHolidayProvider
from .adapters.leave.config_ledger import ConfigLeaveProvider
from .adapters.notify import make_notifier
from .adapters.state.csv_cache import CsvWeekCache
from .adapters.state.submission_store import JsonSubmissionStore
from .adapters.timesheet.damia_playwright import DEFAULT_CDP_URL, DamiaTimesheetDriver
from .core.bot import (
    HealthReport,
    SubsystemHealth,
    TickAction,
    WeekSnapshot,
    diff_notifications,
    due_renudges,
    pending_human,
    plan_tick,
)
from .core.classify import ApprovalConfig
from .core.config import Config
from .core.hydrate import build_view, hydrate, write_view
from .core.paths import DataPaths
from .core.state import reconcile_week_state, WeekState
from .core.weekplan import build_week_plan, sunday_of
from .runner import locate_proof, run_attach, run_draft, run_watch_week


# --------------------------------------------------------------------- health probes

def _probe_cdp(cdp_url: str, timeout: float = 2.0) -> SubsystemHealth:
    """Is the debug Chrome reachable? A GET to /json/version is cheap and doesn't disturb the
    user's tabs (no attach, no focus)."""
    base = cdp_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/json/version", timeout=timeout) as resp:
            if resp.status == 200:
                info = json.loads(resp.read().decode("utf-8", "replace"))
                return SubsystemHealth(ok=True, detail=info.get("Browser", cdp_url))
            return SubsystemHealth(ok=False, detail=cdp_url, error=f"HTTP {resp.status}")
    except Exception as e:
        return SubsystemHealth(ok=False, detail=cdp_url,
                               error=f"no debug Chrome at {cdp_url} ({type(e).__name__})")


def _probe_outlook() -> tuple[SubsystemHealth, OutlookComEmailDriver | None]:
    """Can we reach classic Outlook over COM? Returns the health plus the connected driver to
    reuse this tick (so we connect once)."""
    try:
        drv = OutlookComEmailDriver().connect()
        return SubsystemHealth(ok=True, detail="classic Outlook (COM)"), drv
    except Exception as e:
        return SubsystemHealth(ok=False, error=f"classic Outlook not reachable ({type(e).__name__}: {e})"), None


def probe_health(cdp_url: str) -> tuple[HealthReport, OutlookComEmailDriver | None]:
    outlook, odrv = _probe_outlook()
    chrome = _probe_cdp(cdp_url)
    # Proof rendering rides on the same CDP Chrome (Chromium download is often blocked on work PCs).
    render = SubsystemHealth(ok=chrome.ok, detail="via CDP Chrome" if chrome.ok else "",
                             error=None if chrome.ok else "needs the debug Chrome")
    return HealthReport(outlook=outlook, chrome_cdp=chrome, render=render), odrv


# --------------------------------------------------------------------- runtime memory

_EMPTY_RUNTIME = {"tick_seq": 0, "last_notified": {}, "last_nudged": {},
                  "last_portal_poll": {}, "last_sweep_date": None}


def load_runtime(paths: DataPaths) -> dict:
    p = paths.bot_runtime_json
    if not p.exists():
        return dict(_EMPTY_RUNTIME)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {**_EMPTY_RUNTIME, **data}
    except Exception:
        return dict(_EMPTY_RUNTIME)


def save_runtime(paths: DataPaths, runtime: dict) -> None:
    paths.bot_runtime_json.parent.mkdir(parents=True, exist_ok=True)
    paths.bot_runtime_json.write_text(json.dumps(runtime, indent=2), encoding="utf-8")


# --------------------------------------------------------------------- state helpers

def _billable_by_week(paths, config, records, emit=None) -> dict:
    """Billable days per week. On failure this returns {} and every week loses its billable-day
    count, which makes `reconcile_week_state` unable to tell a full-leave week (NOTHING_TO_DO)
    from an unfilled one — so it reports NEEDS_FILLING for both. That is exactly the kind of
    silent mislabelling we must not hide: behind a corporate TLS proxy the gov.uk bank-holiday
    fetch raises SSLError, and the provider only re-raises once its cache AND vendored snapshot
    are both unusable. Surface it rather than swallowing it."""
    out: dict = {}
    try:
        leave = ConfigLeaveProvider.from_config(config)
        holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")
        out = {r.week_start: build_week_plan(r.week_start, holidays, leave).billable_days
               for r in records}
    except Exception as e:
        if emit is not None:
            emit("warn", f"billable-day calc failed ({type(e).__name__}: {e}) — leave/bank-holiday "
                         f"weeks may show as 'Needs filling'. Check the bank-holiday fetch.")
    return out


def _snapshots(records, subs, billable, now, target_week) -> list[WeekSnapshot]:
    """The managed working set for planning: the current target week (for a possible DRAFT) plus
    every week that already has a submission (for attach / re-check / watch). Old, unmanaged weeks
    are excluded so the bot never auto-drafts ancient empty weeks."""
    by_week = {r.week_start: r for r in records}
    managed = {target_week} | set(subs.keys())
    snaps: list[WeekSnapshot] = []
    for wk in sorted(managed):
        rec = by_week.get(wk)
        sub = subs.get(wk)
        state = reconcile_week_state(portal=rec, submission=sub, now=now,
                                     billable_days=billable.get(wk))
        snaps.append(WeekSnapshot(week_start=wk, state=state,
                                  since=sub.updated_at if sub else None))
    return snaps


# --------------------------------------------------------------------- one tick

def run_tick(paths: DataPaths, config: Config, *, cdp_url: str, notifier, log) -> dict:
    """Execute one full tick. Returns the `bot` block for view.json. Never raises out of a phase —
    a failure is logged into messages and the loop continues."""
    now = datetime.now()
    runtime = load_runtime(paths)
    runtime["tick_seq"] = int(runtime.get("tick_seq", 0)) + 1
    messages: list[dict] = []

    def emit(level: str, text: str) -> None:
        messages.append({"when": now.isoformat(timespec="seconds"), "level": level, "text": text})
        log(f"[{level}] {text}")

    paused = paths.pause_flag.exists()
    health, odrv = probe_health(cdp_url)
    emit("info", f"tick {runtime['tick_seq']}  outlook={'ok' if health.outlook.ok else 'DOWN'}  "
                 f"chrome={'ok' if health.chrome_cdp.ok else 'DOWN'}"
                 + ("  [PAUSED]" if paused else ""))

    store = JsonSubmissionStore(paths.submissions_json)
    approval_cfg = ApprovalConfig.from_dict(config.approval)
    target_week = sunday_of(date.today()) - timedelta(days=7)

    driving_chrome = False
    if not paused:
        try:
            _sense_and_act(paths, config, store, approval_cfg, health, odrv, runtime, now,
                           target_week, cdp_url=cdp_url, emit=emit)
        except Exception as e:  # a whole-tick guard; the loop must survive any single tick
            emit("error", f"tick failed: {type(e).__name__}: {e}")
        driving_chrome = runtime.get("_drove_chrome", False)
    runtime.pop("_drove_chrome", None)

    # ---- reconcile FINAL state (fresh reads) + notify -------------------------------------
    records = CsvWeekCache(paths.csv_path).read()
    subs = store.all_by_week()
    # `emit` is passed only here: this runs exactly once per tick (even when paused), so a
    # persistent bank-holiday/SSL failure warns once a tick instead of three times.
    billable = _billable_by_week(paths, config, records, emit)
    snaps = _snapshots(records, subs, billable, now, target_week)

    if not paused:
        _notify(snaps, runtime, now, notifier, emit)

    # ---- emit view.json with the bot block ------------------------------------------------
    next_tick = now  # filled by the loop; single-tick callers may ignore
    bot_block = {
        "enabled": True,
        "mode": "auto",
        "paused": paused,
        "tick_seq": runtime["tick_seq"],
        "last_tick_at": now.isoformat(timespec="seconds"),
        "next_tick_at": None,
        "driving_chrome": driving_chrome,
        "health": health.to_dict(),
        "messages": messages[-12:],
    }
    if records:
        view = build_view(records, config, paths, submissions=subs,
                          billable_by_week=billable, now=now, bot=bot_block)
        write_view(view, paths.view_json)

    save_runtime(paths, runtime)
    return bot_block


def _sense_and_act(paths, config, store, approval_cfg, health: HealthReport, odrv,
                   runtime, now, target_week, *, cdp_url, emit) -> None:
    """SENSE Outlook (every tick) → SENSE/ACT portal (event-driven). Mutates the ledger + portal
    only through the guarded runners. Records what it touched into `runtime`."""
    # --- SENSE: Outlook (background-safe) --------------------------------------------------
    portal_status = {r.week_start: (r.status or "").lower()
                     for r in CsvWeekCache(paths.csv_path).read()}
    if health.outlook.ok and odrv is not None:
        for s in [x for x in store.list_recent(weeks=12) if x.status.is_in_flight]:
            res = run_watch_week(paths=paths, store=store, drv=odrv, s=s,
                                 approval_cfg=approval_cfg, portal_status=portal_status,
                                 cdp_url=cdp_url, dry_run=False, render=health.render.ok)
            if res.changed:
                emit("info", f"{s.week_start}: {res.state_hint} — " + (res.messages[-1] if res.messages else ""))
    elif not health.outlook.ok:
        emit("warn", "Outlook down — cannot detect sends/replies this tick. Open classic Outlook.")

    # --- plan the portal side (event-driven) ----------------------------------------------
    records = CsvWeekCache(paths.csv_path).read()
    subs = store.all_by_week()
    billable = _billable_by_week(paths, config, records)
    snaps = _snapshots(records, subs, billable, now, target_week)

    last_poll = {date.fromisoformat(k): datetime.fromisoformat(v)
                 for k, v in runtime.get("last_portal_poll", {}).items()}
    today_iso = date.today().isoformat()
    force_full_sweep = runtime.get("last_sweep_date") != today_iso  # one reverse walk per day
    plan = plan_tick(snaps, now=now, last_portal_poll=last_poll, force_full_sweep=force_full_sweep)

    if not plan.needs_portal:
        emit("info", "no portal work this tick (Outlook-only).")
        return
    if not health.chrome_cdp.ok:
        emit("warn", f"portal work needed ({plan.portal_reason or 'draft/attach'}) but the debug "
                     f"Chrome is down — start it. Nudging only this tick.")
        return

    runtime["_drove_chrome"] = True
    with DamiaTimesheetDriver(cdp_url=cdp_url).attached() as drv:
        # Full reverse walk (startup / new day): rebuild the whole summary from portal truth.
        if force_full_sweep:
            emit("info", "full reverse walk (rebuilding the complete summary from the portal).")
            try:
                fresh = hydrate(drv, paths, download_pdf=True, pull_attachments=True, log=lambda *_: None)
                CsvWeekCache(paths.csv_path).write(fresh)
                runtime["last_sweep_date"] = today_iso
            except Exception as e:
                emit("error", f"full sweep failed: {type(e).__name__}: {e}")

        # Re-plan against fresh records after the sweep.
        records = CsvWeekCache(paths.csv_path).read()
        subs = store.all_by_week()
        billable = _billable_by_week(paths, config, records)
        snaps = _snapshots(records, subs, billable, now, target_week)
        plan = plan_tick(snaps, now=now, last_portal_poll=last_poll, force_full_sweep=False)

        leave = ConfigLeaveProvider.from_config(config)
        holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")

        for intent in plan.intents:
            wk = intent.week_start
            if intent.action is TickAction.DRAFT:
                if not (health.outlook.ok and odrv is not None):
                    emit("warn", f"{wk}: ready to draft but Outlook is down — can't create the draft.")
                    continue
                wkplan = build_week_plan(wk, holidays, leave)
                sub = store.get_by_week(wk)
                res = run_draft(paths, config, drv, odrv, wkplan, sub, store, force=False, dry_run=False)
                _log_result(emit, wk, "draft", res)
                runtime.setdefault("last_portal_poll", {})[wk.isoformat()] = now.isoformat()
            elif intent.action is TickAction.ATTACH:
                sub = store.get_by_week(wk)
                proof = locate_proof(paths, wk, sub, None)
                if proof is None or not proof.exists():
                    emit("warn", f"{wk}: approved but no proof file to attach yet — run watch/render.")
                    continue
                res = run_attach(paths, store, drv, wk, proof, sub, replace=False, save=True)
                _log_result(emit, wk, "attach", res)
                runtime.setdefault("last_portal_poll", {})[wk.isoformat()] = now.isoformat()

        # Refresh status for SUBMITTED weeks awaiting the agency decision (light per-week read).
        for s in snaps:
            if s.state is WeekState.SUBMITTED:
                try:
                    drv.navigate_to_week(s.week_start)
                    if drv.current_week_range()[0] == s.week_start:
                        _update_record_status(paths, s.week_start, drv.status_word())
                        runtime.setdefault("last_portal_poll", {})[s.week_start.isoformat()] = now.isoformat()
                except Exception as e:
                    emit("warn", f"{s.week_start}: agency-decision re-check failed ({type(e).__name__}).")


def _log_result(emit, wk, kind, res) -> None:
    level = "info" if res.ok else "warn"
    tail = res.messages[-1] if res.messages else ""
    emit(level, f"{wk}: {kind} {'ok' if res.ok else 'ABORTED'} — {tail}")


def _update_record_status(paths, week_start: date, status: str) -> None:
    cache = CsvWeekCache(paths.csv_path)
    records = cache.read()
    for r in records:
        if r.week_start == week_start:
            r.status = status
    cache.write(records)


def _notify(snaps, runtime, now, notifier, emit) -> None:
    """Fire transition toasts (once) + standing-gate re-nudges (on cooldown). Persist the markers."""
    last_notified = {date.fromisoformat(k): v for k, v in runtime.get("last_notified", {}).items()}
    notes, new_notified = diff_notifications(last_notified, snaps)

    last_nudged = {date.fromisoformat(k): datetime.fromisoformat(v)
                   for k, v in runtime.get("last_nudged", {}).items()}
    # A week that just fired a transition toast this tick shouldn't ALSO get a re-nudge — the
    # transition already told the user. Suppress renudges for those weeks.
    just_notified = {n.week_start for n in notes}
    renudges = [n for n in due_renudges(snaps, last_nudged, now)
                if n.week_start not in just_notified]
    gate_weeks = {s.week_start for s in snaps if pending_human(s.state) is not None}

    for n in notes + renudges:
        fired = notifier.notify(n.title, n.body)
        emit("info", f"{'toast' if fired else 'note'}: {n.title} — {n.body}")
        # Reset the re-nudge cooldown whenever we toast about an open human gate (transition or
        # renudge), so the next reminder is a full cooldown away rather than immediate.
        if n.week_start in gate_weeks:
            last_nudged[n.week_start] = now

    runtime["last_notified"] = {k.isoformat(): v for k, v in new_notified.items()}
    runtime["last_nudged"] = {k.isoformat(): v.isoformat() for k, v in last_nudged.items()}


# --------------------------------------------------------------------- the loop

def poll_loop(paths: DataPaths, config: Config, *, cdp_url: str, interval: float,
              notify_enabled: bool, once: bool, log) -> int:
    notifier = make_notifier(enabled=notify_enabled)
    log(f">>> damia-bot poll — every {interval:g}s, data root {paths.root}")
    log("    NEVER sends, NEVER submits. Ctrl-C to stop.\n")
    try:
        while True:
            bot = run_tick(paths, config, cdp_url=cdp_url, notifier=notifier, log=log)
            if once:
                return 0
            nxt = datetime.now() + timedelta(seconds=interval)
            # stamp next_tick into the written view so the TUI can show a countdown
            _stamp_next_tick(paths, nxt)
            log(f"    next tick ~{nxt.strftime('%H:%M:%S')}  (focus health: "
                f"{'ready' if bot['health']['can_work_fully'] else 'DEGRADED'})")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("\n>>> poll stopped.")
        return 0


def _stamp_next_tick(paths: DataPaths, nxt: datetime) -> None:
    try:
        data = json.loads(paths.view_json.read_text(encoding="utf-8"))
        if "bot" in data:
            data["bot"]["next_tick_at"] = nxt.isoformat(timespec="seconds")
            paths.view_json.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
