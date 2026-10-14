"""normalize's data-quality rules, one messy-data case at a time."""
import copy
import json
import shutil
from pathlib import Path

import pytest

from usajobs_datapull import codelists, normalize

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "archive-2024-11-12-sample.json"
HIRING = codelists.load("hiringpaths", FIXTURES / "codelists")


@pytest.fixture
def base():
    return copy.deepcopy(json.loads(SAMPLE.read_text(encoding="utf-8"))[3])   # 780196200, a clean GS posting


def rec(desc):
    return normalize.to_record("1", desc, HIRING)


def details(desc):
    return desc["UserArea"]["Details"]


def test_clean_posting_has_no_flags(base):
    assert rec(base)["quality_flags"] == []


def test_sentinels_become_null(base):
    base["DepartmentName"] = "N/A"
    base["PositionLocationDisplay"] = "  Unknown "
    details(base)["SecurityClearance"] = "TBD"
    r = rec(base)
    assert r["department"] is None and r["location_display"] is None
    assert r["clearance"] is None and r["clearance_required"] is None


def test_salary_reversed_is_swapped_and_annualized(base):
    base["PositionRemuneration"] = [{"MinimumRange": "60.50", "MaximumRange": "40", "RateIntervalCode": "PH",
                                     "Description": "Per Hour"}]
    r = rec(base)
    assert (r["salary_min"], r["salary_max"]) == (40.0, 60.5)
    assert (r["salary_annual_min"], r["salary_annual_max"]) == (40 * 2087, round(60.5 * 2087))
    assert "salary_min_gt_max" in r["quality_flags"]


def test_piece_work_is_not_per_week(base):
    base["PositionRemuneration"] = [{"MinimumRange": "10", "MaximumRange": "20", "RateIntervalCode": "PW",
                                     "Description": "Piece Work"}]
    r = rec(base)
    assert r["salary_annual_min"] is None
    assert "salary_not_annualizable" in r["quality_flags"]


def test_zero_or_missing_salary(base):
    base["PositionRemuneration"] = [{"MinimumRange": "0", "MaximumRange": "", "RateIntervalCode": "PA"}]
    r = rec(base)
    assert r["salary_min"] is None and "salary_missing" in r["quality_flags"]


def test_salary_outlier(base):
    base["PositionRemuneration"] = [{"MinimumRange": "1", "MaximumRange": "2", "RateIntervalCode": "PA"}]
    assert "salary_outlier" in rec(base)["quality_flags"]


def test_grades_normalized(base):
    details(base)["LowGrade"], details(base)["HighGrade"] = "09", "07"
    r = rec(base)
    assert (r["grade_low"], r["grade_high"]) == ("7", "9")
    assert "grade_low_gt_high" in r["quality_flags"]
    details(base)["LowGrade"], details(base)["HighGrade"] = "00", "b"
    r = rec(base)
    assert (r["grade_low"], r["grade_high"]) == (None, "B")


def test_dates(base):
    base["PublicationStartDate"], base["ApplicationCloseDate"] = "2024-05-01T00:00:00", "2024-04-01T00:00:00"
    assert "closes_before_opens" in rec(base)["quality_flags"]
    base["ApplicationCloseDate"] = "not a date"
    r = rec(base)
    assert r["closes_at"] is None and "missing_dates" in r["quality_flags"]


def test_remote_location_contradicting_flag(base):
    base["PositionLocation"] = [{"LocationName": "Anywhere in the U.S. (remote job), United States",
                                 "CountryCode": "United States", "CityName": "Anywhere in the U.S. (remote job)"}]
    details(base)["RemoteIndicator"] = False
    r = rec(base)
    assert r["remote"] is False                    # the API's flag is kept...
    assert "remote_flag_mismatch" in r["quality_flags"]   # ...but the contradiction is visible


def test_duplicate_locations_removed(base):
    loc = base["PositionLocation"][0]
    base["PositionLocation"] = [loc, dict(loc), loc]
    assert len(rec(base)["locations"]) == 1


def test_broad_notice(base):
    base["JobCategory"] = [{"Code": f"{i:04d}", "Name": "x"} for i in range(12)]
    r = rec(base)
    assert r["broad_notice"] is True and "broad_notice" in r["quality_flags"]


def test_text_cleaning(base):
    details(base)["JobSummary"] = "It’s fine.​ <p>Itâ€™s &amp; <b>bold</b></p>  end"
    details(base)["MajorDuties"] = ["Lead the team", "", "Write code."]
    r = rec(base)
    assert r["summary"] == "It’s fine. It’s & bold end"
    assert r["duties"] == "Lead the team. Write code."


def test_boolean_strings(base):
    details(base)["TeleworkEligible"] = "Yes"
    details(base)["RemoteIndicator"] = "false"
    r = rec(base)
    assert r["telework"] is True and r["remote"] is False


def test_unmapped_hiring_path(base):
    details(base)["HiringPath"] = ["public", "brand-new-path"]
    r = rec(base)
    assert "brand-new-path" in r["hiring_paths"] and "public" not in r["hiring_paths"]   # known code -> label
    assert "unmapped_hiring_path" in r["quality_flags"]


def test_missing_sections_flagged(base):
    details(base)["MajorDuties"] = []
    base["QualificationSummary"] = "   "
    flags = rec(base)["quality_flags"]
    assert "missing_duties" in flags and "missing_qualifications" in flags


def test_contact_details_never_copied(base):
    details(base)["AgencyContactEmail"] = "person@agency.gov"
    details(base)["AgencyContactPhone"] = "555-123-4567"
    details(base)["AgencyContactWebsite"] = "https://hr.agency.gov/jane"
    out = json.dumps(rec(base))
    assert "person@agency.gov" not in out and "555-123-4567" not in out and "hr.agency.gov" not in out


def test_near_duplicate_announcements_share_a_group(base):
    a, b, c = rec(base), rec(base), rec(base)
    a["id"], b["id"], c["id"] = "1", "2", "3"
    c["title"] = "Something else"
    postings = {"1": a, "2": b, "3": c}
    assert normalize.assign_groups(postings) == 2
    assert a["group_id"] == b["group_id"] != c["group_id"]
    assert "duplicate_announcement" in a["quality_flags"] and "duplicate_announcement" not in c["quality_flags"]


def test_report_counts_drift_and_completeness(tmp_path, base):
    d = tmp_path / "archive-2024-11-12"
    d.mkdir()
    shutil.copy(SAMPLE, d / "batch.json")
    new_field = dict(base, BrandNewField=1, PositionURI="https://x/GetJob/ViewDetails/555")
    lost_field = dict(base, PositionURI="https://x/GetJob/ViewDetails/556")
    del lost_field["QualificationSummary"]
    (d / "z_drift.json").write_text(json.dumps([new_field, lost_field]), encoding="utf-8")

    _, _, counts, report = normalize.normalize_dir(d, HIRING)
    drift = report["schema_drift"]
    assert drift["unexpected_fields"] == {"BrandNewField": 1}
    assert drift["warn"] == []                          # 1 of 30 records: under the 5% bar
    assert drift["missing_used_fields"] == {"QualificationSummary": 1}
    assert report["completeness"]["title"] == 1.0
    assert 0 < report["completeness"]["duties"] <= 1.0
    assert counts["postings"] == 30


def test_widespread_new_field_is_warned(tmp_path, base):
    d = tmp_path / "a"
    d.mkdir()
    rows = [dict(base, BrandNewField=1, PositionURI=f"https://x/GetJob/ViewDetails/{i}") for i in range(3)]
    (d / "p.json").write_text(json.dumps(rows), encoding="utf-8")
    _, _, _, report = normalize.normalize_dir(d, HIRING)
    assert report["schema_drift"]["warn"] == ["BrandNewField"]


def test_archive_sample_has_no_schema_drift(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    shutil.copy(SAMPLE, d / "batch.json")
    _, _, _, report = normalize.normalize_dir(d, HIRING)
    assert report["schema_drift"]["unexpected_fields"] == {}
    assert report["schema_drift"]["missing_used_fields"] == {}


def test_contact_sentences_are_dropped_whole(base):
    details(base)["JobSummary"] = ("Join our team. Questions? Email jane.doe@agency.gov or call (555) 123-4567 today. "
                                   "Telework is available.")
    r = rec(base)
    assert r["summary"] == "Join our team. Telework is available."
    assert "contact_info_redacted" in r["quality_flags"]


@pytest.mark.parametrize("sentence", [
    "For more information, contact Jane Doe.",
    "Please contact HR Specialist John Q. Public with questions.",
    "Direct questions to Ms. Smith in the servicing office.",
    "POC: Maria Lopez.",
    "Call 555.123.4567 for details.",
    "Send your resume to hr-jobs@agency.gov.",
])
def test_hr_contact_forms_are_removed(base, sentence):
    details(base)["JobSummary"] = f"Great job. {sentence} Apply now."
    r = rec(base)
    assert r["summary"] == "Great job. Apply now."
    assert "contact_info_redacted" in r["quality_flags"]


@pytest.mark.parametrize("sentence", [
    "Serves as the Technical Point of Contact/financial lead for contract efforts.",
    "Career paths include Human Resources Specialist (0201) and Economist (0110).",
    "This can be found in the local Human Resources Office.",
    "Contact the Human Resources Office for reasonable accommodation.",
    "Requires public contact and record-keeping skills.",
    "Contacting homeowners by personal visit.",
])
def test_ordinary_text_mentioning_contact_is_kept(base, sentence):
    details(base)["JobSummary"] = sentence
    r = rec(base)
    assert r["summary"] == sentence
    assert "contact_info_redacted" not in r["quality_flags"]


def test_contact_only_text_becomes_missing(base):
    details(base)["KeyRequirements"] = ["U.S. citizenship required.", "Questions: contact Jane Doe at 555-123-4567."]
    details(base)["JobSummary"] = "Contact Jane Doe."
    r = rec(base)
    assert r["key_requirements"] == ["U.S. citizenship required."]
    assert r["summary"] is None and "missing_summary" in r["quality_flags"]


def test_report_counts_hr_contact_removal(tmp_path, base):
    d = tmp_path / "a"
    d.mkdir()
    dirty = dict(base, PositionURI="https://x/GetJob/ViewDetails/1")
    dirty["UserArea"] = copy.deepcopy(base["UserArea"])
    details(dirty)["JobSummary"] = "Contact Jane Doe at jane@agency.gov."
    (d / "p.json").write_text(json.dumps([dirty, dict(base, PositionURI="https://x/GetJob/ViewDetails/2")]),
                              encoding="utf-8")
    postings, _, _, report = normalize.normalize_dir(d, HIRING)
    assert report["hr_contacts"]["postings_with_contacts_removed"] == 1
    assert "jane" not in json.dumps(postings).lower()


def test_grade_numbers_are_not_mistaken_for_phones(base):
    details(base)["JobSummary"] = "GS-2210-13 position, salary $120,000 - $150,000, control 721195600."
    assert "[phone removed]" not in rec(base)["summary"]
