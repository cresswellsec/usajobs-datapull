"""probe against a fake USAJOBS that behaves like the real one did on 2026-10-13."""
import json

import pytest

from usajobs_datapull import config
from usajobs_datapull.client import AuthError, Client
from usajobs_datapull.probe import probe, summarize

DATE = "Tue, 13 Oct 2026 19:22:56 GMT"


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers if headers is not None else {
            "Content-Type": "application/hr+json; charset=utf-8", "Date": DATE,
            "Set-Cookie": "akavpau_DATA_USAJ=123~id=abc; Domain=data.usajobs.gov"}
        self.content = json.dumps(body).encode() if body is not None else b""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeUsajobs:
    """10,000-result window, no caching, optional throttling after N requests."""
    def __init__(self, total=10_000, throttle_after=None, status=200):
        self.total, self.throttle_after, self.status = total, throttle_after, status
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"headers": headers, "params": params})
        if self.status != 200:
            return Resp(self.status, headers={})
        if self.throttle_after and len(self.calls) > self.throttle_after:
            return Resp(429, headers={"Retry-After": "60"})
        per = params.get("ResultsPerPage", 25)
        page = params.get("Page", 1)
        got = max(0, min(per, min(self.total, config.SEARCH_RESULT_WINDOW) - (page - 1) * per))
        body = {"SearchResult": {"SearchResultCount": got, "SearchResultCountAll": self.total,
                                 "SearchResultItems": [{}] * got}}
        return Resp(body=body)


def client_for(session):
    return Client("key", "me@example.com", session=session, sleep=lambda s: None, clock=lambda: 0.0)


def test_report_records_headers_caching_sizes_and_window():
    api = FakeUsajobs()
    report = probe(client_for(api), timer=lambda: 0.0)

    assert report["headers"]["rate_limit_like"] == []
    assert report["headers"]["response"]["Set-Cookie"] == "akavpau_DATA_USAJ=<dropped>"
    assert report["caching"]["supports_304"] is False
    assert api.calls[1]["headers"]["If-Modified-Since"] == DATE
    assert [s["got"] for s in report["page_size"]] == list(config.PROBE_PAGE_SIZES)
    assert [p["got"] for p in report["window"]["pages"]] == [500, 0]
    assert report["window"]["at_known_cap"] is True
    assert "burst" not in report
    assert report["requests"] == 2 + len(config.PROBE_PAGE_SIZES) + 2


def test_secrets_never_reach_the_report():
    report = probe(client_for(FakeUsajobs()), timer=lambda: 0.0)
    text = json.dumps(report)
    assert "Authorization-Key" not in text and "me@example.com" not in text


def test_burst_stops_at_first_throttle():
    api = FakeUsajobs(throttle_after=10)   # the 8 probe requests, then 2 burst requests succeed
    report = probe(client_for(api), burst=50, timer=lambda: 0.0)
    assert report["burst"]["sent"] == 3
    assert report["burst"]["throttled"]["status"] == 429
    assert report["burst"]["throttled"]["headers"]["Retry-After"] == "60"
    assert any("THROTTLED after 3" in line for line in summarize(report))


def test_burst_is_capped():
    report = probe(client_for(FakeUsajobs()), burst=10_000, timer=lambda: 0.0)
    assert report["burst"]["asked"] == config.PROBE_MAX_BURST
    assert report["burst"]["throttled"] is None


def test_bad_key_stops_immediately():
    api = FakeUsajobs(status=401)
    with pytest.raises(AuthError):
        probe(client_for(api), timer=lambda: 0.0)
    assert len(api.calls) == 1


def test_cli_accepts_probe_burst(monkeypatch):
    from usajobs_datapull import __main__ as cli, probe as probe_mod
    seen = {}
    monkeypatch.setattr(probe_mod, "run", lambda burst=0, rate=False: seen.update(burst=burst, rate=rate))
    cli.main(["probe", "--burst", "20"])
    assert seen == {"burst": 20, "rate": False}
    cli.main(["probe", "--rate"])
    assert seen == {"burst": 0, "rate": True}


def test_env_example_blank_settings_stay_blank():
    """`NAME=   # comment` makes python-dotenv read the comment as the value,
    so `check` reported a fake HF_TOKEN as set."""
    from dotenv import dotenv_values
    values = dotenv_values(config.ROOT / ".env.example")
    assert not [k for k, v in values.items() if (v or "").startswith("#")]
    assert values["USAJOBS_API_KEY"] == values["HF_TOKEN"] == values["USAJOBS_USER_AGENT"] == ""


class Clock:
    """Fake time shared by the client's pacing and the probe's timer; each request takes 0.1 s."""
    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += s

    def time(self):
        return self.now


class TimedFake(FakeUsajobs):
    def __init__(self, clock, **kw):
        super().__init__(**kw)
        self.clock = clock

    def get(self, *a, **kw):
        self.clock.now += 0.1
        return super().get(*a, **kw)


def paced_client(api, clock):
    return Client("key", "me@example.com", session=api, sleep=clock.sleep, clock=clock.time)


def test_rate_ramp_paces_each_step_and_reports_achieved_rate():
    clock = Clock()
    api = TimedFake(clock)
    report = probe(paced_client(api, clock), rate=True, timer=clock.time)
    steps = report["rate"]["steps"]
    assert [s["target_rps"] for s in steps] == list(config.PROBE_RATE_STEPS)
    assert all(s["sent"] == config.PROBE_RATE_STEP_REQUESTS for s in steps)
    assert report["rate"]["throttled"] is None
    by_target = {s["target_rps"]: s["achieved_rps"] for s in steps}
    assert by_target[1] == pytest.approx(1, abs=0.1)
    assert by_target[2] == pytest.approx(2, abs=0.2)
    # 0.1 s per answer caps sequential requests near 10/s, so 8/s is still reachable
    assert by_target[8] == pytest.approx(8, abs=0.5)
    assert report["rate"]["fastest_clean_rps"] == max(by_target.values())


def test_rate_ramp_stops_at_first_throttle():
    clock = Clock()
    probe_requests = 2 + len(config.PROBE_PAGE_SIZES) + 2
    api = TimedFake(clock, throttle_after=probe_requests + config.PROBE_RATE_STEP_REQUESTS + 5)
    report = probe(paced_client(api, clock), rate=True, timer=clock.time)
    r = report["rate"]
    assert [s["target_rps"] for s in r["steps"]] == list(config.PROBE_RATE_STEPS[:2])
    assert r["throttled"]["target_rps"] == config.PROBE_RATE_STEPS[1]
    assert r["throttled"]["after_in_step"] == 6
    assert r["throttled"]["headers"]["Retry-After"] == "60"
    assert r["fastest_clean_rps"] == r["steps"][0]["achieved_rps"]
    assert len(api.calls) == probe_requests + config.PROBE_RATE_STEP_REQUESTS + 6   # nothing after the throttle
    assert any("THROTTLED at 2 req/s" in line for line in summarize(report))


def test_rate_is_off_by_default():
    assert "rate" not in probe(client_for(FakeUsajobs()), timer=lambda: 0.0)


def test_timings_exclude_pacing():
    """At 1 req/s the client sleeps ~0.9 s between requests; only the 0.1 s answer counts."""
    clock = Clock()
    report = probe(paced_client(TimedFake(clock), clock), rate=True, timer=clock.time)
    assert report["headers"]["seconds"] == pytest.approx(0.1)
    assert all(s["seconds"] == pytest.approx(0.1) for s in report["page_size"])
    assert all(s["avg_seconds"] == pytest.approx(0.1) for s in report["rate"]["steps"])
