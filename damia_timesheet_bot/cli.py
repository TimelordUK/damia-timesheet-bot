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
    view = build_view(records, config, paths)
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

    anchor = date.fromisoformat(args.week) if args.week else date.today()
    week_start = sunday_of(anchor)
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


def _units_match(a, b) -> bool:
    return len(a) == len(b) and all(round(x, 2) == round(y, 2) for x, y in zip(a, b))


def _read_live_record(drv, plan) -> WeekRecord:
    live = drv.read_week()
    return WeekRecord(
        week_start=plan.week_start, week_end=plan.week_end, status=drv.status_word(),
        total_units=live.total_units, worked_days=live.worked_days,
        day_units=tuple(d.units for d in live.days),
    )


def _navigate_decide_fill(drv, plan, submission, *, do_fill: bool):
    """Navigate to the plan's week, decide against the LIVE portal, and (if do_fill and the
    decision is READY and the sheet isn't already correct) fill per the plan + Save draft.
    Returns (decision, live_record_after). Mutates only on READY_TO_DRAFT."""
    drv.navigate_to_week(plan.week_start)
    landed = drv.current_week_range()[0]
    if landed != plan.week_start:
        return (Decision(DecisionKind.MANUAL_INTERVENTION, plan.week_start,
                         f"driver landed on {landed}, not {plan.week_start}."), None)

    rec = _read_live_record(drv, plan)
    decision = decide_week(plan, rec, submission)

    if do_fill and decision.kind is DecisionKind.READY_TO_DRAFT \
            and not _units_match(rec.day_units, plan.day_units):
        week = Week(start=plan.week_start, days=[
            Day(date=plan.week_start + timedelta(days=i), units=plan.day_units[i])
            for i in range(7)
        ])
        drv.fill_week(week)
        drv.save_draft()
        rec = _read_live_record(drv, plan)
    return decision, rec


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

    anchor = date.fromisoformat(args.week) if args.week else date.today()
    week_start = sunday_of(anchor)
    plan = build_week_plan(week_start, holidays, leave)
    submission = JsonSubmissionStore(paths.submissions_json).get_by_week(week_start)

    print(f"Week {plan.week_start} – {plan.week_end}: plan = {plan.billable_days} day(s), "
          f"units {','.join(f'{u:g}' for u in plan.day_units)} (Sun..Sat)")
    if plan.billable_days == 0:
        print(">>> 0 billable days — nothing to fill.")
        return 0

    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        decision, rec = _navigate_decide_fill(drv, plan, submission, do_fill=not args.dry_run)
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
    if config.is_placeholder:
        print("[abort] config.yml is still the template — set your name first.", file=sys.stderr)
        return 2
    approvers = [a for a in config.approver_emails if a and "example.com" not in a]
    if not approvers:
        print("[abort] no real approver_emails in config.yml.", file=sys.stderr)
        return 2

    holidays = UkGovUkHolidayProvider(cache_dir=paths.root / "holidays")
    anchor = date.fromisoformat(args.week) if args.week else date.today()
    week_start = sunday_of(anchor)
    plan = build_week_plan(week_start, holidays, leave)
    store = JsonSubmissionStore(paths.submissions_json)
    submission = store.get_by_week(week_start)

    print(f"Week {plan.week_start} – {plan.week_end}: plan = {plan.billable_days} day(s)")
    if plan.billable_days == 0:
        print(">>> 0 billable days — no email to draft.")
        return 0

    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        decision, rec = _navigate_decide_fill(drv, plan, submission, do_fill=not args.dry_run)
        if rec is not None:
            print(f"  live portal: {rec.status}  units="
                  f"{','.join(f'{u:g}' for u in rec.day_units)}")
        print(f"  decision   : {decision.kind.value} — {decision.reason}")
        if decision.kind is not DecisionKind.READY_TO_DRAFT:
            print(">>> Not READY_TO_DRAFT — no email drafted, no changes made.")
            return 0 if decision.kind is DecisionKind.NOTHING_TO_DO else 1

        png = drv.screenshot_timesheet()

    tracking_id = new_tracking_id(date.today())
    subject = approval_subject(plan, tracking_id)
    body = approval_body_html(plan, config.name, SCREENSHOT_CID)

    paths.ensure_proofs()
    shot_path = paths.proofs_dir / f"request_{week_start.isoformat()}_{tracking_id.split('-')[-1]}.png"
    shot_path.write_bytes(png)

    print(f"  tracking id: {tracking_id}")
    print(f"  to         : {', '.join(approvers)}")
    print(f"  subject    : {subject}")
    print(f"  screenshot : {shot_path}  ({len(png)} bytes)")

    if args.dry_run:
        print(">>> --dry-run: would create the above as an Outlook DRAFT. Outlook untouched.")
        return 0

    entry_id = OutlookComEmailDriver().connect().draft_submission_email(
        to=approvers, subject=subject, body_html=body, attachment_png=png,
        tracking_id=tracking_id,
    )
    now = datetime.now()
    store.put(Submission(
        tracking_id=tracking_id, week_start=week_start, status=SubmissionStatus.EMAIL_DRAFTED,
        created_at=now, updated_at=now, approver_emails=approvers,
        timesheet_screenshot_path=shot_path,
    ))
    print(f">>> Created Outlook DRAFT (EntryID {entry_id[:12]}…) in your Drafts folder. "
          f"Review and send it yourself — the bot never sends.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Check in-flight submissions for an approval reply. On a clean 'Approved' reply, render
    the proof PNG and advance the week to APPROVED. A non-approval reply (a query) flags the
    week needs_attention. Read-only against Outlook except writing proofs + state."""
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, _ = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    store = JsonSubmissionStore(paths.submissions_json)
    approval_cfg = ApprovalConfig.from_dict(config.approval)
    inflight = [s for s in store.list_recent(weeks=args.weeks) if s.status.is_in_flight]
    if not inflight:
        print("No in-flight submissions to check.")
        return 0

    drv = OutlookComEmailDriver().connect()
    for s in inflight:
        print(f"\n{s.week_start}  {s.tracking_id}  ({s.status.value})")
        replies = [r for r in (drv.reply_summary(mid)
                               for mid in drv.find_by_tracking_id(s.tracking_id))
                   if r["is_reply"]]
        if not replies:
            print("  no reply yet — still awaiting approval.")
            continue

        approved = None
        others: list = []
        for r in replies:
            verdict = classify_reply(extract_new_text(r["body"]), approval_cfg)
            if verdict.is_approval:
                approved = r
            else:
                others.append((r, verdict))

        if approved is not None:
            out = (paths.proofs_dir /
                   f"approval_{s.week_start.isoformat()}_{s.tracking_id.split('-')[-1]}.png")
            if args.dry_run:
                print(f"  APPROVED by {approved['sender_smtp']} — would render proof to "
                      f"{out.name} (dry-run).")
                continue
            paths.ensure_proofs()
            drv.render_proof(approved["entry_id"], out)
            store.mark_status(s.tracking_id, SubmissionStatus.APPROVED)
            print(f"  APPROVED by {approved['sender_smtp']} ({approved['received']}).")
            print(f"  proof: {out}")
            print("  -> upload this to the Damia week's Attachments tab (manual final step).")
        else:
            r, verdict = others[-1]
            if not args.dry_run:
                store.mark_status(s.tracking_id, SubmissionStatus.NEEDS_ATTENTION)
            verb = "would mark" if args.dry_run else "marked"
            print(f"  reply from {r['sender_smtp']} is NOT a clean approval — {verb} "
                  f"needs_attention.")
            print(f"    {verdict.reason}")
            print(f"    reply text: {verdict.cleaned[:120]!r}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    # Import lazily so non-TUI commands don't pull in textual.
    from .tui.app import run_app
    run_app(args.data_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="damia-bot",
                                description="Damia timesheet bot (draft-only; never submits).")
    p.add_argument("--data-dir",
                   help=r"Override data root (default %%LOCALAPPDATA%%\damia-timesheet-bot). Dev: ./state")
    sub = p.add_subparsers(dest="command", required=True)

    h = sub.add_parser("hydrate", help="Rebuild the cache + view.json by walking the portal.")
    h.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    h.add_argument("--max-weeks", type=int, default=60)
    h.add_argument("--no-download", action="store_true", help="Skip PDF downloads.")
    h.add_argument("--no-attachments", action="store_true",
                   help="Skip pulling approval screenshots.")
    h.set_defaults(func=cmd_hydrate)

    v = sub.add_parser("view", help="Print the current view.json.")
    v.set_defaults(func=cmd_view)

    pl = sub.add_parser("plan", help="Show the planned week (leave + bank holidays); no I/O.")
    pl.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: today.")
    pl.set_defaults(func=cmd_plan)

    fd = sub.add_parser("fill-draft",
                        help="Fill a week per the plan and Save draft (never submits).")
    fd.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: today.")
    fd.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    fd.add_argument("--dry-run", action="store_true",
                    help="Read + decide only; make no changes.")
    fd.set_defaults(func=cmd_fill_draft)

    dr = sub.add_parser("draft",
                        help="Fill the week + draft the approval email in Outlook (never sends).")
    dr.add_argument("--week", help="Any date (YYYY-MM-DD) in the target week. Default: today.")
    dr.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    dr.add_argument("--dry-run", action="store_true",
                    help="Fill + screenshot + show the email, but don't touch Outlook.")
    dr.set_defaults(func=cmd_draft)

    w = sub.add_parser("watch",
                       help="Check in-flight submissions for approval replies; render proofs.")
    w.add_argument("--weeks", type=int, default=12, help="How far back to consider (default 12).")
    w.add_argument("--dry-run", action="store_true",
                   help="Classify + report only; render no proof, change no state.")
    w.set_defaults(func=cmd_watch)

    t = sub.add_parser("tui", help="Launch the Textual TUI (reads view.json).")
    t.set_defaults(func=cmd_tui)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
