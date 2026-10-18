"""Runs fixtures/conformance.json against both stores. The vitest runner in web/ runs the same file."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fallible_memory import AccessContext, ask

FIXTURES = json.loads((Path(__file__).parent.parent / "fixtures" / "conformance.json").read_text())
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def day(n):
    return BASE + timedelta(days=n)


def ctx_of(c):
    if c is None:
        return None
    return AccessContext(scopes=c.get("scopes"), include_sensitive=c.get("includeSensitive", True))


def dump(svc) -> str:
    facts = svc.store.list_facts()
    return json.dumps(
        [{k: v for k, v in f.__dict__.items() if k != "embedding"} for f in facts], default=str
    ).lower()


@pytest.mark.parametrize("scenario", FIXTURES["scenarios"], ids=lambda s: s["name"][:60])
def test_scenario(svc, scenario):
    for i, step in enumerate(scenario["steps"]):
        op = step["op"]
        exp, where = step.get("expect", {}), f"step {i} ({op})"
        if op == "write":
            r = svc.write(
                scope=step.get("scope", "user"), subject=step["subject"], predicate=step["predicate"],
                value=step["value"], source_type=step["source"], volatility=step.get("volatility", "durable"),
                cardinality=step.get("cardinality", "single"), now=day(step["day"]),
            )
            assert r.outcome == exp["outcome"], where
            if "contradictedValue" in exp:
                assert r.contradicted.value == exp["contradictedValue"], where
            if "againstValue" in exp:
                assert r.against.value == exp["againstValue"], where
        elif op == "ask":
            a = ask(svc, scope=step.get("scope", "user"), subject=step["subject"], predicate=step["predicate"],
                    now=day(step["day"]), ctx=ctx_of(step.get("ctx")))
            assert a.mode == exp["mode"], f"{where}: {a.text}"
            if "value" in exp:
                assert a.value == exp["value"], where
            if "confidence" in exp:
                assert a.confidence == exp["confidence"], f"{where}: {a.confidence}"
            if "text" in exp:
                assert a.text == exp["text"], where
            for part in exp.get("textContains", []):
                assert part in a.text, f"{where}: {part!r} not in {a.text!r}"
            if "optionValues" in exp:
                assert [o.value for o in a.options] == exp["optionValues"], where
        elif op == "revoke":
            n = svc.revoke(scope=step.get("scope", "user"), subject=step["subject"], predicate=step["predicate"],
                           value=step.get("value"), now=day(step["day"]))
            assert n == exp["removed"], where
        elif op == "forgetScope":
            assert svc.forget_scope(step["scope"], now=day(step["day"])) == exp["removed"], where
        elif op == "check":
            if "factCount" in step:
                assert len(svc.store.list_facts()) == step["factCount"], where
            if "tombstones" in step:
                assert svc.store.tombstone_count() == step["tombstones"], where
            for needle in step.get("storeLacks", []):
                assert needle.lower() not in dump(svc), f"{where}: {needle} still stored"
        else:
            raise AssertionError(f"unknown op {op}")
