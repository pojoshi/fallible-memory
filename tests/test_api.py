import pytest
from fastapi.testclient import TestClient

from fallible_memory.api import create_app


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


def post(c, path, **body):
    r = c.post(path, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_write_dispute_ask_flow(client):
    post(client, "/facts", subject="user", predicate="employer", value="Acme", source_type="stated", now="2026-01-01T00:00:00Z")
    r = post(client, "/facts", subject="user", predicate="employer", value="Hooli", source_type="inferred", now="2026-01-02T00:00:00Z")
    assert r["outcome"] == "disputed" and r["against"]["value"] == "Acme"
    a = post(client, "/ask", subject="user", predicate="employer", now="2026-01-03T00:00:00Z")
    assert a["mode"] == "ask" and "conflicting" in a["text"] and "embedding" not in str(a)


def test_sensitive_is_hidden_by_default_and_inspector_respects_it(client):
    post(client, "/facts", subject="user", predicate="home_address", value="1 Road", source_type="stated")
    assert post(client, "/ask", subject="user", predicate="home_address")["mode"] == "withheld"
    assert client.get("/facts").json() == []
    assert len(client.get("/facts", params={"include_sensitive": True}).json()) == 1


def test_free_text_ask_and_search(client):
    post(client, "/facts", subject="user", predicate="employer", value="Acme", source_type="stated")
    assert post(client, "/ask", query="user employer")["value"] == "Acme"
    hits = post(client, "/search", query="employer")
    assert hits[0]["fact"]["predicate"] == "employer" and hits[0]["score"] > 0.5


def test_forget_and_stats(client):
    post(client, "/facts", subject="user", predicate="employer", value="Acme", source_type="stated")
    assert post(client, "/forget", subject="user", predicate="employer")["removed"] == 1
    assert client.get("/stats").json()["tombstones"] == 1
    r = post(client, "/facts", subject="user", predicate="employer", value="Acme", source_type="inferred")
    assert r["outcome"] == "rejected"
    post(client, "/facts", subject="p", predicate="x", value="y", source_type="stated", scope="proj")
    assert post(client, "/forget/scope", scope="proj")["removed"] == 1


def test_validation_errors(client):
    assert client.post("/ask", json={}).status_code == 422
    assert client.post("/facts", json={"subject": "a", "predicate": "b", "value": "c", "source_type": "rumour"}).status_code == 422
