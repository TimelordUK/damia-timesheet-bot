"""Per-contractor config — the one PRECIOUS, user-authored file. Everything else is
disposable cache. YAML so a contractor and the future mailtriage rules share one format.

First run scaffolds a commented template; the contractor edits their name, day rate and
approver emails, then re-runs. The rest of the tool rebuilds itself by hydrating the portal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_TEMPLATE = """\
# damia-timesheet-bot — contractor config.
# This file is PRECIOUS (yours to edit). Everything under cache/ is disposable and is
# rebuilt by hydrating from the Damia portal, which is the source of truth.

name: Your Name
day_rate: 500            # your day rate, in the currency below — used for revenue stats
currency: GBP
week_start: sunday       # Damia weeks run Sunday..Saturday

# Approval-request emails go to these people. Each contractor sets their own managers.
approver_emails:
  - manager1@example.com
  - manager2@example.com

# Your days-off ledger. Bank holidays are detected automatically (gov.uk) — list ONLY
# personal leave here. The bot subtracts these from the working week so it never claims a
# day you didn't work, and a week that is entirely leave/holiday produces NO email at all.
# Single day:   - date: 2026-08-25
# Range (incl): - {start: 2026-12-24, end: 2026-12-31}
# type is one of: annual | sick | unpaid   (default: annual)
leave: []
#   - date: 2026-08-25
#     type: annual
#     note: "long weekend"
#   - start: 2026-12-24
#     end: 2026-12-31
#     type: annual

# How to recognise a manager's approval reply. Conservative on purpose: a reply is only an
# approval if, once quoted history is stripped, it is SHORT (<= max_words) and contains an
# approval keyword. Anything longer (e.g. "did you not take Monday off?") is treated as a
# query and the week is flagged for manual handling — the bot never assumes.
approval:
  keywords: [approved, approve, approval, ok, okay, yes, agreed, confirmed, fine]
  max_words: 2

# Optional: contract periods (for revenue grouping / sanity checks). Rate may change per job.
# job_periods:
#   - name: "ACME"
#     start: 2026-04-13
#     end: 2027-04-12
#     day_rate: 500
"""

PLACEHOLDER_NAME = "Your Name"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    name: str
    day_rate: float
    currency: str = "GBP"
    week_start: str = "sunday"
    approver_emails: list[str] = field(default_factory=list)
    leave: list[dict] = field(default_factory=list)
    approval: dict = field(default_factory=dict)
    job_periods: list[dict] = field(default_factory=list)

    @property
    def is_placeholder(self) -> bool:
        """True while the file is still the unedited scaffold."""
        return self.name.strip() == PLACEHOLDER_NAME

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        try:
            return cls(
                name=str(data["name"]),
                day_rate=float(data["day_rate"]),
                currency=str(data.get("currency", "GBP")),
                week_start=str(data.get("week_start", "sunday")),
                approver_emails=list(data.get("approver_emails") or []),
                leave=list(data.get("leave") or []),
                approval=dict(data.get("approval") or {}),
                job_periods=list(data.get("job_periods") or []),
            )
        except KeyError as e:
            raise ConfigError(f"config.yml is missing required key: {e}") from e

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise ConfigError(f"No config at {path}. Run with the CLI to scaffold one.")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)


def scaffold_config(path: Path) -> bool:
    """Write the template if no config exists. Returns True if it was just created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True


def load_or_scaffold(path: Path) -> tuple[Config, bool]:
    """Load config, scaffolding the template first if absent. Returns (config, was_scaffolded)."""
    was_scaffolded = scaffold_config(path)
    return Config.load(path), was_scaffolded
