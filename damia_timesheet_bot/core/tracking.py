"""Tracking-id grammar: `TS:YYYYMMDD-<8 lowercase hex>`.

The date is the SEND/draft date (not the week start), matching the user's existing prototype
mail so the bot's threads are continuous with already-sent approvals. The 8-hex suffix is the
per-thread correlator. The week range in the subject — not this id — is the join key to
portal rows. See the project memory on the subject grammar.
"""
from __future__ import annotations

import re
import secrets
from datetime import date

TRACKING_RE = re.compile(r"TS:(\d{8})-([0-9a-f]{8})")


def new_tracking_id(send_date: date) -> str:
    return f"TS:{send_date.strftime('%Y%m%d')}-{secrets.token_hex(4)}"


def parse_tracking_id(text: str) -> str | None:
    """Return the full `TS:...` token found in a subject/body, or None."""
    m = TRACKING_RE.search(text)
    return m.group(0) if m else None
