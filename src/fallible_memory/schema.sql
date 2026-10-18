-- fallible-memory schema. __DIM__ is replaced with the embedder's dimension.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS facts (
    id                    text PRIMARY KEY,
    scope                 text NOT NULL,
    scope_key             text NOT NULL,           -- normalized scope, used for filtering
    subject               text NOT NULL,
    predicate             text NOT NULL,
    value                 text NOT NULL,
    fingerprint           text NOT NULL,
    slot_base             text NOT NULL,
    slot_text             text NOT NULL,
    source_type           text NOT NULL CHECK (source_type IN ('stated', 'tool_output', 'inferred')),
    volatility            text NOT NULL CHECK (volatility IN ('durable', 'volatile')),
    sensitivity           text NOT NULL CHECK (sensitivity IN ('normal', 'sensitive')),
    cardinality           text NOT NULL CHECK (cardinality IN ('single', 'multi')),
    confidence            integer NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    created_at            timestamptz NOT NULL,
    last_confirmed_at     timestamptz NOT NULL,
    ttl_days              double precision NOT NULL CHECK (ttl_days > 0),
    status                text NOT NULL CHECK (status IN ('active', 'disputed', 'contradicted')),
    supersedes            text,
    disputes              text,
    possible_duplicate_of text,
    retired_at            timestamptz,
    retired_reason        text CHECK (retired_reason IN ('superseded', 'rejected', 'replaced')),
    embedding             vector(__DIM__)
);

-- Invariant: at most one ACTIVE fact per slot identity. Disputed and contradicted
-- rows may share a fingerprint with it; they are the audit trail.
CREATE UNIQUE INDEX IF NOT EXISTS facts_one_active_per_fp ON facts (fingerprint) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS facts_slot_base_idx ON facts (slot_base);
CREATE INDEX IF NOT EXISTS facts_fingerprint_idx ON facts (fingerprint);
CREATE INDEX IF NOT EXISTS facts_scope_status_idx ON facts (scope_key, status);
CREATE INDEX IF NOT EXISTS facts_dup_idx ON facts (possible_duplicate_of) WHERE possible_duplicate_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS facts_slot_text_trgm ON facts USING gin (slot_text gin_trgm_ops);
-- No ANN index on purpose: slot names per scope are few, so an exact scan is fast and
-- keeps the match scores deterministic. Add HNSW if a scope grows to many thousands.

-- What remains after a revoke: a keyed one-way marker, never the value.
CREATE TABLE IF NOT EXISTS tombstones (
    marker     text PRIMARY KEY,
    revoked_at timestamptz NOT NULL
);
