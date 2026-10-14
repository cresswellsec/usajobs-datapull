"""Embedding model profiles, run with ONNX Runtime.

Same ONNX weights Transformers.js uses in the browser, so query and document
vectors match. Each profile sets pooling ("cls" or "mean") and query/doc
prefixes; the manifest copies it. A self-check on load (paraphrases must score
closer than an unrelated sentence) catches bad pooling or a broken download.
"""
from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from . import config

log = logging.getLogger("usajobs_datapull.embed")

PROFILES = {
    "Xenova/bge-small-en-v1.5": {
        "dim": 384, "pooling": "cls", "max_tokens": 512, "onnx_file": "onnx/model.onnx",
        "query_prefix": "Represent this sentence for searching relevant passages: ", "doc_prefix": "",
    },
    "Xenova/bge-base-en-v1.5": {
        "dim": 768, "pooling": "cls", "max_tokens": 512, "onnx_file": "onnx/model.onnx",
        "query_prefix": "Represent this sentence for searching relevant passages: ", "doc_prefix": "",
    },
    "Xenova/all-MiniLM-L6-v2": {
        "dim": 384, "pooling": "mean", "max_tokens": 256, "onnx_file": "onnx/model.onnx",
        "query_prefix": "", "doc_prefix": "",
    },
    "Xenova/multilingual-e5-small": {
        "dim": 384, "pooling": "mean", "max_tokens": 512, "onnx_file": "onnx/model.onnx",
        "query_prefix": "query: ", "doc_prefix": "passage: ",
    },
}


def profile(model_id: str) -> dict:
    if model_id not in PROFILES:
        raise ValueError(f"EMBED_MODEL={model_id!r} is not supported. Choose one of: {', '.join(PROFILES)}")
    return {"model": model_id, "runtime": "onnx", **PROFILES[model_id]}


class Embedder(Protocol):
    model_id: str
    spec: dict

    def embed(self, texts: list[str]) -> np.ndarray: ...       # already prefixed; returns normalized rows
    def count_tokens(self, text: str) -> int: ...


def normalize_rows(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (x / norms).astype(np.float32)


def embed_query(embedder: Embedder, text: str) -> np.ndarray:
    return embedder.embed([embedder.spec["query_prefix"] + text])[0]


def self_check(embedder: Embedder) -> None:
    a, b, c = embedder.embed([
        "The cat is sitting on the mat.",
        "A kitten rests on the rug.",
        "Congress passed the annual appropriations bill.",
    ])
    close, far = float(a @ b), float(a @ c)
    if not close > far + 0.02:
        raise RuntimeError(f"embedding self-check failed for {embedder.model_id}: "
                           f"paraphrase {close:.3f} vs unrelated {far:.3f} -- wrong pooling or broken model files")
    log.info("self-check ok: paraphrase %.3f > unrelated %.3f", close, far)


class OnnxEmbedder:
    def __init__(self, model_id: str, cache_dir=None, batch_size: int = config.EMBED_BATCH):
        from huggingface_hub import hf_hub_download
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_id = model_id
        self.spec = profile(model_id)
        cache_dir = str(cache_dir or config.MODELS)
        log.info("loading %s (first run downloads it to %s)", model_id, cache_dir)
        tok_path = hf_hub_download(model_id, "tokenizer.json", cache_dir=cache_dir)
        onnx_path = hf_hub_download(model_id, self.spec["onnx_file"], cache_dir=cache_dir)

        self._counter = Tokenizer.from_file(tok_path)        # honest token counts, even if the file
        self._counter.no_truncation()                         # ships with truncation/padding settings
        self._counter.no_padding()
        self.tokenizer = Tokenizer.from_file(tok_path)
        self.tokenizer.enable_truncation(max_length=self.spec["max_tokens"])
        pad_id = self.tokenizer.token_to_id("[PAD]")
        pad_id = 0 if pad_id is None else pad_id
        self.tokenizer.enable_padding(pad_id=pad_id, pad_token=self.tokenizer.id_to_token(pad_id) or "[PAD]")

        self.session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.batch_size = batch_size

    def count_tokens(self, text: str) -> int:
        return len(self._counter.encode(text).ids)

    def embed(self, texts: list[str]) -> np.ndarray:
        out = []
        for start in range(0, len(texts), self.batch_size):
            enc = self.tokenizer.encode_batch(texts[start:start + self.batch_size])
            feeds = {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in enc], dtype=np.int64),
            }
            hidden = self.session.run(None, {k: v for k, v in feeds.items() if k in self.input_names})[0]
            if hidden.ndim == 2:                       # model already pooled
                pooled = hidden
            elif self.spec["pooling"] == "cls":
                pooled = hidden[:, 0, :]
            else:
                mask = feeds["attention_mask"][..., None].astype(np.float32)
                pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
            out.append(pooled)
        vectors = normalize_rows(np.concatenate(out, axis=0)) if out else np.zeros((0, self.spec["dim"]), np.float32)
        if vectors.shape[1] != self.spec["dim"]:
            raise RuntimeError(f"{self.model_id} produced dim {vectors.shape[1]}, profile says {self.spec['dim']}")
        return vectors


def load(model_id: str | None = None) -> OnnxEmbedder:
    embedder = OnnxEmbedder(model_id or config.EMBED_MODEL)
    self_check(embedder)
    return embedder
