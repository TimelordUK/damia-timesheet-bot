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

    week_start = _target_week(args)
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
    week_start = _target_week(args)
    plan = build_week_plan(week_start, holidays, leave)
    store = JsonSubmissionStore(paths.submissions_json)
    submission = store.get_by_week(week_start)
    # --force lets us regenerate a week already in flight: ignore its in-flight submission for
    # the decision and delete the stale Outlook draft before re-drafting.
    superseding = bool(args.force and submission is not None and submission.status.is_in_flight)
    decision_sub = None if superseding else submission

    print(f"Week {plan.week_start} – {plan.week_end}: plan = {plan.billable_days} day(s)")
    if plan.billable_days == 0:
        print(">>> 0 billable days — no email to draft.")
        return 0

    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        decision, rec = _navigate_decide_fill(drv, plan, decision_sub, do_fill=not args.dry_run)
        if rec is not None:
            print(f"  live portal: {rec.status}  units="
                  f"{','.join(f'{u:g}' for u in rec.day_units)}")
        print(f"  decision   : {decision.kind.value} — {decision.reason}")
        if decision.kind is not DecisionKind.READY_TO_DRAFT:
            print(">>> Not READY_TO_DRAFT — no email drafted, no changes made.")
            return 0 if decision.kind is DecisionKind.NOTHING_TO_DO else 1

        # Full-page capture of the portal — same context as the downloaded proofs
        # (name, date range, Timesheet Id, status, grid).
        png = drv.screenshot_week()
        img_width = getattr(drv, "last_screenshot_css_width", None)

    tracking_id = new_tracking_id(date.today())
    subject = approval_subject(plan, tracking_id)
    body = approval_body_html(plan, config.name, SCREENSHOT_CID, img_width=img_width)

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

    email_drv = OutlookComEmailDriver().connect()
    if superseding:
        removed = email_drv.delete_drafts_by_tracking_id(submission.tracking_id)
        print(f"  superseded prior draft {submission.tracking_id} (removed {removed}).")
    entry_id = email_drv.draft_submission_email(
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


def cmd_attach_proof(args: argparse.Namespace) -> int:
    """Upload the approval-proof PNG to a week's Damia Attachments panel. This attaches
    evidence only — it never clicks Submit. Defaults to the week's approval proof; --file
    overrides. Marks the submission SENT_TO_PORTAL on success."""
    from pathlib import Path
    paths = DataPaths.resolve(args.data_dir)
    try:
        config, _ = load_or_scaffold(paths.config_file)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    week_start = _target_week(args)
    store = JsonSubmissionStore(paths.submissions_json)
    sub = store.get_by_week(week_start)

    if args.file:
        proof = Path(args.file)
    elif sub is not None:
        proof = paths.proofs_dir / f"approval_{week_start.isoformat()}_{sub.tracking_id.split('-')[-1]}.png"
    else:
        print(f"[abort] no submission for {week_start} and no --file given.", file=sys.stderr)
        return 2

    if not proof.exists():
        print(f"[abort] proof not found: {proof}\n        Run `watch` to generate it, or pass "
              f"--file.", file=sys.stderr)
        return 2
    _ok_states = (SubmissionStatus.APPROVED, SubmissionStatus.SENT_TO_PORTAL)
    if sub is not None and sub.status not in _ok_states and not args.file:
        print(f"[abort] {week_start} is {sub.status.value}, not approved — refusing to attach an "
              f"unapproved proof. Use --file to override.", file=sys.stderr)
        return 1

    print(f"Week {week_start}: attaching {proof.name} ({proof.stat().st_size} bytes)")
    if args.dry_run:
        print(">>> --dry-run: would upload the above to the Damia Attachments panel. No changes.")
        return 0

    with DamiaTimesheetDriver(cdp_url=args.cdp_url).attached() as drv:
        drv.navigate_to_week(week_start)
        if drv.current_week_range()[0] != week_start:
            print(f"[abort] driver landed on {drv.current_week_range()[0]}, not {week_start}.",
                  file=sys.stderr)
            return 1
        drv.open_attachments_tab()
        existing = []
        try:
            existing = drv.attachment_urls()
        except Exception:
            pass
        before_count = len(existing)

        uploaded = False
        if existing and not args.replace:
            print(f"  {before_count} attachment(s) already on this week — skipping upload "
                  f"(use --replace to add another).")
        else:
            if not drv.upload_attachment(proof):
                print(">>> Upload did not confirm within the timeout — check the portal "
                      "manually.", file=sys.stderr)
                return 1
            uploaded = True

        saved = False
        if not args.no_save:
            drv.save_draft()   # bottom-left Save draft — NEVER Submit
            saved = True

        # Verify the upload actually PERSISTED. The panel can keep showing a freshly-picked
        # file that a reload reveals was never saved server-side (the silent failure seen on
        # the corporate portal). Reload + recount signed attachments before trusting it.
        if uploaded and saved:
            try:
                after_count = drv.reload_and_count_attachments(week_start)
            except Exception as e:
                print(f">>> Could NOT verify the attachment after reload ({e}). Check the "
                      f"portal — it may not have saved. Proof: {proof}", file=sys.stderr)
                return 1
            if after_count <= before_count:
                print(f">>> VERIFY FAILED: after reload the week still has {after_count} "
                      f"attachment(s) — the upload did NOT persist. Nothing marked done.\n"
                      f"    Attach it by hand from: {proof}", file=sys.stderr)
                return 1
            print(f"  verified: {after_count} attachment(s) now persisted on the week.")

    if sub is not None and (saved or args.no_save):
        store.mark_status(sub.tracking_id, SubmissionStatus.SENT_TO_PORTAL)
    tail = "and Saved the draft" if saved else "(draft NOT saved — use without --no-save)"
    print(f">>> Proof attached {tail}. Submit was NOT clicked — do the final submit yourself.")
    return 0


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
        print(f"\n{s.week_start}  {s.tracking_id}  ({s.status.value})")

        # Once the agency already has the week (submitted/approved/rejected) there's nothing to
        # redo — re-rendering or re-attaching is pointless, so skip it loudly.
        if portal_status.get(s.week_start) in _AGENCY_TERMINAL:
            print(f"  already '{portal_status[s.week_start]}' at the agency — too late to redo. "
                  f"Skipping.")
            continue

        # Probe Outlook up-front (across ALL accounts — see the adapter) so the precedence below
        # reads off one consistent snapshot, and so we can log exactly what was found. This is
        # also the diagnostics the work-PC Exchange case needs: is the Sent copy visible? is the
        # draft really still in Drafts? did the approval reply land?
        sent_at = drv.find_sent_original(s.tracking_id)
        draft_ids = drv.find_drafts_by_tracking_id(s.tracking_id)
        inbox_ids = drv.find_by_tracking_id(s.tracking_id)
        replies = [r for r in (drv.reply_summary(mid) for mid in inbox_ids) if r["is_reply"]]
        approved = None
        others: list = []
        for r in replies:
            verdict = classify_reply(extract_new_text(r["body"]), approval_cfg)
            if verdict.is_approval:
                approved = r
            else:
                others.append((r, verdict))
        print(f"  scan: sent={('yes @ ' + sent_at.isoformat()) if sent_at else 'no'}  "
              f"drafts={len(draft_ids)}  inbox={len(inbox_ids)}  "
              f"replies={len(replies)} (approved={'yes' if approved else 'no'})")

        # ---- PRECEDENCE -----------------------------------------------------------------
        # 1) An approval in the inbox trumps everything. It can only exist if the request was
        #    actually sent, so we don't care what the local Drafts/Sent folders say.
        if approved is not None:
            out = (paths.proofs_dir /
                   f"approval_{s.week_start.isoformat()}_{s.tracking_id.split('-')[-1]}.png")
            if args.dry_run:
                print(f"  APPROVED by {approved['sender_smtp']} — would render proof to "
                      f"{out.name} (dry-run).")
                continue
            paths.ensure_proofs()
            drv.render_proof(approved["entry_id"], out, cdp_url=args.cdp_url)
            print(f"  APPROVED by {approved['sender_smtp']} ({approved['received']}).")
            print(f"  proof: {out}")
            if s.status.is_in_flight:
                store.mark_status(s.tracking_id, SubmissionStatus.APPROVED)
                print("  -> upload this to the Damia week's Attachments tab (manual final step).")
            else:
                # forced re-render of a settled week — don't regress its status.
                print(f"  re-rendered (status left at {s.status.value}).")
                print(f"  -> re-attach with: damia-bot attach-proof --week {s.week_start} "
                      f"--replace")
            continue

        # 2) A non-approval reply is a manager query/rejection — flag it for a human.
        if others:
            r, verdict = others[-1]
            if args.dry_run:
                verb = "would mark"
            elif s.status.is_in_flight:
                store.mark_status(s.tracking_id, SubmissionStatus.NEEDS_ATTENTION)
                verb = "marked"
            else:
                verb = "left"  # forced re-check of a settled week — don't regress
            print(f"  reply from {r['sender_smtp']} is NOT a clean approval — {verb} "
                  f"needs_attention.")
            print(f"    {verdict.reason}")
            print(f"    reply text: {verdict.cleaned[:120]!r}")
            continue

        # 3) No reply yet — but have we actually SENT it? A Sent copy in ANY account flips a
        #    drafted week to awaiting, EVEN IF a draft is still lingering in Drafts (sending
        #    doesn't always delete the draft, and a stale draft must not mask a real send).
        if sent_at is not None:
            if s.status is SubmissionStatus.EMAIL_DRAFTED and not args.dry_run:
                store.mark_status(s.tracking_id, SubmissionStatus.AWAITING_APPROVAL, when=sent_at)
                s.status = SubmissionStatus.AWAITING_APPROVAL
                s.updated_at = sent_at
                print(f"  detected SENT at {sent_at} -> awaiting approval (no reply yet).")
            else:
                print(f"  sent at {sent_at}; awaiting approval (no reply yet).")
            continue

        # 4) No approval, no reply, no Sent copy anywhere. Is the draft genuinely still in Drafts?
        if draft_ids:
            print(f"  still sitting in Drafts ({len(draft_ids)}) — not sent yet; "
                  f"waiting for you to send.")
        elif s.status is SubmissionStatus.EMAIL_DRAFTED:
            print("  no Sent copy, no reply, and nothing in Drafts — the draft looks deleted "
                  "here, or was sent/approved on another machine we can't see. Left as-is.")
        else:
            print("  no reply yet — still awaiting approval.")

    _rebuild_view(paths, config)  # refresh the state board after any updates
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

    rt = sub.add_parser("render-test",
                        help="Render a sample proof to check proof-rendering works on this box.")
    rt.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    rt.set_defaults(func=cmd_render_test)

    t = sub.add_parser("tui", help="Launch the Textual TUI (reads view.json).")
    t.set_defaults(func=cmd_tui)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
