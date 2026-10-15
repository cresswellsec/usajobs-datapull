"""work.db -> dist/bundle/, per docs/BUNDLE.md.

Built in dist/bundle.partial/, verified by reading it back (schemas, checksums,
ids, vector count/dim, int8 cosine >= 0.99, conformance), then swapped in.
Output is deterministic: sorted records and keys, "\\n" line endings.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import config, retrieval, store
from .embed import lookup

SCHEMA_VERSION = "1.1.4"
SCHEMAS = Path(__file__).parent / "schemas"
REPO = Path(__file__).resolve().parent.parent
DOCS = {"CONTRACT.md": REPO / "CONTRACT.md", "BUNDLE.md": REPO / "docs" / "BUNDLE.md"}   # shipped in every bundle
HASHED_EXCLUDE = {"manifest.json", "README.md"}


# --- writing ---------------------------------------------------------------

def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    _write_text(path, "".join(_dumps(r) + "\n" for r in rows))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def facets(postings: list[dict]) -> dict:
    def ranked(counter: Counter) -> list[dict]:
        return [{"value": v, "count": c} for v, c in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))]

    f = {k: Counter() for k in ("agency", "department", "grade", "state", "clearance", "remote",
                                "telework", "hiring_path", "series", "broad_notice")}
    for p in postings:
        for key in ("agency", "department", "clearance"):
            if p[key]:
                f[key][p[key]] += 1
        if p["grade_low"]:
            f["grade"][f"{p['pay_plan'] + '-' if p['pay_plan'] else ''}{p['grade_low']}"] += 1
        for state in {l["state"] for l in p["locations"] if l["state"]}:
            f["state"][state] += 1
        for key in ("remote", "telework", "broad_notice"):
            if p[key] is not None:
                f[key][p[key]] += 1
        for h in set(p["hiring_paths"]):
            f["hiring_path"][h] += 1
        for s in set(p["series"]):
            f["series"][s] += 1
    return {k: ranked(Counter({v: c for v, c in counter.items() if v is not None and c}))
            for k, counter in f.items()}


def conformance(chunks: list[dict], matrix: np.ndarray, embedder) -> dict:
    from .embedders import embed_query
    bm25 = retrieval.BM25([c["text"] for c in chunks])
    k = config.CONFORMANCE_K
    cases = []
    for query, mode in config.CONFORMANCE_QUERIES:
        rows = (bm25.top(query, k) if mode == "lexical"
                else retrieval.dense_top(embed_query(embedder, query), matrix, k))
        cases.append({"query": query, "mode": mode, "expected_top": [chunks[i]["chunk_id"] for i in rows]})
    return {"k": k, "cases": cases}


def dataset_card(manifest: dict, quality: dict) -> str:
    src, emb, counts = manifest["source"], manifest["embedding"], manifest["counts"]
    flags = quality["bundle"]["quality_flags"]
    flag_lines = "\n".join(f"| `{k}` | {v} |" for k, v in sorted(flags.items(), key=lambda kv: -kv[1])) or "| (none) | 0 |"
    return f"""---
license: other
pretty_name: USAJOBS postings (series {", ".join(src["series"])}), cleaned and embedded
tags:
- usajobs
- federal-jobs
- rag
- embeddings
configs:
- config_name: postings
  data_files: postings.jsonl
- config_name: chunks
  data_files: chunks.jsonl
---

# usajobs-postings

Open federal job postings from the [USAJOBS API](https://developer.usajobs.gov/),
occupational series **{", ".join(src["series"])}**, as of **{src["as_of"]}**
(snapshot `{src["snapshot"]}`): cleaned, de-duplicated, chunked and embedded,
ready for retrieval-augmented generation in a server or a browser.

- **{counts["postings"]} postings**, **{counts["chunks"]} chunks**
- embeddings: `{emb["model"]}` ({emb["dim"]}-d, {emb["pooling"]} pooling, L2-normalized, int8 per-row quantized)
- bundle `{manifest["bundle_id"]}`, schema `{manifest["schema_version"]}`

Built by [usajobs-datapull](https://github.com/cresswellsec/usajobs-datapull).
**Building on this data? Read [CONTRACT.md](CONTRACT.md) first**: what goes in,
what comes out, what is promised and what is not.
[BUNDLE.md](BUNDLE.md) is the exact file spec (vector layout, query prefix,
keyword tokenizer, conformance checks).

## Files

| File | Contents |
|---|---|
| `manifest.json` | what this bundle is and how to read it (read first) |
| `postings.jsonl` | one posting per line |
| `chunks.jsonl` | one searchable chunk per line; line *i* = vector row *i* |
| `vectors.int8.bin`, `scales.f32.bin` | embeddings, int8 + per-row float32 scale |
| `facets.json` | filter values and counts |
| `conformance.json` | queries with expected results, for testing a consumer |
| `quality.json` | data-quality summary for this build |
| `schema/` | JSON Schemas for the records and manifest |
| `CONTRACT.md`, `BUNDLE.md` | the data contract and the file spec |

## Data quality flags in this build

| Flag | Postings |
|---|---|
{flag_lines}

Flags mark suspicious data; flagged postings are still included. HR contact
details (emails, phones, named contacts) are deliberately removed.
"""


def build(con, label: str, embedder, series: list[str], out_root: Path, built_at: str | None = None) -> tuple[Path, dict]:
    spec = embedder.spec
    as_of = store.snapshot_date(label)
    postings = store.select_postings(con, label, series)
    if not postings:
        raise LookupError(f"no in-scope postings in {label} for series {series}")
    chunks = store.chunks_for(con, label, [p["id"] for p in postings])
    if not chunks:
        raise LookupError(f"no chunks for {label} -- run `chunk` first")
    vectors, tokens = lookup(con, chunks, embedder.model_id, spec["doc_prefix"])
    q, scales = retrieval.quantize(vectors)
    deq = retrieval.dequantize(q, scales)

    worst = float(np.min(np.sum(deq * vectors, axis=1) /
                         (np.linalg.norm(deq, axis=1) * np.linalg.norm(vectors, axis=1))))
    if worst < 0.99:
        raise AssertionError(f"int8 quantization too lossy: worst cosine {worst:.4f}")

    partial = out_root / "bundle.partial"
    final = out_root / "bundle"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)

    chunk_rows = [{"chunk_id": c["chunk_id"], "posting_id": c["posting_id"], "section": c["section"],
                   "text": c["text"], "tokens": t} for c, t in zip(chunks, tokens)]
    _write_jsonl(partial / "postings.jsonl", postings)
    _write_jsonl(partial / "chunks.jsonl", chunk_rows)
    (partial / "vectors.int8.bin").write_bytes(q.tobytes(order="C"))
    (partial / "scales.f32.bin").write_bytes(scales.astype("<f4").tobytes())
    _write_text(partial / "facets.json", _dumps(facets(postings)) + "\n")
    _write_text(partial / "conformance.json", _dumps(conformance(chunk_rows, deq, embedder)) + "\n")
    (partial / "schema").mkdir()
    for schema in sorted(SCHEMAS.glob("*.schema.json")):
        shutil.copyfile(schema, partial / "schema" / schema.name)
    for name, source in DOCS.items():   # \n line endings on every OS, so the checksum is stable
        _write_text(partial / name, source.read_text(encoding="utf-8").replace("\r\n", "\n"))

    n = len(postings)
    quality = {
        "snapshot": label,
        "normalize": store.last_counts(con, "normalize", label),
        "chunk": store.last_counts(con, "chunk", label),
        "embed": store.last_counts(con, "embed", label),
        "bundle": {
            "postings": n, "chunks": len(chunk_rows),
            "quality_flags": dict(sorted(Counter(f for p in postings for f in p["quality_flags"]).items())),
            "completeness": {f: round(sum(1 for p in postings if p[f] not in (None, [], "")) / n, 3)
                             for f in ("summary", "duties", "qualifications", "salary_annual_min",
                                       "grade_low", "clearance", "remote", "telework", "closes_at")},
            "quantization_worst_cosine": round(worst, 5),
        },
    }
    _write_text(partial / "quality.json", _dumps(quality) + "\n")

    files = {}
    for path in sorted(p for p in partial.rglob("*") if p.is_file()):
        rel = path.relative_to(partial).as_posix()
        files[rel] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    digest = hashlib.sha256("".join(f"{k}:{v['sha256']}\n" for k, v in sorted(files.items())
                                    if k not in HASHED_EXCLUDE).encode()).hexdigest()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": f"{as_of}_{digest[:8]}",
        "built_at": built_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"api": "usajobs", "series": series, "snapshot": label, "as_of": as_of, "status": "open"},
        "counts": {"postings": n, "chunks": len(chunk_rows)},
        "embedding": {**spec, "normalized": True, "dtype": "int8", "quantization": "per_row_symmetric"},
        "chunking": {"strategy": "per_section", "sections": list(store.SECTION_ORDER),
                     "max_words": config.MAX_CHUNK_WORDS,
                     "header_fields": ["title", "agency", "location", "grade"],
                     "boilerplate_removed": True},
        "lexical": {"tokenizer": "v1", "bm25": {"k1": config.BM25_K1, "b": config.BM25_B}},
        "files": files,
    }
    _write_text(partial / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    _write_text(partial / "README.md", dataset_card(manifest, quality))

    verify(partial, embedder)

    if final.exists():
        shutil.rmtree(final)
    os.replace(partial, final)
    return final, manifest


# --- reading back ----------------------------------------------------------

def read(folder: Path) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    postings = [json.loads(l) for l in (folder / "postings.jsonl").read_text(encoding="utf-8").splitlines() if l]
    chunks = [json.loads(l) for l in (folder / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    dim = manifest["embedding"]["dim"]
    q = np.frombuffer((folder / "vectors.int8.bin").read_bytes(), dtype=np.int8)
    scales = np.frombuffer((folder / "scales.f32.bin").read_bytes(), dtype="<f4")
    if q.size != len(chunks) * dim or scales.size != len(chunks):
        raise AssertionError(f"vector files don't match {len(chunks)} chunks x {dim} dims")
    return {"manifest": manifest, "postings": postings, "chunks": chunks,
            "matrix": retrieval.dequantize(q.reshape(len(chunks), dim), scales)}


def verify(folder: Path, embedder=None) -> dict:
    """Check a bundle folder as a consumer would. Raises AssertionError on any problem."""
    from .schema_check import errors as schema_errors
    b = read(folder)
    m = b["manifest"]
    schema_dir = folder / "schema"
    load = lambda name: json.loads((schema_dir / name).read_text(encoding="utf-8"))
    problems = schema_errors(m, load("manifest.schema.json"))
    if problems:
        raise AssertionError(f"manifest fails schema: {problems[0]}")
    for rel, info in m["files"].items():
        path = folder / rel
        if not path.exists() or _sha256(path) != info["sha256"] or path.stat().st_size != info["bytes"]:
            raise AssertionError(f"checksum/size mismatch: {rel}")

    posting_schema, chunk_schema = load("postings.schema.json"), load("chunks.schema.json")
    for p in b["postings"]:
        problems = schema_errors(p, posting_schema)
        if problems:
            raise AssertionError(f"posting {p.get('id')} fails schema: {problems[0]}")
    for c in b["chunks"]:
        problems = schema_errors(c, chunk_schema)
        if problems:
            raise AssertionError(f"chunk {c.get('chunk_id')} fails schema: {problems[0]}")

    ids = [p["id"] for p in b["postings"]]
    if len(ids) != len(set(ids)):
        raise AssertionError("duplicate posting ids")
    cids = [c["chunk_id"] for c in b["chunks"]]
    if len(cids) != len(set(cids)):
        raise AssertionError("duplicate chunk ids")
    orphan = {c["posting_id"] for c in b["chunks"]} - set(ids)
    if orphan:
        raise AssertionError(f"chunks reference missing postings: {sorted(orphan)[:5]}")
    chunkless = set(ids) - {c["posting_id"] for c in b["chunks"]}
    if chunkless:
        raise AssertionError(f"postings without chunks: {sorted(chunkless)[:5]}")
    if (m["counts"]["postings"], m["counts"]["chunks"]) != (len(ids), len(cids)):
        raise AssertionError("manifest counts don't match the files")
    if embedder is not None and embedder.spec["dim"] != m["embedding"]["dim"]:
        raise AssertionError("model dimension doesn't match the manifest")

    conf = json.loads((folder / "conformance.json").read_text(encoding="utf-8"))
    bm25 = retrieval.BM25([c["text"] for c in b["chunks"]])
    from .embedders import embed_query
    for case in conf["cases"]:
        if case["mode"] == "lexical":
            got = [cids[i] for i in bm25.top(case["query"], conf["k"])]
        elif embedder is not None:
            got = [cids[i] for i in retrieval.dense_top(embed_query(embedder, case["query"]), b["matrix"], conf["k"])]
        else:
            continue
        if got != case["expected_top"]:
            raise AssertionError(f"conformance mismatch for {case['mode']} query {case['query']!r}")
    return {"postings": len(ids), "chunks": len(cids), "bundle_id": m["bundle_id"]}


def run() -> None:
    from . import embedders, log as logsetup
    from .fetch import series_list
    logsetup.setup("bundle")
    con = store.connect()
    try:
        label = store.resolve_label(con, config.SNAPSHOT)
    except LookupError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
    run_id = store.start_run(con, "bundle", label)
    try:
        embedder = embedders.load()
        folder, manifest = build(con, label, embedder, series_list(config.JOB_SERIES), config.DIST)
    except (LookupError, AssertionError) as e:
        store.finish_run(con, run_id, {}, status="failed")
        print(f"bundle failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    store.finish_run(con, run_id, {"bundle_id": manifest["bundle_id"], **manifest["counts"]})
    print(f"bundle [{label}] -> {folder}")
    print(f"  bundle_id   {manifest['bundle_id']}")
    print(f"  postings    {manifest['counts']['postings']}")
    print(f"  chunks      {manifest['counts']['chunks']}")
    print("  verified    schemas, checksums, ids, vectors, quantization, conformance")
