"""OutlookComEmailDriver — drafts approval-request emails via classic Outlook COM.

DRAFT ONLY. Creates a MailItem with the timesheet screenshot embedded inline (CID), and
Save()s it to the Drafts folder. It NEVER calls .Send(). The human reviews the draft in
Outlook and sends it themselves.

COM belongs to classic Outlook only (see project memory): GetActiveObject attaches to the
running instance. On the dev box this reaches the personal Gmail/IMAP profile, so drafts land
in [Google Mail]/Drafts. The read/approval-watch side lives separately (added next).
"""
from __future__ import annotations

import html as _html
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..render import render_html_dir_to_png
from ...core.tracking import parse_tracking_id

# Inline-image content-id shared with the body builder: the body references
# <img src="cid:{SCREENSHOT_CID}"> and we set the attachment's PR_ATTACH_CONTENT_ID to match.
SCREENSHOT_CID = "damia_timesheet_screenshot"

_PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
_PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
_OL_MAIL_ITEM = 0
_OL_BY_VALUE = 1
_OL_FOLDER_INBOX = 6
_OL_FOLDER_SENT = 5
_OL_FOLDER_DRAFTS = 16


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "file")


@dataclass
class OutlookComEmailDriver:
    """EmailDriver (draft side) over classic Outlook COM."""
    _app: object | None = field(default=None, repr=False)

    def connect(self) -> "OutlookComEmailDriver":
        import win32com.client  # imported lazily so non-Windows / non-email paths don't need it
        try:
            self._app = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            self._app = win32com.client.Dispatch("Outlook.Application")
        return self

    @property
    def app(self):
        if self._app is None:
            self.connect()
        return self._app

    def draft_submission_email(
        self,
        *,
        to: list[str],
        subject: str,
        body_html: str,
        attachment_png: bytes,
        tracking_id: str,
    ) -> str:
        """Create a Drafts MailItem with the screenshot embedded inline. Returns the EntryID.
        `body_html` must reference the screenshot as <img src="cid:{SCREENSHOT_CID}">.
        NEVER sends."""
        import pythoncom
        from pywintypes import com_error

        mail = self.app.CreateItem(_OL_MAIL_ITEM)
        mail.To = "; ".join(to)
        mail.Subject = subject

        tmp_path = None
        try:
            # Outlook attaches from a file path, so spill the PNG to a temp file first.
            fd, tmp_path = tempfile.mkstemp(prefix="damia_ts_", suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(attachment_png)

            att = mail.Attachments.Add(tmp_path, _OL_BY_VALUE, 0, "timesheet.png")
            try:
                att.PropertyAccessor.SetProperty(_PR_ATTACH_CONTENT_ID, SCREENSHOT_CID)
            except com_error:
                pass  # worst case the image shows as a normal attachment, not inline
            mail.HTMLBody = body_html
            self._reset_compose_zoom(mail)

            self._save_with_retry(mail, pythoncom, com_error)
            return mail.EntryID
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _reset_compose_zoom(mail, percent: int = 100) -> None:
        """Force the draft's editor zoom to 100%. Outlook persists the compose-window zoom
        globally, so a level the user once dialled down (e.g. to tame a previously-oversized
        image) sticks to every later draft and makes everything look tiny. Setting the
        WordEditor zoom resets that persisted value. Classic Outlook only — new Outlook exposes
        no WordEditor — and the WordEditor needs Word as the email editor; swallow anything."""
        try:
            inspector = mail.GetInspector
            doc = inspector.WordEditor  # Word.Document, or None if Word isn't the editor
            if doc is None:
                return
            doc.Windows(1).View.Zoom.Percentage = percent
        except Exception:
            pass  # new Outlook / non-Word editor / no GUI — zoom stays as-is, harmless

    @staticmethod
    def _save_with_retry(mail, pythoncom, com_error, attempts: int = 3) -> None:
        """IMAP sync can transiently reject a Save ('the message has been changed'); retry a
        couple of times. The item is brand-new so there's nothing to re-bind to."""
        last: Exception | None = None
        for i in range(attempts):
            try:
                mail.Save()
                return
            except com_error as e:
                last = e
                pythoncom.PumpWaitingMessages()
        if last is not None:
            raise last

    # ---- read / approval-watch side ---------------------------------------

    def _ns(self):
        return self.app.GetNamespace("MAPI")

    @staticmethod
    def _item_brief(it) -> dict:
        """A tiny read-only summary of a MailItem for the smoke test / diagnostics."""
        def g(attr):
            try:
                return getattr(it, attr, None)
            except Exception:
                return None
        return {
            "subject": (g("Subject") or "")[:80],
            "to": (g("To") or "")[:60],
            "sender": (g("SenderName") or ""),
            "sent": str(g("SentOn") or ""),
            "received": str(g("ReceivedTime") or ""),
            "unread": g("UnRead"),
        }

    def folder_overview(self, *, per_folder: int = 1) -> list[dict]:
        """Read-only smoke test. For every connected store, report its Inbox / Sent / Drafts:
        the folder path, the item count, and the most-recent item(s). Proves the bot can open
        and READ those folders on this machine — without touching the timesheet flow. Never
        writes. Returns one dict per (store, folder)."""
        ns = self._ns()
        report: list[dict] = []
        specs = (("Inbox", _OL_FOLDER_INBOX, "[ReceivedTime]"),
                 ("Sent", _OL_FOLDER_SENT, "[SentOn]"),
                 ("Drafts", _OL_FOLDER_DRAFTS, "[CreationTime]"))
        try:
            stores = ns.Stores
            store_count = stores.Count
        except Exception as e:
            return [{"error": f"cannot enumerate stores: {e}"}]
        for i in range(1, store_count + 1):
            try:
                store = stores.Item(i)
                store_name = getattr(store, "DisplayName", None) or f"store#{i}"
            except Exception as e:
                report.append({"store": f"store#{i}", "error": f"open failed: {e}"})
                continue
            for label, ftype, sortkey in specs:
                entry: dict = {"store": store_name, "folder": label}
                try:
                    folder = store.GetDefaultFolder(ftype)
                except Exception as e:
                    entry["error"] = f"no default {label} ({e})"
                    report.append(entry)
                    continue
                try:
                    entry["path"] = getattr(folder, "FolderPath", "") or ""
                    items = folder.Items
                    entry["count"] = items.Count
                    try:
                        items.Sort(sortkey, True)
                    except Exception:
                        pass  # some folders reject the sort key; unsorted recent is fine
                    recent: list[dict] = []
                    for n, it in enumerate(items):
                        if n >= per_folder:
                            break
                        recent.append(self._item_brief(it))
                    entry["recent"] = recent
                except Exception as e:
                    entry["error"] = f"read failed ({e})"
                report.append(entry)
        return report

    @staticmethod
    def _smtp_of(item) -> str:
        """Best-effort real SMTP. Exchange senders come back as X.500, so try the Exchange-user
        lookup then the SMTP property tag before falling back to SenderEmailAddress."""
        try:
            sender = item.Sender
            if sender is not None:
                try:
                    return sender.GetExchangeUser().PrimarySmtpAddress
                except Exception:
                    try:
                        return sender.PropertyAccessor.GetProperty(_PR_SMTP_ADDRESS)
                    except Exception:
                        pass
        except Exception:
            pass
        return getattr(item, "SenderEmailAddress", "") or ""

    def _default_folders(self, folder_type: int) -> list:
        """Default folder of this type (Inbox=6 / Sent=5 / Drafts=16) for every connected
        account, most-authoritative first.

        The namespace's GetDefaultFolder(type) returns the PRIMARY account's folder and is
        rock-solid — it's what worked before multi-store support. We ALWAYS include it first, so
        a single-account (e.g. one corporate Exchange mailbox) machine behaves exactly as it did
        before. We then ADD each other store's default folder for multi-account machines (the
        personal-+-Exchange work-PC case). Never a replacement — a superset — because per-store
        GetDefaultFolder can resolve to a secondary/archive/public-folder store whose 'Inbox'
        isn't your real one. Deduped by EntryID; matching is by the globally-unique tracking id,
        so scanning extra stores can't cause a false positive."""
        ns = self._ns()
        folders: list = []
        seen: set = set()

        def _add(f) -> None:
            if f is None:
                return
            try:
                fid = f.EntryID
            except Exception:
                fid = None
            if fid is not None:
                if fid in seen:
                    return
                seen.add(fid)
            folders.append(f)

        # 1) Primary account — the proven path; must always be scanned.
        try:
            _add(ns.GetDefaultFolder(folder_type))
        except Exception:
            pass
        # 2) Any other connected stores (secondary accounts, shared mailboxes with a default).
        try:
            stores = ns.Stores
            for i in range(1, stores.Count + 1):
                try:
                    _add(stores.Item(i).GetDefaultFolder(folder_type))
                except Exception:
                    continue  # store has no folder of this type (public-folder / PST / archive)
        except Exception:
            pass
        return folders

    @staticmethod
    def _match_by_tracking_id(folder, tracking_id: str, *, cap: int = 500) -> list:
        """Items in `folder` whose subject contains the tracking id. Tries the fast DASL
        Restrict first, then ALWAYS falls back to a capped manual scan when Restrict returns
        nothing — Exchange online can accept the LIKE '%…%' query yet return zero rows because
        the leading wildcard needs a content index the mailbox may not have (whereas the
        Gmail/IMAP dev box honours it, which is why this only bites on the work PC)."""
        items = folder.Items
        found: list = []
        seen: set = set()
        try:
            dasl = f"@SQL=\"urn:schemas:httpmail:subject\" LIKE '%{tracking_id}%'"
            for it in items.Restrict(dasl):
                eid = getattr(it, "EntryID", None)
                if eid and eid not in seen:
                    seen.add(eid)
                    found.append(it)
        except Exception:
            pass  # Restrict unsupported on this store/mode — the manual scan below covers it
        if not found:
            for key in ("[ReceivedTime]", "[SentOn]"):
                try:
                    items.Sort(key, True)
                    break
                except Exception:
                    continue
            for n, it in enumerate(items):
                if n > cap:
                    break
                if tracking_id in (getattr(it, "Subject", "") or ""):
                    eid = getattr(it, "EntryID", None)
                    if eid and eid not in seen:
                        seen.add(eid)
                        found.append(it)
        return found

    def find_by_tracking_id(self, tracking_id: str) -> list[str]:
        """EntryIDs of Inbox messages (across ALL accounts) whose subject contains the id."""
        out: list[str] = []
        for folder in self._default_folders(_OL_FOLDER_INBOX):
            for it in self._match_by_tracking_id(folder, tracking_id):
                out.append(it.EntryID)
        return out

    def find_drafts_by_tracking_id(self, tracking_id: str) -> list[str]:
        """EntryIDs of Drafts messages (across ALL accounts) bearing this tracking id. Lets the
        watcher actually verify whether a draft is still sitting in Drafts, instead of merely
        inferring it from the ledger status."""
        out: list[str] = []
        for folder in self._default_folders(_OL_FOLDER_DRAFTS):
            for it in self._match_by_tracking_id(folder, tracking_id):
                subj = (getattr(it, "Subject", "") or "").strip().lower()
                if subj.startswith("re:"):
                    continue  # a reply we're composing, not the original request draft
                out.append(it.EntryID)
        return out

    @staticmethod
    def _pytime_to_dt(when) -> datetime | None:
        """Convert a pywintypes time (Outlook SentOn/ReceivedTime) to a naive local datetime —
        consistent with the datetime.now() stamps the submission store uses elsewhere."""
        if when is None:
            return None
        try:
            return datetime(when.year, when.month, when.day,
                            when.hour, when.minute, when.second)
        except Exception:
            return None

    def find_sent_original(self, tracking_id: str) -> datetime | None:
        """If the ORIGINAL approval request bearing this tracking id is in ANY account's Sent
        folder, return when it was sent (naive local), else None. Replies (RE:) are excluded so
        only the user's outgoing request counts — this is how the bot learns the draft has
        actually been sent, without ever sending anything itself."""
        best: datetime | None = None
        for folder in self._default_folders(_OL_FOLDER_SENT):
            for it in self._match_by_tracking_id(folder, tracking_id):
                subj = (getattr(it, "Subject", "") or "").strip().lower()
                if subj.startswith("re:"):
                    continue  # a reply we sent, not the original request
                # A matching non-reply original in Sent IS proof of sending; don't let a quirky
                # SentOn (some Exchange sent items report it oddly) discard that — fall back to
                # CreationTime / ReceivedTime so we still return a timestamp.
                ts = (self._pytime_to_dt(getattr(it, "SentOn", None))
                      or self._pytime_to_dt(getattr(it, "CreationTime", None))
                      or self._pytime_to_dt(getattr(it, "ReceivedTime", None)))
                if ts and (best is None or ts > best):
                    best = ts
        return best

    def delete_drafts_by_tracking_id(self, tracking_id: str) -> int:
        """Delete any Drafts-folder messages (across ALL accounts) carrying this tracking id
        (used when re-drafting a week). Returns how many were removed."""
        n = 0
        for folder in self._default_folders(_OL_FOLDER_DRAFTS):
            for it in self._match_by_tracking_id(folder, tracking_id):
                try:
                    it.Delete()
                    n += 1
                except Exception:
                    pass
        return n

    def reply_summary(self, message_id: str) -> dict:
        """Lightweight, classify-ready view of a message. `body` is plain text (for the
        classifier); `is_reply` flags an actual RE: reply vs our own original."""
        item = self._ns().GetItemFromID(message_id)
        subject = getattr(item, "Subject", "") or ""
        received = getattr(item, "ReceivedTime", None)
        return {
            "entry_id": message_id,
            "subject": subject,
            "sender_name": getattr(item, "SenderName", "") or "",
            "sender_smtp": self._smtp_of(item),
            "received": str(received) if received else "",
            "body": getattr(item, "Body", "") or "",
            "is_reply": subject.strip().lower().startswith("re:"),
        }

    def render_proof(self, message_id: str, out_path, *, cdp_url: str | None = None) -> Path:
        """Render an approval reply (its 'Approved' + the quoted request + the embedded
        timesheet screenshot) to a single proof PNG, wrapped with the same metadata header the
        agency is used to. Inline images are saved and their cid: refs rewritten to files.

        cdp_url (if given) renders via the already-running Chrome — no Chromium download."""
        item = self._ns().GetItemFromID(message_id)
        out_path = Path(out_path)
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            body_html = getattr(item, "HTMLBody", "") or ""
            if not body_html.strip():
                body_html = "<pre>" + _html.escape(getattr(item, "Body", "") or "") + "</pre>"

            for att in list(item.Attachments):
                try:
                    cid = att.PropertyAccessor.GetProperty(_PR_ATTACH_CONTENT_ID)
                except Exception:
                    cid = None
                fn = _safe_name(getattr(att, "FileName", None) or f"att_{att.Index}")
                try:
                    att.SaveAsFile(str(tdp / fn))
                except Exception:
                    continue
                if cid:
                    body_html = body_html.replace(f"cid:{cid}", fn).replace(f"cid:<{cid}>", fn)

            (tdp / "index.html").write_text(self._proof_html(item, body_html), encoding="utf-8")
            render_html_dir_to_png(tdp, "index.html", out_path, cdp_url=cdp_url)
        return out_path

    def _proof_html(self, item, body_html: str) -> str:
        subject = _html.escape(getattr(item, "Subject", "") or "")
        sender = _html.escape(f"{getattr(item, 'SenderName', '') or ''} "
                              f"<{self._smtp_of(item)}>")
        received = _html.escape(str(getattr(item, "ReceivedTime", "") or ""))
        tracking = parse_tracking_id(getattr(item, "Subject", "") or "") or ""
        return (
            '<!doctype html><html><head><meta charset="utf-8"><style>'
            "body{font-family:Calibri,Arial,sans-serif;font-size:11pt;margin:24px;color:#111}"
            "h1{font-size:20pt;margin:0 0 12px}"
            ".meta div{margin:2px 0}"
            "hr{border:none;border-top:2px solid #1e57b0;margin:14px 0}"
            "img{max-width:100%;border:1px solid #ccc}"
            ".footer{margin-top:18px;color:#888;font-size:9pt}"
            "</style></head><body>"
            "<h1>Timesheet Approval Proof</h1><div class='meta'>"
            f"<div><b>Subject:</b> {subject}</div>"
            f"<div><b>From:</b> {sender}</div>"
            f"<div><b>Received:</b> {received}</div>"
            f"<div><b>TrackingId:</b> {tracking}</div></div><hr>"
            f"{body_html}"
            "<div class='footer'>Generated by damia-timesheet-bot</div>"
            "</body></html>"
        )
