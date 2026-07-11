"""Shared action runners — the ONE guarded code path for the three mechanical actions.

Both the interactive CLI (`draft` / `attach-proof` / `watch`) and the autonomous `poll` loop
call these, so the loop can never drift from the safe, proven commands. Each runner takes an
already-open driver (so the loop can reuse one connection per tick) and returns a structured
`ActionResult` — no printing; the caller decides how to surface `messages`.

Hard invariants live here and a layer down: `run_draft` only ever fills + Saves a draft and
creates an Outlook *draft* (never `.Send()`); `run_attach` only uploads + Saves a draft (never
Submit); `run_watch_week` only reads + records. The circuit-breaker (`decide_week`) gates every
draft, re-checked against the LIVE portal immediately before any mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .adapters.email.outlook_com import SCREENSHOT_CID
from .core.classify import classify_reply, extract_new_text
from .core.decide import Decision, DecisionKind, decide_week
from .core.models import Day, Submission, SubmissionStatus, Week, WeekRecord
from .core.tracking import new_tracking_id
from .core.weekplan import approval_body_html, approval_subject


@dataclass
class ActionResult:
    ok: bool                              # completed without error/abort
    changed: bool                         # did it mutate portal / Outlook / the ledger?
    messages: list[str] = field(default_factory=list)
    state_hint: str | None = None         # new submission status value, for the loop's log

    def log(self, msg: str) -> "ActionResult":
        self.messages.append(msg)
        return self


# --------------------------------------------------------------------- portal decide/fill

def units_match(a, b) -> bool:
    return len(a) == len(b) and all(round(x, 2) == round(y, 2) for x, y in zip(a, b))


def read_live_record(drv, plan) -> WeekRecord:
    live = drv.read_week()
    return WeekRecord(
        week_start=plan.week_start, week_end=plan.week_end, status=drv.status_word(),
        total_units=live.total_units, worked_days=live.worked_days,
        day_units=tuple(d.units for d in live.days),
    )


def navigate_decide_fill(drv, plan, submission, *, do_fill: bool):
    """Navigate to the plan's week, decide against the LIVE portal, and (if do_fill and READY and
    not already correct) fill per the plan + Save draft. Returns (decision, live_record_after).
    Mutates only on READY_TO_DRAFT."""
    drv.navigate_to_week(plan.week_start)
    landed = drv.current_week_range()[0]
    if landed != plan.week_start:
        return (Decision(DecisionKind.MANUAL_INTERVENTION, plan.week_start,
                         f"driver landed on {landed}, not {plan.week_start}."), None)

    rec = read_live_record(drv, plan)
    decision = decide_week(plan, rec, submission)

    if do_fill and decision.kind is DecisionKind.READY_TO_DRAFT \
            and not units_match(rec.day_units, plan.day_units):
        week = Week(start=plan.week_start, days=[
            Day(date=plan.week_start + timedelta(days=i), units=plan.day_units[i])
            for i in range(7)
        ])
        drv.fill_week(week)
        drv.save_draft()
        rec = read_live_record(drv, plan)
    return decision, rec


# --------------------------------------------------------------------- draft

def draft_preflight(config) -> str | None:
    """Config-only guard shared by CLI + loop. Returns an error message, or None if OK."""
    if config.is_placeholder:
        return "config.yml is still the template — set your name first."
    approvers = [a for a in config.approver_emails if a and "example.com" not in a]
    if not approvers:
        return "no real approver_emails in config.yml."
    return None


def run_draft(paths, config, drv, email_drv, plan, submission, store, *,
              force: bool = False, dry_run: bool = False) -> ActionResult:
    """Fill the week + draft the approval email into Outlook Drafts (screenshot embedded).
    NEVER sends. Records EMAIL_DRAFTED so the week can't be re-drafted. `email_drv` may be None
    when dry_run=True. Reuses the week's existing tracking id (stable per-week thread id)."""
    r = ActionResult(ok=True, changed=False)
    err = draft_preflight(config)
    if err:
        return ActionResult(ok=False, changed=False, messages=[f"[abort] {err}"])
    approvers = [a for a in config.approver_emails if a and "example.com" not in a]

    if plan.billable_days == 0:
        return r.log("0 billable days — no email to draft.")

    # --force regenerates a week already in flight: ignore its in-flight submission for the
    # decision and delete the stale Outlook draft before re-drafting.
    superseding = bool(force and submission is not None and submission.status.is_in_flight)
    decision_sub = None if superseding else submission

    decision, rec = navigate_decide_fill(drv, plan, decision_sub, do_fill=not dry_run)
    if rec is not None:
        r.log(f"live portal: {rec.status}  units={','.join(f'{u:g}' for u in rec.day_units)}")
    r.log(f"decision: {decision.kind.value} — {decision.reason}")
    if decision.kind is not DecisionKind.READY_TO_DRAFT:
        r.ok = decision.kind is DecisionKind.NOTHING_TO_DO
        return r.log("not READY_TO_DRAFT — no email drafted, no changes made.")

    png = drv.screenshot_week()
    img_width = getattr(drv, "last_screenshot_css_width", None)

    # Stable per-week thread correlator: reuse the existing id on a re-draft; only mint for a new week.
    tracking_id = submission.tracking_id if submission is not None else new_tracking_id(date.today())
    subject = approval_subject(plan, tracking_id)
    body = approval_body_html(plan, config.name, SCREENSHOT_CID, img_width=img_width)

    paths.ensure_proofs()
    shot_path = paths.proofs_dir / f"request_{plan.week_start.isoformat()}_{tracking_id.split('-')[-1]}.png"
    shot_path.write_bytes(png)
    r.log(f"tracking id: {tracking_id}  to: {', '.join(approvers)}")
    r.log(f"subject: {subject}")

    if dry_run:
        return r.log("--dry-run: would create the above as an Outlook DRAFT. Outlook untouched.")

    if superseding:
        removed = email_drv.delete_drafts_by_tracking_id(submission.tracking_id)
        r.log(f"superseded prior draft {submission.tracking_id} (removed {removed}).")
    entry_id = email_drv.draft_submission_email(
        to=approvers, subject=subject, body_html=body, attachment_png=png, tracking_id=tracking_id,
    )
    now = datetime.now()
    store.put(Submission(
        tracking_id=tracking_id, week_start=plan.week_start, status=SubmissionStatus.EMAIL_DRAFTED,
        created_at=(submission.created_at if submission else now), updated_at=now,
        approver_emails=approvers, timesheet_screenshot_path=shot_path,
    ))
    r.changed = True
    r.state_hint = SubmissionStatus.EMAIL_DRAFTED.value
    return r.log(f"created Outlook DRAFT (EntryID {entry_id[:12]}…). Review & send it yourself — "
                 f"the bot never sends.")


# --------------------------------------------------------------------- attach proof

def locate_proof(paths, week_start: date, sub, file: str | None) -> Path | None:
    if file:
        return Path(file)
    if sub is not None:
        return paths.proofs_dir / f"approval_{week_start.isoformat()}_{sub.tracking_id.split('-')[-1]}.png"
    return None


_ATTACH_OK_STATES = (SubmissionStatus.APPROVED, SubmissionStatus.SENT_TO_PORTAL)


def run_attach(paths, store, drv, week_start: date, proof: Path, sub, *,
               replace: bool = False, save: bool = True, allow_unapproved: bool = False) -> ActionResult:
    """Upload the approval proof to the week's Damia Attachments panel + Save draft. NEVER Submit.
    Verifies the upload PERSISTED (reload + recount signed attachments) before marking done, so a
    silent server-side non-save can't be mistaken for success. `drv` is an already-attached driver."""
    r = ActionResult(ok=True, changed=False)
    if not proof.exists():
        return ActionResult(ok=False, changed=False,
                            messages=[f"[abort] proof not found: {proof}"])
    if sub is not None and sub.status not in _ATTACH_OK_STATES and not allow_unapproved:
        return ActionResult(ok=False, changed=False,
                            messages=[f"[abort] {week_start} is {sub.status.value}, not approved — "
                                      f"refusing to attach an unapproved proof."])

    drv.navigate_to_week(week_start)
    if drv.current_week_range()[0] != week_start:
        return ActionResult(ok=False, changed=False,
                            messages=[f"[abort] driver landed on {drv.current_week_range()[0]}, "
                                      f"not {week_start}."])
    drv.open_attachments_tab()
    existing = []
    try:
        existing = drv.attachment_urls()
    except Exception:
        pass
    before_count = len(existing)

    uploaded = False
    if existing and not replace:
        r.log(f"{before_count} attachment(s) already on this week — skipping upload "
              f"(use --replace to add another).")
    else:
        if not drv.upload_attachment(proof):
            return ActionResult(ok=False, changed=False,
                                messages=["upload did not confirm within the timeout — check the portal."])
        uploaded = True

    saved = False
    if save:
        drv.save_draft()   # bottom-left Save draft — NEVER Submit
        saved = True

    if uploaded and saved:
        try:
            after_count = drv.reload_and_count_attachments(week_start)
        except Exception as e:
            return ActionResult(ok=False, changed=True,
                                messages=[f"could NOT verify the attachment after reload ({e}). "
                                          f"Check the portal. Proof: {proof}"])
        if after_count <= before_count:
            return ActionResult(ok=False, changed=False,
                                messages=[f"VERIFY FAILED: after reload the week still has "
                                          f"{after_count} attachment(s) — the upload did NOT persist. "
                                          f"Attach by hand from: {proof}"])
        r.log(f"verified: {after_count} attachment(s) now persisted on the week.")

    if sub is not None and (saved or not save):
        store.mark_status(sub.tracking_id, SubmissionStatus.SENT_TO_PORTAL)
        r.changed = True
        r.state_hint = SubmissionStatus.SENT_TO_PORTAL.value
    tail = "and Saved the draft" if saved else "(draft NOT saved)"
    return r.log(f"proof attached {tail}. Submit was NOT clicked — do the final submit yourself.")


# --------------------------------------------------------------------- watch (per week)

def run_watch_week(*, paths, store, drv, s, approval_cfg, portal_status: dict,
                   cdp_url: str | None, dry_run: bool = False,
                   render: bool = True) -> ActionResult:
    """Process ONE week's Outlook state, exactly as `watch` does: detect send / approval / query,
    self-heal a drifted tracking id, render the approval proof, and advance the ledger. Reads +
    records only — never sends. `drv` is a connected OutlookComEmailDriver. On a non-approval reply
    it persists the manager's cleaned query text on the submission so the TUI can show it."""
    r = ActionResult(ok=True, changed=False)
    _AGENCY_TERMINAL = {"approved", "submitted", "rejected"}
    r.log(f"{s.week_start}  {s.tracking_id}  ({s.status.value})")

    if portal_status.get(s.week_start) in _AGENCY_TERMINAL:
        return r.log(f"already '{portal_status[s.week_start]}' at the agency — too late to redo. Skipping.")

    sent_at = drv.find_sent_original(s.tracking_id)
    draft_ids = drv.find_drafts_by_tracking_id(s.tracking_id)
    inbox_ids = drv.find_by_tracking_id(s.tracking_id)

    # Self-heal a drifted tracking id: if nothing matches the ledger id, recover the real one by the
    # week's date-range in the subject (the true join key) and adopt it.
    if not sent_at and not inbox_ids and not draft_ids:
        week_end = s.week_start + timedelta(days=6)
        week_range = f"{s.week_start.strftime('%d/%m/%Y')} - {week_end.strftime('%d/%m/%Y')}"
        found = drv.discover_tracking_id(week_range)
        if found is not None and found[0] != s.tracking_id:
            real_id = found[0]
            r.log(f"ledger id {s.tracking_id} not found; discovered {real_id} by week range "
                  f"{week_range!r} -> adopting{' (dry-run)' if dry_run else ''}.")
            if not dry_run:
                s.tracking_id = real_id
                store.put(s)
            sent_at = drv.find_sent_original(s.tracking_id)
            draft_ids = drv.find_drafts_by_tracking_id(s.tracking_id)
            inbox_ids = drv.find_by_tracking_id(s.tracking_id)

    replies = [rr for rr in (drv.reply_summary(mid) for mid in inbox_ids) if rr["is_reply"]]
    approved = None
    others: list = []
    for rr in replies:
        verdict = classify_reply(extract_new_text(rr["body"]), approval_cfg)
        if verdict.is_approval:
            approved = rr
        else:
            others.append((rr, verdict))
    r.log(f"scan: sent={('yes @ ' + sent_at.isoformat()) if sent_at else 'no'}  "
          f"drafts={len(draft_ids)}  inbox={len(inbox_ids)}  "
          f"replies={len(replies)} (approved={'yes' if approved else 'no'})")

    # 1) An approval in the inbox trumps everything.
    if approved is not None:
        out = (paths.proofs_dir /
               f"approval_{s.week_start.isoformat()}_{s.tracking_id.split('-')[-1]}.png")
        if dry_run:
            return r.log(f"APPROVED by {approved['sender_smtp']} — would render proof to {out.name} (dry-run).")
        # Render the proof BEFORE advancing state. If rendering is unavailable (Chrome/CDP down,
        # which is how the proof is produced) we must NOT mark the week APPROVED — an APPROVED week
        # is no longer in-flight and would never be revisited, orphaning it without a proof. Leave
        # it in-flight so a later tick (Chrome up) re-detects and renders.
        rendered = out.exists()
        if render:
            try:
                paths.ensure_proofs()
                drv.render_proof(approved["entry_id"], out, cdp_url=cdp_url)
                rendered = True
                r.log(f"proof: {out}")
            except Exception as e:
                r.log(f"proof render failed: {type(e).__name__}: {e}")
        if not rendered:
            return r.log(f"APPROVED by {approved['sender_smtp']} but proof not rendered "
                         f"(Chrome/CDP down?) — leaving in-flight to retry when it's back up.")
        r.log(f"APPROVED by {approved['sender_smtp']} ({approved['received']}).")
        if s.status.is_in_flight:
            store.mark_status(s.tracking_id, SubmissionStatus.APPROVED)
            r.changed = True
            r.state_hint = SubmissionStatus.APPROVED.value
            r.log("-> proof ready; the loop will attach it (or attach-proof manually).")
        else:
            r.log(f"re-rendered (status left at {s.status.value}).")
        return r

    # 2) A non-approval reply is a manager query/rejection — flag it + persist the text.
    if others:
        rr, verdict = others[-1]
        if dry_run:
            verb = "would mark"
        elif s.status.is_in_flight:
            s.status = SubmissionStatus.NEEDS_ATTENTION
            s.updated_at = datetime.now()
            s.last_reply_text = verdict.cleaned
            store.put(s)                       # persist status + the query text together
            r.changed = True
            r.state_hint = SubmissionStatus.NEEDS_ATTENTION.value
            verb = "marked"
        else:
            verb = "left"
        r.log(f"reply from {rr['sender_smtp']} is NOT a clean approval — {verb} needs_attention.")
        r.log(f"  {verdict.reason}")
        r.log(f"  reply text: {verdict.cleaned[:120]!r}")
        return r

    # 3) No reply yet — but have we actually SENT it?
    if sent_at is not None:
        if s.status is SubmissionStatus.EMAIL_DRAFTED and not dry_run:
            store.mark_status(s.tracking_id, SubmissionStatus.AWAITING_APPROVAL, when=sent_at)
            s.status = SubmissionStatus.AWAITING_APPROVAL
            s.updated_at = sent_at
            r.changed = True
            r.state_hint = SubmissionStatus.AWAITING_APPROVAL.value
            r.log(f"detected SENT at {sent_at} -> awaiting approval (no reply yet).")
        else:
            r.log(f"sent at {sent_at}; awaiting approval (no reply yet).")
        return r

    # 4) No approval, no reply, no Sent copy. Is the draft genuinely still in Drafts?
    if draft_ids:
        r.log(f"still sitting in Drafts ({len(draft_ids)}) — not sent yet; waiting for you to send.")
    elif s.status is SubmissionStatus.EMAIL_DRAFTED:
        r.log("no Sent copy, no reply, and nothing in Drafts — draft looks deleted here, or "
              "sent/approved on another machine. Left as-is.")
    else:
        r.log("no reply yet — still awaiting approval.")
    return r
