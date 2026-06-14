"""OutlookComEmailDriver — drafts approval-request emails via classic Outlook COM.

DRAFT ONLY. Creates a MailItem with the timesheet screenshot embedded inline (CID), and
Save()s it to the Drafts folder. It NEVER calls .Send(). The human reviews the draft in
Outlook and sends it themselves.

COM belongs to classic Outlook only (see project memory): GetActiveObject attaches to the
running instance. On the dev box this reaches the personal Gmail/IMAP profile, so drafts land
in [Google Mail]/Drafts. The read/approval-watch side lives separately (added next).
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

# Inline-image content-id shared with the body builder: the body references
# <img src="cid:{SCREENSHOT_CID}"> and we set the attachment's PR_ATTACH_CONTENT_ID to match.
SCREENSHOT_CID = "damia_timesheet_screenshot"

_PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
_OL_MAIL_ITEM = 0
_OL_BY_VALUE = 1


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
