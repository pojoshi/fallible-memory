"""MemoryService: every decision the memory makes lives here.

Stores only persist and filter. Retrieval never mutates state (staleness is derived
from ``now``); writes are serialized per scope by the store's transaction.
"""

import uuid
from datetime import datetime
from typing import Literal, Optional, Sequence

from . import policy as P
from .embedders import Embedder, HashingEmbedder
from .models import (
    AccessContext,
    Candidate,
    Cardinality,
    Fact,
    FactStatus,
    InspectorEntry,
    RetiredReason,
    Retrieval,
    Sensitivity,
    SlotMatch,
    SourceType,
    Volatility,
    WriteResult,
)
from .store import InMemoryStore, Store

DEFAULT_SCOPE = "user"
SOURCES = ("stated", "tool_output", "inferred")
VOLATILITIES = ("durable", "volatile")
CARDINALITIES = ("single", "multi")


def _new_id() -> str:
    return "f_" + uuid.uuid4().hex[:12]


class MemoryService:
    def __init__(
        self,
        store: Optional[Store] = None,
        embedder: Optional[Embedder] = None,
        *,
        tombstone_key: bytes = b"dev-only-change-me",
        match_threshold: float = 0.70,
        uncertain_threshold: float = 0.55,
        semantic_matching: bool = True,
    ) -> None:
        if not 0.0 <= uncertain_threshold <= match_threshold <= 1.0:
            raise ValueError("Need 0 <= uncertain_threshold <= match_threshold <= 1.")
        self.store: Store = store if store is not None else InMemoryStore()
        self.embedder: Embedder = embedder if embedder is not None else HashingEmbedder()
        self._key = tombstone_key
        self.match_threshold = match_threshold
        self.uncertain_threshold = uncertain_threshold
        #: False = exact-slot matching only (the baseline the evals compare against).
        self.semantic_matching = semantic_matching

    # ------------------------------------------------------------ slot matching

    def _embed(self, text: str) -> list[float]:
        return self.embedder.embed([text])[0]

    def _nearest_slot(self, scope: str, slot_text: str, embedding: Sequence[float], include_sensitive: bool = True) -> Optional[Candidate]:
        if not self.semantic_matching:
            return None
        hits = self.store.search(
            embedding, slot_text, scopes=[scope], statuses=("active",), cardinality="single",
            include_sensitive=include_sensitive, limit=1,
        )
        return hits[0] if hits else None

    # -------------------------------------------------------------------- write

    def write(
        self,
        *,
        subject: str,
        predicate: str,
        value: str,
        source_type: SourceType,
        scope: str = DEFAULT_SCOPE,
        volatility: Volatility = "durable",
        sensitivity: Optional[Sensitivity] = None,
        cardinality: Cardinality = "single",
        ttl_days: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> WriteResult:
        """Record a claim. See docs/ARCHITECTURE.md, "Write path"."""
        if source_type not in SOURCES:
            raise ValueError(f"source_type must be one of {SOURCES}")
        if volatility not in VOLATILITIES:
            raise ValueError(f"volatility must be one of {VOLATILITIES}")
        if cardinality not in CARDINALITIES:
            raise ValueError(f"cardinality must be one of {CARDINALITIES}")
        if ttl_days is not None and ttl_days <= 0:
            raise ValueError("ttl_days must be positive")

        scope, subject, predicate, value = (scope or "").strip(), (subject or "").strip(), (predicate or "").strip(), (value or "").strip()
        if not (scope and subject and predicate and value):
            return WriteResult("rejected", reason="Scope, subject, predicate and value are all required.")
        now = now or P.utcnow()

        sens = P.sensitivity_for(predicate, sensitivity)
        base = P.slot_base_of(scope, subject, predicate)
        slot_text = P.slot_text_of(subject, predicate)
        embedding: Optional[list[float]] = None
        possible_dup: Optional[str] = None
        match = SlotMatch("new")

        with self.store.transaction(scope):
            fp = P.fingerprint_of(base, value, cardinality)
            existing = self.store.find_active(fp)

            if existing is not None:
                match = SlotMatch("exact", 1.0, existing.id)
                slot_text, embedding = existing.slot_text, existing.embedding
            elif cardinality == "single":
                embedding = self._embed(slot_text)
                near = self._nearest_slot(scope, slot_text, embedding)
                if near is not None and near.score >= self.match_threshold:
                    canon = near.fact
                    base = fp = canon.slot_base
                    slot_text = canon.slot_text
                    embedding = canon.embedding or self._embed(canon.slot_text)
                    sens = P.stricter(sens, canon.sensitivity)
                    existing = canon
                    match = SlotMatch("semantic", near.score, canon.id)
                elif near is not None and near.score >= self.uncertain_threshold:
                    possible_dup = near.fact.id
                    match = SlotMatch("uncertain", near.score, near.fact.id)
            if embedding is None:
                embedding = self._embed(slot_text)

            def rejected(reason: str) -> WriteResult:
                return WriteResult("rejected", reason=reason, slot_match=match)

            if sens == "sensitive" and source_type == "inferred":
                return rejected(
                    f'"{predicate}" is a sensitive topic. Sensitive facts can only be stated by the user, never inferred.'
                )

            fp_marker, base_marker = P.marker(self._key, fp), P.marker(self._key, base)
            if self.store.has_tombstone(fp_marker) or self.store.has_tombstone(base_marker):
                if source_type != "stated":
                    return rejected(f'The user asked me to forget "{predicate}". Only the user can tell me this again.')
                self.store.remove_tombstone(fp_marker)

            incoming = P.BASE_CONFIDENCE[source_type]
            ttl = ttl_days if ttl_days is not None else P.DEFAULT_TTL_DAYS[volatility][source_type]

            def make(status: FactStatus, supersedes: Optional[str] = None, disputes: Optional[str] = None) -> Fact:
                fact = Fact(
                    id=_new_id(), scope=scope, subject=subject, predicate=predicate, value=value,
                    fingerprint=fp, slot_base=base, slot_text=slot_text, source_type=source_type,
                    volatility=volatility, sensitivity=sens, cardinality=cardinality, confidence=incoming,
                    created_at=now, last_confirmed_at=now, ttl_days=ttl, status=status,
                    supersedes=supersedes, disputes=disputes, possible_duplicate_of=possible_dup,
                    embedding=embedding,
                )
                self.store.insert(fact)
                return fact

            def retire(f: Fact, reason: RetiredReason) -> None:
                f.status = "contradicted"
                f.retired_at = now
                f.retired_reason = reason
                self.store.save(f)

            if existing is not None and P.normalize(existing.value) == P.normalize(value):
                existing.last_confirmed_at = max(existing.last_confirmed_at, now)
                existing_base = P.BASE_CONFIDENCE[existing.source_type]
                if incoming > existing_base:
                    existing.source_type = source_type
                    existing.confidence = max(existing.confidence, incoming)
                    existing.ttl_days = ttl
                elif incoming == existing_base:
                    existing.confidence = min(100, existing.confidence + 5)
                existing.sensitivity = P.stricter(existing.sensitivity, sens)
                self.store.save(existing)
                if source_type == "stated":
                    for d in self.store.find_disputes(fp):
                        retire(d, "rejected")
                return WriteResult("reconfirmed", fact=existing, slot_match=match)

            if existing is not None:
                existing_now = P.decayed_confidence(existing, now)
                disputes = self.store.find_disputes(fp)

                repeat = next((d for d in disputes if P.normalize(d.value) == P.normalize(value)), None)
                if repeat is not None and source_type != "stated":
                    repeat.last_confirmed_at = max(repeat.last_confirmed_at, now)
                    self.store.save(repeat)
                    return WriteResult("disputed", fact=repeat, against=existing, slot_match=match)

                if incoming >= existing_now:
                    retire(existing, "superseded")
                    for d in disputes:
                        retire(d, "rejected")
                    fact = make("active", supersedes=existing.id)
                    return WriteResult("superseded", fact=fact, contradicted=existing, slot_match=match)

                for d in disputes:
                    retire(d, "replaced")
                fact = make("disputed", disputes=existing.id)
                return WriteResult("disputed", fact=fact, against=existing, slot_match=match)

            fact = make("active")
            return WriteResult("created", fact=fact, slot_match=match)

    # ---------------------------------------------------------------- retrieval

    def _visible(self, ctx: Optional[AccessContext], f: Fact) -> bool:
        return P.visible_to(ctx, f)

    def _hidden_sensitive(self, ctx: Optional[AccessContext], f: Fact) -> bool:
        return f.sensitivity == "sensitive" and not P.sensitive_allowed(ctx)

    def _build(self, f: Fact, now: datetime, ctx: Optional[AccessContext], match: SlotMatch) -> Retrieval:
        stale = P.is_stale(f, now)
        conf = P.decayed_confidence(f, now)
        since = int(P.days_since(f, now))

        history: list[Fact] = []
        cursor = f.supersedes
        seen: set[str] = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            prev = self.store.get(cursor)
            if prev is None:
                break
            history.append(prev)
            cursor = prev.supersedes
        disputes = self.store.find_disputes(f.fingerprint)

        dups: list[Fact] = []
        linked = [self.store.get(f.possible_duplicate_of)] if f.possible_duplicate_of else []
        for other in [*linked, *self.store.facts_pointing_to(f.id)]:
            if (
                other is not None and other.status == "active" and other.id != f.id
                and self._visible(ctx, other) and not self._hidden_sensitive(ctx, other)
                and all(other.id != d.id for d in dups)
            ):
                dups.append(other)

        def day(dt: datetime) -> str:
            return dt.date().isoformat()

        notes: list[str] = []
        if stale:
            notes.append(
                f"Last confirmed {since} day(s) ago; this kind of fact is normally good for {f.ttl_days:g} day(s). "
                "I might be wrong, it could have changed since."
            )
        if disputes:
            d = disputes[0]
            notes.append(
                f'A weaker claim disagrees: "{d.value}" ({d.source_type}, {day(d.created_at)}). '
                f'I am keeping "{f.value}" until the user settles it.'
            )
        if history:
            prev = history[0]
            notes.append(
                f'This replaced an earlier claim ("{prev.value}", {prev.source_type}, {day(prev.created_at)}) on {day(f.created_at)}.'
            )
        if f.source_type == "inferred":
            notes.append("This was inferred, not stated directly, so its starting trust is lower.")
        if dups:
            names = ", ".join(f'"{d.predicate}"' for d in dups)
            notes.append(f"A similarly named slot exists ({names}); I did not merge them because I'm not sure they mean the same thing.")

        return Retrieval(
            found=True, fact=f, display_confidence=conf, band=P.band_of(conf), is_stale=stale,
            days_since_confirmed=since, history=history, disputes=disputes, possible_duplicates=dups,
            slot_match=match, note=" ".join(notes),
        )

    @staticmethod
    def _empty(note: str, withheld: bool = False) -> Retrieval:
        return Retrieval(found=False, withheld=withheld, note=note)

    def _resolve_slot(self, scope: str, subject: str, predicate: str) -> tuple[Optional[Fact], SlotMatch]:
        """Exact slot first, then the nearest single-valued slot above the match threshold.

        Scope and sensitivity are enforced by the caller once the fact is found, not here.
        """
        base = P.slot_base_of(scope, subject, predicate)
        f = self.store.find_active(base)
        if f is not None:
            return f, SlotMatch("exact", 1.0, f.id)
        text = P.slot_text_of(subject, predicate)
        near = self._nearest_slot(scope, text, self._embed(text))
        if near is not None and near.score >= self.match_threshold:
            return near.fact, SlotMatch("semantic", near.score, near.fact.id)
        return None, SlotMatch("new", near.score if near else None)

    def retrieve(
        self, *, subject: str, predicate: str, scope: str = DEFAULT_SCOPE,
        now: Optional[datetime] = None, ctx: Optional[AccessContext] = None,
    ) -> Retrieval:
        """Single-valued retrieval. Never returns a bare value; never mutates state."""
        now = now or P.utcnow()
        scope = scope.strip()
        label = f"{subject} {predicate}"
        f, match = self._resolve_slot(scope, subject.strip(), predicate.strip())
        if f is None or not self._visible(ctx, f):
            return self._empty(f'No memory of "{label}".')
        if self._hidden_sensitive(ctx, f):
            return self._empty(f'"{label}" is marked sensitive and is not available in this context.', withheld=True)
        return self._build(f, now, ctx, match)

    def retrieve_all(
        self, *, subject: str, predicate: str, scope: str = DEFAULT_SCOPE,
        now: Optional[datetime] = None, ctx: Optional[AccessContext] = None,
    ) -> list[Retrieval]:
        """Multi-valued retrieval (e.g. languages spoken). Exact slot only."""
        now = now or P.utcnow()
        base = P.slot_base_of(scope.strip(), subject.strip(), predicate.strip())
        out = []
        for f in self.store.facts_in_slot(base):
            if f.status == "active" and self._visible(ctx, f) and not self._hidden_sensitive(ctx, f):
                out.append(self._build(f, now, ctx, SlotMatch("exact", 1.0, f.id)))
        return out

    def search(
        self, query: str, *, ctx: Optional[AccessContext] = None, scope: Optional[str] = None,
        statuses: Sequence[str] = ("active",), cardinality: Optional[str] = None, limit: int = 5,
    ) -> list[Candidate]:
        """Free-text hybrid search over slot names, with metadata filters pushed down."""
        text = P.normalize(query.replace("_", " ").replace("-", " "))
        if not text:
            return []
        scopes: Optional[list[str]]
        if scope is not None:
            scopes = [scope]
            if ctx is not None and ctx.scopes is not None:
                allowed = {P.normalize(s) for s in ctx.scopes}
                scopes = [s for s in scopes if P.normalize(s) in allowed]
        else:
            scopes = list(ctx.scopes) if ctx is not None and ctx.scopes is not None else None
        if scopes == []:
            return []
        return self.store.search(
            self._embed(text), text, scopes=scopes, statuses=statuses, cardinality=cardinality,
            include_sensitive=P.sensitive_allowed(ctx), limit=limit,
        )

    def list(
        self, *, now: Optional[datetime] = None, ctx: Optional[AccessContext] = None, include_history: bool = False,
    ) -> list[InspectorEntry]:
        """Memory inspector: what do you remember, and how sure are you?"""
        now = now or P.utcnow()
        facts = self.store.list_facts(
            scopes=ctx.scopes if ctx else None,
            include_sensitive=P.sensitive_allowed(ctx),
            include_contradicted=include_history,
        )
        entries: list[InspectorEntry] = []
        for f in facts:
            active = f.status == "active"
            stale = active and P.is_stale(f, now)
            conf = P.decayed_confidence(f, now) if active else f.confidence
            status: Literal["active", "stale", "disputed", "contradicted"] = (
                ("stale" if stale else "active") if active else f.status
            )
            entries.append(InspectorEntry(f, status, conf, P.band_of(conf), stale))
        entries.sort(key=lambda e: (e.fact.created_at, e.fact.id), reverse=True)
        return entries

    def stats(self) -> dict[str, int]:
        facts = self.store.list_facts()
        counts = {"active": 0, "disputed": 0, "contradicted": 0}
        for f in facts:
            counts[f.status] += 1
        return {**counts, "total": len(facts), "tombstones": self.store.tombstone_count()}

    # --------------------------------------------------------------- forgetting

    def revoke(
        self, *, subject: str, predicate: str, scope: str = DEFAULT_SCOPE, value: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> int:
        """Erase a slot (or one value of a multi-valued slot) and leave a tombstone.

        If the named slot doesn't exist but a near-identical one does, that slot is
        erased too, and both names are tombstoned.
        """
        now = now or P.utcnow()
        scope, subject, predicate = scope.strip(), subject.strip(), predicate.strip()
        base = P.slot_base_of(scope, subject, predicate)
        bases = [base]
        with self.store.transaction(scope):
            if not self.store.facts_in_slot(base):
                text = P.slot_text_of(subject, predicate)
                near = self._nearest_slot(scope, text, self._embed(text))
                if near is not None and near.score >= self.match_threshold:
                    bases.append(near.fact.slot_base)
            vkey = P.normalize(value) if value is not None else None
            doomed: list[str] = []
            for b in bases:
                for f in self.store.facts_in_slot(b):
                    if vkey is None or f.cardinality == "single" or P.normalize(f.value) == vkey:
                        doomed.append(f.id)
            removed = self.store.delete_facts(doomed)
            for b in bases:
                key = f"{b}#{vkey}" if vkey is not None else b
                self.store.add_tombstone(P.marker(self._key, key), now)
            return removed

    def forget_scope(self, scope: str, now: Optional[datetime] = None) -> int:
        """Forget everything in a scope; tombstone every slot that was in it."""
        now = now or P.utcnow()
        with self.store.transaction(scope):
            doomed = self.store.list_facts(scopes=[scope])
            bases = {f.slot_base for f in doomed}
            removed = self.store.delete_facts(f.id for f in doomed)
            for b in bases:
                self.store.add_tombstone(P.marker(self._key, b), now)
            return removed
