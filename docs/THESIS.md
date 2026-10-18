# Where agent memory goes in the next two years

Most agent memory today is a vector store with a timestamp: retrieve the nearest match, hand it to the model, trust it. That works until the memory is wrong, and it will be, because the world it describes keeps changing. People change jobs, plans slip, decisions reverse. The problem isn't storage or recall. It's that memory has no sense of its own shelf life, and no sense of what it has no business knowing.

In two years, memory that's actually useful will look less like a database and more like a monitored pipeline. Every fact will carry provenance, because something a person says directly can't weigh the same as something a model guessed. Every fact will carry a freshness window, so a stale fact gets flagged before it's used, not served as if new. Disagreement will be first-class: a weak guess doesn't overwrite a strong fact, it gets recorded as a conflict and surfaced. And forgetting won't be one button. Stale, contradicted, and revoked are different problems with different right answers, and revoked has to mean erased, not hidden.

Privacy belongs in the memory layer, not in a system prompt. Scope decides who can see a fact; sensitive topics get stated by the user, never inferred. An assistant that quietly profiles you from your behavior is the real failure mode here, and no prompt fixes that after the fact.

The part nobody's solved well yet is matching: deciding that two differently phrased claims are about the same thing, reliably enough to trigger a conflict check. Exact keys dodge this. Embeddings blur it. Whoever solves it properly, with freshness windows learned per fact type instead of hand-set, is the one who makes memory worth acting on instead of just worth chatting with.
