# Failure tests: the "I might be wrong" moments

Each scenario is a case where the memory is wrong, or might be, and the system has to know it, say so and handle it: not silently overwrite and not silently serve stale data. Names are fictional and days are simulated.

```bash
python examples/walkthrough.py   # prints the output below, offline
pytest                           # asserts every behaviour described here
```

The output below is the real output of `examples/walkthrough.py`.

## 1. Contradiction: the user changes employer

Same slot, different value, and the incoming claim (stated, 90) is at least as trusted as the existing one (90), so the write supersedes. The old fact becomes `contradicted` and is kept for audit; the new fact links back to it. The agent says it changed its mind, and when. Plain vector RAG would return whichever embedding scored higher, with no hint that either was ever wrong.

## 2. Stale: an inferred deadline nobody confirmed

A fact is never deleted for being old (it might still be right) but is never served as settled truth past its shelf life. The agent acts on that: it asks. When the user confirms, the inference is upgraded to a stated fact.

This scenario guards a real bug from an earlier draft: retrieving a stale fact used to flip its stored status, after which every later lookup said "no memory". Staleness is now derived at read time; a test asserts that asking leaves the stored fact unchanged.

## 3. Conflict: a weak guess disagrees with a strong fact

An earlier draft let any different value retire the existing fact, so a guess could overwrite something the user said. Now the incoming baseline is compared with the existing fact's current confidence, so the guess is stored as a disputed claim and the strong fact stays active. The guess can win only once the strong fact has decayed below it, which is when it should.

## 4. Same slot, different wording

`Home City` written as an inference after `home_city` was stated is recognized as the same slot by hybrid matching, so the conflict is caught instead of creating a second, unrelated-looking fact. This is the case exact matching gets wrong. Paraphrases such as `works_at` vs `employer` are *not* reliably caught by the default embedder; see `evals/REPORT.md` for numbers.

## 5. Privacy: sensitive topics and revocation

- Inference is blocked for sensitive topics, so the assistant cannot build a profile of someone's health, address or income from behaviour.
- Scope and sensitivity gate retrieval; a restricted requester gets "withheld", not a guess.
- Revocation erases the values. Only a keyed one-way marker remains so nothing can quietly re-learn the slot.

## Transcript

```text

=== 1. Contradiction: the user changes employer ===
day 0   write employer='Acme Robotics' [stated] -> created  slot:new
day 12  write employer='Globex Corp' [stated] -> superseded  slot:exact
day 14  ask 'employer'
  [assert] Globex Corp. (This changed on 2026-01-13; it used to be "Acme Robotics".)
  Relying on "Globex Corp": you told me this; first recorded 2026-01-13, last confirmed 2026-01-13; 90% confidence.

=== 2. Stale: an inferred deadline nobody confirmed ===
day 0   write deadline='Oct 10' [inferred] -> created  slot:new
day 3   ask 'deadline'
  [hedge] I think it's "Oct 10", but I'm not certain: I inferred this, 50% confidence.
  Relying on "Oct 10": I inferred this; first recorded 2026-01-01, last confirmed 2026-01-01; 50% confidence.
day 40  ask 'deadline'
  [ask] I have "Oct 10" for deadline, but I haven't confirmed it in 40 days (it's normally good for 5 days). Is that still right?
  Relying on "Oct 10": I inferred this; first recorded 2026-01-01, last confirmed 2026-01-01; 10% confidence.
day 40  write deadline='Oct 10' [stated] -> reconfirmed  slot:exact
day 41  ask 'deadline'
  [assert] Oct 10.
  Relying on "Oct 10": you told me this; first recorded 2026-01-01, last confirmed 2026-02-10; 90% confidence.

=== 3. Conflict: a weak guess disagrees with a strong fact ===
day 0   write employer='Acme Robotics' [stated] -> created  slot:new
day 3   write employer='Hooli' [inferred] -> disputed  slot:exact
day 4   ask 'employer'
  [ask] I have two conflicting answers for employer: "Acme Robotics" (you told me this) and "Hooli" (I inferred this). I'm leaning towards "Acme Robotics", but which one is right?
  Relying on "Acme Robotics": you told me this; first recorded 2026-01-01, last confirmed 2026-01-01; 90% confidence.
day 5   write employer='Acme Robotics' [stated] -> reconfirmed  slot:exact
day 6   ask 'employer'
  [assert] Acme Robotics.
  Relying on "Acme Robotics": you told me this; first recorded 2026-01-01, last confirmed 2026-01-06; 95% confidence.

=== 4. Same slot, different wording: the conflict is still caught ===
day 0   write home_city='Manama' [stated] -> created  slot:new
day 2   write Home City='Dubai' [inferred] -> disputed  slot:semantic
day 3   ask 'home city'
  [ask] I have two conflicting answers for home city: "Manama" (you told me this) and "Dubai" (I inferred this). I'm leaning towards "Manama", but which one is right?
  Relying on "Manama": you told me this; first recorded 2026-01-01, last confirmed 2026-01-01; 90% confidence.

=== 5. Privacy: sensitive topics and revocation ===
day 0   write medical_condition='asthma' [inferred] -> rejected ("medical_condition" is a sensitive topic. Sensitive facts can only be stated by the user, never inferred.)  slot:new
day 0   write home_address='12 Palm Road' [stated] -> created  slot:new
day 1   ask 'home_address'
  [withheld] I may have something on that, but it's marked sensitive and I'm not using it in this context.
day 1   ask 'home_address'
  [assert] 12 Palm Road.
  Relying on "12 Palm Road": you told me this; first recorded 2026-01-01, last confirmed 2026-01-01; 90% confidence.
day 2   forget home_address -> 1 record(s) erased, 1 tombstone marker kept
'12 Palm Road' anywhere in stored facts? False
day 3   write home_address='12 Palm Road' [tool_output] -> rejected (The user asked me to forget "home_address". Only the user can tell me this again.)  slot:new
```

## Tests

| Scenario | Where |
|---|---|
| 1-3, 5 | `tests/test_conformance.py` (shared fixtures, in-memory and Postgres), `tests/test_service.py` |
| 4 | `tests/test_service.py` (hybrid slot matching), `evals/` |
| Database invariants | `tests/test_pg.py` (unique active fact, concurrent writers, no values left after revoke, pg_trgm parity) |
| API | `tests/test_api.py` |
| Playground parity | `web/test/conformance.test.ts` |
