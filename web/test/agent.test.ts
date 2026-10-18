import { describe, expect, it } from 'vitest';
import { ask } from '../src/agent';
import { MemoryEngine } from '../src/memory-engine';

const employer = { subject: 'user', predicate: 'employer' };
const everything = { includeSensitive: true };
const assistant = { scopes: ['user', 'shared'], includeSensitive: false };

describe('agent behavior follows the memory', () => {
  it('asserts a fresh, stated fact and shows what it relies on', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    const a = ask(mem, employer, 5, assistant);
    expect(a.mode).toBe('assert');
    expect(a.text).toBe('Acme Robotics.');
    expect(a.evidence).toContain('you told me this');
    expect(a.evidence).toContain('90%');
  });

  it('mentions that it changed its mind', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Globex Corp', sourceType: 'stated', now: 12 });
    const a = ask(mem, employer, 14, assistant);
    expect(a.mode).toBe('assert');
    expect(a.text).toContain('used to be "Acme Robotics"');
  });

  it('hedges on a medium-confidence inferred fact', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 0 });
    const a = ask(mem, employer, 1, assistant);
    expect(a.mode).toBe('hedge');
    expect(a.text).toContain("I'm not certain");
  });

  it('asks for reconfirmation when a fact is stale', () => {
    const mem = new MemoryEngine();
    mem.write({ scope: 'user', subject: 'project', predicate: 'deadline', value: 'Oct 10', sourceType: 'inferred', volatility: 'volatile', now: 0 });
    const a = ask(mem, { subject: 'project', predicate: 'deadline' }, 40, assistant);
    expect(a.mode).toBe('ask');
    expect(a.options[0]?.kind).toBe('confirm');
    expect(a.text).toContain('Is that still right?');
  });

  it('asks which claim is right when two disagree, rather than silently picking', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.write({ ...employer, value: 'Hooli', sourceType: 'inferred', now: 2 });
    const a = ask(mem, employer, 3, assistant);
    expect(a.mode).toBe('ask');
    expect(a.options.map((o) => o.value)).toEqual(['Acme Robotics', 'Hooli']);
  });

  it('says it does not know, and does not distinguish forgotten from never known', () => {
    const mem = new MemoryEngine();
    mem.write({ ...employer, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
    mem.revoke({ ...employer, now: 1 });
    const forgotten = ask(mem, employer, 2, everything);
    const never = ask(new MemoryEngine(), employer, 2, everything);
    expect(forgotten.mode).toBe('unknown');
    expect(forgotten.text).toBe(never.text);
  });

  it('withholds sensitive facts from restricted contexts and shares them with full access', () => {
    const mem = new MemoryEngine();
    mem.write({ subject: 'user', predicate: 'home_address', value: '12 Palm Road', sourceType: 'stated', now: 0 });
    const q = { subject: 'user', predicate: 'home_address' };
    const blocked = ask(mem, q, 1, assistant);
    expect(blocked.mode).toBe('withheld');
    expect(blocked.text).not.toContain('Palm');
    expect(blocked.value).toBeNull();
    expect(ask(mem, q, 1, everything).text).toContain('12 Palm Road');
  });

  it('does not leak facts from scopes outside its context', () => {
    const mem = new MemoryEngine();
    mem.write({ scope: 'project:atlas', subject: 'project', predicate: 'deadline', value: 'Oct 10', sourceType: 'stated', now: 0 });
    const a = ask(mem, { scope: 'project:atlas', subject: 'project', predicate: 'deadline' }, 1, assistant);
    expect(a.mode).toBe('unknown');
  });
});
