from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from ...core.models import Holiday

GOVUK_BANK_HOLIDAYS_URL = "https://www.gov.uk/bank-holidays.json"

REGIONS = ("england-and-wales", "scotland", "northern-ireland")


@dataclass
class UkGovUkHolidayProvider:
    region: str = "england-and-wales"
    cache_dir: Path | None = None
    cache_ttl: timedelta = timedelta(days=30)
    request_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if self.region not in REGIONS:
            raise ValueError(f"region must be one of {REGIONS}, got {self.region!r}")
        if self.cache_dir is None:
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
            self.cache_dir = Path(base) / "damia-timesheet-bot" / "holidays"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._holidays_by_date: dict[date, Holiday] | None = None

    def is_holiday(self, day: date) -> bool:
        return day in self._load()

    def holidays_in_range(self, start: date, end: date) -> list[Holiday]:
        if start > end:
            raise ValueError("start must be on or before end")
        return [h for d, h in sorted(self._load().items()) if start <= d <= end]

    def _load(self) -> dict[date, Holiday]:
        if self._holidays_by_date is not None:
            return self._holidays_by_date
        raw = self._fetch_cached()
        events = raw[self.region]["events"]
        self._holidays_by_date = {
            date.fromisoformat(e["date"]): Holiday(
                date=date.fromisoformat(e["date"]),
                title=e["title"],
                region=self.region,
            )
            for e in events
        }
        return self._holidays_by_date

    def _fetch_cached(self) -> dict:
        cache_path = self.cache_dir / "bank-holidays.json"
        if cache_path.exists():
            age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
            if age < self.cache_ttl:
                return json.loads(cache_path.read_text(encoding="utf-8"))
        response = requests.get(GOVUK_BANK_HOLIDAYS_URL, timeout=self.request_timeout_s)
        response.raise_for_status()
        text = response.text
        cache_path.write_text(text, encoding="utf-8")
        return json.loads(text)
