/**
 * memory-engine.ts
 *
 * Dependency-free memory-layer core for the fallible-memory project (browser playground).
 * The live demo bundles exactly this file (see scripts/build-demo.mjs), so the
 * demo and the repo cannot drift apart.
 *
 * Design rationale lives in docs/ARCHITECTURE.md. Short version:
 *  - Every fact is a claim: source, confidence, freshness, scope, sensitivity.
 *  - WRITE compares against what is already believed (via a slot fingerprint)
 *    and decides: create, reconfirm, supersede, or dispute. A weak claim never
 *    silently overwrites a strong one.
 *  - RETRIEVE never returns a bare value: confidence, freshness, history and
 *    competing claims come with it. Staleness is derived at read time and never
 *    mutates stored state.
 *  - FORGET has distinct paths: stale (derived), contradicted (audit kept),
 *    revoked (value erased, only a one-way tombstone marker remains).
 *
 * Time is a plain number in "days" supplied by the caller, so the demo can use
 * a virtual clock.
 */

export type SourceType = 'stated' | 'tool_output' | 'inferred';
export type Volatility = 'durable' | 'volatile';
export type Sensitivity = 'normal' | 'sensitive';
export type Cardinality = 'single' | 'multi';
/** Revoked facts are erased, not stored, so there is no 'revoked' status. */
export type FactStatus = 'active' | 'disputed' | 'contradicted';
export type Band = 'high' | 'medium' | 'low';

export interface Fact {
  id: string;
  /** Who/what this memory applies to, e.g. 'user', 'project:atlas', 'shared'. */
  scope: string;
  subject: string;
  predicate: string;
  value: string;
  /** slotBase, plus the normalized value for multi-valued facts. */
  fingerprint: string;
  /** scope + subject + predicate, independent of value. */
  slotBase: string;
  sourceType: SourceType;
  volatility: Volatility;
  sensitivity: Sensitivity;
  cardinality: Cardinality;
  /** Base confidence (0-100) at write / last confirmation. Decay is applied on read. */
  confidence: number;
  createdAt: number;
  lastConfirmedAt: number;
  /** Days this fact may go unconfirmed before it counts as stale. */
  ttl: number;
  status: FactStatus;
  /** The fact this one replaced (audit link). */
  supersedes: string | null;
  /** For status 'disputed': the active fact this claim disagrees with. */
  disputes: string | null;
  retiredAt: number | null;
  retiredReason: 'superseded' | 'rejected' | 'replaced' | null;
}

/** What remains after a revoke: a one-way marker, never the value. */
export interface Tombstone {
  marker: string;
  revokedAt: number;
}

export interface AccessContext {
  /** Scopes the requester may read. Undefined means all scopes. */
  scopes?: string[];
  /** Whether sensitive facts may be returned. Defaults to true (full access). */
  includeSensitive?: boolean;
}

export interface WriteInput {
  scope?: string;
  subject: string;
  predicate: string;
  value: string;
  sourceType: SourceType;
  volatility?: Volatility;
  sensitivity?: Sensitivity;
  cardinality?: Cardinality;
  now: number;
  ttlOverride?: number;
}

export type WriteOutcome = 'created' | 'reconfirmed' | 'superseded' | 'disputed' | 'rejected';

export interface WriteResult {
  outcome: WriteOutcome;
  /** The fact written or touched (null when rejected). */
  fact: Fact | null;
  /** The fact retired by this write (superseded outcome). */
  contradicted: Fact | null;
  /** The active fact a disputed claim lost to (disputed outcome). */
  against: Fact | null;
  reason: string | null;
}

export interface Retrieval {
  found: boolean;
  /** True when a fact exists but is hidden from this context for sensitivity. */
  withheld: boolean;
  fact: Fact | null;
  displayConfidence: number;
  band: Band | null;
  isStale: boolean;
  daysSinceConfirmed: number;
  /** What this fact replaced, most recent first. */
  history: Fact[];
  /** Competing lower-trust claims that have not been resolved. */
  disputes: Fact[];
  note: string;
}

export interface InspectorEntry {
  fact: Fact;
  effectiveStatus: 'active' | 'stale' | 'disputed' | 'contradicted';
  displayConfidence: number;
  band: Band;
  isStale: boolean;
}

export const BASE_CONFIDENCE: Record<SourceType, number> = {
  stated: 90,
  tool_output: 75,
  inferred: 50,
};

/** Days a fact may go unconfirmed before it is stale, by volatility and source. */
export const DEFAULT_TTL: Record<Volatility, Record<SourceType, number>> = {
  durable: { stated: 180, tool_output: 60, inferred: 30 },
  volatile: { stated: 21, tool_output: 10, inferred: 5 },
};

/** Linear decay past TTL: points of confidence lost per day overdue. */
export const DECAY_PER_DAY = 2;
export const CONFIDENCE_FLOOR = 10;

/**
 * Predicates that raise sensitivity automatically. A keyword heuristic that
 * errs conservative; callers can also mark any fact sensitive.
 */
export const SENSITIVE_PREDICATE_HINTS = [
  'health',
  'medical',
  'medication',
  'diagnos',
  'religio',
  'salary',
  'income',
  'politic',
  'immigration',
  'sexual',
  'orientation',
  'ethnic',
  'address',
  'passport',
  'ssn',
  'password',
];

export function normalize(s: string): string {
  return s.trim().toLowerCase().replace(/\s+/g, ' ');
}

/** Small non-cryptographic hash. A tombstone is a marker, not a secret. */
function marker(s: string): string {
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
    h2 = Math.imul(h2 ^ (c + i), 0x85ebca6b) >>> 0;
  }
  return h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0');
}

function slotBaseOf(scope: string, subject: string, predicate: string): string {
  return JSON.stringify([normalize(scope), normalize(subject), normalize(predicate)]);
}

export function daysSince(f: Fact, now: number): number {
  return Math.max(0, now - f.lastConfirmedAt);
}

export function isStale(f: Fact, now: number): boolean {
  return daysSince(f, now) > f.ttl;
}

export function decayedConfidence(f: Fact, now: number): number {
  const over = daysSince(f, now) - f.ttl;
  if (over <= 0) return f.confidence;
  return Math.min(f.confidence, Math.max(CONFIDENCE_FLOOR, f.confidence - over * DECAY_PER_DAY));
}

export function bandOf(confidence: number): Band {
  if (confidence >= 75) return 'high';
  if (confidence >= 40) return 'medium';
  return 'low';
}

function sensitivityFor(predicate: string, requested: Sensitivity | undefined): Sensitivity {
  if (requested === 'sensitive') return 'sensitive';
  const p = normalize(predicate);
  return SENSITIVE_PREDICATE_HINTS.some((h) => p.includes(h)) ? 'sensitive' : 'normal';
}

function visibleTo(ctx: AccessContext | undefined, f: Fact): boolean {
  if (!ctx || !ctx.scopes) return true;
  return ctx.scopes.map(normalize).includes(normalize(f.scope));
}

function sensitiveAllowed(ctx: AccessContext | undefined): boolean {
  return ctx?.includeSensitive !== false;
}

interface SerializedState {
  version: 1;
  nextId: number;
  facts: Fact[];
  tombstones: Tombstone[];
}

export class MemoryEngine {
  private facts = new Map<string, Fact>();
  private tombstones: Tombstone[] = [];
  private nextId = 1;

  // ---------------------------------------------------------------- reads

  /** Every stored fact, including contradicted history. Revoked values are gone. */
  getAll(): Fact[] {
    return [...this.facts.values()];
  }

  tombstoneCount(): number {
    return this.tombstones.length;
  }

  private findActive(fp: string): Fact | undefined {
    for (const f of this.facts.values()) {
      if (f.fingerprint === fp && f.status === 'active') return f;
    }
    return undefined;
  }

  private findDisputes(fp: string): Fact[] {
    return [...this.facts.values()].filter((f) => f.fingerprint === fp && f.status === 'disputed');
  }

  private isTombstoned(fp: string, base: string): boolean {
    const a = marker(fp);
    const b = marker(base);
    return this.tombstones.some((t) => t.marker === a || t.marker === b);
  }

  // ---------------------------------------------------------------- write

  /**
   * WRITE PATH. See docs/ARCHITECTURE.md, "Write path".
   *
   * 1. Reject empty input, and reject inferred claims about sensitive topics.
   * 2. Reject non-stated claims about a slot the user has revoked.
   * 3. Look up the active fact in this slot.
   * 4. Same value: reconfirm (refresh freshness, nudge confidence).
   * 5. Different value: compare the incoming baseline confidence with the
   *    existing fact's CURRENT (decayed) confidence. Incoming >= existing:
   *    supersede, keeping the old fact for audit. Otherwise: store the
   *    incoming claim as 'disputed' and leave the existing fact active.
   */
  write(input: WriteInput): WriteResult {
    const scope = (input.scope ?? 'user').trim();
    const subject = input.subject.trim();
    const predicate = input.predicate.trim();
    const value = input.value.trim();
    if (!scope || !subject || !predicate || !value) {
      return this.rejected('Scope, subject, predicate and value are all required.');
    }

    const cardinality: Cardinality = input.cardinality ?? 'single';
    const volatility: Volatility = input.volatility ?? 'durable';
    const sensitivity = sensitivityFor(predicate, input.sensitivity);

    if (sensitivity === 'sensitive' && input.sourceType === 'inferred') {
      return this.rejected(
        `"${predicate}" is a sensitive topic. Sensitive facts can only be stated by the user, never inferred.`,
      );
    }

    const base = slotBaseOf(scope, subject, predicate);
    const fp = cardinality === 'multi' ? `${base}#${normalize(value)}` : base;

    if (this.isTombstoned(fp, base)) {
      if (input.sourceType !== 'stated') {
        return this.rejected(
          `The user asked me to forget "${predicate}". Only the user can tell me this again.`,
        );
      }
      // The user is re-stating it themselves: lift the tombstone for this exact slot.
      const m = marker(fp);
      this.tombstones = this.tombstones.filter((t) => t.marker !== m);
    }

    const incomingConf = BASE_CONFIDENCE[input.sourceType];
    const existing = this.findActive(fp);
    const ttl = input.ttlOverride ?? DEFAULT_TTL[volatility][input.sourceType];

    if (existing && normalize(existing.value) === normalize(value)) {
      // Reconfirmation. Any source refreshes freshness; only an equal-or-stronger
      // source adds confidence. A user restating the value settles open disputes.
      existing.lastConfirmedAt = Math.max(existing.lastConfirmedAt, input.now);
      const existingBase = BASE_CONFIDENCE[existing.sourceType];
      if (incomingConf > existingBase) {
        // A stronger source confirming a weaker claim upgrades it: the user
        // saying "yes, that's right" turns an inference into a stated fact.
        existing.sourceType = input.sourceType;
        existing.confidence = Math.max(existing.confidence, incomingConf);
        existing.ttl = ttl;
      } else if (incomingConf === existingBase) {
        existing.confidence = Math.min(100, existing.confidence + 5);
      }
      if (input.sourceType === 'stated') {
        for (const d of this.findDisputes(fp)) this.retire(d, input.now, 'rejected');
      }
      return { outcome: 'reconfirmed', fact: existing, contradicted: null, against: null, reason: null };
    }

    if (existing) {
      const existingNow = decayedConfidence(existing, input.now);

      // A non-stated claim repeating an already-disputed value just refreshes it.
      const repeat = this.findDisputes(fp).find((d) => normalize(d.value) === normalize(value));
      if (repeat && input.sourceType !== 'stated') {
        repeat.lastConfirmedAt = Math.max(repeat.lastConfirmedAt, input.now);
        return { outcome: 'disputed', fact: repeat, contradicted: null, against: existing, reason: null };
      }

      if (incomingConf >= existingNow) {
        this.retire(existing, input.now, 'superseded');
        for (const d of this.findDisputes(fp)) this.retire(d, input.now, 'rejected');
        const fact = this.create({
          scope, subject, predicate, value, base, fp, cardinality, volatility, sensitivity,
          sourceType: input.sourceType, ttl, now: input.now, supersedes: existing.id, disputes: null,
          status: 'active',
        });
        return { outcome: 'superseded', fact, contradicted: existing, against: null, reason: null };
      }

      // The existing fact is more trustworthy right now: record the disagreement.
      for (const d of this.findDisputes(fp)) this.retire(d, input.now, 'replaced');
      const fact = this.create({
        scope, subject, predicate, value, base, fp, cardinality, volatility, sensitivity,
        sourceType: input.sourceType, ttl, now: input.now, supersedes: null, disputes: existing.id,
        status: 'disputed',
      });
      return { outcome: 'disputed', fact, contradicted: null, against: existing, reason: null };
    }

    const fact = this.create({
      scope, subject, predicate, value, base, fp, cardinality, volatility, sensitivity,
      sourceType: input.sourceType, ttl, now: input.now, supersedes: null, disputes: null,
      status: 'active',
    });
    return { outcome: 'created', fact, contradicted: null, against: null, reason: null };
  }

  private rejected(reason: string): WriteResult {
    return { outcome: 'rejected', fact: null, contradicted: null, against: null, reason };
  }

  private retire(f: Fact, now: number, reason: NonNullable<Fact['retiredReason']>): void {
    f.status = 'contradicted';
    f.retiredAt = now;
    f.retiredReason = reason;
  }

  private create(a: {
    scope: string; subject: string; predicate: string; value: string; base: string; fp: string;
    cardinality: Cardinality; volatility: Volatility; sensitivity: Sensitivity; sourceType: SourceType;
    ttl: number; now: number; supersedes: string | null; disputes: string | null; status: FactStatus;
  }): Fact {
    const fact: Fact = {
      id: `fact_${this.nextId++}`,
      scope: a.scope,
      subject: a.subject,
      predicate: a.predicate,
      value: a.value,
      fingerprint: a.fp,
      slotBase: a.base,
      sourceType: a.sourceType,
      volatility: a.volatility,
      sensitivity: a.sensitivity,
      cardinality: a.cardinality,
      confidence: BASE_CONFIDENCE[a.sourceType],
      createdAt: a.now,
      lastConfirmedAt: a.now,
      ttl: a.ttl,
      status: a.status,
      supersedes: a.supersedes,
      disputes: a.disputes,
      retiredAt: null,
      retiredReason: null,
    };
    this.facts.set(fact.id, fact);
    return fact;
  }

  // ------------------------------------------------------------- retrieval

  private emptyRetrieval(note: string, withheld = false): Retrieval {
    return {
      found: false, withheld, fact: null, displayConfidence: 0, band: null,
      isStale: false, daysSinceConfirmed: 0, history: [], disputes: [], note,
    };
  }

  private build(f: Fact, now: number): Retrieval {
    const stale = isStale(f, now);
    const conf = decayedConfidence(f, now);
    const since = daysSince(f, now);

    const history: Fact[] = [];
    let cursor = f.supersedes;
    while (cursor) {
      const prev = this.facts.get(cursor);
      if (!prev) break;
      history.push(prev);
      cursor = prev.supersedes;
    }
    const disputes = this.findDisputes(f.fingerprint);

    const notes: string[] = [];
    if (stale) {
      notes.push(
        `Last confirmed ${since} day(s) ago; this kind of fact is normally good for ${f.ttl} day(s). I might be wrong, it could have changed since.`,
      );
    }
    if (disputes.length > 0) {
      const d = disputes[0]!;
      notes.push(
        `A weaker claim disagrees: "${d.value}" (${d.sourceType}, day ${d.createdAt}). I am keeping "${f.value}" until the user settles it.`,
      );
    }
    if (history.length > 0) {
      const prev = history[0]!;
      notes.push(`This replaced an earlier claim ("${prev.value}", ${prev.sourceType}, day ${prev.createdAt}) on day ${f.createdAt}.`);
    }
    if (f.sourceType === 'inferred') {
      notes.push('This was inferred, not stated directly, so its starting trust is lower.');
    }

    return {
      found: true, withheld: false, fact: f, displayConfidence: conf, band: bandOf(conf),
      isStale: stale, daysSinceConfirmed: since, history, disputes, note: notes.join(' '),
    };
  }

  /**
   * RETRIEVAL PATH for single-valued slots. Never returns a bare value, and
   * never mutates stored state (staleness is derived from `now`).
   */
  retrieve(q: { scope?: string; subject: string; predicate: string; now: number; ctx?: AccessContext }): Retrieval {
    const scope = q.scope ?? 'user';
    const base = slotBaseOf(scope, q.subject, q.predicate);
    const label = `${q.subject} ${q.predicate}`;
    const f = this.findActive(base);
    if (!f || !visibleTo(q.ctx, f)) return this.emptyRetrieval(`No memory of "${label}".`);
    if (f.sensitivity === 'sensitive' && !sensitiveAllowed(q.ctx)) {
      return this.emptyRetrieval(`"${label}" is marked sensitive and is not available in this context.`, true);
    }
    return this.build(f, q.now);
  }

  /** Retrieval for multi-valued slots (e.g. languages spoken). */
  retrieveAll(q: { scope?: string; subject: string; predicate: string; now: number; ctx?: AccessContext }): Retrieval[] {
    const base = slotBaseOf(q.scope ?? 'user', q.subject, q.predicate);
    return this.getAll()
      .filter((f) => f.slotBase === base && f.status === 'active' && visibleTo(q.ctx, f))
      .filter((f) => f.sensitivity !== 'sensitive' || sensitiveAllowed(q.ctx))
      .map((f) => this.build(f, q.now));
  }

  /** Memory inspector: "what do you remember about me?" */
  list(opts: { now: number; ctx?: AccessContext; includeHistory?: boolean }): InspectorEntry[] {
    return this.getAll()
      .filter((f) => visibleTo(opts.ctx, f))
      .filter((f) => f.sensitivity !== 'sensitive' || sensitiveAllowed(opts.ctx))
      .filter((f) => opts.includeHistory || f.status !== 'contradicted')
      .map((f): InspectorEntry => {
        const stale = f.status === 'active' && isStale(f, opts.now);
        const conf = f.status === 'active' ? decayedConfidence(f, opts.now) : f.confidence;
        const effectiveStatus: InspectorEntry['effectiveStatus'] =
          f.status === 'active' ? (stale ? 'stale' : 'active') : f.status;
        return { fact: f, effectiveStatus, displayConfidence: conf, band: bandOf(conf), isStale: stale };
      })
      .sort((a, b) => b.fact.createdAt - a.fact.createdAt || Number(b.fact.id.slice(5)) - Number(a.fact.id.slice(5)));
  }

  // ------------------------------------------------------------ forgetting

  /**
   * FORGETTING PATH, explicit revoke ("forget that").
   *
   * Unlike a contradiction, a revoke ERASES the values: the active fact, any
   * superseded history and any disputed claims in the slot are deleted. All that
   * remains is a one-way tombstone marker, which stops the system re-learning
   * the same slot from inference or tool output. Only the user re-stating it
   * lifts the tombstone. Revoking a slot the memory never held still adds the
   * tombstone ("never infer this about me").
   *
   * Pass `value` to revoke a single value of a multi-valued slot.
   */
  revoke(a: { scope?: string; subject: string; predicate: string; value?: string; now: number }): { removed: number } {
    const base = slotBaseOf(a.scope ?? 'user', a.subject, a.predicate);
    const valueKey = a.value !== undefined ? normalize(a.value) : undefined;
    const doomed = this.getAll().filter(
      (f) => f.slotBase === base && (valueKey === undefined || f.cardinality === 'single' || normalize(f.value) === valueKey),
    );
    const tomb = valueKey !== undefined ? `${base}#${valueKey}` : base;
    this.purge(doomed, [tomb], a.now);
    return { removed: doomed.length };
  }

  /** Forget everything in a scope (e.g. "forget everything about this project"). */
  forgetScope(scope: string, now: number): { removed: number } {
    const s = normalize(scope);
    const doomed = this.getAll().filter((f) => normalize(f.scope) === s);
    const bases = [...new Set(doomed.map((f) => f.slotBase))];
    this.purge(doomed, bases, now);
    return { removed: doomed.length };
  }

  private purge(doomed: Fact[], tombstoneKeys: string[], now: number): void {
    const ids = new Set(doomed.map((f) => f.id));
    for (const f of doomed) this.facts.delete(f.id);
    for (const f of this.facts.values()) {
      if (f.supersedes && ids.has(f.supersedes)) f.supersedes = null;
      if (f.disputes && ids.has(f.disputes)) f.disputes = null;
    }
    for (const key of tombstoneKeys) {
      const m = marker(key);
      if (!this.tombstones.some((t) => t.marker === m)) this.tombstones.push({ marker: m, revokedAt: now });
    }
  }

  // ----------------------------------------------------------- persistence

  toJSON(): SerializedState {
    return {
      version: 1,
      nextId: this.nextId,
      facts: this.getAll().map((f) => ({ ...f })),
      tombstones: this.tombstones.map((t) => ({ ...t })),
    };
  }

  serialize(): string {
    return JSON.stringify(this.toJSON());
  }

  static fromJSON(state: unknown): MemoryEngine {
    const s = state as Partial<SerializedState> | null;
    if (!s || s.version !== 1 || !Array.isArray(s.facts) || !Array.isArray(s.tombstones) || typeof s.nextId !== 'number') {
      throw new Error('Unrecognized memory state');
    }
    const engine = new MemoryEngine();
    for (const f of s.facts) engine.facts.set(f.id, { ...f });
    engine.tombstones = s.tombstones.map((t) => ({ ...t }));
    engine.nextId = s.nextId;
    return engine;
  }

  static deserialize(json: string): MemoryEngine {
    return MemoryEngine.fromJSON(JSON.parse(json));
  }
}
