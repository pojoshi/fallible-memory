/**
 * Runs the four failure scenarios from docs/FAILURE_TESTS.md against the real
 * engine and prints what happens. `npm run scenarios`
 */
import { ask } from '../src/agent';
import { MemoryEngine } from '../src/memory-engine';

const assistant = { scopes: ['user', 'project:atlas', 'shared'], includeSensitive: false };
const line = (s = '') => console.log(s);
const h = (s: string) => { line(); line(`== ${s}`); };
const say = (label: string, a: ReturnType<typeof ask>) => {
  line(`  ${label}`);
  line(`    [${a.mode}] ${a.text}`);
  if (a.evidence) line(`    ${a.evidence}`);
};

h('1. Contradiction: the user changes employer');
{
  const mem = new MemoryEngine();
  mem.write({ subject: 'user', predicate: 'employer', value: 'Acme Robotics', sourceType: 'stated', now: 0 });
  const r = mem.write({ subject: 'user', predicate: 'employer', value: 'Globex Corp', sourceType: 'stated', now: 12 });
  line(`  day 12 write -> ${r.outcome}; "${r.contradicted?.value}" retired, kept for audit`);
  say('day 14 ask', ask(mem, { subject: 'user', predicate: 'employer' }, 14, assistant));
}

h('2. Stale: an inferred deadline nobody confirmed');
{
  const mem = new MemoryEngine();
  const slot = { scope: 'project:atlas', subject: 'project', predicate: 'deadline' };
  mem.write({ ...slot, value: 'Oct 10', sourceType: 'inferred', volatility: 'volatile', now: 0 });
  say('day 3 ask', ask(mem, slot, 3, assistant));
  say('day 40 ask', ask(mem, slot, 40, assistant));
  say('day 40 ask again (still there, not silently dropped)', ask(mem, slot, 40, assistant));
  const r = mem.write({ ...slot, value: 'Oct 10', sourceType: 'stated', volatility: 'volatile', now: 40 });
  line(`  user confirms -> ${r.outcome}`);
  say('day 41 ask', ask(mem, slot, 41, assistant));
}

h('3. Conflict: a weak guess disagrees with a strong fact');
{
  const mem = new MemoryEngine();
  const slot = { subject: 'user', predicate: 'employer' };
  mem.write({ ...slot, value: 'Acme Robotics', sourceType: 'stated', now: 0 });
  const r = mem.write({ ...slot, value: 'Hooli', sourceType: 'inferred', now: 3 });
  line(`  day 3 inferred write -> ${r.outcome} (existing fact stays active)`);
  say('day 4 ask', ask(mem, slot, 4, assistant));
  mem.write({ ...slot, value: 'Acme Robotics', sourceType: 'stated', now: 5 });
  say('user restates the original on day 5', ask(mem, slot, 6, assistant));
}

h('4. Privacy: sensitive topics and revocation');
{
  const mem = new MemoryEngine();
  const inferred = mem.write({ subject: 'user', predicate: 'medical_condition', value: 'knee injury', sourceType: 'inferred', now: 0 });
  line(`  inferred sensitive fact -> ${inferred.outcome}: ${inferred.reason}`);

  mem.write({ subject: 'user', predicate: 'home_address', value: '12 Palm Road', sourceType: 'stated', now: 0 });
  say('assistant context asks for the address', ask(mem, { subject: 'user', predicate: 'home_address' }, 1, assistant));
  say('user explicitly asks', ask(mem, { subject: 'user', predicate: 'home_address' }, 1, { includeSensitive: true }));

  const rev = mem.revoke({ subject: 'user', predicate: 'home_address', now: 2 });
  line(`  user says "forget that" -> ${rev.removed} record(s) erased, ${mem.tombstoneCount()} tombstone marker kept`);
  line(`  "12 Palm Road" still present anywhere in stored state? ${mem.serialize().includes('Palm')}`);
  const back = mem.write({ subject: 'user', predicate: 'home_address', value: '12 Palm Road', sourceType: 'tool_output', now: 3 });
  line(`  a tool tries to re-learn it -> ${back.outcome}: ${back.reason}`);
}
