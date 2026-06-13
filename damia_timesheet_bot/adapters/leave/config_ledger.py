"""ConfigLeaveProvider — the contractor's days-off ledger, read from config.yml `leave:`.

Implements the LeaveProvider port over the raw list parsed by Config. Each entry is either
a single day (`date:`) or an inclusive range (`start:`/`end:`); ranges are expanded here
into one LeaveEntry per covered day. Parsing/validation errors raise ConfigError so a typo
in the precious config file is caught loudly at load, not silently ignored mid-run.

A later OutlookCalendarLeaveProvider can implement the same port without touching callers.
"""
from __future__ import annotations

from datetime import date, timedelta

from ...core.config import Config, ConfigError
from ...core.models import LeaveEntry, LeaveType


def _parse_date(value: object, ctx: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"leave: invalid date {value!r} in {ctx} (use YYYY-MM-DD).") from e


def _parse_type(value: object, ctx: str) -> LeaveType:
    raw = str(value or "annual").strip().lower()
    try:
        return LeaveType(raw)
    except ValueError as e:
        allowed = ", ".join(t.value for t in LeaveType)
        raise ConfigError(f"leave: invalid type {raw!r} in {ctx} (use one of: {allowed}).") from e


def _expand_entry(entry: dict) -> list[LeaveEntry]:
    if not isinstance(entry, dict):
        raise ConfigError(f"leave: each entry must be a mapping, got {entry!r}.")
    ltype = _parse_type(entry.get("type"), ctx=repr(entry))
    note = str(entry.get("note", "") or "")

    if "date" in entry:
        d = _parse_date(entry["date"], ctx=repr(entry))
        return [LeaveEntry(date=d, type=ltype, note=note)]

    if "start" in entry and "end" in entry:
        start = _parse_date(entry["start"], ctx=repr(entry))
        end = _parse_date(entry["end"], ctx=repr(entry))
        if end < start:
            raise ConfigError(f"leave: range end {end} is before start {start} in {entry!r}.")
        out: list[LeaveEntry] = []
        d = start
        while d <= end:
            out.append(LeaveEntry(date=d, type=ltype, note=note))
            d += timedelta(days=1)
        return out

    raise ConfigError(
        f"leave: entry must have either `date:` or both `start:` and `end:` — got {entry!r}."
    )


class ConfigLeaveProvider:
    """LeaveProvider backed by the config.yml ledger."""

    def __init__(self, entries: list[LeaveEntry]):
        # Last-wins on duplicate dates so a specific override after a range behaves intuitively.
        self._by_date: dict[date, LeaveEntry] = {e.date: e for e in entries}

    @classmethod
    def from_config(cls, config: Config) -> "ConfigLeaveProvider":
        expanded: list[LeaveEntry] = []
        for raw in config.leave:
            expanded.extend(_expand_entry(raw))
        return cls(expanded)

    def leave_on(self, day: date) -> LeaveEntry | None:
        return self._by_date.get(day)

    def leave_in_range(self, start: date, end: date) -> list[LeaveEntry]:
        if start > end:
            raise ValueError("start must be on or before end")
        return [e for d, e in sorted(self._by_date.items()) if start <= d <= end]
