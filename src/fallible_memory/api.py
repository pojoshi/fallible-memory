"""FastAPI service.

There is no authentication here: the access context (which scopes a requester may
read, and whether sensitive facts are allowed) comes from the request body. In a
real deployment, derive it from the caller's identity at a gateway and never trust
the client to set it. Sensitive facts are excluded unless a request opts in.
"""

import dataclasses
import os
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import agent
from .embedders import make_embedder
from .models import AccessContext, Fact
from .service import MemoryService
from .store import InMemoryStore

Source = Literal["stated", "tool_output", "inferred"]


class AccessIn(BaseModel):
    scopes: Optional[list[str]] = Field(None, description="Scopes the requester may read. Omit for all.")
    include_sensitive: bool = Field(False, description="Sensitive facts are hidden unless this is true.")

    def to_ctx(self) -> AccessContext:
        return AccessContext(scopes=self.scopes, include_sensitive=self.include_sensitive)


class WriteIn(BaseModel):
    subject: str
    predicate: str
    value: str
    source_type: Source
    scope: str = "user"
    volatility: Literal["durable", "volatile"] = "durable"
    sensitivity: Optional[Literal["normal", "sensitive"]] = None
    cardinality: Literal["single", "multi"] = "single"
    ttl_days: Optional[float] = Field(None, gt=0)
    now: Optional[datetime] = Field(None, description="Simulated clock (for demos and tests).")


class RetrieveIn(BaseModel):
    subject: str
    predicate: str
    scope: str = "user"
    now: Optional[datetime] = None
    ctx: AccessIn = AccessIn()


class AskIn(BaseModel):
    subject: Optional[str] = None
    predicate: Optional[str] = None
    query: Optional[str] = Field(None, description="Free-text alternative to subject + predicate.")
    scope: Optional[str] = None
    now: Optional[datetime] = None
    ctx: AccessIn = AccessIn()


class SearchIn(BaseModel):
    query: str
    scope: Optional[str] = None
    statuses: list[Literal["active", "disputed", "contradicted"]] = ["active"]
    limit: int = Field(5, ge=1, le=50)
    ctx: AccessIn = AccessIn()


class ForgetIn(BaseModel):
    subject: str
    predicate: str
    scope: str = "user"
    value: Optional[str] = None
    now: Optional[datetime] = None


class ForgetScopeIn(BaseModel):
    scope: str
    now: Optional[datetime] = None


def fact_dict(f: Fact) -> dict[str, Any]:
    d = dataclasses.asdict(f)
    d.pop("embedding", None)
    return d


def _retrieval_dict(r) -> dict[str, Any]:
    return {
        "found": r.found, "withheld": r.withheld, "fact": fact_dict(r.fact) if r.fact else None,
        "display_confidence": r.display_confidence, "band": r.band, "is_stale": r.is_stale,
        "days_since_confirmed": r.days_since_confirmed, "history": [fact_dict(f) for f in r.history],
        "disputes": [fact_dict(f) for f in r.disputes],
        "possible_duplicates": [fact_dict(f) for f in r.possible_duplicates],
        "slot_match": dataclasses.asdict(r.slot_match), "note": r.note,
    }


def create_app(service: Optional[MemoryService] = None) -> FastAPI:
    svc = service or MemoryService(InMemoryStore())
    app = FastAPI(
        title="fallible-memory",
        version="0.1.0",
        description="A memory layer that tracks source, confidence, freshness and scope, and says when it might be wrong.",
    )
    app.state.service = svc

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "embedder": svc.embedder.name, "store": type(svc.store).__name__}

    @app.post("/facts")
    def write_fact(body: WriteIn) -> dict[str, Any]:
        r = svc.write(**body.model_dump())
        return {
            "outcome": r.outcome, "reason": r.reason,
            "fact": fact_dict(r.fact) if r.fact else None,
            "contradicted": fact_dict(r.contradicted) if r.contradicted else None,
            "against": fact_dict(r.against) if r.against else None,
            "slot_match": dataclasses.asdict(r.slot_match),
        }

    @app.get("/facts")
    def inspect(
        scopes: Optional[list[str]] = Query(None),
        include_sensitive: bool = False,
        include_history: bool = False,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Memory inspector: what is remembered, how sure, and why."""
        entries = svc.list(now=now, ctx=AccessContext(scopes=scopes, include_sensitive=include_sensitive), include_history=include_history)
        return [
            {"fact": fact_dict(e.fact), "effective_status": e.effective_status,
             "display_confidence": e.display_confidence, "band": e.band, "is_stale": e.is_stale}
            for e in entries
        ]

    @app.post("/retrieve")
    def retrieve(body: RetrieveIn) -> dict[str, Any]:
        return _retrieval_dict(svc.retrieve(subject=body.subject, predicate=body.predicate, scope=body.scope, now=body.now, ctx=body.ctx.to_ctx()))

    @app.post("/ask")
    def ask(body: AskIn) -> dict[str, Any]:
        ctx = body.ctx.to_ctx()
        if body.query:
            a = agent.ask_text(svc, body.query, scope=body.scope, now=body.now, ctx=ctx)
        elif body.subject and body.predicate:
            a = agent.ask(svc, subject=body.subject, predicate=body.predicate, scope=body.scope or "user", now=body.now, ctx=ctx)
        else:
            raise HTTPException(422, "Provide either `query`, or both `subject` and `predicate`.")
        return dataclasses.asdict(a)

    @app.post("/search")
    def search(body: SearchIn) -> list[dict[str, Any]]:
        hits = svc.search(body.query, ctx=body.ctx.to_ctx(), scope=body.scope, statuses=tuple(body.statuses), limit=body.limit)
        return [
            {"fact": fact_dict(h.fact), "score": h.score, "vector_similarity": h.vector_similarity, "text_similarity": h.text_similarity}
            for h in hits
        ]

    @app.post("/forget")
    def forget(body: ForgetIn) -> dict[str, int]:
        return {"removed": svc.revoke(subject=body.subject, predicate=body.predicate, scope=body.scope, value=body.value, now=body.now)}

    @app.post("/forget/scope")
    def forget_scope(body: ForgetScopeIn) -> dict[str, int]:
        return {"removed": svc.forget_scope(body.scope, now=body.now)}

    @app.get("/stats")
    def stats() -> dict[str, int]:
        return svc.stats()

    return app


def create_app_from_env() -> FastAPI:
    """``uvicorn fallible_memory.api:create_app_from_env --factory``

    DATABASE_URL        Postgres DSN. Unset: in-memory store (data is lost on restart).
    EMBEDDER            ``hashing`` (default), ``hashing:512`` or ``st:<model>``.
    TOMBSTONE_KEY       Secret for tombstone markers. Required with DATABASE_URL.
    MATCH_THRESHOLD / UNCERTAIN_THRESHOLD   Slot matching thresholds.
    """
    embedder = make_embedder(os.environ.get("EMBEDDER"))
    dsn = os.environ.get("DATABASE_URL")
    key = os.environ.get("TOMBSTONE_KEY")
    if dsn and not key:
        raise RuntimeError("TOMBSTONE_KEY must be set when DATABASE_URL is set (a random key would orphan tombstones on restart).")
    kwargs: dict[str, Any] = {"tombstone_key": (key or os.urandom(32).hex()).encode()}
    if os.environ.get("MATCH_THRESHOLD"):
        kwargs["match_threshold"] = float(os.environ["MATCH_THRESHOLD"])
    if os.environ.get("UNCERTAIN_THRESHOLD"):
        kwargs["uncertain_threshold"] = float(os.environ["UNCERTAIN_THRESHOLD"])
    if dsn:
        from .pg_store import PostgresStore

        store: Any = PostgresStore(dsn, dim=embedder.dim)
    else:
        store = InMemoryStore()
    return create_app(MemoryService(store, embedder, **kwargs))
