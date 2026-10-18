"""The failure scenarios, narrated. Runs offline against the in-memory store:

    python examples/walkthrough.py
"""
from datetime import datetime, timedelta, timezone

from fallible_memory import AccessContext, MemoryService, ask

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def day(n: float) -> datetime:
    return BASE + timedelta(days=n)


EVERYTHING = AccessContext(include_sensitive=True)
ASSISTANT = AccessContext(scopes=["user"], include_sensitive=False)


def show(title: str) -> None:
    print(f"\n=== {title} ===")


def say(svc, d, pred, ctx=ASSISTANT, subject="user", scope="user"):
    a = ask(svc, subject=subject, predicate=pred, scope=scope, now=day(d), ctx=ctx)
    print(f"day {d:<3} ask {pred!r}\n  [{a.mode}] {a.text}")
    if a.evidence:
        print(f"  {a.evidence}")


def w(svc, d, pred, value, source, **kw):
    r = svc.write(subject="user", predicate=pred, value=value, source_type=source, now=day(d), **kw)
    extra = f" ({r.reason})" if r.reason else ""
    print(f"day {d:<3} write {pred}={value!r} [{source}] -> {r.outcome}{extra}  slot:{r.slot_match.kind}")
    return r


show("1. Contradiction: the user changes employer")
s = MemoryService()
w(s, 0, "employer", "Acme Robotics", "stated")
w(s, 12, "employer", "Globex Corp", "stated")
say(s, 14, "employer")

show("2. Stale: an inferred deadline nobody confirmed")
s = MemoryService()
w(s, 0, "deadline", "Oct 10", "inferred", volatility="volatile")
say(s, 3, "deadline")
say(s, 40, "deadline")
w(s, 40, "deadline", "Oct 10", "stated", volatility="volatile")
say(s, 41, "deadline")

show("3. Conflict: a weak guess disagrees with a strong fact")
s = MemoryService()
w(s, 0, "employer", "Acme Robotics", "stated")
w(s, 3, "employer", "Hooli", "inferred")
say(s, 4, "employer")
w(s, 5, "employer", "Acme Robotics", "stated")
say(s, 6, "employer")

show("4. Same slot, different wording: the conflict is still caught")
s = MemoryService()
w(s, 0, "home_city", "Manama", "stated")
w(s, 2, "Home City", "Dubai", "inferred")
say(s, 3, "home city")

show("5. Privacy: sensitive topics and revocation")
s = MemoryService()
w(s, 0, "medical_condition", "asthma", "inferred")
w(s, 0, "home_address", "12 Palm Road", "stated")
say(s, 1, "home_address")
say(s, 1, "home_address", ctx=EVERYTHING)
n = s.revoke(subject="user", predicate="home_address", now=day(2))
print(f"day 2   forget home_address -> {n} record(s) erased, {s.store.tombstone_count()} tombstone marker kept")
print("'12 Palm Road' anywhere in stored facts?", any("12 palm road" in repr(f).lower() for f in s.store.list_facts()))
w(s, 3, "home_address", "12 Palm Road", "tool_output")
