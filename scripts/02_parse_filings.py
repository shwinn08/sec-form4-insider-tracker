#!/usr/bin/env python3
"""Step 2: fetch and parse Form 4 XML into flat transaction records.

Reads the newest enumeration output from step 1, downloads each filing's raw
XML (cached on disk), parses it, and writes flattened transactions to
data/processed/.

Does NOT touch a database. That's step 3.

Usage:
    python scripts/02_parse_filings.py
    python scripts/02_parse_filings.py --input data/raw/form4_filings_...json
    python scripts/02_parse_filings.py --limit 25 --verbose
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_form4 import config, storage  # noqa: E402
from sec_form4.client import SECClient  # noqa: E402
from sec_form4.fetcher import fetch_all  # noqa: E402
from sec_form4.parser import parse_form4, to_row  # noqa: E402

log = logging.getLogger("parse")


def newest_enumeration() -> Path:
    matches = sorted(glob.glob(str(config.RAW_DIR / "form4_filings_*.json")))
    if not matches:
        raise FileNotFoundError(
            "No enumeration output found. Run scripts/01_enumerate_filings.py first."
        )
    return Path(matches[-1])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, help="Enumeration JSON (default: newest)")
    p.add_argument("--limit", type=int, help="Only process the first N filings")
    p.add_argument("--force-refresh", action="store_true", help="Ignore the XML cache")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    source = args.input or newest_enumeration()
    log.info("Reading enumeration: %s", source)
    enumeration = json.loads(source.read_text())
    rows = enumeration["filings"]
    if args.limit:
        rows = rows[: args.limit]
    log.info("Filings to process: %d", len(rows))

    try:
        client = SECClient()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    # --- fetch (cached) ------------------------------------------------------
    documents = fetch_all(rows, client, force_refresh=args.force_refresh)

    # --- parse ---------------------------------------------------------------
    filings, owners, transactions, holdings, warnings = [], [], [], [], []
    for row in rows:
        xml_text = documents.get(row["accession_number"])
        if xml_text is None:
            warnings.append(f"{row['accession_number']}: no XML fetched")
            continue
        result = parse_form4(xml_text, row)
        if result.filing is not None:
            filings.append(result.filing)
        owners.extend(result.owners)
        transactions.extend(result.transactions)
        holdings.extend(result.holdings)
        warnings.extend(result.warnings)

    # --- write ---------------------------------------------------------------
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    txn_rows = [to_row(t) for t in transactions]
    hold_rows = [to_row(h) for h in holdings]
    filing_rows = [to_row(f) for f in filings]
    owner_rows = [to_row(o) for o in owners]

    metadata = {
        "source_enumeration": str(source),
        "filings_processed": len(documents),
        "transaction_count": len(txn_rows),
        "holding_count": len(hold_rows),
        "filing_count": len(filing_rows),
        "filing_owner_count": len(owner_rows),
        "warnings": warnings,
    }

    txn_json = config.PROCESSED_DIR / f"form4_transactions_{stamp}.json"
    txn_csv = config.PROCESSED_DIR / f"form4_transactions_{stamp}.csv"
    hold_csv = config.PROCESSED_DIR / f"form4_holdings_{stamp}.csv"
    filing_csv = config.PROCESSED_DIR / f"form4_filing_records_{stamp}.csv"
    owner_csv = config.PROCESSED_DIR / f"form4_filing_owners_{stamp}.csv"

    storage.write_dicts_json(txn_rows, metadata, txn_json, key="transactions")
    storage.write_dicts_csv(txn_rows, txn_csv)
    storage.write_dicts_csv(hold_rows, hold_csv)
    storage.write_dicts_csv(filing_rows, filing_csv)
    storage.write_dicts_csv(owner_rows, owner_csv)

    # --- summary -------------------------------------------------------------
    codes = Counter(t["transaction_code"] for t in txn_rows)
    mismatched = [t for t in txn_rows if not t["issuer_matches_searched"]]
    no_price = sum(1 for t in txn_rows if not t["price_is_disclosed"])

    print("\n" + "=" * 66)
    print("PARSED FORM 4 TRANSACTIONS")
    print("=" * 66)
    print(f"  filings parsed        {len(documents)}")
    print(f"  transactions          {len(txn_rows)}")
    print(f"  holdings (separate)   {len(hold_rows)}")
    print(f"  price not disclosed   {no_price}")
    print(f"  issuer != searched    {len(mismatched)} rows "
          f"({len({t['accession_number'] for t in mismatched})} filings)")
    print(f"\n  transaction codes: {dict(codes.most_common())}")

    by_ticker = Counter(t["searched_ticker"] for t in txn_rows)
    print("\n  transactions by searched ticker:")
    for ticker, count in sorted(by_ticker.items()):
        distinct = len({t["security_title"] for t in txn_rows
                        if t["searched_ticker"] == ticker})
        print(f"    {ticker:<6} {count:>5}   ({distinct} distinct securities)")

    if warnings:
        print(f"\n  warnings: {len(warnings)} (see JSON metadata)")
        for w in warnings[:5]:
            print(f"    {w}")
        if len(warnings) > 5:
            print(f"    ... and {len(warnings) - 5} more")

    print(f"\n  JSON: {txn_json}")
    for path in (txn_csv, hold_csv, filing_csv, owner_csv):
        print(f"  CSV:  {path}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
