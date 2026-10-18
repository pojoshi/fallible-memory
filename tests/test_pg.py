"""Postgres-specific behaviour: parity with the in-memory store, invariants, concurrency."""
import threading
from datetime import datetime, timezone

import psycopg
import pytest

from fallible_memory import HashingEmbedder, InMemoryStore, MemoryService
from fallible_memory import policy as P
from fallible_memory.matching import trigram_similarity

from .conftest import DSN

pytestmark = pytest.mark.skipif(not DSN, reason="FALLIBLE_TEST_DATABASE_URL not set")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

PAIRS = [
    ("home city", "home_city"), ("employer", "employers"), ("works at", "employer"),
    ("favourite language", "favorite language"), ("user email", "email address"), ("a", "b"),
]


def test_trigram_parity_with_pg_trgm(pg_store):
    # pg_store isn't used directly; it's the fixture that boots the vector/pg_trgm extensions.
    with psycopg.connect(DSN) as c:
        for a, b in PAIRS:
            (sql_val,) = c.execute("SELECT similarity(%s, %s)", (a, b)).fetchone()
            assert trigram_similarity(a, b) == pytest.approx(sql_val, abs=1e-6), (a, b)


def test_hybrid_scores_match_between_stores(pg_store):
    pg_store.reset()
    mem = MemoryService(InMemoryStore())
    pg = MemoryService(pg_store)
    for s in (mem, pg):
        for pred in ("employer", "home_city", "favourite_language", "phone number"):
            s.write(subject="user", predicate=pred, value="x", source_type="stated", now=NOW)
    for q in ("user employer", "where do I live city", "language i like", "mobile phone"):
        a, b = mem.search(q, limit=4), pg.search(q, limit=4)
        # Ties (e.g. several 0.0 scores) are ordered by random id, so only rank real hits by name.
        assert [c.fact.predicate for c in a if c.score > 0] == [c.fact.predicate for c in b if c.score > 0]
        for x, y in zip(a, b, strict=True):
            assert x.score == pytest.approx(y.score, abs=1e-4)


def test_metadata_filters_are_applied_in_sql(pg_store):
    pg_store.reset()
    s = MemoryService(pg_store)
    s.write(subject="user", predicate="home_address", value="1 Rd", source_type="stated", now=NOW)
    s.write(subject="p", predicate="home_address", value="2 Rd", source_type="stated", scope="proj", now=NOW)
    from fallible_memory import AccessContext
    got = s.search("home address", ctx=AccessContext(scopes=["proj"], include_sensitive=True))
    assert [c.fact.scope for c in got] == ["proj"]
    assert s.search("home address", ctx=AccessContext(include_sensitive=False)) == []


def test_one_active_fact_per_slot_is_enforced_by_the_database(pg_store):
    pg_store.reset()
    s = MemoryService(pg_store)
    f = s.write(subject="user", predicate="employer", value="Acme", source_type="stated", now=NOW).fact
    dup = type(f)(**{**f.__dict__, "id": "f_dup", "value": "Other"})
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_store.insert(dup)


def test_concurrent_writers_never_create_two_active_facts(pg_store):
    pg_store.reset()
    s = MemoryService(pg_store)
    errors, outcomes = [], []

    def go(i: int):
        try:
            r = s.write(subject="user", predicate="employer", value=f"Co{i}", source_type="stated", now=NOW)
            outcomes.append(r.outcome)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    active = [f for f in pg_store.list_facts() if f.status == "active"]
    assert len(active) == 1
    assert outcomes.count("created") == 1


def test_dimension_mismatch_is_refused(pg_store):
    # pg_store isn't used directly; it's the fixture that guarantees a live database to refuse against.
    from fallible_memory.pg_store import PostgresStore
    with pytest.raises(RuntimeError, match="dimensional"):
        PostgresStore(DSN, dim=HashingEmbedder().dim + 1)


def test_revoke_leaves_no_value_in_the_database(pg_store):
    pg_store.reset()
    s = MemoryService(pg_store)
    s.write(subject="user", predicate="employer", value="SecretCorp", source_type="stated", now=NOW)
    s.revoke(subject="user", predicate="employer", now=NOW)
    with psycopg.connect(DSN) as c:
        assert c.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
        rows = c.execute("SELECT marker FROM tombstones").fetchall()
    assert len(rows) == 1 and "secretcorp" not in rows[0][0] and rows[0][0] == P.marker(s._key, P.slot_base_of("user", "user", "employer"))
