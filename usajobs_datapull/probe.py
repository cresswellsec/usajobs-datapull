"""Measure what the USAJOBS Search API allows.

  1. headers    every response header
  2. caching    does a conditional request get a 304?
  3. page size  PROBE_PAGE_SIZES: count, bytes, time
  4. window     where results stop on an unfiltered query
  5. burst      (opt-in) N requests, no delay; stop at the first 403/429/503
  6. rate       (opt-in) step through PROBE_RATE_STEPS req/s; stop at the first
                403/429/503. Sequential, so "achieved" is the real rate.

Report: data/reference/api-probes/<timestamp>.json
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Callable

import requests

from . import config
from .client import ApiError, AuthError, Client

LIMIT_HINTS = ("ratelimit", "rate-limit", "retry-after", "quota", "throttl", "remaining", "limit")
THROTTLE_CODES = (403, 429, 503)
WINDOW_PAGE_SIZE = 500


def _timed(client: Client, timer: Callable[[], float], params: dict, extra_headers: dict | None = None):
    """One request; returns (response, seconds the API took). The client's
    pacing pause is not counted."""
    resp = client.get_raw(config.USAJOBS_SEARCH_URL, params, extra_headers)
    return resp, round(client.last_seconds, 3)


def _counts(resp) -> dict:
    try:
        result = resp.json().get("SearchResult") or {}
    except ValueError:
        return {"got": None, "total": None}
    return {"got": result.get("SearchResultCount"), "total": result.get("SearchResultCountAll")}


def _check_auth(resp) -> None:
    if resp.status_code == 401:
        raise AuthError("401 from USAJOBS: check USAJOBS_API_KEY and USAJOBS_USER_AGENT")


def _headers(resp) -> dict:
    """Response headers, with cookie values dropped (only their names are kept)."""
    out = {}
    for name, value in resp.headers.items():
        if name.lower() == "set-cookie":
            value = value.split("=", 1)[0] + "=<dropped>"
        out[name] = value
    return out


def _p95(times: list[float]) -> float:
    ordered = sorted(times)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def _rate_ramp(client: Client, timer: Callable[[], float]) -> dict:
    """Step through PROBE_RATE_STEPS; stop everything at the first throttle."""
    steps, throttled = [], None
    for rps in config.PROBE_RATE_STEPS:
        client.delay = 1.0 / rps        # Client._pace keeps at least this gap between requests
        times, statuses = [], {}
        start = timer()
        for i in range(1, config.PROBE_RATE_STEP_REQUESTS + 1):
            resp, secs = _timed(client, timer, {"ResultsPerPage": 25})
            times.append(secs)
            statuses[str(resp.status_code)] = statuses.get(str(resp.status_code), 0) + 1
            if resp.status_code in THROTTLE_CODES:
                throttled = {"target_rps": rps, "after_in_step": i, "status": resp.status_code,
                             "headers": _headers(resp)}
                break
        elapsed = timer() - start
        steps.append({"target_rps": rps, "sent": len(times), "statuses": statuses,
                      "achieved_rps": round(len(times) / elapsed, 2) if elapsed > 0 else None,
                      "avg_seconds": round(sum(times) / len(times), 3), "p95_seconds": _p95(times)})
        if throttled:
            break
    ok = [s for s in steps if not throttled or s["target_rps"] != throttled["target_rps"]]
    return {"steps": steps, "throttled": throttled,
            "fastest_clean_rps": max((s["achieved_rps"] or 0 for s in ok), default=None)}


def probe(client: Client, burst: int = 0, rate: bool = False,
          timer: Callable[[], float] = time.perf_counter) -> dict:
    """Run the probe steps; return the report (nothing is written)."""
    report: dict = {"probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "endpoint": config.USAJOBS_SEARCH_URL}

    # 1. headers
    first, secs = _timed(client, timer, {"Keyword": "Software"})
    _check_auth(first)
    headers = _headers(first)
    report["headers"] = {"status": first.status_code, "seconds": secs, "bytes": len(first.content),
                         "response": headers,
                         "rate_limit_like": sorted(h for h in headers if any(k in h.lower() for k in LIMIT_HINTS))}

    # 2. caching
    validators = {"If-Modified-Since": first.headers.get("Last-Modified") or first.headers.get("Date")}
    if first.headers.get("ETag"):
        validators["If-None-Match"] = first.headers["ETag"]
    conditional, _ = _timed(client, timer, {"Keyword": "Software"}, validators)
    report["caching"] = {name: first.headers.get(name) for name in
                         ("ETag", "Last-Modified", "Cache-Control", "Content-Encoding")}
    report["caching"]["conditional_status"] = conditional.status_code
    report["caching"]["supports_304"] = conditional.status_code == 304

    # 3. page size (no keyword: all open jobs)
    sizes = []
    for n in config.PROBE_PAGE_SIZES:
        resp, secs = _timed(client, timer, {"ResultsPerPage": n})
        sizes.append({"asked": n, "status": resp.status_code, **_counts(resp),
                      "bytes": len(resp.content), "seconds": secs})
    report["page_size"] = sizes

    # 4. result window: the last page the reported total implies, and one past it
    total = next((s["total"] for s in sizes if s["asked"] == WINDOW_PAGE_SIZE and s["total"]), None)
    window: dict = {"reported_total": total, "page_size": WINDOW_PAGE_SIZE, "pages": []}
    if total:
        last = math.ceil(int(total) / WINDOW_PAGE_SIZE)
        for page in (last, last + 1):
            resp, secs = _timed(client, timer, {"ResultsPerPage": WINDOW_PAGE_SIZE, "Page": page})
            window["pages"].append({"page": page, "status": resp.status_code, "got": _counts(resp)["got"]})
        window["at_known_cap"] = int(total) >= config.SEARCH_RESULT_WINDOW
    report["window"] = window

    # 5. burst (opt-in): no pacing, stop at the first sign of throttling
    if burst:
        burst = min(burst, config.PROBE_MAX_BURST)
        client.delay = 0
        times, stopped = [], None
        for i in range(1, burst + 1):
            resp, secs = _timed(client, timer, {"ResultsPerPage": 25})
            times.append(secs)
            if resp.status_code in THROTTLE_CODES:
                stopped = {"after": i, "status": resp.status_code, "headers": _headers(resp)}
                break
        report["burst"] = {"asked": burst, "sent": len(times), "throttled": stopped,
                           "avg_seconds": round(sum(times) / len(times), 3), "max_seconds": max(times)}

    # 6. rate ramp (opt-in)
    if rate:
        report["rate"] = _rate_ramp(client, timer)

    report["requests"] = client.requests_made
    return report


def summarize(report: dict) -> list[str]:
    """Human-readable lines for the console."""
    h, c, w = report["headers"], report["caching"], report["window"]
    lines = [
        f"headers: {h['status']} in {h['seconds']}s; rate-limit-like: {', '.join(h['rate_limit_like']) or 'none'}",
        f"caching: ETag={c['ETag']} Cache-Control={c['Cache-Control']} gzip={c['Content-Encoding']}; "
        f"conditional request -> {c['conditional_status']}",
        "page size: asked/got/total/KB/s",
    ]
    for s in report["page_size"]:
        kb = s["bytes"] / 1024
        lines.append(f"  {s['asked']:>5} {str(s['got']):>5} {str(s['total']):>7} {kb:>8,.0f} {s['seconds']:>6}"
                     + ("" if s["status"] == 200 else f"  status {s['status']}"))
    lines.append(f"window: reported total {w['reported_total']}"
                 + ("".join(f"; page {p['page']} -> {p['got']}" for p in w["pages"]))
                 + ("  (at the known 10,000 cap: the real total may be higher)" if w.get("at_known_cap") else ""))
    if "burst" in report:
        b = report["burst"]
        t = b["throttled"]
        lines.append(f"burst: {b['sent']}/{b['asked']} sent, avg {b['avg_seconds']}s, max {b['max_seconds']}s; "
                     + (f"THROTTLED after {t['after']} ({t['status']})" if t else "no throttling"))
    if "rate" in report:
        r = report["rate"]
        lines.append("rate: target req/s -> achieved, sent, avg/p95 seconds, statuses")
        for s in r["steps"]:
            lines.append(f"  {s['target_rps']:>3} -> {s['achieved_rps']}, {s['sent']} sent, "
                         f"{s['avg_seconds']}/{s['p95_seconds']}s, {s['statuses']}")
        t = r["throttled"]
        lines.append(f"  THROTTLED at {t['target_rps']} req/s after {t['after_in_step']} requests ({t['status']}, "
                     f"Retry-After {t['headers'].get('Retry-After', 'not given')})" if t
                     else f"  no throttling; fastest clean rate {r['fastest_clean_rps']} req/s")
    lines.append(f"{report['requests']} requests")
    return lines


def run(burst: int = 0, rate: bool = False) -> None:
    config.require("USAJOBS_API_KEY", "USAJOBS_USER_AGENT")
    config.ensure_dirs()
    client = Client(config.USAJOBS_API_KEY, config.USAJOBS_USER_AGENT)
    try:
        report = probe(client, burst=burst, rate=rate)
    except (ApiError, requests.exceptions.RequestException) as e:
        raise SystemExit(f"probe stopped: {e}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    path = config.API_PROBES / f"{stamp}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("\n".join(summarize(report)))
    print(f"report: {path}")
