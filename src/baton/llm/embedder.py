"""Embedders turn text into vectors for retrieval.

`HashingEmbedder` is the default: deterministic, offline and dependency-light (numpy only).
It uses the "hashing trick": each token and token bigram is hashed into one of `dim` buckets
with a +/-1 sign, counts are log-scaled, and the vector is L2-normalised so a dot product is
cosine similarity. It's a bag-of-words model, not semantic, but good enough for a help center.
"""

import hashlib
import itertools
import re
from typing import Protocol

import numpy as np
import numpy.typing as npt

Vector = npt.NDArray[np.float32]

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> Vector:
        """Return a `(len(texts), dim)` float32 array of L2-normalised rows."""
        ...


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class HashingEmbedder:
    def __init__(self, dim: int = 512) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def _bucket(self, feature: str) -> tuple[int, float]:
        # blake2b is stable across processes and platforms (unlike Python's salted hash()).
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little")
        sign = 1.0 if value & 1 else -1.0
        return (value >> 1) % self.dim, sign

    def embed_one(self, text: str) -> Vector:
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = tokenize(text)
        features = tokens + [f"{a} {b}" for a, b in itertools.pairwise(tokens)]
        for feature in features:
            index, sign = self._bucket(feature)
            vec[index] += sign
        vec = np.sign(vec) * np.log1p(np.abs(vec))
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec

    def embed(self, texts: list[str]) -> Vector:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self.embed_one(t) for t in texts]).astype(np.float32)
