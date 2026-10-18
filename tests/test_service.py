from datetime import datetime, timedelta, timezone

import pytest

from fallible_memory import AccessContext, MemoryService, ask, ask_text
from fallible_memory import policy as P

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def day(n: float) -> datetime:
    return BASE + timedelta(days=n)


def w(svc, value, source="stated", now=0, predicate="employer", **kw):
    return svc.write(subject="user", predicate=predicate, value=value, source_type=source, now=day(now), **kw)


# --- policy ---------------------------------------------------------------

def test_decay_and_bands():
    s = MemoryService()
    f = w(s, "Acme", volatility="volatile").fact  # stated volatile: ttl 21
    assert P.decayed_confidence(f, day(21)) == 90
    assert P.decayed_confidence(f, day(31)) == 70
    assert P.decayed_confidence(f, day(400)) == P.CONFIDENCE_FLOOR
    assert P.band_of(75) == "high" and P.band_of(40) == "medium" and P.band_of(39) == "low"


# --- write path -----------------------------------------------------------

def test_create_and_reconfirm_refreshes(svc):
    assert w(svc, "Acme").outcome == "created"
    r = w(svc, "acme", now=10)
    assert r.outcome == "reconfirmed" and r.fact.confidence == 95
    assert r.fact.last_confirmed_at == day(10)


def test_weak_claim_does_not_overwrite_strong(svc):
    w(svc, "Acme", "stated", 0)
    r = w(svc, "Hooli", "inferred", 3)
    assert r.outcome == "disputed" and r.against.value == "Acme"
    a = ask(svc, subject="user", predicate="employer", now=day(4))
    assert a.mode == "ask" and "conflicting" in a.text and a.value == "Acme"


def test_stated_supersedes_and_keeps_audit(svc):
    w(svc, "Acme", "stated", 0)
    r = w(svc, "Globex", "stated", 5)
    assert r.outcome == "superseded" and r.contradicted.status == "contradicted"
    ret = svc.retrieve(subject="user", predicate="employer", now=day(6))
    assert ret.fact.value == "Globex" and ret.history[0].value == "Acme"
    assert "it used to be" in ask(svc, subject="user", predicate="employer", now=day(6)).text


def test_stated_settles_dispute(svc):
    w(svc, "Acme", "stated", 0)
    w(svc, "Hooli", "inferred", 1)
    w(svc, "Acme", "stated", 2)
    assert svc.retrieve(subject="user", predicate="employer", now=day(3)).disputes == []


def test_sensitive_cannot_be_inferred(svc):
    r = w(svc, "diabetes", "inferred", predicate="medical_condition")
    assert r.outcome == "rejected"
    assert w(svc, "diabetes", "stated", predicate="medical_condition").fact.sensitivity == "sensitive"


def test_reconfirm_upgrades_source(svc):
    w(svc, "Acme", "inferred", 0)
    r = w(svc, "Acme", "stated", 1)
    assert r.fact.source_type == "stated" and r.fact.confidence == 90


# --- retrieval ------------------------------------------------------------

def test_stale_fact_asks_and_retrieval_is_pure(svc):
    w(svc, "Acme", "stated", 0, volatility="volatile")
    before = svc.stats()
    a = ask(svc, subject="user", predicate="employer", now=day(40))
    assert a.mode == "ask" and a.options[0].kind == "confirm"
    assert svc.stats() == before
    f = svc.store.find_active(P.slot_base_of("user", "user", "employer"))
    assert f.last_confirmed_at == day(0) and f.confidence == 90


def test_hedge_for_medium_and_assert_for_high(svc):
    w(svc, "Acme", "stated", 0)
    assert ask(svc, subject="user", predicate="employer", now=day(1)).mode == "assert"
    w(svc, "Berlin", "inferred", 0, predicate="city")
    assert ask(svc, subject="user", predicate="city", now=day(1)).mode == "hedge"


def test_unknown(svc):
    assert ask(svc, subject="user", predicate="nothing").mode == "unknown"


def test_scope_and_sensitivity_gate(svc):
    svc.write(subject="user", predicate="home_address", value="1 Road", source_type="stated", now=day(0))
    svc.write(subject="p", predicate="deadline", value="Friday", source_type="stated", scope="proj", now=day(0))
    bot = AccessContext(scopes=["proj"], include_sensitive=False)
    assert ask(svc, subject="user", predicate="home_address", ctx=bot, now=day(1)).mode == "unknown"
    guarded = AccessContext(scopes=["user"], include_sensitive=False)
    assert ask(svc, subject="user", predicate="home_address", ctx=guarded, now=day(1)).mode == "withheld"
    assert ask(svc, subject="p", predicate="deadline", scope="proj", ctx=bot, now=day(1)).mode == "assert"
    assert [e.fact.predicate for e in svc.list(ctx=bot, now=day(1))] == ["deadline"]


def test_multi_cardinality(svc):
    for lang in ("English", "Hindi"):
        svc.write(subject="user", predicate="languages", value=lang, source_type="stated", cardinality="multi", now=day(0))
    got = svc.retrieve_all(subject="user", predicate="languages", now=day(1))
    assert {r.fact.value for r in got} == {"English", "Hindi"}


# --- revoke / forget ------------------------------------------------------

def test_revoke_erases_values_and_blocks_relearning(svc):
    w(svc, "Acme", "stated", 0)
    w(svc, "Globex", "stated", 5)
    w(svc, "Hooli", "inferred", 6)
    assert svc.revoke(subject="user", predicate="employer", now=day(7)) == 3
    assert svc.list(include_history=True, now=day(8)) == []
    assert w(svc, "Hooli", "inferred", 9).outcome == "rejected"
    assert w(svc, "tool", "tool_output", 9).outcome == "rejected"
    assert w(svc, "Acme", "stated", 10).outcome == "created"
    assert svc.store.tombstone_count() == 0


def test_tombstone_is_keyed_and_holds_no_value(svc):
    w(svc, "SecretCorp", "stated", 0)
    svc.revoke(subject="user", predicate="employer", now=day(1))
    assert svc.store.tombstone_count() == 1
    assert P.marker(b"another", "x") != P.marker(b"dev-only-change-me", "x")


def test_forget_scope(svc):
    svc.write(subject="p", predicate="deadline", value="Fri", source_type="stated", scope="proj", now=day(0))
    w(svc, "Acme", "stated", 0)
    assert svc.forget_scope("proj", now=day(1)) == 1
    assert [e.fact.predicate for e in svc.list(now=day(2))] == ["employer"]
    assert svc.write(subject="p", predicate="deadline", value="Mon", source_type="inferred", scope="proj", now=day(3)).outcome == "rejected"


# --- hybrid slot matching -------------------------------------------------

def test_formatting_variants_join_one_slot(svc):
    svc.write(subject="user", predicate="home_city", value="Manama", source_type="stated", now=day(0))
    r = svc.write(subject="user", predicate="Home City", value="Dubai", source_type="stated", now=day(2))
    assert r.slot_match.kind == "semantic" and r.outcome == "superseded"
    assert len(svc.list(now=day(3))) == 1


def test_variant_conflict_is_detected_not_duplicated(svc):
    svc.write(subject="user", predicate="home_city", value="Manama", source_type="stated", now=day(0))
    r = svc.write(subject="user", predicate="home city", value="Dubai", source_type="inferred", now=day(1))
    assert r.outcome == "disputed" and r.slot_match.kind == "semantic"


def test_unrelated_slots_stay_separate(svc):
    w(svc, "Acme", predicate="employer")
    r = w(svc, "Python", predicate="favourite_language")
    assert r.outcome == "created" and r.slot_match.kind == "new"


def test_retrieve_and_revoke_by_variant(svc):
    svc.write(subject="user", predicate="home_city", value="Manama", source_type="stated", now=day(0))
    assert ask(svc, subject="user", predicate="Home City", now=day(1)).value == "Manama"
    assert svc.revoke(subject="user", predicate="home city", now=day(2)) == 1
    assert svc.list(now=day(3)) == []


def test_ask_text(svc):
    w(svc, "Acme", predicate="employer")
    a = ask_text(svc, "user employer", now=day(1))
    assert a.mode == "assert" and a.value == "Acme"
    assert ask_text(svc, "zzzz qqqq", now=day(1)).mode == "unknown"


def test_validation(svc):
    assert svc.write(subject="", predicate="x", value="y", source_type="stated").outcome == "rejected"
    with pytest.raises(ValueError):
        svc.write(subject="a", predicate="b", value="c", source_type="rumour")  # type: ignore[arg-type]
