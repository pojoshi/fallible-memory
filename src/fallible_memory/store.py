"""Storage interface and the in-memory implementation.

The store is dumb on purpose: it holds facts and tombstones and answers
filtered queries. Every decision (supersede vs dispute, matching thresholds,
sensitivity, tombstones) lives in ``MemoryService``, so the in-memory and
Postgres stores behave identically.
"""

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import ContextManager, Iterable, Iterator, Optional, Protocol, Sequence

from .matching import cosine, fused_score, trigram_similarity
from .models import Candidate, Fact
from .policy import normalize


class Store(Protocol):
    def transaction(self, scope: str) -> ContextManager[None]:
        """Serialize writers touching ``scope`` (and commit atomically if supported)."""
        ...

    def get(self, fact_id: str) -> Optional[Fact]: ...
    def find_active(self, fingerprint: str) -> Optional[Fact]: ...
    def find_disputes(self, fingerprint: str) -> list[Fact]: ...
    def facts_in_slot(self, slot_base: str) -> list[Fact]: ...
    def facts_pointing_to(self, fact_id: str) -> list[Fact]:
        """Facts whose ``possible_duplicate_of`` is ``fact_id``."""
        ...

    def insert(self, fact: Fact) -> None: ...
    def save(self, fact: Fact) -> None: ...

    def list_facts(
        self,
        *,
        scopes: Optional[Sequence[str]] = None,
        include_sensitive: bool = True,
        include_contradicted: bool = True,
    ) -> list[Fact]: ...

    def search(
        self,
        embedding: Sequence[float],
        text: str,
        *,
        scopes: Optional[Sequence[str]] = None,
        statuses: Sequence[str] = ("active",),
        cardinality: Optional[str] = None,
        include_sensitive: bool = True,
        limit: int = 5,
    ) -> list[Candidate]:
        """Hybrid nearest-neighbour over ``slot_text`` with metadata filters.

        One candidate per fact, best score first.
        """
        ...

    def delete_facts(self, fact_ids: Iterable[str]) -> int:
        """Erase facts and null any audit links that pointed at them."""
        ...

    def has_tombstone(self, marker: str) -> bool: ...
    def add_tombstone(self, marker: str, at: datetime) -> None: ...
    def remove_tombstone(self, marker: str) -> None: ...
    def tombstone_count(self) -> int: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self._tombstones: dict[str, datetime] = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self, _scope: str) -> Iterator[None]:
        with self._lock:
            yield

    # ------------------------------------------------------------------ reads

    def get(self, fact_id: str) -> Optional[Fact]:
        return self._facts.get(fact_id)

    def find_active(self, fingerprint: str) -> Optional[Fact]:
        for f in self._facts.values():
            if f.fingerprint == fingerprint and f.status == "active":
                return f
        return None

    def find_disputes(self, fingerprint: str) -> list[Fact]:
        return [f for f in self._facts.values() if f.fingerprint == fingerprint and f.status == "disputed"]

    def facts_in_slot(self, slot_base: str) -> list[Fact]:
        return [f for f in self._facts.values() if f.slot_base == slot_base]

    def facts_pointing_to(self, fact_id: str) -> list[Fact]:
        return [f for f in self._facts.values() if f.possible_duplicate_of == fact_id]

    def list_facts(
        self,
        *,
        scopes: Optional[Sequence[str]] = None,
        include_sensitive: bool = True,
        include_contradicted: bool = True,
    ) -> list[Fact]:
        wanted = {normalize(s) for s in scopes} if scopes is not None else None
        out = []
        for f in self._facts.values():
            if wanted is not None and normalize(f.scope) not in wanted:
                continue
            if f.sensitivity == "sensitive" and not include_sensitive:
                continue
            if f.status == "contradicted" and not include_contradicted:
                continue
            out.append(f)
        return out

    def search(
        self,
        embedding: Sequence[float],
        text: str,
        *,
        scopes: Optional[Sequence[str]] = None,
        statuses: Sequence[str] = ("active",),
        cardinality: Optional[str] = None,
        include_sensitive: bool = True,
        limit: int = 5,
    ) -> list[Candidate]:
        wanted = {normalize(s) for s in scopes} if scopes is not None else None
        hits: list[Candidate] = []
        for f in self._facts.values():
            if f.status not in statuses:
                continue
            if wanted is not None and normalize(f.scope) not in wanted:
                continue
            if cardinality is not None and f.cardinality != cardinality:
                continue
            if f.sensitivity == "sensitive" and not include_sensitive:
                continue
            if f.embedding is None:
                continue
            v = cosine(embedding, f.embedding)
            t = trigram_similarity(text, f.slot_text)
            hits.append(Candidate(f, fused_score(v, t), v, t))
        hits.sort(key=lambda c: (-c.score, c.fact.id))
        return hits[:limit]

    # ----------------------------------------------------------------- writes

    def insert(self, fact: Fact) -> None:
        self._facts[fact.id] = fact

    def save(self, fact: Fact) -> None:
        self._facts[fact.id] = fact

    def delete_facts(self, fact_ids: Iterable[str]) -> int:
        ids = set(fact_ids)
        removed = 0
        for i in ids:
            if self._facts.pop(i, None) is not None:
                removed += 1
        for f in self._facts.values():
            if f.supersedes in ids:
                f.supersedes = None
            if f.disputes in ids:
                f.disputes = None
            if f.possible_duplicate_of in ids:
                f.possible_duplicate_of = None
        return removed

    def has_tombstone(self, marker: str) -> bool:
        return marker in self._tombstones

    def add_tombstone(self, marker: str, at: datetime) -> None:
        self._tombstones.setdefault(marker, at)

    def remove_tombstone(self, marker: str) -> None:
        self._tombstones.pop(marker, None)

    def tombstone_count(self) -> int:
        return len(self._tombstones)
