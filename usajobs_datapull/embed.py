"""work.db chunks -> embeddings.

Records each chunk's token count and reports any the model would truncate.
Vectors are cached by (model id, hash of the embedded text), so unchanged text
is never re-embedded. The model self-checks before use (embedders.py).
"""
from __future__ import annotations

import hashlib
import logging
import sys

import numpy as np

from . import config, store

log = logging.getLogger("usajobs_datapull.embed")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_chunks(con, chunks: list[dict], embedder) -> dict:
    """Embed every chunk not already cached for this model. Returns counts."""
    prefix = embedder.spec["doc_prefix"]
    texts = {text_hash(prefix + c["text"]): prefix + c["text"] for c in chunks}
    cached = {row[0] for row in con.execute(
        "SELECT text_hash FROM embeddings WHERE model_id = ?", (embedder.model_id,))}
    todo = [(h, t) for h, t in texts.items() if h not in cached]
    log.info("%d chunks, %d unique texts, %d cached, %d to embed", len(chunks), len(texts),
             len(texts) - len(todo), len(todo))

    truncated = 0
    batch = config.EMBED_BATCH * 8
    for start in range(0, len(todo), batch):
        part = todo[start:start + batch]
        vectors = embedder.embed([t for _, t in part])
        rows = []
        for (h, t), v in zip(part, vectors):
            tokens = embedder.count_tokens(t)
            over = tokens > embedder.spec["max_tokens"]
            truncated += over
            rows.append((embedder.model_id, h, int(v.shape[0]), tokens, int(over), v.astype(np.float32).tobytes()))
        with con:
            con.executemany("INSERT OR REPLACE INTO embeddings (model_id, text_hash, dim, tokens, truncated, vector) "
                            "VALUES (?, ?, ?, ?, ?, ?)", rows)
        log.info("embedded %d/%d", min(start + batch, len(todo)), len(todo))
    return {"chunks": len(chunks), "unique_texts": len(texts), "cached": len(texts) - len(todo),
            "embedded": len(todo), "truncated": truncated, "model": embedder.model_id}


def lookup(con, chunks: list[dict], model_id: str, doc_prefix: str) -> tuple[np.ndarray, list[int]]:
    """Vectors (row i = chunk i) and token counts for chunks, from the cache."""
    vectors, tokens = [], []
    for c in chunks:
        row = con.execute("SELECT vector, dim, tokens FROM embeddings WHERE model_id = ? AND text_hash = ?",
                          (model_id, text_hash(doc_prefix + c["text"]))).fetchone()
        if row is None:
            raise LookupError(f"no {model_id} embedding for chunk {c['chunk_id']} -- run `embed` first")
        vectors.append(np.frombuffer(row[0], dtype=np.float32, count=row[1]))
        tokens.append(row[2])
    return (np.vstack(vectors) if vectors else np.zeros((0, 0), np.float32)), tokens


def run() -> None:
    from . import embedders, log as logsetup
    from .fetch import series_list
    logsetup.setup("embed")
    con = store.connect()
    try:
        label = store.resolve_label(con, config.SNAPSHOT)
    except LookupError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
    postings = store.select_postings(con, label, series_list(config.JOB_SERIES))
    chunks = store.chunks_for(con, label, [p["id"] for p in postings])
    if not chunks:
        print(f"no chunks for snapshot {label} -- run `chunk` first", file=sys.stderr)
        raise SystemExit(1)
    run_id = store.start_run(con, "embed", label)
    try:
        embedder = embedders.load()
        counts = embed_chunks(con, chunks, embedder)
    except Exception:
        store.finish_run(con, run_id, {}, status="failed")
        raise
    store.finish_run(con, run_id, counts)
    print(f"embed [{label}] with {counts['model']}")
    for k, v in counts.items():
        if k != "model":
            print(f"  {k:<24}{v}")
    if counts["truncated"]:
        print(f"  WARNING {counts['truncated']} chunk(s) exceed the model's token limit and were truncated")
