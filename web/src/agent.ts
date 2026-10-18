/**
 * agent.ts
 *
 * A thin policy layer over the engine: it turns a Retrieval into BEHAVIOR.
 * The same memory produces a confident answer, a hedge, or a clarifying
 * question depending on source, confidence, freshness and disputes. This is
 * the "acts accordingly" half of the brief.
 *
 * It never calls a model to generate language: replies are templated so the
 * behavior is deterministic and testable. Swap `text` for an LLM call if you
 * like; the `mode` and `evidence` are what should stay.
 */

import { type AccessContext, type Band, type Fact, type MemoryEngine, type SourceType } from './memory-engine';

export type AnswerMode = 'assert' | 'hedge' | 'ask' | 'unknown' | 'withheld';

export interface AgentOption {
  kind: 'confirm' | 'choose';
  /** For 'confirm': the value to re-confirm. For 'choose': a candidate value. */
  value: string;
  label: string;
}

export interface AgentAnswer {
  mode: AnswerMode;
  /** What the agent says. */
  text: string;
  /** What it is relying on and how sure it is. Null when it relies on nothing. */
  evidence: string | null;
  value: string | null;
  confidence: number | null;
  band: Band | null;
  factId: string | null;
  options: AgentOption[];
}

const SOURCE_PHRASE: Record<SourceType, string> = {
  stated: 'you told me this',
  tool_output: 'a tool reported this',
  inferred: 'I inferred this',
};

function evidenceFor(f: Fact, confidence: number): string {
  return `Relying on "${f.value}": ${SOURCE_PHRASE[f.sourceType]}; first recorded day ${f.createdAt}, last confirmed day ${f.lastConfirmedAt}; ${confidence}% confidence.`;
}

const none = (mode: AnswerMode, text: string): AgentAnswer => ({
  mode, text, evidence: null, value: null, confidence: null, band: null, factId: null, options: [],
});

export function ask(
  engine: MemoryEngine,
  q: { scope?: string; subject: string; predicate: string },
  now: number,
  ctx: AccessContext,
): AgentAnswer {
  const r = engine.retrieve({ ...q, now, ctx });

  if (r.withheld) {
    return none('withheld', "I may have something on that, but it's marked sensitive and I'm not using it in this context.");
  }
  if (!r.found || !r.fact || !r.band) {
    return none('unknown', "I don't have anything on that.");
  }

  const f = r.fact;
  const conf = r.displayConfidence;
  const base = {
    value: f.value, confidence: conf, band: r.band, factId: f.id, evidence: evidenceFor(f, conf),
  };

  // 1. Conflicting claims: do not pick silently, ask.
  if (r.disputes.length > 0) {
    const d = r.disputes[0]!;
    return {
      ...base,
      mode: 'ask',
      text: `I have two conflicting answers for ${q.predicate}: "${f.value}" (${SOURCE_PHRASE[f.sourceType]}) and "${d.value}" (${SOURCE_PHRASE[d.sourceType]}). I'm leaning towards "${f.value}", but which one is right?`,
      options: [
        { kind: 'choose', value: f.value, label: `Keep "${f.value}"` },
        { kind: 'choose', value: d.value, label: `Use "${d.value}"` },
      ],
    };
  }

  // 2. Stale or low confidence: say so and ask for reconfirmation.
  if (r.isStale || r.band === 'low') {
    return {
      ...base,
      mode: 'ask',
      text: `I have "${f.value}" for ${q.predicate}, but I haven't confirmed it in ${r.daysSinceConfirmed} days (it's normally good for ${f.ttl}). Is that still right?`,
      options: [{ kind: 'confirm', value: f.value, label: 'Yes, still right' }],
    };
  }

  const changed = r.history.length > 0
    ? ` (This changed on day ${f.createdAt}; it used to be "${r.history[0]!.value}".)`
    : '';

  // 3. Medium confidence: answer, but hedge.
  if (r.band === 'medium') {
    return {
      ...base,
      mode: 'hedge',
      text: `I think it's "${f.value}", but I'm not certain: ${SOURCE_PHRASE[f.sourceType]}, ${conf}% confidence.${changed}`,
      options: [{ kind: 'confirm', value: f.value, label: 'Yes, that is right' }],
    };
  }

  // 4. High confidence: assert.
  return { ...base, mode: 'assert', text: `${f.value}.${changed}`, options: [] };
}
