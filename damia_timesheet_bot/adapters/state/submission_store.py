"""JsonSubmissionStore — the email-side submission overlay (StateStore port).

One JSON object keyed by week_start (ISO), one Submission per week. Human-readable and
hand-editable on purpose. Rebuildable from an Outlook recovery scan, so still a cache — but
it holds the tracking-ids we issued, so it lives outside cache/ (see DataPaths).

Writes are whole-file rewrites: the dataset is tiny (one row per week) and atomicity beats
cleverness here.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from ...core.models import Submission, SubmissionStatus


def _submission_to_dict(s: Submission) -> dict:
    return {
        "tracking_id": s.tracking_id,
        "week_start": s.week_start.isoformat(),
        "status": s.status.value,
        "created_at": s.created_at.isoformat(timespec="seconds"),
        "updated_at": s.updated_at.isoformat(timespec="seconds"),
        "approver_emails": list(s.approver_emails),
        "timesheet_screenshot_path": (
            str(s.timesheet_screenshot_path) if s.timesheet_screenshot_path else None
        ),
    }


def _dict_to_submission(d: dict) -> Submission:
    shot = d.get("timesheet_screenshot_path")
    return Submission(
        tracking_id=d["tracking_id"],
        week_start=date.fromisoformat(d["week_start"]),
        status=SubmissionStatus(d["status"]),
        created_at=datetime.fromisoformat(d["created_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        approver_emails=list(d.get("approver_emails") or []),
        timesheet_screenshot_path=Path(shot) if shot else None,
    )


class JsonSubmissionStore:
    """StateStore backed by a single JSON file keyed by week_start."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # ---- persistence ------------------------------------------------------

    def _load(self) -> dict[date, Submission]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        out: dict[date, Submission] = {}
        for key, rec in raw.items():
            sub = _dict_to_submission(rec)
            out[sub.week_start] = sub
        return out

    def _save(self, by_week: dict[date, Submission]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            wk.isoformat(): _submission_to_dict(sub)
            for wk, sub in sorted(by_week.items())
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ---- StateStore port --------------------------------------------------

    def get(self, tracking_id: str) -> Submission | None:
        for sub in self._load().values():
            if sub.tracking_id == tracking_id:
                return sub
        return None

    def get_by_week(self, week_start: date) -> Submission | None:
        return self._load().get(week_start)

    def all_by_week(self) -> dict[date, Submission]:
        return self._load()

    def put(self, submission: Submission) -> None:
        by_week = self._load()
        by_week[submission.week_start] = submission
        self._save(by_week)

    def list_recent(self, weeks: int) -> list[Submission]:
        cutoff = date.today() - timedelta(weeks=weeks)
        subs = [s for s in self._load().values() if s.week_start >= cutoff]
        return sorted(subs, key=lambda s: s.week_start)

    def mark_status(self, tracking_id: str, status: SubmissionStatus) -> None:
        by_week = self._load()
        for wk, sub in by_week.items():
            if sub.tracking_id == tracking_id:
                sub.status = status
                sub.updated_at = datetime.now()
                self._save(by_week)
                return
        raise KeyError(f"No submission with tracking_id {tracking_id!r}")
