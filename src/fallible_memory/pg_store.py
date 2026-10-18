"""PostgreSQL + pgvector store.

Hybrid search runs in one SQL statement: pgvector cosine distance fused with
pg_trgm similarity, with scope / status / sensitivity / cardinality filters in the
WHERE clause. Writers are serialized per scope with a transaction-level advisory
lock, and a partial unique index enforces "one active fact per slot".
"""

import threading
from contextlib import contextmanager
from datetime import datetime
from importlib import resources
from typing import Any, Iterable, Iterator, Optional, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .matching import W_TEXT, W_VECTOR
from .models import Candidate, Fact
from .policy import normalize

_COLS = (
    "id, scope, subject, predicate, value, fingerprint, slot_base, slot_text, source_type, volatility, "
    "sensitivity, cardinality, confidence, created_at, last_confirmed_at, ttl_days, status, supersedes, "
    "disputes, possible_duplicate_of, retired_at, retired_reason"
)


def _fact(row: dict[str, Any]) -> Fact:
    emb = row.get("embedding")
    return Fact(
        id=row["id"], scope=row["scope"], subject=row["subject"], predicate=row["predicate"], value=row["value"],
        fingerprint=row["fingerprint"], slot_base=row["slot_base"], slot_text=row["slot_text"],
        source_type=row["source_type"], volatility=row["volatility"], sensitivity=row["sensitivity"],
        cardinality=row["cardinality"], confidence=row["confidence"], created_at=row["created_at"],
        last_confirmed_at=row["last_confirmed_at"], ttl_days=row["ttl_days"], status=row["status"],
        supersedes=row["supersedes"], disputes=row["disputes"], possible_duplicate_of=row["possible_duplicate_of"],
        retired_at=row["retired_at"], retired_reason=row["retired_reason"],
        embedding=[float(x) for x in emb[1:-1].split(",")] if emb else None,
    )


def _vec(v: Sequence[float]) -> str:
    # Sent as text and cast with ::vector. (A numpy parameter would make psycopg
    # request binary results, which returns text columns as bytes.)
    return "[" + ",".join(f"{float(x):.8g}" for x in v) + "]"


class PostgresStore:
    def __init__(self, dsn: str, *, dim: int, min_size: int = 1, max_size: int = 10) -> None:
        self.dim = dim
        self._tl = threading.local()
        # Bootstrap with a plain connection: the vector type must exist before the
        # pool registers its adapter.
        ddl = resources.files("fallible_memory").joinpath("schema.sql").read_text().replace("__DIM__", str(int(dim)))
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(ddl)  # type: ignore[arg-type]
            row = c.execute(
                "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'facts'::regclass AND attname = 'embedding'"
            ).fetchone()
        if row is not None and row[0] != dim:
            raise RuntimeError(
                f"facts.embedding is vector({row[0]}) but the embedder produces {dim}-dimensional vectors. "
                "Use a fresh database/schema or re-embed; refusing to continue."
            )
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=True,
        )

    def close(self) -> None:
        self._pool.close()

    def reset(self) -> None:
        """Delete all data (tests and demos)."""
        with self._conn() as c:
            c.execute("TRUNCATE facts, tombstones")

    # ------------------------------------------------------------ connections

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        current = getattr(self._tl, "conn", None)
        if current is not None:
            yield current
        else:
            with self._pool.connection() as c:
                yield c

    @contextmanager
    def transaction(self, scope: str) -> Iterator[None]:
        if getattr(self._tl, "conn", None) is not None:  # re-entrant
            yield
            return
        with self._pool.connection() as c:
            with c.transaction():
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (normalize(scope),))
                self._tl.conn = c
                try:
                    yield
                finally:
                    self._tl.conn = None

    # ------------------------------------------------------------------ reads

    def _one(self, sql: str, params: tuple, embedding: bool = False) -> Optional[Fact]:
        cols = _COLS + (", embedding::text AS embedding" if embedding else "")
        with self._conn() as c:
            row = c.execute(sql.replace("{cols}", cols), params).fetchone()
        return _fact(row) if row else None

    def _many(self, sql: str, params: tuple) -> list[Fact]:
        with self._conn() as c:
            rows = c.execute(sql.replace("{cols}", _COLS), params).fetchall()
        return [_fact(r) for r in rows]

    def get(self, fact_id: str) -> Optional[Fact]:
        return self._one("SELECT {cols} FROM facts WHERE id = %s", (fact_id,), embedding=True)

    def find_active(self, fingerprint: str) -> Optional[Fact]:
        return self._one(
            "SELECT {cols} FROM facts WHERE fingerprint = %s AND status = 'active'", (fingerprint,), embedding=True
        )

    def find_disputes(self, fingerprint: str) -> list[Fact]:
        return self._many(
            "SELECT {cols} FROM facts WHERE fingerprint = %s AND status = 'disputed' ORDER BY created_at, id",
            (fingerprint,),
        )

    def facts_in_slot(self, slot_base: str) -> list[Fact]:
        return self._many("SELECT {cols} FROM facts WHERE slot_base = %s ORDER BY created_at, id", (slot_base,))

    def facts_pointing_to(self, fact_id: str) -> list[Fact]:
        return self._many("SELECT {cols} FROM facts WHERE possible_duplicate_of = %s ORDER BY id", (fact_id,))

    def list_facts(
        self, *, scopes: Optional[Sequence[str]] = None, include_sensitive: bool = True,
        include_contradicted: bool = True,
    ) -> list[Fact]:
        where, params = ["TRUE"], []
        if scopes is not None:
            where.append("scope_key = ANY(%s)")
            params.append([normalize(s) for s in scopes])
        if not include_sensitive:
            where.append("sensitivity <> 'sensitive'")
        if not include_contradicted:
            where.append("status <> 'contradicted'")
        return self._many(f"SELECT {{cols}} FROM facts WHERE {' AND '.join(where)} ORDER BY created_at, id", tuple(params))

    def search(
        self, embedding: Sequence[float], text: str, *, scopes: Optional[Sequence[str]] = None,
        statuses: Sequence[str] = ("active",), cardinality: Optional[str] = None,
        include_sensitive: bool = True, limit: int = 5,
    ) -> list[Candidate]:
        vec = _vec(embedding)
        where = ["status = ANY(%s)", "embedding IS NOT NULL"]
        where_params: list[Any] = [list(statuses)]
        if scopes is not None:
            where.append("scope_key = ANY(%s)")
            where_params.append([normalize(s) for s in scopes])
        if cardinality is not None:
            where.append("cardinality = %s")
            where_params.append(cardinality)
        if not include_sensitive:
            where.append("sensitivity <> 'sensitive'")
        sql = f"""
            SELECT {_COLS},
                   GREATEST(0.0, LEAST(1.0, 1 - (embedding <=> %s::vector)))::float8 AS vec_sim,
                   similarity(slot_text, %s)::float8 AS txt_sim
              FROM facts
             WHERE {' AND '.join(where)}
             ORDER BY ({W_VECTOR} * GREATEST(0.0, LEAST(1.0, 1 - (embedding <=> %s::vector))) + {W_TEXT} * similarity(slot_text, %s)) DESC, id
             LIMIT %s
        """
        params = (vec, text, *where_params, vec, text, limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()  # type: ignore[arg-type]
        out = []
        for r in rows:
            v, t = float(r["vec_sim"]), float(r["txt_sim"])
            out.append(Candidate(_fact(r), W_VECTOR * v + W_TEXT * t, v, t))
        return out

    # ----------------------------------------------------------------- writes

    @staticmethod
    def _values(f: Fact) -> dict[str, Any]:
        d = {k: getattr(f, k) for k in _COLS.replace("\n", " ").split(", ")}
        d["scope_key"] = normalize(f.scope)
        d["embedding"] = _vec(f.embedding) if f.embedding is not None else None
        return d

    def insert(self, fact: Fact) -> None:
        d = self._values(fact)
        cols = list(d)
        sql = f"INSERT INTO facts ({', '.join(cols)}) VALUES ({', '.join('%(' + k + ')s' + ('::vector' if k == 'embedding' else '') for k in cols)})"
        with self._conn() as c:
            c.execute(sql, d)  # type: ignore[arg-type]

    def save(self, fact: Fact) -> None:
        d = self._values(fact)
        d.pop("embedding")  # slot embedding never changes after insert
        cols = [k for k in d if k != "id"]
        sql = f"UPDATE facts SET {', '.join(k + ' = %(' + k + ')s' for k in cols)} WHERE id = %(id)s"
        with self._conn() as c:
            c.execute(sql, d)  # type: ignore[arg-type]

    def delete_facts(self, fact_ids: Iterable[str]) -> int:
        ids = list(set(fact_ids))
        if not ids:
            return 0
        with self._conn() as c:
            n = c.execute("DELETE FROM facts WHERE id = ANY(%s)", (ids,)).rowcount
            c.execute("UPDATE facts SET supersedes = NULL WHERE supersedes = ANY(%s)", (ids,))
            c.execute("UPDATE facts SET disputes = NULL WHERE disputes = ANY(%s)", (ids,))
            c.execute("UPDATE facts SET possible_duplicate_of = NULL WHERE possible_duplicate_of = ANY(%s)", (ids,))
        return n

    def has_tombstone(self, marker: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM tombstones WHERE marker = %s", (marker,)).fetchone() is not None

    def add_tombstone(self, marker: str, at: datetime) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO tombstones (marker, revoked_at) VALUES (%s, %s) ON CONFLICT DO NOTHING", (marker, at)
            )

    def remove_tombstone(self, marker: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM tombstones WHERE marker = %s", (marker,))

    def tombstone_count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT count(*) AS n FROM tombstones").fetchone()["n"])  # type: ignore[index]
