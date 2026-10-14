"""normalize against the real 2024 archive sample (tests/fixtures/)."""
import json
import shutil
from pathlib import Path

import pytest

from usajobs_datapull import codelists, normalize

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "archive-2024-11-12-sample.json"


@pytest.fixture(scope="module")
def hiring():
    return codelists.load("hiringpaths", FIXTURES / "codelists")


@pytest.fixture
def archive_dir(tmp_path):
    d = tmp_path / "archive-2024-11-12"
    d.mkdir()
    shutil.copy(SAMPLE, d / "jobs_batch_p1.json")
    return d


def test_archive_sample_reconciles_and_dedupes(archive_dir, hiring):
    postings, quarantine, counts, _ = normalize.normalize_dir(archive_dir, hiring)
    assert counts["records_read"] == 35
    # the 2024 fetch saved multi-series postings once per group
    assert counts["postings"] == 28
    assert counts["duplicates_identical"] + counts["duplicates_conflicting"] == 7
    assert quarantine == []


def test_ids_are_control_numbers_not_position_ids(archive_dir, hiring):
    postings, _, _, _ = normalize.normalize_dir(archive_dir, hiring)
    assert all(pid.isdigit() for pid in postings)
    assert "721195600" in postings
    assert postings["721195600"]["position_id"] == "Public Notice for PWD/PWTD"


def test_rich_fields_come_from_userarea_details(archive_dir, hiring):
    postings, _, _, _ = normalize.normalize_dir(archive_dir, hiring)
    records = list(postings.values())
    assert all(r["summary"] for r in records)
    assert sum(1 for r in records if r["duties"]) >= len(records) - 2
    assert all(r["clearance"] for r in records)
    r = postings["780196200"]
    assert r["clearance"] == "Sensitive Compartmented Information"
    assert (r["grade_low"], r["grade_high"]) == ("13", "14")
    assert r["telework"] is True and r["remote"] is False
    assert r["opens_at"] == "2024-03-06"
    assert r["url"] == "https://www.usajobs.gov/GetJob/ViewDetails/780196200"


def test_conflicting_duplicate_keeps_the_later_copy(archive_dir, hiring):
    postings, _, counts, _ = normalize.normalize_dir(archive_dir, hiring)
    # 780196200's deadline was extended from Oct 31 to Nov 29 between batches
    assert counts["duplicates_conflicting"] == 1
    assert postings["780196200"]["closes_at"] == "2024-11-29"


def test_salary_location_and_labels(archive_dir, hiring):
    postings, _, _, _ = normalize.normalize_dir(archive_dir, hiring)
    r = postings["721195600"]
    assert (r["salary_min"], r["salary_max"], r["pay_interval"]) == (32357.0, 152771.0, "Per Year")
    assert r["locations"][0]["country"] == "Singapore"
    assert "2210" in r["series"]
    labels = {p for rec in postings.values() for p in rec["hiring_paths"]}
    assert "Individuals with disabilities" in labels   # "disability" mapped via code list
    assert "disability" not in labels


def test_no_empty_strings_anywhere(archive_dir, hiring):
    postings, _, _, _ = normalize.normalize_dir(archive_dir, hiring)
    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                yield from walk(x)
        elif isinstance(v, list):
            for x in v:
                yield from walk(x)
        else:
            yield v
    assert "" not in [v for r in postings.values() for v in walk(r)]


def test_live_page_layout_and_quarantine(tmp_path, hiring):
    d = tmp_path / "2026-10-12"
    d.mkdir()
    desc = json.loads(SAMPLE.read_text(encoding="utf-8"))[3]
    page = {"SearchResult": {"SearchResultItems": [
        {"MatchedObjectId": "999000111", "MatchedObjectDescriptor": desc},
        {"MatchedObjectId": "", "MatchedObjectDescriptor": {"PositionTitle": "No id, no URI"}},
        {"MatchedObjectId": "123", "MatchedObjectDescriptor": {"PositionURI": "x/123"}},
    ]}}
    (d / "page-001.json").write_text(json.dumps(page), encoding="utf-8")
    (d / "page-002.json").write_text("{not json", encoding="utf-8")

    postings, quarantine, counts, _ = normalize.normalize_dir(d, hiring)
    assert list(postings) == ["999000111"]          # MatchedObjectId wins over PositionURI
    reasons = sorted(reason for _, reason, _ in quarantine)
    assert reasons == ["missing_title", "no_control_number", "unreadable_file"]
    assert counts["records_read"] == 3 and counts["files_unreadable"] == 1


def test_raw_dir_picks_newest_live_folder(tmp_path):
    for name in ("2026-10-01", "2026-10-12", "archive-2024-11-12"):
        (tmp_path / name).mkdir()
    assert normalize.raw_dir("live", tmp_path).name == "2026-10-12"
    assert normalize.raw_dir("archive-2024-11-12", tmp_path).name == "archive-2024-11-12"


def test_clean_text():
    assert normalize.clean_text("  a&amp;b <b>bold</b>\n\n c ") == "a&b bold c"
    assert normalize.clean_text(["one", "", "two."]) == "one. two."   # list items become sentences
    assert normalize.clean_text("   ") is None
