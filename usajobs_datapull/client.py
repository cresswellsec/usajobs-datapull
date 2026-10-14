"""Polite USAJOBS API client.

Fixed delay between requests; waits on X-RateLimit-Remaining / 429 (Retry-After
or X-RateLimit-Reset, seconds or epoch, capped); retries 5xx and connection
errors with jittered backoff; stops the run on 401/403; per-request timeouts and
a whole-run deadline. Sleep and clock are injectable for tests.
"""
from __future__ import annotations

import email.utils
import logging
import random
import time
from typing import Callable

import requests

from . import config

log = logging.getLogger("usajobs_datapull.client")


class ApiError(RuntimeError):
    """The API failed in a way retrying won't fix (or retries ran out)."""


class AuthError(ApiError):
    """401/403: bad or missing key / user agent. Stop everything."""


class RuntimeLimit(ApiError):
    """The whole-run ceiling (MAX_RUNTIME_MIN) was reached."""


def wait_from_headers(headers, now: float, default: float = config.DEFAULT_WAIT_S) -> float:
    """Seconds to wait, from Retry-After or X-RateLimit-Reset.

    Retry-After may be seconds or an HTTP date. X-RateLimit-Reset may be seconds
    or a Unix timestamp (anything over 10^9 is treated as a timestamp).
    """
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(retry_after)
            if parsed is not None:
                return max(0.0, parsed.timestamp() - now)
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            value = float(reset)
            return max(0.0, value - now) if value > 1e9 else max(0.0, value)
        except ValueError:
            pass
    return float(default)


class Client:
    def __init__(
        self,
        api_key: str,
        user_agent: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
        delay: float = config.POLITE_DELAY_S,
        max_retries: int = config.MAX_RETRIES,
        max_runtime_s: float = config.MAX_RUNTIME_MIN * 60,
    ):
        if not api_key:
            raise AuthError("USAJOBS_API_KEY is empty -- set it in .env")
        if not user_agent or "@" not in user_agent:
            raise AuthError("USAJOBS_USER_AGENT must be the email you registered with USAJOBS")
        self.headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": user_agent,
            "Authorization-Key": api_key,
        }
        self.session = session or requests.Session()
        self.sleep, self.clock, self.wallclock, self.rand = sleep, clock, wallclock, rand
        self.delay = delay
        self.max_retries = max_retries
        self.deadline = clock() + max_runtime_s
        self._last_request: float | None = None
        self.requests_made = 0
        self.last_seconds = 0.0

    # -- pacing --------------------------------------------------------------

    def _pace(self) -> None:
        if self.clock() > self.deadline:
            raise RuntimeLimit(f"stopped: run exceeded {config.MAX_RUNTIME_MIN} min ceiling")
        if self._last_request is not None:
            gap = self.clock() - self._last_request
            if gap < self.delay:
                self.sleep(self.delay - gap)

    def _wait(self, seconds: float, why: str) -> None:
        if seconds > config.MAX_WAIT_S:
            raise ApiError(f"{why}: server asked for {seconds:.0f}s (> {config.MAX_WAIT_S}s cap) -- try again later")
        log.warning("%s: waiting %.0fs", why, seconds)
        self.sleep(seconds)

    def _backoff(self, attempt: int) -> float:
        return min(config.BACKOFF_CAP_S, 2 ** attempt) + self.rand()

    # -- the request ---------------------------------------------------------

    def get_json(self, url: str, params: dict | None = None) -> dict:
        last_problem = "no attempts made"
        for attempt in range(self.max_retries):
            self._pace()
            self._last_request = self.clock()
            self.requests_made += 1
            try:
                resp = self.session.get(url, headers=self.headers, params=params,
                                        timeout=config.REQUEST_TIMEOUT_S)
            except requests.exceptions.RequestException as e:   # timeouts, DNS, resets
                last_problem = f"{type(e).__name__}: {e}"
            else:
                log.debug("GET %s %s -> %s | remaining=%s reset=%s", url, params, resp.status_code,
                          resp.headers.get("X-RateLimit-Remaining"), resp.headers.get("X-RateLimit-Reset"))
                status = resp.status_code
                if status == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        last_problem = "200 with a body that isn't JSON"
                    else:
                        self._respect_remaining(resp.headers)
                        return data
                elif status == 429:
                    self._wait(wait_from_headers(resp.headers, self.wallclock()), "rate limited (429)")
                    continue  # the wait *is* the backoff
                elif status in (401, 403):
                    raise AuthError(f"{status} from USAJOBS: check USAJOBS_API_KEY and USAJOBS_USER_AGENT "
                                    f"(the email you registered with). Body: {resp.text[:200]}")
                elif status >= 500 or status == 408:
                    last_problem = f"server error {status}"
                else:
                    raise ApiError(f"{status} from USAJOBS for {params}: {resp.text[:300]}")

            if attempt + 1 < self.max_retries:
                wait = self._backoff(attempt)
                log.warning("%s -- retry %d/%d in %.1fs", last_problem, attempt + 1, self.max_retries - 1, wait)
                self.sleep(wait)
        raise ApiError(f"gave up after {self.max_retries} attempts: {last_problem}")

    def get_raw(self, url: str, params: dict | None = None,
                extra_headers: dict | None = None) -> requests.Response:
        """One paced request with no retries, returning the response as-is.

        For `probe`, which needs to see statuses and headers that get_json
        would retry or hide. `last_seconds` is how long the API took to answer
        (pacing excluded).
        """
        self._pace()
        self._last_request = start = self.clock()
        self.requests_made += 1
        resp = self.session.get(url, headers={**self.headers, **(extra_headers or {})}, params=params,
                                timeout=config.REQUEST_TIMEOUT_S)
        self.last_seconds = self.clock() - start
        return resp

    def _respect_remaining(self, headers) -> None:
        remaining = headers.get("X-RateLimit-Remaining")
        try:
            remaining_n = int(remaining) if remaining is not None else None
        except ValueError:
            remaining_n = None
        if remaining_n is not None and remaining_n < config.RATE_LIMIT_FLOOR:
            self._wait(wait_from_headers(headers, self.wallclock()),
                       f"rate limit low ({remaining_n} left)")
