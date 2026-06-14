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

            self._save_with_retry(mail, pythoncom, com_error)
            return mail.EntryID
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

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

    def find_by_tracking_id(self, tracking_id: str) -> list[str]:
        """Return EntryIDs of Inbox messages whose subject contains the tracking id."""
        inbox = self._ns().GetDefaultFolder(_OL_FOLDER_INBOX)
        items = inbox.Items
        out: list[str] = []
        try:
            dasl = f"@SQL=\"urn:schemas:httpmail:subject\" LIKE '%{tracking_id}%'"
            for it in items.Restrict(dasl):
                out.append(it.EntryID)
        except Exception:
            items.Sort("[ReceivedTime]", True)
            for n, it in enumerate(items):
                if n > 300:
                    break
                if tracking_id in (getattr(it, "Subject", "") or ""):
                    out.append(it.EntryID)
        return out

    def delete_drafts_by_tracking_id(self, tracking_id: str) -> int:
        """Delete any Drafts-folder messages carrying this tracking id (used when re-drafting
        a week). Returns how many were removed."""
        drafts = self._ns().GetDefaultFolder(16)  # olFolderDrafts
        items = drafts.Items
        victims = []
        try:
            dasl = f"@SQL=\"urn:schemas:httpmail:subject\" LIKE '%{tracking_id}%'"
            victims = list(items.Restrict(dasl))
        except Exception:
            victims = [it for it in items if tracking_id in (getattr(it, "Subject", "") or "")]
        n = 0
        for it in victims:
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

    def render_proof(self, message_id: str, out_path) -> Path:
        """Render an approval reply (its 'Approved' + the quoted request + the embedded
        timesheet screenshot) to a single proof PNG, wrapped with the same metadata header the
        agency is used to. Inline images are saved and their cid: refs rewritten to files."""
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
            render_html_dir_to_png(tdp, "index.html", out_path)
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
