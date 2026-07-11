"""damia-bot CLI. Draft-only, never submits.

Commands:
  hydrate   Walk the portal back to the job start; rebuild cache/ (CSV + PDFs + attachments)
            and cache/view.json. The portal is the source of truth; the cache is disposable.
  view      Print the current cache/view.json.
"""
from __future__ import annotations

import argparse
import sys

# Windows console is cp1252; portal strings + arrows are UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from datetime import date, datetime, timedelta

from .adapters.email.outlook_com import SCREENSHOT_CID, OutlookComEmailDriver
from .adapters.holidays.uk_govuk import UkGovUkHolidayProvider
from .adapters.leave.config_ledger import ConfigLeaveProvider
from .adapters.state.csv_cache import CsvWeekCache
from .adapters.state.submission_store import JsonSubmissionStore
from .adapters.timesheet.damia_playwright import DEFAULT_CDP_URL, DamiaTimesheetDriver
from .core.classify import ApprovalConfig, classify_reply, extract_new_text
from .core.config import ConfigError, load_or_scaffold
from .core.decide import Decision, DecisionKind, decide_week
from .core.hydrate import build_view, hydrate, write_view
from .core.models import Day, Submission, SubmissionStatus, Week, WeekRecord
from .core.paths import DataPaths
from .core.tracking import new_tracking_id
from .core.weekplan import approval_body_html, approval_subject, build_week_plan, sunday_of
from .runner import (
    locate_proof,
    navigate_decide_fill,
    run_attach,
    run_draft,
    run_watch_week,
)


def cmd_hydrate(args: argparse.Namespace) -> int:
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, scaffolded = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if scaffolded:
        print(f">>> Scaffolded a config template at {paths.config_file}")
    if config.is_placeholder:
        print(f"[note] config.yml is still the template (name={config.name!r}, "
              f"day_rate={config.day_rate}); revenue stats use the template rate for now.")

    print(f">>> Data root: {paths.root}")
    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        records = hydrate(
            drv, paths,
            download_pdf=not args.no_download,
            pull_attachments=not args.no_attachments,
            max_weeks=args.max_weeks,
        )

    CsvWeekCache(paths.csv_path).write(records)
    subs, billable = _state_inputs(paths, config, records)
    view = build_view(records, config, paths, submissions=subs, billable_by_week=billable)
    write_view(view, paths.view_json)

    s = view["stats"]
    cur = s["currency"]
    print(f"\nHydrated {len(records)} week(s).")
    print(f"  CSV:  {paths.csv_path}")
    print(f"  view: {paths.view_json}")
    print(f"  {s['total_units']:g}d total  ~{cur} {s['total_revenue']:,.0f}  "
          f"(approved {cur} {s['approved_revenue']:,.0f}, pending {cur} {s['pending_revenue']:,.0f})")
    if view["actions"]:
        print("\n  Action items:")
        for a in view["actions"]:
            print(f"   - {a['message']}")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    paths = DataPaths.resolve(args.data_dir)
    if not paths.view_json.exists():
        print("No view.json yet — run `hydrate` first.", file=sys.stderr)
        return 2
    print(paths.view_json.read_text(encoding="utf-8"))
    return 0


def _target_week(args: argparse.Namespace) -> date:
    """The week a command operates on. `--week` accepts any date in the target week; with no
    --week we default to the PREVIOUS (just-completed) week — the one you've just worked and
    are submitting — not the current in-progress week."""
    if args.week:
        return sunday_of(date.fromisoformat(args.week))
    return sunday_of(date.today()) - timedelta(days=7)


def _state_inputs(paths: DataPaths, config, records: list):
    """(submissions_by_week, billable_days_by_week) for state reconciliation. Billable is a
    best-effort plan computation (gov.uk falls back to the bundled snapshot offline)."""
    subs = JsonSubmissionStore(paths.submissions_json).all_by_week()
    billable: dict = {}
    try:
        leave = ConfigLeaveProvider.from_config(config)
        holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")
        billable = {r.week_start: build_week_plan(r.week_start, holidays, leave).billable_days
                    for r in records}
    except Exception:
        pass
    return subs, billable


def _rebuild_view(paths: DataPaths, config) -> dict | None:
    """Recompute cache/view.json from the portal cache + submission overlay (no portal/Outlook
    I/O). Returns the view, or None if there's no cache yet."""
    records = CsvWeekCache(paths.csv_path).read()
    if not records:
        return None
    subs, billable = _state_inputs(paths, config, records)
    view = build_view(records, config, paths, submissions=subs, billable_by_week=billable)
    write_view(view, paths.view_json)
    return view


def cmd_status(args: argparse.Namespace) -> int:
    """The passive bot: reconcile the portal cache + email submissions into a per-week state
    board and refresh cache/view.json. Read-only (no portal, no Outlook)."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, _ = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    view = _rebuild_view(paths, config)
    if view is None:
        print("No cache yet — run `hydrate` first.", file=sys.stderr)
        return 2

    focus = view.get("focus")
    weeks = view.get("weeks", [])
    print(f"State board  ({len(weeks)} weeks; focus = {focus or 'none'})\n")
    for w in weeks[-args.weeks:]:
        mark = ">>" if w["week_start"] == focus else "  "
        print(f"{mark} {w['week_start']}  {w['state_label']:<32} [{w['state']}]")
    if focus:
        fw = next((w for w in weeks if w["week_start"] == focus), None)
        if fw and fw.get("events"):
            print(f"\nFocus week {focus} — timeline:")
            for e in fw["events"]:
                print(f"   {e.get('when') or '':16} {e['text']}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Create the data root + a config.yml template (and the cache/proofs folders). Safe to
    run repeatedly — never overwrites an existing config."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, scaffolded = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    paths.ensure_cache()
    paths.ensure_proofs()

    print(f">>> {'Created' if scaffolded else 'Found existing'} config: {paths.config_file}")
    print(f"    Data root: {paths.root}")
    if config.is_placeholder:
        print("\n    Next: edit the config — set `name` and your real `approver_emails` —")
        print("    then run `damia-bot hydrate`.")
        print(f"\n    notepad {paths.config_file}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Read-only: show what the bot WOULD put on the timesheet for a week, and the approval
    subject it would draft. No portal, no Outlook — pure leave + bank-holiday logic."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, scaffolded = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if scaffolded:
        print(f">>> Scaffolded a config template at {paths.config_file}")

    try:
        leave = ConfigLeaveProvider.from_config(config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")

    week_start = _target_week(args)
    plan = build_week_plan(week_start, holidays, leave)

    # Portal truth (cached) + email-side overlay — both read-only local files.
    portal = next((r for r in CsvWeekCache(paths.csv_path).read()
                   if r.week_start == week_start), None)
    submission = JsonSubmissionStore(paths.submissions_json).get_by_week(week_start)
    decision = decide_week(plan, portal, submission)

    print(f"Week {plan.week_start} – {plan.week_end}  (Sun..Sat)")
    print(f"  billable days : {plan.billable_days}")
    print(f"  day_units     : {','.join(f'{u:g}' for u in plan.day_units)}  (Sun..Sat)")
    print(f"  portal        : {portal.status if portal else '(not hydrated)'}"
          + (f"  units={','.join(f'{u:g}' for u in portal.day_units)}" if portal else ""))
    if plan.excluded:
        print("  excluded:")
        for e in plan.excluded:
            print(f"    {e.date} {e.date.strftime('%a')}  {e.kind.value:13} {e.label}")

    marker = {
        DecisionKind.READY_TO_DRAFT: "[READY]",
        DecisionKind.ALREADY_IN_FLIGHT: "[IN FLIGHT]",
        DecisionKind.NOTHING_TO_DO: "[NOTHING TO DO]",
        DecisionKind.MANUAL_INTERVENTION: "[MANUAL]",
    }[decision.kind]
    print(f"\n  decision: {marker} {decision.reason}")

    if decision.kind is DecisionKind.READY_TO_DRAFT:
        subj = approval_subject(plan, new_tracking_id(date.today()))
        print(f"\n  would draft subject:\n    {subj}")
    return 0




def cmd_fill_draft(args: argparse.Namespace) -> int:
    """Fill the timesheet for a week per the plan and SAVE A DRAFT. Never submits.

    Runs the circuit-breaker against the LIVE week first: only a READY_TO_DRAFT decision
    mutates anything. `--dry-run` reads + decides but changes nothing."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, _ = load_or_scaffold(paths.config_file)
        leave = ConfigLeaveProvider.from_config(config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")

    week_start = _target_week(args)
    plan = build_week_plan(week_start, holidays, leave)
    submission = JsonSubmissionStore(paths.submissions_json).get_by_week(week_start)

    print(f"Week {plan.week_start} – {plan.week_end}: plan = {plan.billable_days} day(s), "
          f"units {','.join(f'{u:g}' for u in plan.day_units)} (Sun..Sat)")
    if plan.billable_days == 0:
        print(">>> 0 billable days — nothing to fill.")
        return 0

    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        decision, rec = navigate_decide_fill(drv, plan, submission, do_fill=not args.dry_run)
        if rec is not None:
            print(f"  live portal: {rec.status}  units="
                  f"{','.join(f'{u:g}' for u in rec.day_units)}")
        print(f"  decision   : {decision.kind.value} — {decision.reason}")

        if decision.kind is not DecisionKind.READY_TO_DRAFT:
            print(">>> Not READY_TO_DRAFT — no changes made.")
            return 0 if decision.kind is DecisionKind.NOTHING_TO_DO else 1
        if args.dry_run:
            print(">>> --dry-run: would fill the plan and Save draft (not submit). No changes.")
            return 0
        print(f">>> Filled and saved DRAFT. Portal now: {rec.status}  "
              f"{rec.total_units:g} day(s). (Never submitted.)")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    """Prepare a week (fill+save draft) AND draft the approval email into Outlook Drafts with
    the timesheet screenshot embedded. NEVER sends. Records an EMAIL_DRAFTED submission so the
    week can never be re-drafted (anti-spam). `--dry-run` does everything except touch Outlook."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, _ = load_or_scaffold(paths.config_file)
        leave = ConfigLeaveProvider.from_config(config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")
    week_start = _target_week(args)
    plan = build_week_plan(week_start, holidays, leave)
    store = JsonSubmissionStore(paths.submissions_json)
    submission = store.get_by_week(week_start)

    print(f"Week {plan.week_start} – {plan.week_end}: plan = {plan.billable_days} day(s)")

    # One guarded code path shared with the poll loop (runner.run_draft). Dry-run needs no Outlook.
    email_drv = None if args.dry_run else OutlookComEmailDriver().connect()
    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        result = run_draft(paths, config, drv, email_drv, plan, submission, store,
                           force=args.force, dry_run=args.dry_run)
    for m in result.messages:
        print(m if m.startswith("[abort]") else f"  {m}")
    return 0 if result.ok else 1


def cmd_attach_proof(args: argparse.Namespace) -> int:
    """Upload the approval-proof PNG to a week's Damia Attachments panel. This attaches
    evidence only — it never clicks Submit. Defaults to the week's approval proof; --file
    overrides. Marks the submission SENT_TO_PORTAL on success."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    week_start = _target_week(args)
    store = JsonSubmissionStore(paths.submissions_json)
    sub = store.get_by_week(week_start)

    proof = locate_proof(paths, week_start, sub, args.file)
    if proof is None:
        print(f"[abort] no submission for {week_start} and no --file given.", file=sys.stderr)
        return 2

    print(f"Week {week_start}: attaching {proof.name}")
    if args.dry_run:
        print(">>> --dry-run: would upload the above to the Damia Attachments panel. No changes.")
        return 0

    # Shared guarded path with the poll loop (runner.run_attach): upload + Save draft + verify-persist.
    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        result = run_attach(paths, store, drv, week_start, proof, sub,
                            replace=args.replace, save=not args.no_save,
                            allow_unapproved=bool(args.file))
    for m in result.messages:
        print(m if m.startswith("[abort]") else f"  {m}")
    return 0 if result.ok else 1


def cmd_render_test(args: argparse.Namespace) -> int:
    """Render a tiny sample proof to confirm proof-rendering works on this machine — useful on
    a corporate box where the Chromium download is blocked but Chrome is up on CDP."""
    import tempfile
    from pathlib import Path

    from .adapters.render import render_html_dir_to_png

    paths = DataPaths.resolve(args.data_dir)
    paths.ensure_proofs()
    out = paths.proofs_dir / "_render_test.png"
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "index.html").write_text(
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            "body{font-family:Calibri,Arial,sans-serif;margin:24px}h1{color:#1e57b0}</style>"
            "</head><body><h1>damia-timesheet-bot — render test</h1>"
            "<p>If you can read this PNG, approval-proof rendering works here "
            "(via your running Chrome over CDP — no Chromium download needed).</p>"
            "</body></html>", encoding="utf-8")
        render_html_dir_to_png(tdp, "index.html", out, cdp_url=args.cdp_url)
    print(f">>> Rendered test image to {out} ({out.stat().st_size} bytes).")
    print("    If that worked, `watch` will render real approval proofs the same way.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Passive Outlook sweep over in-flight weeks. For a still-drafted week, detect whether it
    has been sent (original in the Sent folder) and advance it to AWAITING_APPROVAL. For a sent
    week, look for the reply: a clean 'Approved' renders the proof PNG and advances to APPROVED;
    a non-approval reply (a query) flags needs_attention. Never sends; only writes proofs +
    state. `--week DATE` targets one week; `--force` (no week) redoes ONLY the latest week and
    re-renders its proof without regressing status. Weeks the agency already has
    (submitted/approved/rejected) are skipped — too late to redo. Pair with
    `attach-proof --replace` to re-attach."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, _ = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    store = JsonSubmissionStore(paths.submissions_json)
    approval_cfg = ApprovalConfig.from_dict(config.approval)
    portal_status = {r.week_start: (r.status or "").lower()
                     for r in CsvWeekCache(paths.csv_path).read()}
    _AGENCY_TERMINAL = {"approved", "submitted", "rejected"}

    if args.week:
        target = _target_week(args)
        one = store.get_by_week(target)
        if one is None:
            print(f"No submission recorded for {target}.")
            return 0
        work = [one]
    elif args.force:
        # Force without a week redoes ONLY the most recent week — not a sweep of older,
        # already-settled weeks (which would needlessly re-touch accepted timesheets).
        recent = store.list_recent(weeks=args.weeks)
        work = [max(recent, key=lambda s: s.week_start)] if recent else []
        if not work:
            print("No submissions to redo.")
            return 0
    else:
        work = [s for s in store.list_recent(weeks=args.weeks) if s.status.is_in_flight]
        if not work:
            print("No in-flight submissions to check.  "
                  "(use --force [--week DATE] to redo the latest / a specific week.)")
            return 0

    drv = OutlookComEmailDriver().connect()
    for s in work:
        print()
        # One guarded per-week path shared with the poll loop (runner.run_watch_week): detect
        # send / approval / query, self-heal a drifted tracking id, render proof, advance ledger.
        result = run_watch_week(paths=paths, store=store, drv=drv, s=s, approval_cfg=approval_cfg,
                                portal_status=portal_status, cdp_url=args.cdp_url,
                                dry_run=args.dry_run, render=True)
        for m in result.messages:
            print(f"  {m}")

    _rebuild_view(paths, config)  # refresh the state board after any updates
    return 0


def cmd_outlook_check(args: argparse.Namespace) -> int:
    """Read-only Outlook smoke test. Connect, then for every connected account show the most
    recent item(s) in Inbox / Sent / Drafts. Proves the bot can open and read those folders on
    this machine — no timesheet, no writes. Run this on the work PC to confirm Exchange access."""
    try:
        drv = OutlookComEmailDriver().connect()
        report = drv.folder_overview(per_folder=args.count)
    except Exception as e:
        print(f"Could not connect to / read classic Outlook: {e}", file=sys.stderr)
        print("Is classic Outlook (not 'new Outlook') running and signed in?", file=sys.stderr)
        return 2

    print("Connected to classic Outlook (COM).")
    if report and report[0].get("error") and "store" not in report[0]:
        print(f"  {report[0]['error']}", file=sys.stderr)
        return 2

    current_store = None
    for e in report:
        store = e.get("store", "?")
        if store != current_store:
            print(f"\n=== account: {store} ===")
            current_store = store
        folder = e.get("folder", "?")
        if e.get("error"):
            print(f"  {folder:<7} ! {e['error']}")
            continue
        print(f"  {folder:<7} {e.get('count', '?')} items   ({e.get('path', '')})")
        for m in e.get("recent", []):
            who = m["sender"] or m["to"] or ""
            when = m["sent"] or m["received"] or ""
            unread = " [unread]" if m.get("unread") else ""
            subj = m["subject"] or "(no subject)"
            print(f"      • {subj}")
            print(f"        {who}  {when}{unread}")
        if not e.get("recent"):
            print("      (empty)")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Repair a week's ledger tracking id to the one ACTUALLY sent. A re-draft used to mint a
    fresh random id and overwrite the ledger, orphaning the already-sent/approved email (which
    still carries the OLD id) — so `watch` searches Outlook for an id that isn't there. This finds
    the real message by the week's date-range in the subject (the true join key) and rewrites the
    ledger id to match. Use --dry-run to preview without changing anything."""
    paths = DataPaths.resolve(args.data_dir)
    store = JsonSubmissionStore(paths.submissions_json)
    target = _target_week(args)
    sub = store.get_by_week(target)
    if sub is None:
        print(f"No submission recorded for {target}.")
        return 0
    ws = sub.week_start
    week_end = ws + timedelta(days=6)
    week_range = f"{ws.strftime('%d/%m/%Y')} - {week_end.strftime('%d/%m/%Y')}"
    print(f"Week {ws}: ledger tracking id = {sub.tracking_id}")
    print(f"  searching Outlook (all accounts, Sent then Inbox) for subject range "
          f"{week_range!r} ...")
    try:
        drv = OutlookComEmailDriver().connect()
        found = drv.discover_tracking_id(week_range)
    except Exception as e:
        print(f"  Outlook read failed: {e}", file=sys.stderr)
        return 2
    if found is None:
        print("  no sent/received message found for this week's range — nothing to reconcile.")
        print("  (is the send/approval in a folder Outlook can see? run `damia-bot outlook-check`.)")
        return 1
    real_id, when = found
    when_s = when.isoformat() if when else "?"
    if real_id == sub.tracking_id:
        print(f"  ledger already matches the sent id ({real_id}, {when_s}). Nothing to do.")
        return 0
    print(f"  found real id in Outlook: {real_id}  (when={when_s})")
    if args.dry_run:
        print(f"  --dry-run: would rewrite ledger {sub.tracking_id} -> {real_id}.")
        return 0
    sub.tracking_id = real_id
    sub.updated_at = datetime.now()
    store.put(sub)
    print(f"  ledger tracking id updated -> {real_id}")
    print(f"  now run:  damia-bot watch --week {ws}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    # Import lazily so non-TUI commands don't pull in textual.
    from .tui.app import run_app
    run_app(args.data_dir)
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    """The autonomous workflow loop. Each tick: sense Outlook + (event-driven) the portal, let the
    circuit-breaker pick ≤1 mechanical action per week (fill+draft / attach proof), fire
    transition + standing-gate toasts, and write cache/view.json for the TUI. NEVER sends, NEVER
    submits — the two human gates stay manual. Resumable: state is re-derived from ground truth
    each tick, so it picks up wherever things stand."""
    from .poll import poll_loop
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, _ = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if config.is_placeholder:
        print("[abort] config.yml is still the template — run `init`, set your name + approvers "
              "first.", file=sys.stderr)
        return 2
    return poll_loop(paths, config, cdp_url=args.cdp_url, interval=args.interval,
                     notify_enabled=not args.no_notify, once=args.once, log=print)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="damia-bot",
                                description="Damia timesheet bot (draft-only; never submits).")
    p.add_argument("--data-dir",
                   help=r"Override data root (default %%LOCALAPPDATA%%\damia-timesheet-bot). Dev: ./state")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help="Create the data root + config.yml template, then exit.")
    i.set_defaults(func=cmd_init)

    h = sub.add_parser("hydrate", help="Rebuild the cache + view.json by walking the portal.")
    h.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    h.add_argument("--max-weeks", type=int, default=60)
    h.add_argument("--no-download", action="store_true", help="Skip PDF downloads.")
    h.add_argument("--no-attachments", action="store_true",
                   help="Skip pulling approval screenshots.")
    h.set_defaults(func=cmd_hydrate)

    v = sub.add_parser("view", help="Print the current view.json.")
    v.set_defaults(func=cmd_view)

    st = sub.add_parser("status",
                        help="Passive bot: derive per-week state from cache + submissions.")
    st.add_argument("--weeks", type=int, default=8, help="How many recent weeks to show.")
    st.set_defaults(func=cmd_status)

    pl = sub.add_parser("plan", help="Show the planned week (leave + bank holidays); no I/O.")
    pl.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: the previous (just-worked) week.")
    pl.set_defaults(func=cmd_plan)

    fd = sub.add_parser("fill-draft",
                        help="Fill a week per the plan and Save draft (never submits).")
    fd.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: the previous (just-worked) week.")
    fd.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    fd.add_argument("--dry-run", action="store_true",
                    help="Read + decide only; make no changes.")
    fd.set_defaults(func=cmd_fill_draft)

    dr = sub.add_parser("draft",
                        help="Fill the week + draft the approval email in Outlook (never sends).")
    dr.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: the previous (just-worked) week.")
    dr.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    dr.add_argument("--dry-run", action="store_true",
                    help="Fill + screenshot + show the email, but don't touch Outlook.")
    dr.add_argument("--force", action="store_true",
                    help="Re-draft a week already in flight: delete the stale draft and redo.")
    dr.set_defaults(func=cmd_draft)

    ap = sub.add_parser("attach-proof",
                        help="Upload a week's approval-proof PNG to Damia Attachments (no submit).")
    ap.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: the previous (just-worked) week.")
    ap.add_argument("--file", help="Attach this file instead of the week's approval proof.")
    ap.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    ap.add_argument("--dry-run", action="store_true", help="Locate + report only; upload nothing.")
    ap.add_argument("--no-save", action="store_true",
                    help="Attach but don't click Save draft afterwards.")
    ap.add_argument("--replace", action="store_true",
                    help="Upload even if the week already has an attachment.")
    ap.set_defaults(func=cmd_attach_proof)

    w = sub.add_parser("watch",
                       help="Detect sends + approval replies for in-flight weeks; render proofs.")
    w.add_argument("--weeks", type=int, default=12, help="How far back to consider (default 12).")
    w.add_argument("--week", help="Only this week (any date in it). Use with --force to redo.")
    w.add_argument("--force", action="store_true",
                   help="Redo the latest week (or --week) even if settled; skips weeks the agency "
                        "already has. Never regresses state.")
    w.add_argument("--cdp-url", default=DEFAULT_CDP_URL,
                   help="Render the proof via this running Chrome (no Chromium download).")
    w.add_argument("--dry-run", action="store_true",
                   help="Classify + report only; render no proof, change no state.")
    w.set_defaults(func=cmd_watch)

    oc = sub.add_parser("outlook-check",
                        help="Read-only smoke test: show recent Inbox/Sent/Drafts items per account.")
    oc.add_argument("--count", type=int, default=1,
                    help="How many recent items to show per folder (default 1).")
    oc.set_defaults(func=cmd_outlook_check)

    rc = sub.add_parser("reconcile",
                        help="Repair a week's ledger tracking id to the one actually sent (matches "
                             "the email by its subject date-range).")
    rc.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. "
                                   "Default: the previous (just-worked) week.")
    rc.add_argument("--dry-run", action="store_true",
                    help="Find + report only; don't rewrite the ledger.")
    rc.set_defaults(func=cmd_reconcile)

    rt = sub.add_parser("render-test",
                        help="Render a sample proof to check proof-rendering works on this box.")
    rt.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    rt.set_defaults(func=cmd_render_test)

    t = sub.add_parser("tui", help="Launch the Textual TUI (reads view.json).")
    t.set_defaults(func=cmd_tui)

    po = sub.add_parser("poll",
                        help="Autonomous workflow loop: sense→draft/attach→notify. Never sends/submits.")
    po.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    po.add_argument("--interval", type=float, default=180.0,
                    help="Seconds between ticks (default 180).")
    po.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    po.add_argument("--no-notify", action="store_true",
                    help="Suppress desktop toasts (state still lands in the JSON/TUI).")
    po.set_defaults(func=cmd_poll)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
