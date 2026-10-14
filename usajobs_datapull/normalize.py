"""data/raw/<snapshot>/ -> work.db postings (+ quarantine).

  * id = USAJOBS control number (MatchedObjectId, or parsed from PositionURI);
    PositionID is not a key
  * exact duplicates dropped; when copies differ the later file wins
  * same job posted as separate announcements: shared group_id +
    duplicate_announcement flag, not merged
  * malformed records quarantined with a reason
  * reconciliation: read == postings + duplicates + quarantined, else fail
  * text cleaned (HTML, mojibake, invisible chars); sentinels ("N/A", ...) -> null
  * salary parsed and annualized; grades, dates, locations, remote checked;
    anything odd goes in quality_flags
  * HR contacts: contact fields never copied; any sentence naming an email,
    phone or person to contact is dropped (contact_info_redacted)
  * schema drift (fields added/removed by the API) reported to data/reports/

Scope (JOB_SERIES) is applied later, so changing it never needs a re-normalize.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Iterator

from . import codelists, config, store

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRAILING_ID = re.compile(r"/(\d+)/?$")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_INVISIBLE = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b-\u200f\u2028\u2029\u2060\ufeff]")
_MOJIBAKE = re.compile("(?:\u00e2\u20ac|\u00c3[\u0080-\u00bf]|\u00c2[\u00a0-\u00bf])")
# a UTF-8 lead byte seen as cp1252, followed by 1-3 continuation bytes seen as cp1252
_CP1252_CONT = ("\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
                "\u2013\u2014\u2018-\u201a\u201c-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122")
_MOJIBAKE_RUN = re.compile("[\u00c2-\u00f4][" + _CP1252_CONT + "]{1,3}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
# HR contact removal (see redact_contacts)
CONTACT_FIELDS = ("AgencyContactEmail", "AgencyContactPhone", "AgencyContactWebsite")   # never copied
_SENTENCE_END = re.compile(r"(?<!\b[A-Z]\.)(?<!\b[A-Z][a-z]\.)(?<!Mrs\.)(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_HONORIFIC_NAME = re.compile(r"\b(?:Mr|Ms|Mrs|Dr)\.?\s+[A-Z][a-z'-]+")
_CONTACT_NAME = re.compile(
    r"\b(?:[Cc]ontact(?:ing)?|reach out to|POC|[Pp]oint of [Cc]ontact)\b:?,?\s+"
    r"(?:(?:the|our|your)\s+)?(?:(?:HR|Human Resources)\s+(?:Specialist|Assistant|Representative|Officer),?\s+)?"
    r"([A-Z][a-z'-]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z'-]+)")
# capitalized words that make "contact X Y" an office or a thing, not a person
_NOT_A_NAME = {"Human", "Resources", "Office", "Center", "Centre", "Team", "Division", "Branch", "Service",
               "Services", "Department", "Agency", "Help", "Desk", "Customer", "Support", "Staff", "Section",
               "Unit", "Management", "Personnel", "Hiring", "Recruitment", "Applicant", "Technical", "Shared",
               "Security", "Federal", "National", "Regional", "Local", "Contact", "Information", "Program",
               "Fort", "Base", "Camp", "Air", "Army", "Navy", "Marine", "Coast", "Guard", "Joint", "Command"}
_SENTINELS = {"", "n/a", "na", "none", "null", "nil", "unknown", "not specified", "not applicable", "tbd", "-", "--"}

# Pay interval -> multiplier to an annual figure. None = no meaningful annual figure.
ANNUAL_FACTOR = {
    "PA": 1, "SY": 1, "PH": config.HOURS_PER_YEAR, "PD": 260, "BW": 26, "PM": 12,
    "PW": None,  # Piece Work (not "per week")
    "FB": None, "ST": None, "WC": None,
}

# Schema drift. KNOWN_* = every field seen in the 2024 archive or the first
# 2026 live fetch (which added the AnnouncementClosingType* and
# HiringPathDisplay fields). A field outside
# these is new from the API: reported always, warned about when it appears in
# at least DRIFT_WARN_SHARE of records. USED_* = fields this module reads: one
# going missing is reported, because it silently empties a column.
KNOWN_FIELDS = {
    "AnnouncementClosingType", "AnnouncementClosingTypeOption", "ApplicationCloseDate", "ApplyURI", "DepartmentName", "JobCategory", "JobGrade",
    "OrganizationName", "PositionEndDate", "PositionFormattedDescription", "PositionID",
    "PositionLocation", "PositionLocationDisplay", "PositionOfferingType",
    "PositionRemuneration", "PositionSchedule", "PositionStartDate", "PositionTitle",
    "PositionURI", "PublicationStartDate", "QualificationSummary", "SubAgency", "UserArea",
}
KNOWN_DETAILS = {
    "AdjudicationType", "AgencyContactEmail", "AgencyContactPhone", "AgencyContactWebsite",
    "AgencyMarketingStatement", "AnnouncementClosingType", "AnnouncementClosingTypeOption", "ApplyOnlineUrl", "BargainingUnitStatus",
    "Benefits", "BenefitsDisplayDefaultText", "BenefitsUrl", "CommuteDistance", "DetailStatusUrl",
    "DrugTestRequired", "Education", "Evaluations", "FinancialDisclosure",
    "FinancialDisclosureAdditionalText", "HighGrade", "HiringPath", "HiringPathDisplay", "HowToApply", "JobSummary",
    "KeyRequirements", "LowGrade", "MCOTags", "MajorDuties", "OrganizationCodes", "OtherInformation",
    "PositionSensitivitiy", "PreviewQuestionnaireurl", "PromotionPotential",
    "PromotionPotentialAdditionalText", "Relocation", "RemoteIndicator", "RequiredDocuments",
    "Requirements", "SecurityClearance", "ServiceType", "SubAgencyName", "TeleworkEligible",
    "TotalOpenings", "TravelCode", "WhatToExpectNext", "WhoMayApply", "WithinArea",
}
USED_FIELDS = {"ApplicationCloseDate", "ApplyURI", "DepartmentName", "JobCategory", "JobGrade",
               "OrganizationName", "PositionID", "PositionLocation", "PositionLocationDisplay",
               "PositionRemuneration", "PositionTitle", "PositionURI", "PublicationStartDate",
               "QualificationSummary", "UserArea"}
USED_DETAILS = {"HighGrade", "HiringPath", "JobSummary", "KeyRequirements", "LowGrade", "MajorDuties",
                "RemoteIndicator", "SecurityClearance", "TeleworkEligible"}
DRIFT_WARN_SHARE = 0.05
FIELDS_TRACKED = ("title", "agency", "summary", "duties", "qualifications", "salary_min",
                  "salary_annual_min", "grade_low", "clearance", "remote", "telework",
                  "opens_at", "closes_at", "locations", "apply_url")


# --- locating raw data -----------------------------------------------------

def raw_dir(snapshot: str, root: Path | None = None) -> Path:
    """data/raw/<snapshot>/, or the newest dated folder when snapshot is 'live'."""
    root = root or config.RAW
    if snapshot != "live":
        return root / snapshot
    dated = sorted(d for d in root.glob("*") if d.is_dir() and _DATE_DIR.match(d.name))
    if not dated:
        raise FileNotFoundError(f"no live fetches under {root} -- run `fetch` first")
    return dated[-1]


def iter_raw(directory: Path) -> Iterator[tuple[str, object]]:
    """Yield (raw_ref, item) for every record in every raw file.

    Two layouts are accepted:
      * live API pages: {"SearchResult": {"SearchResultItems": [item, ...]}}
        where item = {"MatchedObjectId": ..., "MatchedObjectDescriptor": {...}}
      * the 2024 archive: a JSON list of bare descriptors
    An unreadable file is yielded once as (raw_ref, None). Files starting with
    "_" (e.g. _fetch.json) are metadata and skipped.
    """
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            yield path.name, None
            continue
        if isinstance(data, dict):
            items = (data.get("SearchResult") or {}).get("SearchResultItems") or []
        elif isinstance(data, list):
            items = data
        else:
            items = [data]
        for i, item in enumerate(items):
            yield f"{path.name}#{i}", item


# --- field helpers ---------------------------------------------------------

def fix_mojibake(text: str) -> str:
    """Repair UTF-8 that was decoded as Windows-1252 ("â€™" -> "’").

    Works run by run, so a string that mixes correct characters with broken
    ones is still repaired; a run that doesn't decode cleanly is left alone.
    """
    if not _MOJIBAKE.search(text):
        return text

    def repair(match: re.Match) -> str:
        run = match.group(0)
        try:
            return run.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return run
    return _MOJIBAKE_RUN.sub(repair, text)


def clean_text(value: object) -> str | None:
    """Plain text: entities decoded, tags removed, mojibake repaired, invisible
    characters removed, whitespace collapsed. List items become sentences.
    Empty -> None."""
    if value is None:
        return None
    if isinstance(value, list):
        items = [clean_text(v) for v in value]
        items = [i if i[-1] in ".!?:;" else i + "." for i in items if i]
        value = " ".join(items)
    text = html.unescape(str(value))
    text = _TAG.sub(" ", text)
    text = fix_mojibake(text)
    text = unicodedata.normalize("NFC", text).replace("\u00a0", " ")
    text = _INVISIBLE.sub("", text)
    text = _SPACE.sub(" ", text).strip()
    return text or None


def names_contact(sentence: str) -> bool:
    """True if a sentence carries an HR contact: email, phone or a named person to contact."""
    if _EMAIL.search(sentence) or _PHONE.search(sentence) or _HONORIFIC_NAME.search(sentence):
        return True
    return any(not set(m.group(1).split()) & _NOT_A_NAME for m in _CONTACT_NAME.finditer(sentence))


def redact_contacts(text: str | None) -> tuple[str | None, bool]:
    """Drop every sentence that names an HR contact; also a short lead-in ("Questions?") before one.

    Returns (text or None if nothing is left, whether anything was removed).
    Text without contacts is returned unchanged.
    """
    if not text:
        return text, False
    sentences = _SENTENCE_END.split(text)
    drop = [names_contact(s) for s in sentences]
    if not any(drop):
        return text, False
    for i in range(1, len(sentences)):
        lead = sentences[i - 1].strip()
        if drop[i] and lead.endswith(("?", ":")) and len(lead.split()) <= 3:
            drop[i - 1] = True
    kept = " ".join(s.strip() for s, d in zip(sentences, drop) if not d).strip()
    return kept or None, True


def _str(value: object) -> str | None:
    """Short scalar field: cleaned, and sentinel values ("N/A", "Unknown") -> None."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = clean_text(value)
    return None if text is None or text.lower() in _SENTINELS else text


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        text = str(value).replace(",", "").replace("$", "").strip()
        number = float(text) if text else None
    except (TypeError, ValueError):
        return None
    return number if number else None   # 0 means "not given" in USAJOBS pay data


def _date(value: object) -> str | None:
    text = str(value or "")
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if value is not None else ""
    return {"true": True, "yes": True, "y": True, "1": True,
            "false": False, "no": False, "n": False, "0": False}.get(text)


def _grade(value: object) -> str | None:
    text = _str(value)
    if text is None:
        return None
    if text.isdigit():
        text = text.lstrip("0")
        return text or None            # "0" / "00" = no grade
    return text.upper()


def _url(value: object) -> str | None:
    text = _str(value)
    return text.replace(":443/", "/", 1) if text else None


def control_number(item: dict) -> tuple[str | None, dict]:
    """Return (control number, descriptor) for either raw layout."""
    if "MatchedObjectDescriptor" in item:
        desc = item.get("MatchedObjectDescriptor") or {}
        mid = _str(item.get("MatchedObjectId"))
        if mid and mid.isdigit():
            return mid, desc
    else:
        desc = item
    match = _TRAILING_ID.search(str(desc.get("PositionURI") or "")) if isinstance(desc, dict) else None
    return (match.group(1) if match else None), desc


# --- the mapping -----------------------------------------------------------

def to_record(cid: str, desc: dict, hiring_labels: dict[str, str]) -> dict:
    """Map one USAJOBS descriptor to a postings.jsonl record (docs/BUNDLE.md)."""
    details = (desc.get("UserArea") or {}).get("Details") or {}
    pay = (desc.get("PositionRemuneration") or [{}])[0] or {}
    grade = (desc.get("JobGrade") or [{}])[0] or {}
    apply = desc.get("ApplyURI") or []
    flags: list[str] = []

    # locations, de-duplicated in order
    locations, seen = [], set()
    for loc in desc.get("PositionLocation") or []:
        city = _str(loc.get("CityName"))
        entry = {
            "name": _str(loc.get("LocationName")),
            "city": city.split(",")[0].strip() if city else None,
            "state": _str(loc.get("CountrySubDivisionCode")),
            "country": _str(loc.get("CountryCode")),
        }
        key = tuple(entry.values())
        if key not in seen:
            seen.add(key)
            locations.append(entry)

    # hiring paths: codes -> labels, unknown codes kept and flagged
    hiring_paths = []
    for code in details.get("HiringPath") or []:
        label = hiring_labels.get(str(code).upper())
        if label is None and hiring_labels:
            flags.append("unmapped_hiring_path")
        hiring_paths.append(label or str(code))

    # salary
    salary_min, salary_max = _number(pay.get("MinimumRange")), _number(pay.get("MaximumRange"))
    if salary_min and salary_max and salary_min > salary_max:
        salary_min, salary_max = salary_max, salary_min
        flags.append("salary_min_gt_max")
    interval_code = (_str(pay.get("RateIntervalCode")) or "").upper()
    factor = ANNUAL_FACTOR.get(interval_code)
    annual_min = round(salary_min * factor) if factor and salary_min else None
    annual_max = round(salary_max * factor) if factor and salary_max else None
    if salary_min is None and salary_max is None:
        flags.append("salary_missing")
    elif interval_code in ANNUAL_FACTOR and factor is None:
        flags.append("salary_not_annualizable")
    for value in (annual_min, annual_max):
        if value is not None and not (config.SALARY_SANE_MIN <= value <= config.SALARY_SANE_MAX):
            flags.append("salary_outlier")
            break

    # grades
    grade_low, grade_high = _grade(details.get("LowGrade")), _grade(details.get("HighGrade"))
    if grade_low and grade_high and grade_low.isdigit() and grade_high.isdigit() \
            and int(grade_low) > int(grade_high):
        grade_low, grade_high = grade_high, grade_low
        flags.append("grade_low_gt_high")

    # dates
    opens_at, closes_at = _date(desc.get("PublicationStartDate")), _date(desc.get("ApplicationCloseDate"))
    if opens_at and closes_at and closes_at < opens_at:
        flags.append("closes_before_opens")
    if not opens_at or not closes_at:
        flags.append("missing_dates")

    # remote
    remote = _bool(details.get("RemoteIndicator"))
    says_remote = any("remote" in (l["name"] or "").lower() for l in locations)
    if says_remote and remote is False:
        flags.append("remote_flag_mismatch")

    series = [c["Code"] for c in desc.get("JobCategory") or [] if c.get("Code")]
    broad = len(series) >= config.BROAD_NOTICE_SERIES or len(locations) >= config.BROAD_NOTICE_LOCATIONS
    if broad:
        flags.append("broad_notice")

    clearance = _str(details.get("SecurityClearance"))
    record = {
        "id": cid,
        "position_id": _str(desc.get("PositionID")),
        "title": _str(desc.get("PositionTitle")),
        "agency": _str(desc.get("OrganizationName")),
        "department": _str(desc.get("DepartmentName")),
        "series": series,
        "pay_plan": _str(grade.get("Code")),
        "grade_low": grade_low,
        "grade_high": grade_high,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "pay_interval": _str(pay.get("Description")) or (interval_code or None),
        "salary_annual_min": annual_min,
        "salary_annual_max": annual_max,
        "locations": locations,
        "location_display": _str(desc.get("PositionLocationDisplay")),
        "remote": remote,
        "telework": _bool(details.get("TeleworkEligible")),
        "clearance": clearance,
        "clearance_required": None if clearance is None else clearance.lower() != "not required",
        "hiring_paths": hiring_paths,
        "opens_at": opens_at,
        "closes_at": closes_at,
        "url": _url(desc.get("PositionURI")),
        "apply_url": _url(apply[0]) if apply else None,
        "summary": clean_text(details.get("JobSummary")),
        "duties": clean_text(details.get("MajorDuties")),
        "qualifications": clean_text(desc.get("QualificationSummary")),
        "key_requirements": [t for t in (clean_text(k) for k in details.get("KeyRequirements") or []) if t],
        "broad_notice": broad,
        "group_id": None,       # set once all postings are known
        "quality_flags": [],
    }
    for field in ("summary", "duties", "qualifications"):
        record[field], redacted = redact_contacts(record[field])
        if redacted:
            flags.append("contact_info_redacted")
        if not record[field]:
            flags.append(f"missing_{field}")
    cleaned = [redact_contacts(k) for k in record["key_requirements"]]
    record["key_requirements"] = [k for k, _ in cleaned if k]
    if any(r for _, r in cleaned):
        flags.append("contact_info_redacted")
    record["quality_flags"] = sorted(set(flags))
    return record


def content_hash(record: dict) -> str:
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assign_groups(postings: dict[str, dict]) -> int:
    """Give near-duplicate announcements a shared group_id. Returns the number
    of postings that share a group with at least one other posting."""
    def key(r: dict) -> str:
        parts = [(r["title"] or "").lower(), r["agency"] or "", r["opens_at"] or "", r["closes_at"] or "",
                 "|".join(sorted((l["name"] or "") for l in r["locations"]))]
        return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]

    groups: dict[str, list[str]] = {}
    for cid, r in postings.items():
        groups.setdefault(key(r), []).append(cid)
    shared = 0
    for gid, members in groups.items():
        for cid in members:
            r = postings[cid]
            r["group_id"] = gid
            if len(members) > 1:
                r["quality_flags"] = sorted(set(r["quality_flags"]) | {"duplicate_announcement"})
                shared += 1
    return shared


def normalize_dir(directory: Path, hiring_labels: dict[str, str]) -> tuple[dict, list, dict, dict]:
    """Normalize every raw record in a folder. Pure: no database, no config paths.

    Returns (postings {id: record}, quarantine [(raw_ref, reason, payload)], counts, report).
    """
    postings: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    quarantine: list[tuple[str, str, object]] = []
    counts = {"files_unreadable": 0, "records_read": 0, "postings": 0,
              "duplicates_identical": 0, "duplicates_conflicting": 0, "quarantined": 0}
    unexpected, missing = Counter(), Counter()

    for raw_ref, item in iter_raw(directory):
        if item is None:
            counts["files_unreadable"] += 1
            quarantine.append((raw_ref, "unreadable_file", None))
            continue
        counts["records_read"] += 1
        if not isinstance(item, dict):
            quarantine.append((raw_ref, "not_an_object", item))
            continue
        cid, desc = control_number(item)
        if not isinstance(desc, dict):
            quarantine.append((raw_ref, "not_an_object", item))
            continue
        if not cid:
            quarantine.append((raw_ref, "no_control_number", item))
            continue
        if not _str(desc.get("PositionTitle")):
            quarantine.append((raw_ref, "missing_title", item))
            continue

        # schema drift
        keys = set(desc)
        detail_keys = set(((desc.get("UserArea") or {}).get("Details") or {}))
        unexpected.update(keys - KNOWN_FIELDS)
        unexpected.update(f"Details.{k}" for k in detail_keys - KNOWN_DETAILS)
        missing.update(USED_FIELDS - keys)
        missing.update(f"Details.{k}" for k in USED_DETAILS - detail_keys)

        record = to_record(cid, desc, hiring_labels)
        digest = content_hash(record)
        if cid in postings:
            if hashes[cid] == digest:
                counts["duplicates_identical"] += 1
                continue
            counts["duplicates_conflicting"] += 1   # later file wins (see module docstring)
        postings[cid] = record
        hashes[cid] = digest

    counts["postings"] = len(postings)
    counts["quarantined"] = sum(1 for _, reason, _ in quarantine if reason != "unreadable_file")
    accounted = (counts["postings"] + counts["duplicates_identical"]
                 + counts["duplicates_conflicting"] + counts["quarantined"])
    if accounted != counts["records_read"]:
        raise AssertionError(f"reconciliation failed: read {counts['records_read']}, accounted {accounted}")
    counts["grouped_announcements"] = assign_groups(postings)

    n = len(postings) or 1
    report = {
        "counts": counts,
        "quarantine_reasons": dict(Counter(reason for _, reason, _ in quarantine)),
        "quality_flags": dict(Counter(f for r in postings.values() for f in r["quality_flags"])),
        "completeness": {f: round(sum(1 for r in postings.values() if r[f] not in (None, [], "")) / n, 3)
                         for f in FIELDS_TRACKED},
        "schema_drift": {
            "unexpected_fields": dict(unexpected),
            "missing_used_fields": dict(missing),
            "warn": sorted(k for k, c in unexpected.items() if c >= DRIFT_WARN_SHARE * n),
        },
        "hr_contacts": {
            "postings_with_contacts_removed": sum(1 for r in postings.values()
                                                  if "contact_info_redacted" in r["quality_flags"]),
            "contact_fields_dropped": list(CONTACT_FIELDS),
        },
    }
    return postings, quarantine, counts, report


def save(con, label: str, postings: dict, quarantine: list) -> None:
    """Replace one snapshot's postings and quarantine rows, atomically."""
    with con:
        con.execute("DELETE FROM postings WHERE snapshot = ?", (label,))
        con.execute("DELETE FROM quarantine WHERE snapshot = ?", (label,))
        con.executemany(
            "INSERT INTO postings (snapshot, id, position_id, content_hash, record_json) VALUES (?, ?, ?, ?, ?)",
            [(label, r["id"], r["position_id"], content_hash(r), json.dumps(r, ensure_ascii=False))
             for r in postings.values()],
        )
        con.executemany(
            "INSERT INTO quarantine (snapshot, raw_ref, reason, payload_json) VALUES (?, ?, ?, ?)",
            [(label, ref, reason, json.dumps(payload, ensure_ascii=False)) for ref, reason, payload in quarantine],
        )


def run() -> None:
    snapshot = config.SNAPSHOT
    try:
        directory = raw_dir(snapshot)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
    if not directory.is_dir():
        print(f"raw folder not found: {directory}", file=sys.stderr)
        raise SystemExit(1)
    label = snapshot if snapshot != "live" else directory.name

    hiring = codelists.load("hiringpaths", codelists.latest_dir())
    if not hiring:
        print("note: no code lists under data/reference/codelists/ -- hiring paths keep raw codes",
              file=sys.stderr)

    con = store.connect()
    run_id = store.start_run(con, "normalize", label)
    try:
        postings, quarantine, counts, report = normalize_dir(directory, hiring)
        save(con, label, postings, quarantine)
    except Exception:
        store.finish_run(con, run_id, {}, status="failed")
        raise
    store.finish_run(con, run_id, counts)
    path = store.write_report(f"normalize-{label}", report)

    print(f"normalize [{label}] from {directory}")
    for key, value in counts.items():
        print(f"  {key:<24}{value}")
    if report["quality_flags"]:
        print("  quality flags:")
        for flag, n in sorted(report["quality_flags"].items(), key=lambda kv: -kv[1]):
            print(f"    {flag:<26}{n}")
    drift = report["schema_drift"]
    if drift["warn"]:
        print(f"  WARNING schema drift -- new fields from the API: {', '.join(drift['warn'])}")
    if drift["missing_used_fields"]:
        print(f"  WARNING fields this pipeline reads are missing: {drift['missing_used_fields']}")
    print(f"  report: {path}")
