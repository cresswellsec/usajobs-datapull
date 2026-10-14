"""work.db postings -> chunks.

Per in-scope posting: split into sections, drop boilerplate sentences (in at
least BOILERPLATE_MIN_POSTINGS postings and BOILERPLATE_SHARE of them), pack to
MAX_CHUNK_WORDS, prefix a "Title | Agency | Location | Grade" header. A posting
with no section text still gets one chunk. Ids: "<posting id>:<section>:<n>".
"""
from __future__ import annotations

import math
import re
import sys
from collections import Counter

from . import config, store

SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9\"'(\[•\-])")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_END.split(text or "") if s.strip()]


def _norm(sentence: str) -> str:
    return re.sub(r"\s+", " ", sentence.lower()).strip()


def grade_label(r: dict) -> str | None:
    if not r.get("grade_low"):
        return None
    plan = f"{r['pay_plan']}-" if r.get("pay_plan") else ""
    if r.get("grade_high") and r["grade_high"] != r["grade_low"]:
        return f"{plan}{r['grade_low']} to {r['grade_high']}"
    return f"{plan}{r['grade_low']}"


def location_label(r: dict) -> str | None:
    if r.get("location_display"):
        return r["location_display"]
    locs = r.get("locations") or []
    return locs[0]["name"] if locs and locs[0].get("name") else None


def header(r: dict) -> str:
    """Present parts only, joined with " | " (docs/BUNDLE.md, chunking.header)."""
    parts = [("Title", r.get("title")), ("Agency", r.get("agency")),
             ("Location", location_label(r)), ("Grade", grade_label(r))]
    return " | ".join(f"{name}: {value}" for name, value in parts if value)


def sections(r: dict) -> list[tuple[str, str]]:
    quals = " ".join(x for x in [r.get("qualifications") or ""] + list(r.get("key_requirements") or []) if x)
    out = [("summary", r.get("summary") or ""), ("duties", r.get("duties") or ""), ("qualifications", quals)]
    return [(name, text) for name, text in out if text.strip()]


def find_boilerplate(postings: list[dict]) -> set[str]:
    counts = Counter()
    for r in postings:
        seen = {_norm(s) for _, text in sections(r) for s in split_sentences(text)
                if len(s) >= config.BOILERPLATE_MIN_CHARS}
        counts.update(seen)
    threshold = max(config.BOILERPLATE_MIN_POSTINGS, math.ceil(config.BOILERPLATE_SHARE * len(postings)))
    return {s for s, n in counts.items() if n >= threshold}


def pack(sentences: list[str], max_words: int = config.MAX_CHUNK_WORDS) -> list[str]:
    chunks, current, size = [], [], 0
    for s in sentences:
        words = s.split()
        while len(words) > max_words:                      # one huge sentence
            if current:
                chunks.append(" ".join(current))
                current, size = [], 0
            chunks.append(" ".join(words[:max_words]))
            words = words[max_words:]
        if not words:
            continue
        if size + len(words) > max_words and current:
            chunks.append(" ".join(current))
            current, size = [], 0
        current.append(" ".join(words))
        size += len(words)
    if current:
        chunks.append(" ".join(current))
    return chunks


SECTION_LABEL = {"summary": "Summary", "duties": "Duties", "qualifications": "Qualifications"}


def chunk_postings(postings: list[dict]) -> tuple[list[dict], dict]:
    """Pure: postings -> chunk rows, plus stats."""
    boiler = find_boilerplate(postings)
    rows, removed, fallback = [], 0, 0
    for r in postings:
        head = header(r)
        made = 0
        for name, text in sections(r):
            kept = []
            for s in split_sentences(text):
                if _norm(s) in boiler:
                    removed += 1
                else:
                    kept.append(s)
            for n, body in enumerate(pack(kept)):
                rows.append({"chunk_id": f"{r['id']}:{name}:{n}", "posting_id": r["id"], "section": name,
                             "seq": n, "text": f"{head}. {SECTION_LABEL[name]}: {body}"})
                made += 1
        if not made:
            fallback += 1
            rows.append({"chunk_id": f"{r['id']}:summary:0", "posting_id": r["id"], "section": "summary",
                         "seq": 0, "text": f"{head}. Summary: {r.get('title') or ''}".strip()})
    stats = {"postings": len(postings), "chunks": len(rows), "boilerplate_sentences": len(boiler),
             "boilerplate_removed": removed, "fallback_chunks": fallback,
             "max_words": max((len(c["text"].split()) for c in rows), default=0)}
    return rows, stats


def save(con, label: str, rows: list[dict]) -> None:
    with con:
        con.execute("DELETE FROM chunks WHERE snapshot = ?", (label,))
        con.executemany(
            "INSERT INTO chunks (snapshot, chunk_id, posting_id, section, seq, text) VALUES (?, ?, ?, ?, ?, ?)",
            [(label, c["chunk_id"], c["posting_id"], c["section"], c["seq"], c["text"]) for c in rows])


def run() -> None:
    from .fetch import series_list
    con = store.connect()
    try:
        label = store.resolve_label(con, config.SNAPSHOT)
    except LookupError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
    postings = store.select_postings(con, label, series_list(config.JOB_SERIES))
    if not postings:
        print(f"no in-scope postings in snapshot {label} (JOB_SERIES={config.JOB_SERIES}) -- "
              f"run normalize, or check the scope", file=sys.stderr)
        raise SystemExit(1)
    run_id = store.start_run(con, "chunk", label)
    rows, stats = chunk_postings(postings)
    save(con, label, rows)
    store.finish_run(con, run_id, stats)
    print(f"chunk [{label}]")
    for k, v in stats.items():
        print(f"  {k:<24}{v}")
