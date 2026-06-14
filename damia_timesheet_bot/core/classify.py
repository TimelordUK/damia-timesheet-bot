"""Reply classification — is a manager's reply a straightforward approval?

Deliberately conservative, per the user's own heuristic: an approval reply is almost always
one or two words ("Approved", "Approved thanks"). So a reply only counts as APPROVED when,
after stripping quoted history, it is SHORT (<= max_words) AND contains an approval keyword.
Anything longer — e.g. "did you not take Monday off?" — is a QUERY and the week goes to
manual handling. The thresholds live in config (`approval:`), never hard-coded policy.

Pure module: feed it text, get a verdict. No Outlook, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_DEFAULT_KEYWORDS = ("approved", "approve", "approval", "ok", "okay",
                     "yes", "agreed", "confirmed", "fine")
_DEFAULT_MAX_WORDS = 2

# Lines at/after which the quoted original begins, in typical Outlook/Gmail replies.
_QUOTE_MARKERS = (
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}", re.I),
    re.compile(r"^\s*from:\s", re.I),
    re.compile(r"^\s*on .+ wrote:\s*$", re.I),
    re.compile(r"^\s*sent from my ", re.I),
    re.compile(r"^\s*_{5,}\s*$"),               # the long underscore divider Outlook inserts
    re.compile(r"^\s*>", ),                       # quoted line
)


@dataclass(frozen=True)
class ApprovalConfig:
    keywords: tuple[str, ...] = _DEFAULT_KEYWORDS
    max_words: int = _DEFAULT_MAX_WORDS

    @classmethod
    def from_dict(cls, data: dict | None) -> "ApprovalConfig":
        data = data or {}
        kws = data.get("keywords")
        keywords = tuple(str(k).strip().lower() for k in kws) if kws else _DEFAULT_KEYWORDS
        mw = data.get("max_words", _DEFAULT_MAX_WORDS)
        try:
            max_words = max(1, int(mw))
        except (TypeError, ValueError):
            max_words = _DEFAULT_MAX_WORDS
        return cls(keywords=keywords, max_words=max_words)


class ReplyKind(str, Enum):
    APPROVED = "approved"      # short + contains an approval keyword
    QUERY = "query"            # anything non-trivial — treat as manual
    EMPTY = "empty"            # nothing meaningful above the quote — manual


@dataclass(frozen=True)
class ReplyVerdict:
    kind: ReplyKind
    cleaned: str
    word_count: int
    reason: str

    @property
    def is_approval(self) -> bool:
        return self.kind is ReplyKind.APPROVED


def extract_new_text(body: str) -> str:
    """Return only the NEW reply text — everything above the quoted original message."""
    lines: list[str] = []
    for line in (body or "").splitlines():
        if any(m.search(line) for m in _QUOTE_MARKERS):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _words(text: str) -> list[str]:
    # Keep letters only; drop punctuation/emoji/signatures-ish noise.
    return [w for w in re.findall(r"[a-zA-Z']+", text.lower()) if w]


def classify_reply(new_text: str, cfg: ApprovalConfig | None = None) -> ReplyVerdict:
    """Classify the already-extracted NEW reply text. Use extract_new_text() first on a raw
    body. Conservative: only short, keyword-bearing replies are approvals."""
    cfg = cfg or ApprovalConfig()
    cleaned = (new_text or "").strip()
    words = _words(cleaned)

    if not words:
        return ReplyVerdict(ReplyKind.EMPTY, cleaned, 0,
                            "no text above the quoted message — can't confirm approval.")
    if len(words) > cfg.max_words:
        return ReplyVerdict(ReplyKind.QUERY, cleaned, len(words),
                            f"reply has {len(words)} words (> max_words={cfg.max_words}) — "
                            f"not a straightforward approval; treat as a query.")
    if any(w in cfg.keywords for w in words):
        return ReplyVerdict(ReplyKind.APPROVED, cleaned, len(words),
                            f"short reply ({len(words)} word(s)) containing an approval keyword.")
    return ReplyVerdict(ReplyKind.QUERY, cleaned, len(words),
                        f"short reply but no approval keyword ({cleaned!r}) — treat as a query.")
