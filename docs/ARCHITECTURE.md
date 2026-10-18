# Architecture

## The problem with plain RAG memory

Most agent "memory" is vector RAG with a timestamp bolted on: embed everything, retrieve by similarity, hand the nearest match to the model as fact. It cannot tell whether something is 6 minutes or 6 months old. It cannot tell "the user told me this directly" from "I guessed this once and never checked". A fact can stop being true only by being overwritten without a trace or drowned out by newer embeddings. It has no notion of who a memory is for, or of topics an assistant should never infer. It can't be wrong, because it never claims a confidence in the first place.

This grew out of a classification pipeline I built at work, where every label needed an answer to the same three questions: how much do I trust it, when does it go stale, and what happens when a rescan disagrees with what's already there? We handled that by versioning inputs and flagging drift instead of silently overwriting. Same idea here, just applied to facts an agent remembers instead of labels a model assigns.

## Components

```
            FastAPI (api.py)                      agent.py
   POST /facts  /retrieve  /ask  /search     retrieval -> assert | hedge | ask | unknown | withheld
   POST /forget /forget/scope  GET /facts           │
                    │                                │
                    └───────────►  MemoryService  ◄──┘     all decisions live here (service.py)
                                       │
                 policy.py (pure rules) │ matching.py + embedders.py (slot similarity)
                                       │
                         Store protocol (store.py)
                        ┌──────────────┴───────────────┐
                  InMemoryStore                   PostgresStore (pg_store.py)
                  (tests, demos)                  pgvector + pg_trgm + advisory locks
```

Stores only persist and filter. Every decision (supersede vs dispute, matching thresholds, sensitivity, tombstones) is in `MemoryService`, so the two stores behave identically; the same test suite and the same conformance fixtures run against both.

## Data model

Every fact is a **claim**, not a value:

| Field | Meaning |
|---|---|
| `scope` | Who or what it applies to: `user`, `project-x`, `shared` |
| `subject`, `predicate`, `value` | The claim |
| `fingerprint`, `slot_base` | Slot identity (scope + subject + predicate; for multi-valued facts the normalized value is appended to the fingerprint) |
| `slot_text`, `embedding` | Normalized "subject predicate" text and its vector, used for slot matching |
| `source_type` | `stated`, `tool_output` or `inferred` |
| `volatility` | `durable` or `volatile` (how fast this kind of fact changes) |
| `sensitivity` | `normal` or `sensitive` |
| `cardinality` | `single` or `multi` |
| `confidence` | Base trust, 0-100, at write or last confirmation |
| `created_at`, `last_confirmed_at`, `ttl_days` | Freshness |
| `status` | `active`, `disputed` or `contradicted` |
| `supersedes`, `disputes`, `possible_duplicate_of` | Audit and ambiguity links |

There is no stored `stale` status and no `revoked` status, on purpose. **Staleness is derived** (`now - last_confirmed_at > ttl_days`) at read time, so reading memory never changes it. **Revoked facts are erased**, so there is nothing left to give a status to.

| Source | Base confidence | Durable TTL (days) | Volatile TTL (days) |
|---|---|---|---|
| stated | 90 | 180 | 21 |
| tool_output | 75 | 60 | 10 |
| inferred | 50 | 30 | 5 |

Past its TTL a fact loses 2 confidence points per day (linear), floored at 10. A stale fact is "maybe wrong", never "definitely wrong". Bands: high at 75 or more, medium at 40 or more, low below. These numbers are hand-set; see Known limits.

## Slot matching (hybrid)

A write has to recognize "this is about something I already believe", even when the slot is named differently (`home_city`, `Home City`, `city_of_residence`). Exact matching misses those, and a missed match is the worst failure here: the conflict is never detected and the memory confidently answers from whichever slot is asked about.

For single-valued slots, a write that doesn't hit an exact slot looks for the nearest slot **in the same scope** with a hybrid score:

```
score = 0.7 x cosine(embedding) + 0.3 x trigram_similarity(slot_text)
```

It is a weighted sum, not rank fusion, because the decision needs a calibrated absolute score to compare with thresholds. `trigram_similarity` reproduces PostgreSQL `pg_trgm.similarity`; a test checks the Python implementation against the database, and another checks that both stores return the same scores.

| Score | Action |
|---|---|
| >= `match_threshold` (0.70) | Join the existing slot: inherit its name and embedding, take the stricter sensitivity, and re-run the sensitive-inference check |
| >= `uncertain_threshold` (0.55) | Keep a separate slot but record `possible_duplicate_of`; retrieval mentions the similar slot instead of silently ignoring it |
| below | New slot |

Multi-valued slots get no semantic matching. Retrieval and revoke use the same resolution, so asking for "Home City" finds `home_city`, and "forget my home city" erases it (both names are tombstoned).

Metadata filters (scope, status, sensitivity, cardinality) are pushed into the SQL `WHERE` clause, not applied after the search.

The default embedder is a deterministic feature-hashing embedder (words plus character trigrams, 256 dimensions, no downloads). It is **lexical**: it handles formatting, case and spelling variants but not paraphrase. A semantic embedder can be plugged in (`EMBEDDER=st:...`); it is untested here. `evals/REPORT.md` has the measured numbers and the failure cases.

## Write path

1. **Validate.** Empty input is rejected.
2. **Resolve the slot** (exact first, then hybrid matching for single-valued facts).
3. **Sensitivity gate.** The predicate is checked against a keyword list (health, address, income, religion and so on), which raises sensitivity automatically. A sensitive fact that is `inferred` is **rejected**.
4. **Tombstone gate.** If the slot was revoked, anything that is not `stated` is rejected. A stated write lifts the tombstone.
5. **Look up** the active fact in the slot.
6. **No match:** create it with the source's base confidence.
7. **Same value:** reconfirm, no new row. Any source refreshes `last_confirmed_at`. A *stronger* source upgrades the fact (the user saying "yes, that's right" turns an inference into a stated fact, with the stated TTL). An equal source adds 5 points. A weaker source never lowers trust. A stated reconfirmation also settles open disputes.
8. **Different value:** compare the incoming base confidence with the existing fact's *current, decayed* confidence.
   - **Incoming is at least as trusted:** **supersede.** The old fact becomes `contradicted` (kept for audit), the new fact links back via `supersedes`, and pending disputes are retired.
   - **Incoming is less trusted:** **dispute.** The weaker claim is stored as `disputed`, the existing fact stays active, nothing is overwritten. A repeat of the same weak claim refreshes the dispute.

Step 8 is the overconfidence defence: a guess can only displace a fact once that fact has decayed below the guess, which is when it should.

## Retrieval path

Retrieval never returns a bare value, and never mutates state. It returns the fact plus decayed confidence and band, freshness, history (what it replaced, and when), disputes, similar slots that were not merged, and a plain-language note. **Scope** and **sensitivity** are enforced here: a fact outside the caller's scopes behaves as if it does not exist, and a sensitive fact is reported as *withheld* so the caller knows not to guess.

## The agent policy layer

`agent.py` turns a retrieval into behaviour, which makes the "I might be wrong" moment an action instead of a label:

| Situation | Mode | What the agent does |
|---|---|---|
| Two claims disagree | `ask` | States both, says which it leans towards, asks which is right |
| Stale, or low confidence | `ask` | Says how long since it was confirmed, asks "is that still right?" |
| Medium confidence | `hedge` | Answers but says it is not certain, with source and percentage |
| High confidence | `assert` | Answers plainly, and mentions if the answer recently changed |
| Sensitive, not allowed here | `withheld` | Declines to use it in this context |
| Nothing known (or forgotten) | `unknown` | Says so; forgotten and never-known are indistinguishable |

Every answer also carries an *evidence line*: what it relies on, the source, first recorded and last confirmed dates, and confidence. Reply text is templated so behaviour is deterministic and testable; swap in an LLM for wording and keep `mode` and `evidence`.

## Forgetting path

Four outcomes here, kept separate on purpose instead of collapsed into one "delete":

- **Stale:** TTL exceeded, no reconfirmation. Derived, not stored. Confidence decays and the agent asks. Reversible: any reconfirmation clears it.
- **Disputed:** a weaker claim disagrees. Both are kept until the user settles it.
- **Contradicted:** a newer, at-least-as-trusted fact replaced it. Retired from normal retrieval, kept as an audit trail.
- **Revoked:** an explicit "forget that". The active fact, superseded history and disputed claims in that slot are **deleted**. Only a one-way **tombstone marker** remains, which stops inference or a tool re-learning the slot. Revoking a slot the memory never held still adds the marker. `forget_scope` does the same for a whole scope.

Revocation is the privacy control, so it means erase, not hide. Tests assert that revoked values appear nowhere in stored facts, and the Postgres test counts rows. A tombstone is `HMAC-SHA256(TOMBSTONE_KEY, slot)`: it holds no value, and the key means a stolen database alone can't be used to confirm guesses like `home_address`.

## Postgres specifics

- One `facts` table and one `tombstones` table (`schema.sql`).
- A **partial unique index** (`UNIQUE (fingerprint) WHERE status = 'active'`) makes "at most one active fact per slot" a database guarantee. Disputed and contradicted rows share the fingerprint as the audit trail.
- Writes run in a transaction holding `pg_advisory_xact_lock(hashtext(scope))`, so concurrent writers to a scope are serialized. A test starts 8 concurrent writers to one slot and asserts exactly one active fact.
- The `vector(N)` dimension is checked at startup; a mismatch with the embedder refuses to start instead of corrupting search.
- No ANN index on purpose: slot names per scope are few, an exact scan is fast, and scores stay deterministic. Add HNSW if a scope grows to many thousands of slots.
- No migrations tool: the schema is `CREATE ... IF NOT EXISTS`. Changing the embedder dimension needs a new database or a manual re-embed.

## The browser playground

`web/` is an earlier, dependency-free TypeScript port of the core policy (exact slot matching only) bundled into a single-file demo page, so the behaviour can be tried without running a server. `fixtures/conformance.json` is run by both the Python tests and the playground's tests, so the two implementations can't drift apart silently on the behaviour they share. Semantic slot matching, Postgres and the API exist only in the Python service.

## Known limits

- **Embedder.** The default is lexical. Paraphrases (`employer` vs `works_at`) are mostly missed; see `evals/REPORT.md`. Thresholds were tuned on a small hand-labelled set (52 pairs), so they are a starting point, not a result.
- **Tombstones match slots, not meanings.** Revoking via a paraphrase that doesn't clear the match threshold leaves the original slot's tombstone unmatched, so a differently named slot can re-learn the fact.
- **Sensitivity is a keyword heuristic**, not a classifier. It errs towards flagging; callers can mark any fact sensitive.
- **No authentication.** The access context comes from the request. A real deployment derives it from the caller's identity at a gateway.
- **Copies outside the store.** Revoke can't recall logs, backups or text a model has already seen.
- **Hand-set numbers.** TTLs, base confidences and decay are not learned from data.
- **Single-writer scaling.** Per-scope advisory locks are fine for an agent memory, not a high-throughput write path.

## Where this goes next

Learn TTLs per fact type from how often that type is contradicted instead of using a static table; evaluate semantic embedders and a reranker on a larger labelled set; derive access contexts from real authentication; add a read cache (Redis) if retrieval volume demands it.
