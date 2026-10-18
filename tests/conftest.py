import os

import pytest

from fallible_memory import HashingEmbedder, InMemoryStore, MemoryService

DSN = os.environ.get("FALLIBLE_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_store():
    if not DSN:
        pytest.skip("FALLIBLE_TEST_DATABASE_URL not set")
    from fallible_memory.pg_store import PostgresStore

    store = PostgresStore(DSN, dim=HashingEmbedder().dim)
    yield store
    store.close()


@pytest.fixture(params=["memory", "postgres"])
def svc(request):
    if request.param == "memory":
        return MemoryService(InMemoryStore())
    store = request.getfixturevalue("pg_store")
    store.reset()
    return MemoryService(store)
