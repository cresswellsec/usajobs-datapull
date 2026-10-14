"""data/work.db (SQLite) and the shared selection rules.

  postings    snapshot, id, position_id, content_hash, record_json
  quarantine  snapshot, raw_ref, reason, payload_json
  chunks      snapshot, chunk_id, posting_id, section, seq, text
  embeddings  model_id, text_hash, dim, tokens, truncated, vector
  runs        run_id, stage, snapshot, started, finished, counts_json, status

Rows are keyed by snapshot; embeddings by (model, text hash). Everything is
rebuildable from data/raw/.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    snapshot     TEXT NOT NULL,
    id           TEXT NOT NULL,
    position_id  TEXT,
    content_hash TEXT NOT NULL,
    record_json  TEXT NOT NULL,
    PRIMARY KEY (snapshot, id)
);
CREATE TABLE IF NOT EXISTS quarantine (
    snapshot     TEXT NOT NULL,
    raw_ref      TEXT NOT NULL,
    reason       TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    snapshot     TEXT NOT NULL,
    chunk_id     TEXT NOT NULL,
    posting_id   TEXT NOT NULL,
    section      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    text         TEXT NOT NULL,
    PRIMARY KEY (snapshot, chunk_id)
);
CREATE TABLE IF NOT EXISTS embeddings (
    model_id     TEXT NOT NULL,
    text_hash    TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    tokens       INTEGER NOT NULL,
    truncated    INTEGER NOT NULL,
    vector       BLOB NOT NULL,
    PRIMARY KEY (model_id, text_hash)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    stage        TEXT NOT NULL,
    snapshot     TEXT NOT NULL,
    started      TEXT NOT NULL,
    finished     TEXT,
    counts_json  TEXT,
    status       TEXT NOT NULL
);
"""

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})$")
SECTION_ORDER = ("summary", "duties", "qualifications")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.WORK_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def start_run(con: sqlite3.Connection, stage: str, snapshot: str) -> int:
    cur = con.execute(
        "INSERT INTO runs (stage, snapshot, started, status) VALUES (?, ?, ?, 'running')",
        (stage, snapshot, _now()),
    )
    con.commit()
    return cur.lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, counts: dict, status: str = "ok") -> None:
    con.execute(
        "UPDATE runs SET finished = ?, counts_json = ?, status = ? WHERE run_id = ?",
        (_now(), json.dumps(counts, sort_keys=True), status, run_id),
    )
    con.commit()


def last_counts(con: sqlite3.Connection, stage: str, label: str) -> dict:
    row = con.execute(
        "SELECT counts_json FROM runs WHERE stage = ? AND snapshot = ? AND status = 'ok' "
        "ORDER BY run_id DESC LIMIT 1", (stage, label)).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


# --- which snapshot, which postings -----------------------------------------

def resolve_label(con: sqlite3.Connection, snapshot: str) -> str:
    """'live' -> the newest dated snapshot that has been normalized; else as given."""
    if snapshot != "live":
        return snapshot
    labels = [r[0] for r in con.execute("SELECT DISTINCT snapshot FROM postings")]
    dated = sorted(l for l in labels if re.fullmatch(r"\d{4}-\d{2}-\d{2}", l))
    if not dated:
        raise LookupError("no normalized live snapshot in work.db -- run fetch and normalize first")
    return dated[-1]


def snapshot_date(label: str) -> str:
    """'2026-10-12' or 'archive-2024-11-12' -> the date the data reflects."""
    match = _DATE.search(label)
    if not match:
        raise ValueError(f"snapshot label {label!r} doesn't end in a YYYY-MM-DD date")
    return match.group(1)


def in_scope(record: dict, series: list[str], as_of: str) -> bool:
    """The one rule for which postings a bundle contains (docs/BUNDLE.md):
    tagged with at least one configured series, and still open on the snapshot date."""
    if not set(record.get("series") or []) & set(series):
        return False
    closes = record.get("closes_at")
    return closes is None or closes >= as_of


def select_postings(con: sqlite3.Connection, label: str, series: list[str]) -> list[dict]:
    as_of = snapshot_date(label)
    rows = con.execute("SELECT record_json FROM postings WHERE snapshot = ? ORDER BY id", (label,))
    return [r for r in (json.loads(row[0]) for row in rows) if in_scope(r, series, as_of)]


def chunks_for(con: sqlite3.Connection, label: str, posting_ids: list[str]) -> list[dict]:
    """Chunks in bundle order: posting id, then section order, then sequence."""
    wanted = set(posting_ids)
    order = {s: i for i, s in enumerate(SECTION_ORDER)}
    rows = con.execute("SELECT chunk_id, posting_id, section, seq, text FROM chunks WHERE snapshot = ?", (label,))
    out = [dict(zip(("chunk_id", "posting_id", "section", "seq", "text"), r)) for r in rows if r[1] in wanted]
    out.sort(key=lambda c: (c["posting_id"], order.get(c["section"], 99), c["seq"]))
    return out


def write_report(name: str, data: dict) -> Path:
    config.REPORTS.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS / f"{name}.json"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
    return path
