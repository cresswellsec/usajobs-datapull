"""USAJOBS API -> data/raw/<YYYY-MM-DD>/page-NNN.json

One query for all configured series (ResultsPerPage=500, WhoMayApply=All,
Fields=Full). Pages are saved untouched into <date>.partial/ and renamed only
when every page arrived; a page that fails after retries stops the run.
_fetch.json records the request and result. Capped at MAX_PAGES.

SNAPSHOT=archive-<date> skips the API.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, store
from .client import ApiError, Client

log = logging.getLogger("usajobs_datapull.fetch")


def series_list(spec: str) -> list[str]:
    codes = [c.strip() for c in spec.split(";") if c.strip()]
    if not codes or not all(c.isdigit() and len(c) == 4 for c in codes):
        raise ValueError(f"JOB_SERIES must be 4-digit codes separated by ';', got {spec!r}")
    return codes


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def fetch_snapshot(client: Client, series: list[str], raw_root: Path, day: str) -> tuple[Path, dict]:
    """Fetch every page for `series` into raw_root/<day>/. Returns (folder, summary)."""
    final = raw_root / day
    partial = raw_root / f"{day}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)

    params_base = {
        "JobCategoryCode": ";".join(series),
        "ResultsPerPage": config.RESULTS_PER_PAGE,
        "WhoMayApply": "All",
        "Fields": "Full",
    }
    summary = {
        "snapshot": day,
        "series": series,
        "params": params_base,          # no key in here: headers are not logged
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": 0,
        "items": 0,
        "reported_total": None,
        "complete": False,
    }

    page, pages = 1, 1
    while page <= pages:
        if page > config.MAX_PAGES:
            raise ApiError(f"stopped at MAX_PAGES={config.MAX_PAGES}; narrow JOB_SERIES")
        data = client.get_json(config.USAJOBS_SEARCH_URL, {**params_base, "Page": page})
        result = data.get("SearchResult") or {}
        items = result.get("SearchResultItems") or []
        total = int(result.get("SearchResultCountAll") or 0)
        declared = (result.get("UserArea") or {}).get("NumberOfPages")
        pages = int(declared) if str(declared or "").isdigit() else math.ceil(total / config.RESULTS_PER_PAGE)
        pages = max(pages, 1)
        if total >= config.SEARCH_RESULT_WINDOW:
            raise ApiError(f"the API reports {total} results, and one query never returns more than "
                           f"{config.SEARCH_RESULT_WINDOW}: results would be missing. Narrow JOB_SERIES")

        _write_atomic(partial / f"page-{page:03d}.json", data)
        summary["pages"] = page
        summary["items"] += len(items)
        summary["reported_total"] = total
        log.info("page %d/%d: %d postings (API reports %d total)", page, pages, len(items), total)

        if not items or len(items) < config.RESULTS_PER_PAGE:
            break
        page += 1

    if summary["reported_total"] and summary["items"] != summary["reported_total"]:
        log.warning("received %d postings but the API reported %d -- check the raw pages",
                    summary["items"], summary["reported_total"])

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary["requests"] = client.requests_made
    summary["complete"] = True
    _write_atomic(partial / "_fetch.json", summary)

    if final.exists():                 # re-fetching the same day replaces it
        shutil.rmtree(final)
    os.replace(partial, final)
    return final, summary


def run() -> None:
    if config.SNAPSHOT != "live":
        print(f"SNAPSHOT={config.SNAPSHOT}: using existing raw data, nothing to fetch.")
        return
    config.require("USAJOBS_API_KEY", "USAJOBS_USER_AGENT")
    config.ensure_dirs()
    from . import log as logsetup
    logsetup.setup("fetch")

    series = series_list(config.JOB_SERIES)
    day = config.today().isoformat()
    con = store.connect()
    run_id = store.start_run(con, "fetch", day)
    client = Client(config.USAJOBS_API_KEY, config.USAJOBS_USER_AGENT)
    log.info("fetching series %s into %s", ";".join(series), config.RAW / day)
    try:
        folder, summary = fetch_snapshot(client, series, config.RAW, day)
    except (ApiError, KeyboardInterrupt) as e:
        store.finish_run(con, run_id, {"requests": client.requests_made}, status="failed")
        log.error("fetch stopped: %s", e or "interrupted")
        log.error("partial pages kept in %s (ignored by normalize)", config.RAW / f"{day}.partial")
        raise SystemExit(1)
    store.finish_run(con, run_id, {k: summary[k] for k in ("pages", "items", "reported_total", "requests")})
    log.info("done: %d postings in %d page(s), %d request(s) -> %s",
             summary["items"], summary["pages"], summary["requests"], folder)
