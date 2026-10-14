"""Reference retrieval: the exact search rules in docs/BUNDLE.md, in Python.

The bundle stage uses this to generate conformance.json, and to check its own
output by reading the written files back. The apps implement the same rules
(in Python and TypeScript) and must reproduce conformance.json.

  tokenizer v1   NFKC, lowercase, [a-z0-9]+ tokens, plus joined compounds
                 ("gs-13" -> gs, 13, gs13; "ts/sci" -> ts, sci, tssci)
  BM25           k1=1.2, b=0.75, idf = ln(1 + (N - df + 0.5) / (df + 0.5)),
                 query terms de-duplicated, only score > 0 is a hit
  dense          dot product of the normalized query vector with dequantized rows
  ties           broken by row index (earlier row first)
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

import numpy as np

from . import config

_TOKEN = re.compile(r"[a-z0-9]+")
_COMPOUND = re.compile(r"[a-z0-9]+(?:[-/][a-z0-9]+)+")


def tokenize_v1(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    tokens = _TOKEN.findall(text)
    tokens += [re.sub(r"[-/]", "", m) for m in _COMPOUND.findall(text)]
    return tokens


class BM25:
    def __init__(self, texts: list[str], k1: float = config.BM25_K1, b: float = config.BM25_B):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize_v1(t)) for t in texts]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.n = len(self.docs)
        self.avgdl = (sum(self.lengths) / self.n) if self.n and sum(self.lengths) else 1.0
        df = Counter()
        for d in self.docs:
            df.update(d.keys())
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = list(dict.fromkeys(tokenize_v1(query)))
        out = []
        for d, dl in zip(self.docs, self.lengths):
            s = 0.0
            for t in terms:
                tf = d.get(t, 0)
                if tf:
                    s += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out.append(s)
        return out

    def top(self, query: str, k: int) -> list[int]:
        scores = self.scores(query)
        ranked = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (-scores[i], i))
        return ranked[:k]


def quantize(vectors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-row symmetric int8: scale = max|x| / 127, q = round(x / scale)."""
    vectors = np.asarray(vectors, dtype=np.float32)
    scales = np.abs(vectors).max(axis=1) / 127.0
    scales[scales == 0] = 1.0
    q = np.clip(np.rint(vectors / scales[:, None]), -127, 127).astype(np.int8)
    return q, scales.astype(np.float32)


def dequantize(q: np.ndarray, scales: np.ndarray) -> np.ndarray:
    return q.astype(np.float32) * scales[:, None]


def dense_top(query_vec: np.ndarray, matrix: np.ndarray, k: int) -> list[int]:
    scores = matrix @ np.asarray(query_vec, dtype=np.float32)
    order = np.lexsort((np.arange(len(scores)), -scores))   # score desc, then row index
    return [int(i) for i in order[:k]]
