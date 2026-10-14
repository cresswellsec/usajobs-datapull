"""client + fetch against a fake USAJOBS (no network, no real sleeping)."""
import json
from pathlib import Path

import pytest
import requests

from usajobs_datapull import client as client_mod, normalize
from usajobs_datapull.client import ApiError, AuthError, Client, RuntimeLimit, wait_from_headers
from usajobs_datapull.fetch import fetch_snapshot, series_list

SAMPLE = Path(__file__).parent / "fixtures" / "archive-2024-11-12-sample.json"


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Returns queued responses (or raises queued exceptions) in order."""
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.now += s

    def clock(self):
        return self.now


def make_client(session, t=None, **kw):
    t = t or FakeTime()
    c = Client("key", "me@example.com", session=session, sleep=t.sleep, clock=t.clock,
               wallclock=lambda: 1_700_000_000.0, rand=lambda: 0.0, **kw)
    return c, t


def page(items, total, pages=None):
    ua = {"NumberOfPages": str(pages)} if pages is not None else {}
    return {"SearchResult": {"SearchResultCountAll": total, "SearchResultItems": items, "UserArea": ua}}


# --- client ----------------------------------------------------------------

def test_headers_and_timeout_are_sent():
    s = FakeSession(FakeResponse(body={"ok": 1}))
    c, _ = make_client(s)
    c.get_json("u", {"a": 1})
    call = s.calls[0]
    assert call["headers"] == {"Host": "data.usajobs.gov", "User-Agent": "me@example.com", "Authorization-Key": "key"}
    assert call["timeout"] is not None


def test_user_agent_must_be_an_email():
    with pytest.raises(AuthError):
        Client("key", "not-an-email")


def test_polite_delay_between_requests():
    s = FakeSession(FakeResponse(body={}), FakeResponse(body={}))
    c, t = make_client(s)
    c.get_json("u")
    c.get_json("u")
    assert t.sleeps == [1.0]          # second request waited out the 1 s gap


def test_low_remaining_waits_for_reset_seconds():
    s = FakeSession(FakeResponse(body={}, headers={"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": "42"}))
    c, t = make_client(s)
    c.get_json("u")
    assert t.sleeps == [42.0]


def test_reset_as_unix_timestamp_is_not_a_50_year_sleep():
    assert wait_from_headers({"X-RateLimit-Reset": str(1_700_000_030)}, now=1_700_000_000.0) == 30.0


def test_429_uses_retry_after_then_succeeds():
    s = FakeSession(FakeResponse(429, headers={"Retry-After": "7"}, text="slow down"),
                    FakeResponse(body={"ok": True}))
    c, t = make_client(s)
    assert c.get_json("u") == {"ok": True}
    assert 7.0 in t.sleeps


def test_429_with_no_headers_waits_default():
    s = FakeSession(FakeResponse(429, text=""), FakeResponse(body={}))
    c, t = make_client(s)
    c.get_json("u")
    assert 60.0 in t.sleeps


def test_refuses_absurd_server_wait():
    s = FakeSession(FakeResponse(429, headers={"Retry-After": "86400"}, text=""))
    c, _ = make_client(s)
    with pytest.raises(ApiError, match="cap"):
        c.get_json("u")


def test_5xx_and_network_errors_back_off_then_succeed():
    s = FakeSession(FakeResponse(503, text="down"),
                    requests.exceptions.ConnectionError("reset"),
                    requests.exceptions.ReadTimeout("slow"),
                    FakeResponse(body={"ok": 1}))
    c, t = make_client(s)
    assert c.get_json("u") == {"ok": 1}
    assert t.sleeps == [1.0, 2.0, 4.0]       # exponential backoff (jitter fixed at 0 here)


def test_gives_up_after_max_retries():
    s = FakeSession(*[FakeResponse(500, text="boom") for _ in range(5)])
    c, _ = make_client(s)
    with pytest.raises(ApiError, match="gave up after 5"):
        c.get_json("u")
    assert len(s.calls) == 5


def test_auth_error_stops_immediately():
    s = FakeSession(FakeResponse(401, text="unauthorized"), FakeResponse(body={}))
    c, _ = make_client(s)
    with pytest.raises(AuthError, match="USAJOBS_API_KEY"):
        c.get_json("u")
    assert len(s.calls) == 1                  # no retries with a bad key


def test_other_4xx_is_not_retried():
    s = FakeSession(FakeResponse(400, text="bad param"))
    c, _ = make_client(s)
    with pytest.raises(ApiError, match="400"):
        c.get_json("u")
    assert len(s.calls) == 1


def test_runtime_ceiling():
    s = FakeSession(FakeResponse(body={}), FakeResponse(body={}))
    c, t = make_client(s, max_runtime_s=0.5)
    c.get_json("u")
    t.now += 1
    with pytest.raises(RuntimeLimit):
        c.get_json("u")


# --- fetch -----------------------------------------------------------------

def descriptors(n, start=0):
    base = json.loads(SAMPLE.read_text(encoding="utf-8"))[3]
    out = []
    for i in range(start, start + n):
        d = dict(base, PositionURI=f"https://www.usajobs.gov:443/GetJob/ViewDetails/{900000000 + i}")
        out.append({"MatchedObjectId": str(900000000 + i), "MatchedObjectDescriptor": d})
    return out


def test_series_list_validation():
    assert series_list("2210; 0854") == ["2210", "0854"]
    with pytest.raises(ValueError):
        series_list("2210;IT")


def test_fetch_pages_writes_whole_responses_and_normalize_reads_them(tmp_path):
    s = FakeSession(FakeResponse(body=page(descriptors(500), total=503, pages=2)),
                    FakeResponse(body=page(descriptors(3, start=500), total=503, pages=2)))
    c, _ = make_client(s)
    folder, summary = fetch_snapshot(c, ["2210"], tmp_path, "2026-10-12")

    assert folder == tmp_path / "2026-10-12"
    assert not (tmp_path / "2026-10-12.partial").exists()
    assert sorted(p.name for p in folder.iterdir()) == ["_fetch.json", "page-001.json", "page-002.json"]
    assert summary["items"] == 503 and summary["complete"] is True
    assert [call["params"]["Page"] for call in s.calls] == [1, 2]
    assert s.calls[0]["params"]["JobCategoryCode"] == "2210"
    assert s.calls[0]["params"]["WhoMayApply"] == "All"
    # the page file is the untouched API response, MatchedObjectId included
    first = json.loads((folder / "page-001.json").read_text(encoding="utf-8"))
    assert first["SearchResult"]["SearchResultItems"][0]["MatchedObjectId"] == "900000000"

    postings, quarantine, counts, _ = normalize.normalize_dir(folder, {})
    assert counts["postings"] == 503 and quarantine == []   # _fetch.json is skipped


def test_short_page_stops_without_extra_request(tmp_path):
    s = FakeSession(FakeResponse(body=page(descriptors(334), total=334)))
    c, _ = make_client(s)
    _, summary = fetch_snapshot(c, ["2210"], tmp_path, "2026-10-12")
    assert summary["pages"] == 1 and len(s.calls) == 1


def test_failed_fetch_leaves_only_a_partial_folder(tmp_path):
    s = FakeSession(FakeResponse(body=page(descriptors(500), total=900, pages=2)),
                    *[FakeResponse(500, text="boom") for _ in range(5)])
    c, _ = make_client(s)
    with pytest.raises(ApiError):
        fetch_snapshot(c, ["2210"], tmp_path, "2026-10-12")
    assert not (tmp_path / "2026-10-12").exists()
    assert (tmp_path / "2026-10-12.partial" / "page-001.json").exists()
    with pytest.raises(FileNotFoundError):
        normalize.raw_dir("live", tmp_path)       # a partial is never treated as a snapshot


def test_max_pages_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod.config, "MAX_PAGES", 2)
    s = FakeSession(*[FakeResponse(body=page(descriptors(500, start=i * 500), total=5000, pages=10))
                      for i in range(3)])
    c, _ = make_client(s)
    with pytest.raises(ApiError, match="MAX_PAGES"):
        fetch_snapshot(c, ["2210"], tmp_path, "2026-10-12")
