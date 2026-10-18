"""Pure policy: confidence, freshness, decay, sensitivity and identity helpers.

No I/O and no store access, so the in-memory and Postgres stores share exactly
the same rules.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Optional

from .models import AccessContext, Band, Fact, Sensitivity, SourceType, Volatility

BASE_CONFIDENCE: dict[SourceType, int] = {"stated": 90, "tool_output": 75, "inferred": 50}

#: Days a fact may go unconfirmed before it counts as stale, by volatility and source.
DEFAULT_TTL_DAYS: dict[Volatility, dict[SourceType, float]] = {
    "durable": {"stated": 180, "tool_output": 60, "inferred": 30},
    "volatile": {"stated": 21, "tool_output": 10, "inferred": 5},
}

#: Linear decay past the TTL: confidence points lost per day overdue, floored.
DECAY_PER_DAY = 2
CONFIDENCE_FLOOR = 10

#: Keyword heuristic that raises sensitivity automatically. Errs conservative;
#: callers can also mark any fact sensitive explicitly.
SENSITIVE_PREDICATE_HINTS = (
    "health", "medical", "medication", "diagnos", "religio", "salary", "income",
    "politic", "immigration", "sexual", "orientation", "ethnic", "address",
    "passport", "ssn", "password",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def slot_text_of(subject: str, predicate: str) -> str:
    """Text embedded for slot matching. Underscores and dashes read as spaces."""
    return normalize(re.sub(r"[_\-]+", " ", f"{subject} {predicate}"))


def slot_base_of(scope: str, subject: str, predicate: str) -> str:
    return json.dumps([normalize(scope), normalize(subject), normalize(predicate)], separators=(",", ":"))


def fingerprint_of(base: str, value: str, cardinality: str) -> str:
    return f"{base}#{normalize(value)}" if cardinality == "multi" else base


def marker(key: bytes, text: str) -> str:
    """One-way tombstone marker.

    A keyed hash of the *slot* (never the value). The key means a stolen database
    alone cannot be used to confirm guesses like ``home_address``.
    """
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def days_since(f: Fact, now: datetime) -> float:
    return max(0.0, (now - f.last_confirmed_at).total_seconds() / 86400.0)


def is_stale(f: Fact, now: datetime) -> bool:
    return days_since(f, now) > f.ttl_days


def decayed_confidence(f: Fact, now: datetime) -> int:
    over = days_since(f, now) - f.ttl_days
    if over <= 0:
        return f.confidence
    return int(min(f.confidence, max(CONFIDENCE_FLOOR, round(f.confidence - over * DECAY_PER_DAY))))


def band_of(confidence: int) -> Band:
    if confidence >= 75:
        return "high"
    if confidence >= 40:
        return "medium"
    return "low"


def sensitivity_for(predicate: str, requested: Optional[Sensitivity]) -> Sensitivity:
    if requested == "sensitive":
        return "sensitive"
    p = normalize(predicate)
    return "sensitive" if any(h in p for h in SENSITIVE_PREDICATE_HINTS) else "normal"


def stricter(a: Sensitivity, b: Sensitivity) -> Sensitivity:
    return "sensitive" if "sensitive" in (a, b) else "normal"


def visible_to(ctx: Optional[AccessContext], f: Fact) -> bool:
    if ctx is None or ctx.scopes is None:
        return True
    return normalize(f.scope) in {normalize(s) for s in ctx.scopes}


def sensitive_allowed(ctx: Optional[AccessContext]) -> bool:
    return True if ctx is None else ctx.include_sensitive
