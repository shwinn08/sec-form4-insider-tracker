"""Resolve stock tickers to SEC CIKs.

EDGAR indexes everything by CIK (Central Index Key), not by ticker, and there
is no "look up one ticker" API. Instead the SEC publishes a single static JSON
file mapping every exchange-listed ticker to its CIK. We download that once,
cache it, and resolve all tickers in memory.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .client import SECClient

log = logging.getLogger(__name__)

CACHE_FILE = config.CACHE_DIR / "company_tickers.json"
CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # tickers change rarely; a week is plenty


@dataclass(frozen=True)
class Company:
    """One resolved company, holding both CIK forms.

    EDGAR is inconsistent about CIK formatting:
      - the submissions API wants the 10-digit zero-padded form ("0000320193")
      - the Archives directory path wants the unpadded form ("320193")
    Storing both means callers never have to remember which is which.
    """

    ticker: str
    cik: int          # 320193
    cik_padded: str   # "0000320193"
    name: str         # "Apple Inc."


def load_tickers(path: Path | None = None) -> list[str]:
    """Read the ticker list from config/tickers.txt.

    Ignores blank lines and # comments; uppercases and de-duplicates while
    preserving the order you wrote them in.
    """
    path = path or config.TICKERS_FILE
    if not path.exists():
        raise FileNotFoundError(f"Ticker list not found: {path}")

    seen: set[str] = set()
    tickers: list[str] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line and line not in seen:
            seen.add(line)
            tickers.append(line)

    if not tickers:
        raise ValueError(f"No tickers found in {path}")
    return tickers


def load_cik_overrides(path: Path | None = None) -> dict[str, int]:
    """Read config/cik_overrides.json: {TICKER: cik}.

    Overrides exist because a ticker's CIK is not stable over time. After a
    merger or holding-company reorganization the ticker is reassigned to a new
    registrant, while the filing history stays with the predecessor, and
    company_tickers.json only ever shows you the current one. Without a way to
    pin the CIK you'd silently get zero filings and no error.

    Keys beginning with "_" are treated as comments and skipped.
    """
    path = path or config.CIK_OVERRIDES_FILE
    if not path.exists():
        return {}

    raw = json.loads(path.read_text())
    return {
        ticker.upper(): int(cik)
        for ticker, cik in raw.items()
        if not ticker.startswith("_")
    }


def _fetch_ticker_map(client: SECClient, force_refresh: bool = False) -> dict:
    """Return the raw company_tickers.json payload, using a local cache.

    Caching matters for more than speed: during development you re-run this
    script constantly, and there's no reason to pull a 1 MB file from the SEC
    every time to answer the same eight questions.
    """
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if CACHE_FILE.exists() and not force_refresh:
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_MAX_AGE_SECONDS:
            log.info("Using cached ticker map (%.1f hours old)", age / 3600)
            return json.loads(CACHE_FILE.read_text())
        log.info("Cached ticker map is stale (%.1f days old); refreshing", age / 86400)

    log.info("Downloading ticker->CIK map from %s", config.COMPANY_TICKERS_URL)
    data = client.get_json(config.COMPANY_TICKERS_URL)
    CACHE_FILE.write_text(json.dumps(data))
    log.info("Cached %d companies to %s", len(data), CACHE_FILE)
    return data


def build_lookup(client: SECClient, force_refresh: bool = False) -> dict[str, Company]:
    """Turn the SEC's payload into a {TICKER: Company} dict.

    The raw file looks like:
        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}

    The outer keys are meaningless row numbers, so we throw them away and
    re-key by ticker. Note cik_str is an *integer* with leading zeros already
    stripped, so the zero-padding we need is reconstructed here with zfill(10).
    """
    raw = _fetch_ticker_map(client, force_refresh=force_refresh)

    lookup: dict[str, Company] = {}
    for entry in raw.values():
        cik = int(entry["cik_str"])
        ticker = entry["ticker"].upper()
        lookup[ticker] = Company(
            ticker=ticker,
            cik=cik,
            cik_padded=str(cik).zfill(10),
            name=entry["title"],
        )
    return lookup


def resolve_tickers(
    tickers: list[str], client: SECClient, force_refresh: bool = False
) -> tuple[list[Company], list[str]]:
    """Resolve a list of tickers.

    Returns (resolved, unresolved). Unknown tickers are reported rather than
    raised: one typo shouldn't abandon the other seven companies. Common causes
    of a miss are delistings, foreign issuers that file 20-F instead of 10-K,
    and share-class suffixes (BRK.B is listed as "BRK-B" in this file).

    config/cik_overrides.json takes precedence over the SEC mapping.
    """
    lookup = build_lookup(client, force_refresh=force_refresh)
    overrides = load_cik_overrides()

    resolved: list[Company] = []
    unresolved: list[str] = []
    for ticker in tickers:
        ticker = ticker.upper()

        if ticker in overrides:
            cik = overrides[ticker]
            # The name is provisional: an overridden CIK often isn't in
            # company_tickers.json at all (predecessor entities get dropped).
            # fetch_form4_filings replaces it with the authoritative name from
            # the submissions payload.
            company = Company(
                ticker=ticker,
                cik=cik,
                cik_padded=str(cik).zfill(10),
                name=f"(CIK {cik})",
            )
            log.info("%-6s -> CIK %s (pinned by cik_overrides.json)", ticker, company.cik_padded)
            resolved.append(company)
            continue

        company = lookup.get(ticker)
        if company is None:
            unresolved.append(ticker)
            log.warning("Could not resolve ticker %s to a CIK", ticker)
        else:
            resolved.append(company)
            log.info("%-6s -> CIK %s (%s)", ticker, company.cik_padded, company.name)

    return resolved, unresolved
