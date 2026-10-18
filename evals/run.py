"""Custom evals for slot matching and retrieval. Writes evals/REPORT.md.

    python evals/run.py [--embedder hashing|st:<model>] [--match 0.8] [--uncertain 0.6] [--check]

Three evals:
  1. Slot pairs: do two slot names mean the same fact? Precision/recall across a threshold sweep.
  2. Conflict detection, end to end through MemoryService: does a conflicting claim written
     under a variant slot name get detected (vs. an exact-match-only baseline), and do
     different slots stay separate?
  3. Retrieval: free-text query -> which slot ranks first. Hybrid vs vector-only vs trigram-only.
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fallible_memory import InMemoryStore, MemoryService, make_embedder
from fallible_memory import policy as P
from fallible_memory.matching import cosine, fused_score, trigram_similarity

HERE = Path(__file__).parent
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def load(name):
    return json.loads((HERE / name).read_text())


def slot_text(pred: str) -> str:
    return P.slot_text_of("user", pred)


# ------------------------------------------------------------------ eval 1

def score_pairs(emb, pairs):
    texts = sorted({slot_text(p[k]) for p in pairs for k in ("a", "b")})
    vecs = dict(zip(texts, emb.embed(texts)))
    out = []
    for p in pairs:
        a, b = slot_text(p["a"]), slot_text(p["b"])
        v, t = cosine(vecs[a], vecs[b]), trigram_similarity(a, b)
        out.append({**p, "vec": v, "txt": t, "score": fused_score(v, t)})
    return out


def prf(scored, threshold):
    tp = sum(1 for s in scored if s["same"] and s["score"] >= threshold)
    fp = sum(1 for s in scored if not s["same"] and s["score"] >= threshold)
    fn = sum(1 for s in scored if s["same"] and s["score"] < threshold)
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1, tp, fp, fn


# ------------------------------------------------------------------ eval 2

def conflict_eval(emb, pairs, match, uncertain, semantic):
    """Positives: stated fact under name A, then a conflicting *inferred* value under name B.
    Detected = outcome 'disputed'. Missed = 'created' (a silent duplicate slot).
    Negatives: two genuinely different slots must both be 'created' (no interaction)."""
    detected = missed = false_merge = clean = uncertain_flag = 0
    for p in pairs:
        svc = MemoryService(InMemoryStore(), emb, match_threshold=match, uncertain_threshold=uncertain, semantic_matching=semantic)
        svc.write(subject="user", predicate=p["a"], value="alpha", source_type="stated", now=NOW)
        r = svc.write(subject="user", predicate=p["b"], value="beta", source_type="inferred", now=NOW)
        if p["same"]:
            if r.outcome == "disputed":
                detected += 1
            else:
                missed += 1
                uncertain_flag += r.slot_match.kind == "uncertain"
        else:
            if r.outcome == "created":
                clean += 1
            else:
                false_merge += 1
    return dict(detected=detected, missed=missed, false_merge=false_merge, clean=clean, flagged_uncertain=uncertain_flag)


# ------------------------------------------------------------------ eval 3

def retrieval_eval(emb, data):
    svc = MemoryService(InMemoryStore(), emb)
    for pred in data["slots"]:
        svc.write(subject="user", predicate=pred, value="x", source_type="stated", now=NOW)
    modes = {"hybrid": lambda c: c.score, "vector only": lambda c: c.vector_similarity, "trigram only": lambda c: c.text_similarity}
    res = {m: defaultdict(lambda: [0, 0, 0.0]) for m in modes}  # category -> [n, top1, rr_sum]
    for q in data["queries"]:
        hits = svc.search(q["q"], limit=len(data["slots"]))
        for m, key in modes.items():
            ranked = sorted(hits, key=lambda c: (-key(c), c.fact.predicate))
            # A tie at zero means "no signal": count as not retrieved rather than lucky alphabetical order.
            rank = next((i + 1 for i, c in enumerate(ranked) if c.fact.predicate == q["expect"] and key(c) > 0), None)
            for cat in (q["category"], "ALL"):
                row = res[m][cat]
                row[0] += 1
                row[1] += rank == 1
                row[2] += 1 / rank if rank else 0.0
    return res


# ------------------------------------------------------------------ report

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", default="hashing")
    ap.add_argument("--match", type=float, default=0.70)
    ap.add_argument("--uncertain", type=float, default=0.55)
    ap.add_argument("--check", action="store_true", help="exit non-zero if hybrid regresses below the baseline")
    ap.add_argument("--out", default=str(HERE / "REPORT.md"))
    args = ap.parse_args()

    emb = make_embedder(args.embedder)
    pairs = load("slot_pairs.json")["pairs"]
    rq = load("retrieval_queries.json")
    scored = score_pairs(emb, pairs)
    pos, neg = [s for s in scored if s["same"]], [s for s in scored if not s["same"]]

    L = []
    L += ["# Eval report", "",
          f"Embedder: `{emb.name}` (dim {emb.dim}). Fused score = 0.7 x cosine + 0.3 x trigram similarity. "
          f"Generated by `python evals/run.py`; do not edit by hand.", ""]
    if args.embedder.startswith("hashing"):
        L += ["> The default hashing embedder is **lexical**: it matches shared words and character n-grams, not meaning. "
              "These numbers measure formatting and spelling robustness, and show where paraphrase matching fails, "
              "not what a semantic embedder would do. "
              "Re-run with `--embedder st:sentence-transformers/all-MiniLM-L6-v2` (needs the `ml` extra and a model download) "
              "to measure that.", ""]
    L += [f"Labelled data: {len(pos)} same-slot pairs, {len(neg)} different-slot pairs, {len(rq['queries'])} retrieval queries "
          "(small and hand-written, so treat differences of one or two items as noise).", ""]

    # 1
    L += ["## 1. Slot-pair matching: threshold sweep", "",
          "| threshold | precision | recall | F1 | TP | FP | FN |", "|---|---|---|---|---|---|---|"]
    best = None
    for th in [x / 100 for x in range(40, 100, 5)]:
        p, r, f1, tp, fp, fn = prf(scored, th)
        L.append(f"| {th:.2f} | {p:.2f} | {r:.2f} | {f1:.2f} | {tp} | {fp} | {fn} |")
        if p >= 0.95 and (best is None or r > best[1]):
            best = (th, r)
    L += [""]
    L += [f"Highest-recall threshold with precision >= 0.95: **{best[0]:.2f}** (recall {best[1]:.2f})." if best
          else "No threshold reaches precision 0.95.", ""]
    L += [f"Configured: match >= {args.match:.2f} (auto-merge), {args.uncertain:.2f} <= score < {args.match:.2f} (kept separate, flagged `possible_duplicate_of`).", ""]
    L += ["### Per category at the configured match threshold", "", "| category | n | correctly handled | mean score |", "|---|---|---|---|"]
    by = defaultdict(list)
    for s in scored:
        by[s["category"]].append(s)
    for cat, items in by.items():
        ok = sum((s["score"] >= args.match) == s["same"] for s in items)
        L.append(f"| {cat} | {len(items)} | {ok}/{len(items)} | {sum(s['score'] for s in items) / len(items):.2f} |")
    L += [""]
    misses = [s for s in scored if (s["score"] >= args.match) != s["same"]]
    if misses:
        L += ["Misclassified pairs:", ""]
        L += [f"- `{s['a']}` / `{s['b']}` ({s['category']}): score {s['score']:.2f}, expected {'same' if s['same'] else 'different'}" for s in misses]
        L += [""]

    # 2
    hy = conflict_eval(emb, pairs, args.match, args.uncertain, True)
    ex = conflict_eval(emb, pairs, args.match, args.uncertain, False)
    L += ["## 2. End-to-end conflict detection", "",
          "A stated fact is written under name A, then a conflicting inferred value under name B. "
          "The right behaviour for a same-slot pair is a recorded dispute; a miss means the memory now holds two "
          "unrelated-looking claims and will confidently answer from whichever slot is asked about.", "",
          "| | conflicts detected | conflicts missed | different slots wrongly merged |", "|---|---|---|---|",
          f"| exact-slot baseline | {ex['detected']}/{len(pos)} | {ex['missed']} | {ex['false_merge']}/{len(neg)} |",
          f"| hybrid matching | {hy['detected']}/{len(pos)} | {hy['missed']} | {hy['false_merge']}/{len(neg)} |", "",
          f"Of the {hy['missed']} conflicts the hybrid matcher missed, {hy['flagged_uncertain']} were flagged as a possible duplicate "
          "(shown in the retrieval note) rather than silently ignored.", ""]

    # 3
    res = retrieval_eval(emb, rq)
    cats = ["lexical", "partial", "paraphrase", "ALL"]
    L += ["## 3. Retrieval: which slot does a free-text query find?", "",
          "Top-1 accuracy / MRR. A query with no signal at all (every score 0) counts as not retrieved.", "",
          "| ranking | " + " | ".join(cats) + " |", "|---|" + "---|" * len(cats)]
    for m, table in res.items():
        cells = []
        for c in cats:
            n, top1, rr = table[c]
            cells.append(f"{top1}/{n} / {rr / n:.2f}")
        L.append(f"| {m} | " + " | ".join(cells) + " |")
    L += [""]
    if args.embedder.startswith("hashing"):
        L += ["With the hashing embedder both signals are lexical, so hybrid is about equal to either alone. "
              "Fusion can only show its value with a semantic embedder, which has not been measured here.", ""]

    Path(args.out).write_text("\n".join(L) + "\n")
    print("\n".join(L))

    if args.check:
        ok = hy["detected"] >= ex["detected"] and hy["false_merge"] <= max(ex["false_merge"], 1)
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
