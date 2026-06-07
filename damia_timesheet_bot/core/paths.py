"""Data-root layout resolver.

Hard line between PRECIOUS (user-authored config, back this up) and DISPOSABLE (everything
under cache/, rebuilt by re-hydrating from the portal). Deleting cache/ must never be able
to touch config.yml — they live in separate subtrees for exactly that reason.

Default root: %LOCALAPPDATA%\\damia-timesheet-bot\\. Override with --data-dir (dev: ./state).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

APP_DIR_NAME = "damia-timesheet-bot"


def default_data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @classmethod
    def resolve(cls, data_dir: str | os.PathLike | None = None) -> "DataPaths":
        root = Path(data_dir).expanduser() if data_dir else default_data_root()
        return cls(root=root.resolve())

    # --- precious -------------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.root / "config.yml"

    @property
    def chrome_profile(self) -> Path:
        return self.root / "chrome-profile"

    # --- disposable cache -----------------------------------------------------
    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def csv_path(self) -> Path:
        return self.cache_dir / "timesheets.csv"

    @property
    def pdf_dir(self) -> Path:
        return self.cache_dir / "pdf"

    @property
    def attachments_dir(self) -> Path:
        return self.cache_dir / "attachments"

    @property
    def view_json(self) -> Path:
        return self.cache_dir / "view.json"

    def attachments_for(self, week_start: date) -> Path:
        return self.attachments_dir / week_start.isoformat()

    def ensure_cache(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
