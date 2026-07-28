#!/usr/bin/env python3
"""Step 3: build the SQLite database and load parsed Form 4 records.

Usage:
    python scripts/03_load_database.py
    python scripts/03_load_database.py --rebuild        # drop and recreate
    python scripts/03_load_database.py --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_form4 import config, database  # noqa: E402

log = logging.getLogger("load")


def newest(pattern: str) -> Path:
    matches = sorted(glob.glob(str(config.PROCESSED_DIR / pattern)))
    if not matches:
        raise FileNotFoundError(
            f"No file matching {pattern}. Run scripts/02_parse_filings.py first."
        )
    return Path(matches[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=database.DEFAULT_DB_PATH)
    parser.add_argument("--rebuild", action="store_true", help="Delete the DB first")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.rebuild and args.db.exists():
        args.db.unlink()
        log.info("Removed existing database %s", args.db)

    txn_json = newest("form4_transactions_*.json")
    hold_csv = newest("form4_holdings_*.csv")
    filing_csv = newest("form4_filing_records_*.csv")
    owner_csv = newest("form4_filing_owners_*.csv")
    for label, path in (("Filings", filing_csv), ("Owners", owner_csv),
                        ("Transactions", txn_json), ("Holdings", hold_csv)):
        log.info("%-13s %s", label + ":", path.name)

    conn = database.connect(args.db)
    database.create_schema(conn)
    counts = database.load_from_files(conn, txn_json, hold_csv, filing_csv, owner_csv)

    print("\n" + "=" * 52)
    print(f"DATABASE BUILT: {args.db}")
    print("=" * 52)
    for table, count in counts.items():
        print(f"  {table:<20} {count:>6}")

    size_kb = args.db.stat().st_size / 1024
    print(f"\n  file size            {size_kb:>6.0f} KB")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
