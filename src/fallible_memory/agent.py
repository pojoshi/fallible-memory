"""Policy layer: turn a retrieval into behaviour (assert, hedge, ask, unknown, withheld).

Replies are templated, not generated, so behaviour is deterministic and testable.
Swap ``text`` for an LLM call if you like; ``mode`` and ``evidence`` should stay.
"""

from datetime import datetime
from typing import Optional, Sequence

from .models import AccessContext, AgentAnswer, AgentOption, AnswerMode, Fact, Retrieval
from .service import DEFAULT_SCOPE, MemoryService

SOURCE_PHRASE = {
    "stated": "you told me this",
    "tool_output": "a tool reported this",
    "inferred": "I inferred this",
}


def _day(d: datetime) -> str:
    return d.date().isoformat()


def evidence_for(f: Fact, confidence: int) -> str:
    return (
        f'Relying on "{f.value}": {SOURCE_PHRASE[f.source_type]}; first recorded {_day(f.created_at)}, '
        f"last confirmed {_day(f.last_confirmed_at)}; {confidence}% confidence."
    )


def answer_from(r: Retrieval, label: str) -> AgentAnswer:
    """``label`` is the predicate as the asker phrased it."""
    if r.withheld:
        return AgentAnswer(
            "withheld", "I may have something on that, but it's marked sensitive and I'm not using it in this context."
        )
    if not r.found or r.fact is None or r.band is None:
        return AgentAnswer("unknown", "I don't have anything on that.")

    f, conf = r.fact, r.display_confidence

    def answer(mode: AnswerMode, text: str, options: Sequence[AgentOption] = ()) -> AgentAnswer:
        return AgentAnswer(
            mode=mode, text=text, value=f.value, confidence=conf, band=r.band,
            fact_id=f.id, evidence=evidence_for(f, conf), options=list(options),
        )

    if r.disputes:
        d = r.disputes[0]
        return answer(
            "ask",
            f'I have two conflicting answers for {label}: "{f.value}" ({SOURCE_PHRASE[f.source_type]}) '
            f'and "{d.value}" ({SOURCE_PHRASE[d.source_type]}). '
            f'I\'m leaning towards "{f.value}", but which one is right?',
            [AgentOption("choose", f.value, f'Keep "{f.value}"'), AgentOption("choose", d.value, f'Use "{d.value}"')],
        )

    if r.is_stale or r.band == "low":
        return answer(
            "ask",
            f'I have "{f.value}" for {label}, but I haven\'t confirmed it in {r.days_since_confirmed} days '
            f"(it's normally good for {f.ttl_days:g} days). Is that still right?",
            [AgentOption("confirm", f.value, "Yes, still right")],
        )

    changed = ""
    if r.history:
        changed = f' (This changed on {_day(f.created_at)}; it used to be "{r.history[0].value}".)'

    if r.band == "medium":
        return answer(
            "hedge",
            f'I think it\'s "{f.value}", but I\'m not certain: {SOURCE_PHRASE[f.source_type]}, {conf}% confidence.{changed}',
            [AgentOption("confirm", f.value, "Yes, that is right")],
        )

    return answer("assert", f"{f.value}.{changed}")


def ask(
    service: MemoryService,
    *,
    subject: str,
    predicate: str,
    scope: str = DEFAULT_SCOPE,
    now: Optional[datetime] = None,
    ctx: Optional[AccessContext] = None,
) -> AgentAnswer:
    """Ask about a slot. Paraphrased predicates resolve through hybrid slot matching."""
    r = service.retrieve(subject=subject, predicate=predicate, scope=scope, now=now, ctx=ctx)
    return answer_from(r, predicate)


def ask_text(
    service: MemoryService,
    query: str,
    *,
    scope: Optional[str] = None,
    now: Optional[datetime] = None,
    ctx: Optional[AccessContext] = None,
) -> AgentAnswer:
    """Ask in free text. Finds the best-matching slot (hybrid search + metadata filters),
    then applies the same trust policy. Below the uncertain threshold it says it doesn't know.
    """
    hits = service.search(query, ctx=ctx, scope=scope, cardinality="single", limit=1)
    if not hits or hits[0].score < service.uncertain_threshold:
        return AgentAnswer("unknown", "I don't have anything on that.")
    f = hits[0].fact
    r = service.retrieve(subject=f.subject, predicate=f.predicate, scope=f.scope, now=now, ctx=ctx)
    return answer_from(r, f.predicate)
