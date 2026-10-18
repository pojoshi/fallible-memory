"""Data model for fallible-memory.

A memory is a *claim*, not a value: it carries its source, confidence, freshness
window, scope and sensitivity. Staleness is derived at read time and never stored.
Revoked facts are erased, so there is no "revoked" status.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional, Sequence

SourceType = Literal["stated", "tool_output", "inferred"]
Volatility = Literal["durable", "volatile"]
Sensitivity = Literal["normal", "sensitive"]
Cardinality = Literal["single", "multi"]
Band = Literal["high", "medium", "low"]
FactStatus = Literal["active", "disputed", "contradicted"]
RetiredReason = Literal["superseded", "rejected", "replaced"]
WriteOutcome = Literal["created", "reconfirmed", "superseded", "disputed", "rejected"]
SlotMatchKind = Literal["exact", "semantic", "uncertain", "new"]
AnswerMode = Literal["assert", "hedge", "ask", "unknown", "withheld"]


@dataclass
class Fact:
    id: str
    scope: str
    subject: str
    predicate: str
    value: str
    #: Slot identity. Equals ``slot_base`` for single-valued facts; for multi-valued
    #: facts the normalized value is appended.
    fingerprint: str
    #: scope + subject + predicate (canonical slot, independent of value).
    slot_base: str
    #: Normalized "subject predicate" text that is embedded for slot matching.
    slot_text: str
    source_type: SourceType
    volatility: Volatility
    sensitivity: Sensitivity
    cardinality: Cardinality
    #: Base trust 0-100 at write / last confirmation. Decay is applied on read.
    confidence: int
    created_at: datetime
    last_confirmed_at: datetime
    #: Days this fact may go unconfirmed before it counts as stale.
    ttl_days: float
    status: FactStatus = "active"
    #: The fact this one replaced (audit link).
    supersedes: Optional[str] = None
    #: For a disputed claim: the active fact it disagrees with.
    disputes: Optional[str] = None
    #: A different slot whose name looked similar but not similar enough to merge.
    possible_duplicate_of: Optional[str] = None
    retired_at: Optional[datetime] = None
    retired_reason: Optional[RetiredReason] = None
    embedding: Optional[list[float]] = field(default=None, repr=False)


@dataclass(frozen=True)
class AccessContext:
    """Who is asking. ``scopes=None`` means every scope."""

    scopes: Optional[Sequence[str]] = None
    include_sensitive: bool = True


@dataclass
class SlotMatch:
    kind: SlotMatchKind
    similarity: Optional[float] = None
    matched_fact_id: Optional[str] = None


@dataclass
class WriteResult:
    outcome: WriteOutcome
    fact: Optional[Fact] = None
    #: The fact retired by this write (``superseded`` outcome).
    contradicted: Optional[Fact] = None
    #: The active fact a disputed claim lost to (``disputed`` outcome).
    against: Optional[Fact] = None
    reason: Optional[str] = None
    slot_match: SlotMatch = field(default_factory=lambda: SlotMatch("new"))


@dataclass
class Retrieval:
    found: bool
    #: A fact exists but is hidden from this context because it is sensitive.
    withheld: bool = False
    fact: Optional[Fact] = None
    display_confidence: int = 0
    band: Optional[Band] = None
    is_stale: bool = False
    days_since_confirmed: int = 0
    #: What this fact replaced, most recent first.
    history: list[Fact] = field(default_factory=list)
    #: Competing lower-trust claims that have not been resolved.
    disputes: list[Fact] = field(default_factory=list)
    #: Similar-looking slots that were not merged.
    possible_duplicates: list[Fact] = field(default_factory=list)
    slot_match: SlotMatch = field(default_factory=lambda: SlotMatch("new"))
    note: str = ""


@dataclass
class InspectorEntry:
    fact: Fact
    effective_status: Literal["active", "stale", "disputed", "contradicted"]
    display_confidence: int
    band: Band
    is_stale: bool


@dataclass
class Candidate:
    """A search hit: hybrid score = weighted vector similarity + trigram similarity."""

    fact: Fact
    score: float
    vector_similarity: float
    text_similarity: float


@dataclass
class AgentOption:
    kind: Literal["confirm", "choose"]
    value: str
    label: str


@dataclass
class AgentAnswer:
    mode: AnswerMode
    text: str
    evidence: Optional[str] = None
    value: Optional[str] = None
    confidence: Optional[int] = None
    band: Optional[Band] = None
    fact_id: Optional[str] = None
    options: list[AgentOption] = field(default_factory=list)
