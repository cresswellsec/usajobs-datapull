"""chunk packing / boilerplate, and the reference retrieval rules."""
import numpy as np

from usajobs_datapull import chunk, config, retrieval


def posting(pid, summary="", duties="", quals="", **kw):
    base = {"id": pid, "title": "IT Specialist", "agency": "Agency X", "location_display": "Washington, DC",
            "locations": [], "pay_plan": "GS", "grade_low": "12", "grade_high": "13",
            "summary": summary, "duties": duties, "qualifications": quals, "key_requirements": []}
    return {**base, **kw}


def test_header_has_only_present_parts():
    assert chunk.header(posting("1")) == \
        "Title: IT Specialist | Agency: Agency X | Location: Washington, DC | Grade: GS-12 to 13"
    assert chunk.header(posting("1", agency=None, grade_low=None, location_display=None)) == "Title: IT Specialist"


def test_split_sentences():
    assert chunk.split_sentences("One. Two? Three! four") == ["One.", "Two?", "Three! four"]


def test_pack_respects_max_words():
    sentences = [" ".join(["w"] * 50) + "."] * 9 + [" ".join(["x"] * 400)]
    chunks = chunk.pack(sentences, max_words=180)
    assert all(len(c.split()) <= 180 for c in chunks)
    assert sum(len(c.split()) for c in chunks) == 9 * 50 + 400


def test_boilerplate_removed_from_chunks_only():
    boiler = "This position is covered by the federal employment eligibility rules and policies."
    postings = [posting(str(i), summary=f"Unique summary number {i} about networks. {boiler}") for i in range(12)]
    rows, stats = chunk.chunk_postings(postings)
    assert stats["boilerplate_sentences"] == 1 and stats["boilerplate_removed"] == 12
    assert all(boiler not in r["text"] for r in rows)
    assert boiler in postings[0]["summary"]          # the record itself is untouched


def test_chunk_ids_sections_and_fallback():
    rows, stats = chunk.chunk_postings([
        posting("10", summary="Summary text here.", duties="Do things.", quals="Know things."),
        posting("11"),                                 # nothing to chunk
    ])
    ids = [r["chunk_id"] for r in rows]
    assert ids == ["10:summary:0", "10:duties:0", "10:qualifications:0", "11:summary:0"]
    assert rows[1]["text"].startswith("Title: IT Specialist") and "Duties: Do things." in rows[1]["text"]
    assert stats["fallback_chunks"] == 1


def test_tokenizer_v1_compounds():
    assert retrieval.tokenize_v1("GS-13, TS/SCI clearance") == ["gs", "13", "ts", "sci", "clearance", "gs13", "tssci"]
    assert retrieval.tokenize_v1("Ｐｙｔｈｏｎ") == ["python"]                 # NFKC folds full-width


def test_bm25_ranks_and_breaks_ties_by_row():
    texts = ["python developer", "java developer", "python python developer", "help desk", "java developer"]
    bm = retrieval.BM25(texts)
    assert bm.top("python", 5) == [2, 0]
    assert bm.top("java", 5) == [1, 4]               # identical scores -> earlier row first
    assert bm.top("nothing matches", 5) == []


def test_quantize_roundtrip_is_close():
    rng = np.random.default_rng(0)
    v = rng.normal(size=(50, 384)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    q, s = retrieval.quantize(v)
    d = retrieval.dequantize(q, s)
    cos = (d * v).sum(1) / np.linalg.norm(d, axis=1)
    assert q.dtype == np.int8 and cos.min() > 0.999


def test_dense_top_ties_by_row():
    m = np.array([[1, 0], [0, 1], [1, 0]], dtype=np.float32)
    assert retrieval.dense_top(np.array([1, 0], np.float32), m, 3) == [0, 2, 1]
