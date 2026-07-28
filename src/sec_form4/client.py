"""A small, polite HTTP client for SEC EDGAR.

Everything that talks to the SEC goes through this module, which gives us one
place that guarantees:
  1. the required User-Agent header is present on every request
  2. we never exceed our self-imposed request rate
  3. transient failures are retried with backoff instead of crashing the run
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)


class RateLimiter:
    """Enforces a minimum gap between calls.

    This is the simplest correct approach: remember when the last request
    finished, and if the next one comes too soon, sleep the difference. At 5
    req/s the minimum gap is 0.2s.

    We use time.monotonic() rather than time.time() because monotonic never
    jumps backwards (NTP corrections, DST). A clock that goes backwards would
    make the limiter think no time has passed and stall, or think a lot has
    passed and burst.
    """

    def __init__(self, requests_per_second: float) -> None:
        self.min_interval = 1.0 / requests_per_second
        self._last_call: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self.min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()


class SECClient:
    """Wraps requests.Session with SEC-specific headers, throttling, retries."""

    def __init__(self, user_agent: str | None = None) -> None:
        # get_user_agent() raises if unconfigured, so we fail at construction
        # time rather than on the first request.
        self.user_agent = user_agent or config.get_user_agent()

        # A Session reuses the underlying TCP connection across requests, which
        # both speeds things up and is gentler on the SEC's servers than
        # opening a fresh connection every time.
        self.session = requests.Session()
        self.session.headers.update(
            {
                # THE important header. The SEC's fair-access policy requires
                # automated traffic to identify a real contact. Without it you
                # get a 403 block page; abuse gets your IP banned outright.
                "User-Agent": self.user_agent,
                # Ask for compressed responses. company_tickers.json is ~1 MB
                # raw; gzipped it's a fraction of that. Less bandwidth for both
                # sides is part of being a good citizen.
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            }
        )
        self.limiter = RateLimiter(config.REQUESTS_PER_SECOND)

    def get(self, url: str) -> requests.Response:
        """GET a URL, throttled and retried. Raises on unrecoverable failure."""
        last_error: Exception | None = None

        for attempt in range(config.MAX_RETRIES):
            self.limiter.wait()
            log.debug("GET %s (attempt %d)", url, attempt + 1)

            try:
                response = self.session.get(
                    url, timeout=config.REQUEST_TIMEOUT_SECONDS
                )
            except requests.RequestException as exc:
                # Network-level problem (DNS, connection reset, timeout).
                # Worth retrying.
                last_error = exc
                log.warning("Request error for %s: %s", url, exc)
            else:
                # 429 = we're going too fast. 5xx = SEC-side problem.
                # Both are temporary, so back off and try again.
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = requests.HTTPError(
                        f"HTTP {response.status_code} for {url}"
                    )
                    log.warning(
                        "HTTP %s from %s — backing off", response.status_code, url
                    )
                elif response.status_code == 403:
                    # Almost always a User-Agent problem. Retrying won't help,
                    # so give a diagnosis instead of burning attempts.
                    raise PermissionError(
                        f"403 Forbidden from {url}.\n"
                        f"User-Agent sent: {self.user_agent!r}\n"
                        "The SEC blocks requests without a descriptive "
                        "User-Agent containing a real contact email."
                    )
                elif response.status_code == 404:
                    # Also not retryable — usually a malformed CIK (wrong
                    # zero-padding) or a genuinely nonexistent resource.
                    response.raise_for_status()
                else:
                    response.raise_for_status()
                    return response

            # Exponential backoff: 2s, 4s, 8s. Skip the sleep after the final
            # attempt, since we're about to give up anyway.
            if attempt < config.MAX_RETRIES - 1:
                delay = config.BACKOFF_BASE_SECONDS * (2**attempt)
                log.info("Retrying %s in %.1fs", url, delay)
                time.sleep(delay)

        raise RuntimeError(
            f"Failed to fetch {url} after {config.MAX_RETRIES} attempts"
        ) from last_error

    def get_json(self, url: str) -> Any:
        """GET a URL and parse the body as JSON."""
        response = self.get(url)
        try:
            return response.json()
        except ValueError as exc:
            # If the SEC serves an HTML error/block page with a 200 status,
            # this is where it surfaces. Show a snippet so the cause is obvious.
            snippet = response.text[:200].replace("\n", " ")
            raise RuntimeError(
                f"Expected JSON from {url} but got something else: {snippet!r}"
            ) from exc
