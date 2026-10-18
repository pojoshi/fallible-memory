"""Slot matching: decide whether two slot names mean the same thing.

The score is a *hybrid*: a weighted sum of embedding cosine similarity and
character-trigram similarity. A weighted sum (not rank fusion) is used on purpose,
because the decision needs a calibrated absolute score to compare with thresholds.

``trigram_similarity`` reproduces PostgreSQL ``pg_trgm.similarity`` so the in-memory
store and the SQL store agree (a test checks the two against each other).
"""

import re
from typing import Sequence

W_VECTOR = 0.7
W_TEXT = 0.3

_WORD = re.compile(r"[a-z0-9]+")


def _trigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for word in _WORD.findall(text.lower()):
        padded = f"  {word} "
        for i in range(len(padded) - 2):
            grams.add(padded[i : i + 3])
    return grams


def trigram_similarity(a: str, b: str) -> float:
    ga, gb = _trigrams(a), _trigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, clipped to [0, 1]. Embedders return unit vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def fused_score(vector_similarity: float, text_similarity: float) -> float:
    return W_VECTOR * vector_similarity + W_TEXT * text_similarity
