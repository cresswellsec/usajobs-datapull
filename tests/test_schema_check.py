"""The pure-Python schema validator that replaced the jsonschema package."""
import json
from pathlib import Path

import pytest

from usajobs_datapull.schema_check import errors

SCHEMAS = Path(__file__).parent.parent / "usajobs_datapull" / "schemas"
CHUNK = json.loads((SCHEMAS / "chunks.schema.json").read_text(encoding="utf-8"))
POSTING = json.loads((SCHEMAS / "postings.schema.json").read_text(encoding="utf-8"))


def test_valid_chunk_passes():
    assert errors({"chunk_id": "123:duties:0", "posting_id": "123", "section": "duties",
                   "text": "x", "tokens": 3}, CHUNK) == []


@pytest.mark.parametrize("bad, expected", [
    ({"chunk_id": "123:duties:0", "posting_id": "123", "section": "duties", "text": "x"}, "missing required 'tokens'"),
    ({"chunk_id": "nope", "posting_id": "123", "section": "duties", "text": "x", "tokens": 3}, "doesn't match"),
    ({"chunk_id": "1:duties:0", "posting_id": "1", "section": "benefits", "text": "x", "tokens": 3}, "not one of"),
    ({"chunk_id": "1:duties:0", "posting_id": "1", "section": "duties", "text": "", "tokens": 3}, "shorter than"),
    ({"chunk_id": "1:duties:0", "posting_id": "1", "section": "duties", "text": "x", "tokens": 0}, "minimum"),
    ({"chunk_id": "1:duties:0", "posting_id": "1", "section": "duties", "text": "x", "tokens": True}, "expected integer"),
    ({"chunk_id": "1:duties:0", "posting_id": "1", "section": "duties", "text": "x", "tokens": 3, "extra": 1}, "unexpected field"),
])
def test_invalid_chunks_fail_with_a_reason(bad, expected):
    assert any(expected in e for e in errors(bad, CHUNK))


def test_nullable_and_nested_fields():
    loc_schema = POSTING["properties"]["locations"]
    assert errors([{"name": None, "city": "X", "state": None, "country": "US"}], loc_schema) == []
    assert any("missing required 'country'" in e for e in errors([{"name": None, "city": "X", "state": None}], loc_schema))
    assert any("expected string/null" in e for e in errors(5, POSTING["properties"]["agency"]))


def test_unsupported_keyword_is_refused():
    with pytest.raises(ValueError, match="unsupported"):
        errors(1, {"type": "integer", "maximum": 3})
