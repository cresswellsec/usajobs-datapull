"""Shared test helpers: a deterministic fake embedder (no model download)."""
import hashlib

import numpy as np
import pytest

from usajobs_datapull import embedders, retrieval


class FakeEmbedder:
    """Hashed bag-of-words into 384 dims, L2-normalized. Same text -> same vector;
    texts sharing words -> similar vectors. Good enough to exercise the pipeline."""
    model_id = "test/fake-bow-384"

    def __init__(self):
        self.spec = {**embedders.profile("Xenova/bge-small-en-v1.5"), "model": self.model_id}
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        out = np.zeros((len(texts), 384), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in retrieval.tokenize_v1(t):
                out[i, int(hashlib.md5(tok.encode()).hexdigest(), 16) % 384] += 1.0
        return embedders.normalize_rows(out)

    def count_tokens(self, text):
        return max(1, len(text.split()))


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
