// Runs ../../fixtures/conformance.json, the same file the Python suite runs.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ask } from '../src/agent';
import { MemoryEngine, type AccessContext } from '../src/memory-engine';

const here = dirname(fileURLToPath(import.meta.url));
const fixtures = JSON.parse(readFileSync(join(here, '..', '..', 'fixtures', 'conformance.json'), 'utf8'));

describe('conformance fixtures (shared with the Python service)', () => {
  for (const scenario of fixtures.scenarios) {
    it(scenario.name, () => {
      const mem = new MemoryEngine();
      scenario.steps.forEach((step: any, i: number) => {
        const exp = step.expect ?? {};
        const where = `step ${i} (${step.op})`;
        switch (step.op) {
          case 'write': {
            const r = mem.write({
              scope: step.scope, subject: step.subject, predicate: step.predicate, value: step.value,
              sourceType: step.source, volatility: step.volatility, cardinality: step.cardinality, now: step.day,
            });
            expect(r.outcome, where).toBe(exp.outcome);
            if (exp.contradictedValue) expect(r.contradicted?.value, where).toBe(exp.contradictedValue);
            if (exp.againstValue) expect(r.against?.value, where).toBe(exp.againstValue);
            break;
          }
          case 'ask': {
            const ctx: AccessContext = step.ctx ?? { includeSensitive: true };
            const a = ask(mem, { scope: step.scope, subject: step.subject, predicate: step.predicate }, step.day, ctx);
            expect(a.mode, `${where}: ${a.text}`).toBe(exp.mode);
            if ('value' in exp) expect(a.value, where).toBe(exp.value);
            if ('confidence' in exp) expect(a.confidence, where).toBe(exp.confidence);
            if ('text' in exp) expect(a.text, where).toBe(exp.text);
            for (const part of exp.textContains ?? []) expect(a.text, where).toContain(part);
            if (exp.optionValues) expect(a.options.map((o) => o.value), where).toEqual(exp.optionValues);
            break;
          }
          case 'revoke':
            expect(mem.revoke({ scope: step.scope, subject: step.subject, predicate: step.predicate, value: step.value, now: step.day }).removed, where).toBe(exp.removed);
            break;
          case 'forgetScope':
            expect(mem.forgetScope(step.scope, step.day).removed, where).toBe(exp.removed);
            break;
          case 'check': {
            if ('factCount' in step) expect(mem.getAll().length, where).toBe(step.factCount);
            if ('tombstones' in step) expect(mem.tombstoneCount(), where).toBe(step.tombstones);
            const blob = JSON.stringify(mem.toJSON()).toLowerCase();
            for (const needle of step.storeLacks ?? []) expect(blob, `${where}: ${needle}`).not.toContain(needle.toLowerCase());
            break;
          }
          default:
            throw new Error(`unknown op ${step.op}`);
        }
      });
    });
  }
});
