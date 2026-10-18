import { describe, expect, it } from 'vitest';
import { MemoryEngine, decayedConfidence } from '../src/memory-engine';

const employer = { subject: 'user', predicate: 'employer' };
const deadline = { scope: 'project:atlas', subject: 'project', predicate: 'deadline' };

describe('failure scenario 1: employer contradiction', () => {
  it('retires the old fact for audit, links the new one, and explains the change', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    const res = mem.write({ ...employer, value: 'Globex Corp', sourceType: 'stated', now: 12 });

    expect(res.outcome).toBe('superseded');
    expect(res.contradicted?.value).toBe('Acme Robotics');
    expect(res.contradicted?.status).toBe('contradicted');
    expect(res.fact?.supersedes).toBe(res.contradicted?.id);

    const r = mem.retrieve({ ...employer, now: 12 });
    expect(r.fact?.value).toBe('Globex Corp');
    expect(r.history.map((h) => h.value)).toEqual(['Acme Robotics']);
    expect(r.note).toContain('replaced an earlier claim');
    expect(mem.getAll()).toHaveLength(2); // nothing silently erased
  });
});

describe('failure scenario 2: stale fact nobody corrected', () => {
  const seed = () => {
    const mem = new MemoryEngine();
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'inferred', volatility: 'volatile', now: 0 });
    return mem;
  };

  it('flags itself as possibly wrong and decays confidence', () => {
    const r = seed().retrieve({ ...deadline, now: 40 });
    expect(r.found).toBe(true);
    expect(r.isStale).toBe(true);
    expect(r.displayConfidence).toBeLessThan(50);
    expect(r.note).toContain('I might be wrong');
  });

  it('REGRESSION: a stale fact stays retrievable on every later read', () => {
    const mem = seed();
    for (const day of [6, 10, 40, 40, 41, 80]) {
      const r = mem.retrieve({ ...deadline, now: day });
      expect(r.found, `day ${day}`).toBe(true);
      expect(r.fact?.value).toBe('Oct 10');
    }
  });

  it('retrieval never mutates stored state', () => {
    const mem = seed();
    const before = mem.serialize();
    mem.retrieve({ ...deadline, now: 40 });
    expect(mem.serialize()).toBe(before);
  });

  it('reconfirming a stale fact refreshes it in place, with no duplicate row', () => {
    const mem = seed();
    mem.retrieve({ ...deadline, now: 40 });
    const res = mem.write({ ...deadline, value: 'oct 10 ', sourceType: 'stated', volatility: 'volatile', now: 40 });
    expect(res.outcome).toBe('reconfirmed');
    expect(mem.getAll()).toHaveLength(1);
    const r = mem.retrieve({ ...deadline, now: 41 });
    expect(r.isStale).toBe(false);
  });

  it('a stronger source confirming an inferred fact upgrades its source, trust and TTL', () => {
    const mem = seed();
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'stated', volatility: 'volatile', now: 40 });
    const f = mem.getAll()[0]!;
    expect(f.sourceType).toBe('stated');
    expect(f.confidence).toBe(90);
    expect(f.ttl).toBe(21);
    expect(mem.retrieve({ ...deadline, now: 41 }).band).toBe('high');
  });

  it('a weaker source confirming a stated fact refreshes freshness but never lowers trust', () => {
    const mem = new MemoryEngine();
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'stated', volatility: 'volatile', now: 0 });
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'inferred', volatility: 'volatile', now: 10 });
    const f = mem.getAll()[0]!;
    expect(f.sourceType).toBe('stated');
    expect(f.confidence).toBe(90);
    expect(f.lastConfirmedAt).toBe(10);
  });

  it('decay is linear past the TTL and floored', () => {
    const f = seed().getAll()[0]!;
    expect(decayedConfidence(f, 5)).toBe(50); // at TTL, no decay yet
    expect(decayedConfidence(f, 10)).toBe(40); // 5 days over * 2
    expect(decayedConfidence(f, 1000)).toBe(10); // floor
  });
});

describe('failure scenario 3: a weak guess must not overwrite a strong fact', () => {
  it('stores an inferred contradiction as a dispute and keeps the stated fact active', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    const res = mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 3 });

    expect(res.outcome).toBe('disputed');
    expect(res.against?.value).toBe('Acme Robotics');

    const r = mem.retrieve({ ...employer, now: 3 });
    expect(r.fact?.value).toBe('Acme Robotics');
    expect(r.disputes.map((d) => d.value)).toEqual(['Hooli']);
    expect(r.note).toContain('weaker claim disagrees');
  });

  it('a repeated weak claim refreshes the dispute instead of piling up rows', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 3 });
    mem.write({ ...employer, value: 'hooli', sourceType: 'inferred', now: 4 });
    expect(mem.getAll()).toHaveLength(2);
  });

  it('the user restating the original value settles the dispute', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 3 });
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 5 });
    expect(mem.retrieve({ ...employer, now: 5 }).disputes).toHaveLength(0);
    expect(mem.getAll().find((f) => f.value === 'Hooli')?.status).toBe('contradicted');
  });

  it('the user stating the new value supersedes and settles the dispute', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 3 });
    const res = mem.write({ ...employer, value: 'Hooli', sourceType: 'stated', now: 5 });
    expect(res.outcome).toBe('superseded');
    const r = mem.retrieve({ ...employer, now: 5 });
    expect(r.fact?.value).toBe('Hooli');
    expect(r.disputes).toHaveLength(0);
  });

  it('once the strong fact has decayed below the weak claim, the weak claim wins', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    // durable+stated ttl is 180; 90 - 2*overBy < 50 once overBy > 20
    const res = mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 250 });
    expect(res.outcome).toBe('superseded');
  });

  it('a tool output ties or beats an equally fresh inferred fact', () => {
    const mem = new MemoryEngine();
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'inferred', volatility: 'volatile', now: 0 });
    const res = mem.write({ ...deadline, value: 'Nov 3', sourceType: 'tool_output', volatility: 'volatile', now: 1 });
    expect(res.outcome).toBe('superseded');
  });
});

describe('matching', () => {
  it('treats case and whitespace differences as the same value', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    const res = mem.write({ ...employer, value: '  acme   robotics ', sourceType: 'stated', now: 1 });
    expect(res.outcome).toBe('reconfirmed');
    expect(mem.getAll()).toHaveLength(1);
  });

  it('multi-valued facts coexist instead of contradicting', () => {
    const mem = new MemoryEngine();
    const lang = { subject: 'user', predicate: 'languages', cardinality: 'multi' as const, sourceType: 'stated' as const, now: 0 };
    expect(mem.write({ ...lang, value: 'English' }).outcome).toBe('created');
    expect(mem.write({ ...lang, value: 'Arabic' }).outcome).toBe('created');
    expect(mem.write({ ...lang, value: 'english' }).outcome).toBe('reconfirmed');
    const all = mem.retrieveAll({ subject: 'user', predicate: 'languages', now: 1 });
    expect(all.map((r) => r.fact?.value).sort()).toEqual(['Arabic', 'English']);
  });

  it('rejects empty input', () => {
    const mem = new MemoryEngine();
    expect(mem.write({ ...employer, value: '   ', sourceType: 'stated', now: 0 }).outcome).toBe('rejected');
  });
});

describe('scope', () => {
  it('keeps the same subject+predicate separate across scopes', () => {
    const mem = new MemoryEngine();
    mem.write({ scope: 'project:atlas', subject: 'project', predicate: 'deadline', value: 'Oct 10', sourceType: 'stated', now: 0 });
    const res = mem.write({ scope: 'project:borealis', subject: 'project', predicate: 'deadline', value: 'Dec 1', sourceType: 'stated', now: 0 });
    expect(res.outcome).toBe('created');
  });

  it('hides facts outside the requester scopes as if they did not exist', () => {
    const mem = new MemoryEngine();
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'stated', now: 0 });
    const hidden = mem.retrieve({ ...deadline, now: 1, ctx: { scopes: ['user'] } });
    expect(hidden.found).toBe(false);
    expect(hidden.withheld).toBe(false);
    expect(mem.retrieve({ ...deadline, now: 1, ctx: { scopes: ['project:atlas'] } }).found).toBe(true);
    expect(mem.list({ now: 1, ctx: { scopes: ['user'] } })).toHaveLength(0);
  });
});

describe('privacy: sensitivity', () => {
  it('refuses to infer sensitive facts', () => {
    const mem = new MemoryEngine();
    const res = mem.write({ ...employer, predicate: 'medical_condition', value: 'knee injury', sourceType: 'inferred', now: 0 });
    expect(res.outcome).toBe('rejected');
    expect(res.reason).toContain('sensitive');
    expect(mem.getAll()).toHaveLength(0);
  });

  it('raises sensitivity from the predicate even when the caller says normal', () => {
    const mem = new MemoryEngine();
    const res = mem.write({ subject: 'user', predicate: 'home_address', value: '12 Palm Road', sourceType: 'stated', sensitivity: 'normal', now: 0 });
    expect(res.fact?.sensitivity).toBe('sensitive');
  });

  it('withholds sensitive facts from contexts that do not allow them', () => {
    const mem = new MemoryEngine();
    mem.write({ subject: 'user', predicate: 'home_address', value: '12 Palm Road', sourceType: 'stated', now: 0 });
    const q = { subject: 'user', predicate: 'home_address', now: 1 };
    const blocked = mem.retrieve({ ...q, ctx: { includeSensitive: false } });
    expect(blocked.withheld).toBe(true);
    expect(blocked.fact).toBeNull();
    expect(mem.retrieve({ ...q, ctx: { includeSensitive: true } }).fact?.value).toBe('12 Palm Road');
    expect(mem.list({ now: 1, ctx: { includeSensitive: false } })).toHaveLength(0);
  });
});

describe('privacy: revocation erases, it does not hide', () => {
  const setup = () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Globex Corp', sourceType: 'stated', now: 12 });
    mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 13 });
    return mem;
  };

  it('purges the active value, the superseded history and disputed claims from the store', () => {
    const mem = setup();
    const { removed } = mem.revoke({ ...employer, now: 20 });
    expect(removed).toBe(3);
    const dump = mem.serialize();
    for (const secret of ['Acme Robotics', 'Globex Corp', 'Hooli']) expect(dump).not.toContain(secret);
    expect(mem.retrieve({ ...employer, now: 20 }).found).toBe(false);
    expect(mem.tombstoneCount()).toBe(1);
  });

  it('a tombstone stops inference and tool output re-learning it', () => {
    const mem = setup();
    mem.revoke({ ...employer, now: 20 });
    expect(mem.write({ ...employer, value: 'Globex Corp', sourceType: 'inferred', now: 21 }).outcome).toBe('rejected');
    expect(mem.write({ ...employer, value: 'Globex Corp', sourceType: 'tool_output', now: 21 }).outcome).toBe('rejected');
    expect(mem.getAll()).toHaveLength(0);
  });

  it('only the user re-stating it lifts the tombstone', () => {
    const mem = setup();
    mem.revoke({ ...employer, now: 20 });
    const res = mem.write({ ...employer, value: 'Initech', sourceType: 'stated', now: 30 });
    expect(res.outcome).toBe('created');
    expect(mem.tombstoneCount()).toBe(0);
    expect(mem.write({ ...employer, value: 'Initech', sourceType: 'inferred', now: 31 }).outcome).toBe('reconfirmed');
  });

  it('revoking something never stored still blocks future inference', () => {
    const mem = new MemoryEngine();
    expect(mem.revoke({ ...employer, now: 1 }).removed).toBe(0);
    expect(mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 2 }).outcome).toBe('rejected');
  });

  it('revoking one value of a multi-valued slot leaves the others', () => {
    const mem = new MemoryEngine();
    const lang = { subject: 'user', predicate: 'languages', cardinality: 'multi' as const, sourceType: 'stated' as const, now: 0 };
    mem.write({ ...lang, value: 'English' });
    mem.write({ ...lang, value: 'Arabic' });
    mem.revoke({ subject: 'user', predicate: 'languages', value: 'arabic', now: 1 });
    const left = mem.retrieveAll({ subject: 'user', predicate: 'languages', now: 2 });
    expect(left.map((r) => r.fact?.value)).toEqual(['English']);
    expect(mem.serialize()).not.toContain('Arabic');
  });

  it('forgetScope erases a whole scope and leaves the rest', () => {
    const mem = setup();
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'stated', now: 0 });
    const { removed } = mem.forgetScope('project:atlas', 5);
    expect(removed).toBe(1);
    expect(mem.serialize()).not.toContain('Oct 10');
    expect(mem.retrieve({ ...employer, now: 5 }).found).toBe(true);
  });
});

describe('persistence and ids', () => {
  it('round-trips state and keeps ids unique afterwards', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Globex Corp', sourceType: 'stated', now: 12 });
    mem.revoke({ subject: 'user', predicate: 'home_address', now: 13 });

    const copy = MemoryEngine.deserialize(mem.serialize());
    expect(copy.serialize()).toBe(mem.serialize());
    expect(copy.retrieve({ ...employer, now: 12 }).history).toHaveLength(1);
    expect(copy.write({ subject: 'user', predicate: 'home_address', value: 'x', sourceType: 'inferred', now: 14 }).outcome).toBe('rejected');

    const fresh = copy.write({ subject: 'user', predicate: 'timezone', value: 'Europe/Lisbon', sourceType: 'stated', now: 14 });
    const ids = copy.getAll().map((f) => f.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(fresh.fact?.id).toBe('fact_3');
  });

  it('keeps id counters per instance', () => {
    const a = new MemoryEngine();
    const b = new MemoryEngine();
    expect(a.write({ ...employer, value: 'x', sourceType: 'stated', now: 0 }).fact?.id).toBe('fact_1');
    expect(b.write({ ...employer, value: 'y', sourceType: 'stated', now: 0 }).fact?.id).toBe('fact_1');
  });

  it('rejects unrecognized state', () => {
    expect(() => MemoryEngine.deserialize('{"version":2}')).toThrow();
  });
});

describe('memory inspector', () => {
  it('lists active, stale and disputed entries, with history on request', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Globex Corp', sourceType: 'stated', now: 12 });
    mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 13 });
    mem.write({ ...deadline, value: 'Oct 10', sourceType: 'inferred', volatility: 'volatile', now: 0 });

    const now = 20;
    const statuses = mem.list({ now }).map((e) => e.effectiveStatus).sort();
    expect(statuses).toEqual(['active', 'disputed', 'stale']);
    expect(mem.list({ now, includeHistory: true })).toHaveLength(4);
  });
});
