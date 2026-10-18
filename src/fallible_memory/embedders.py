"""Embedders for slot matching.

``HashingEmbedder`` is the dependency-free default: deterministic, offline, and
*lexical* (it matches shared words and character n-grams, not meaning). It makes
tests and CI reproducible. For real paraphrase matching ("works at" vs
"employer") use a semantic model through ``SentenceTransformerEmbedder``.
"""

import hashlib
import re
from typing import Protocol, Sequence

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one unit-length vector per input text."""
        ...


class HashingEmbedder:
    """Feature-hashing embedder over words and character trigrams."""

    def __init__(self, dim: int = 256, char_weight: float = 0.5) -> None:
        self.dim = dim
        self.char_weight = char_weight
        self.name = f"hashing-{dim}"

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        n = int.from_bytes(digest, "big")
        return n % self.dim, 1.0 if (n >> 63) & 1 else -1.0

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            idx, sign = self._bucket(f"w:{word}")
            vec[idx] += sign
            padded = f"^{word}$"
            for i in range(len(padded) - 2):
                idx, sign = self._bucket(f"c:{padded[i:i + 3]}")
                vec[idx] += sign * self.char_weight
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm else vec

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class SentenceTransformerEmbedder:
    """Optional semantic embedder (``pip install 'fallible-memory[ml]'``).

    Downloads the model on first use. Not exercised by the default test suite.
    """

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # lazy: heavy import

        self._model = SentenceTransformer(model)
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.name = f"st:{model}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


def make_embedder(spec: str | None = None) -> Embedder:
    """``hashing`` (default), ``hashing:512``, or ``st:<model name>``."""
    if not spec or spec == "hashing":
        return HashingEmbedder()
    if spec.startswith("hashing:"):
        return HashingEmbedder(dim=int(spec.split(":", 1)[1]))
    if spec.startswith("st:"):
        model = spec.split(":", 1)[1]
        return SentenceTransformerEmbedder(model) if model else SentenceTransformerEmbedder()
    raise ValueError(f"Unknown embedder spec: {spec!r}")
