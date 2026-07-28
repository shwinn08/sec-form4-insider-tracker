#!/usr/bin/env python3
"""Step 1: enumerate Form 4 filings for a list of tickers.

Resolves tickers -> CIKs, lists each company's Form 4 filings from the last N
months, and writes the results to data/raw/ as JSON and CSV.

Does NOT download or parse filing content — that's the next step.

Usage:
    python scripts/01_enumerate_filings.py
    python scripts/01_enumerate_filings.py --months 6
    python scripts/01_enumerate_filings.py --tickers AAPL MSFT --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make src/ importable without needing to install the package. For a portfolio
# project this keeps `git clone && pip install -r requirements.txt && run` true.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_form4 import config, storage  # noqa: E402
from sec_form4.client import SECClient  # noqa: E402
from sec_form4.filings import fetch_form4_filings, months_ago  # noqa: E402
from sec_form4.tickers import load_tickers, resolve_tickers  # noqa: E402

log = logging.getLogger("enumerate")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months", type=int, default=config.DEFAULT_LOOKBACK_MONTHS,
        help=f"How many months back to look (default: {config.DEFAULT_LOOKBACK_MONTHS})",
    )
    parser.add_argument(
        "--tickers", nargs="+", metavar="TICKER",
        help="Override config/tickers.txt for this run",
    )
    parser.add_argument(
        "--refresh-tickers", action="store_true",
        help="Re-download the ticker->CIK map instead of using the cache",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        client = SECClient()  # raises if SEC_USER_AGENT is missing/invalid
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    log.info("User-Agent: %s", client.user_agent)
    log.info("Rate limit: %.1f requests/second", config.REQUESTS_PER_SECOND)

    tickers = args.tickers or load_tickers()
    since = months_ago(args.months)
    log.info("Tickers: %s", ", ".join(tickers))
    log.info("Window: filings on or after %s (%d months)", since, args.months)

    # --- Step 1: tickers -> CIKs (one request, cached) ----------------------
    companies, unresolved = resolve_tickers(
        tickers, client, force_refresh=args.refresh_tickers
    )
    if not companies:
        log.error("No tickers could be resolved to a CIK. Nothing to do.")
        return 1

    # --- Step 2: CIK -> Form 4 filing list (one request per company) --------
    all_filings = []
    failed: list[str] = []
    empty_notes: dict[str, str] = {}  # ticker -> why it returned nothing
    for company in companies:
        try:
            filings, note = fetch_form4_filings(company, client, since)
            all_filings.extend(filings)
            if note:
                empty_notes[company.ticker] = note
        except Exception as exc:
            # One bad company shouldn't discard the work already done for the
            # others. Record it and keep going.
            log.error("Failed to fetch filings for %s: %s", company.ticker, exc)
            failed.append(company.ticker)

    all_filings.sort(key=lambda f: (f.filing_date, f.ticker), reverse=True)

    # --- Step 3: write it out -----------------------------------------------
    metadata = {
        "requested_tickers": tickers,
        "resolved": {c.ticker: c.cik_padded for c in companies},
        "unresolved_tickers": unresolved,
        "failed_tickers": failed,
        "empty_result_notes": empty_notes,
        "lookback_months": args.months,
        "filings_since": since.isoformat(),
        "user_agent": client.user_agent,
    }
    json_path, csv_path = storage.default_output_paths()
    storage.write_json(all_filings, metadata, json_path)
    storage.write_csv(all_filings, csv_path)

    # --- Summary -------------------------------------------------------------
    print("\n" + "=" * 62)
    print(f"Form 4 filings since {since}")
    print("=" * 62)
    for company in companies:
        rows = [f for f in all_filings if f.ticker == company.ticker]
        amended = sum(1 for f in rows if f.is_amendment)
        note = f"  ({amended} amended)" if amended else ""
        # Filings carry the authoritative registrant name from the submissions
        # payload; fall back to the ticker-file name if there were no filings.
        name = rows[0].company_name if rows else company.name
        print(f"  {company.ticker:<6} {len(rows):>4} filings{note}   {name}")
    print("-" * 62)
    print(f"  {'TOTAL':<6} {len(all_filings):>4} filings")
    if empty_notes:
        # A zero is ambiguous on its own — always say which kind it was.
        print("\n  Empty results:")
        for ticker, note in empty_notes.items():
            print(f"    {ticker}: {note}")
    if unresolved:
        print(f"\n  Unresolved tickers: {', '.join(unresolved)}")
    if failed:
        print(f"  Failed to fetch:    {', '.join(failed)}")
    print(f"\n  JSON: {json_path}")
    print(f"  CSV:  {csv_path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
