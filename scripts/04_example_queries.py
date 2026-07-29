#!/usr/bin/env python3
"""Step 4: example analytical queries against the loaded database.

Each query is written to demonstrate that a specific data-quality finding is
actually handled by the schema, not just documented.

Usage:
    python scripts/04_example_queries.py
    python scripts/04_example_queries.py --db data/form4.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_form4 import database  # noqa: E402


QUERIES: list[tuple[str, str, str]] = [
    (
        "Net open-market insider activity by company",
        # Only P and S are real trades. Joining transaction_codes and filtering
        # on is_open_market keeps grants (A), tax withholding (F) and option
        # exercises (M) out of a "buying vs selling" number. Those are
        # compensation mechanics, not investment decisions.
        #
        # Grouped on issuer_name (from the XML <issuer> block), never on the
        # ticker that was searched.
        """
        SELECT
            issuer_name,
            SUM(CASE WHEN transaction_code = 'P' THEN shares_num ELSE 0 END) AS shares_bought,
            SUM(CASE WHEN transaction_code = 'S' THEN shares_num ELSE 0 END) AS shares_sold,
            SUM(signed_shares)                                               AS net_shares,
            ROUND(SUM(CASE WHEN transaction_code = 'P'
                           THEN transaction_value ELSE 0 END) / 1e6, 2)      AS bought_usd_m,
            ROUND(SUM(CASE WHEN transaction_code = 'S'
                           THEN transaction_value ELSE 0 END) / 1e6, 2)      AS sold_usd_m,
            COUNT(*)                                                         AS txns
        FROM v_transactions
        WHERE is_open_market = 1
        GROUP BY issuer_cik, issuer_name
        ORDER BY sold_usd_m DESC
        """,
        "Only P/S count as trades. Grants and tax withholding excluded.",
    ),
    (
        "Most active insiders (transactions they are named on)",
        # Owners are reached by joining through filing_owners, because a
        # transaction belongs to a filing, and a filing can name several
        # insiders. This counts transactions an owner is named on. For the 12
        # joint filings that means both owners are credited, which is correct
        # for "activity" but would not be correct for summing shares.
        """
        SELECT
            o.owner_name,
            c.name                                   AS issuer_name,
            MAX(CASE WHEN fo.is_officer  = 1 THEN 'officer'  END) AS officer,
            MAX(CASE WHEN fo.is_director = 1 THEN 'director' END) AS director,
            MAX(fo.officer_title)                    AS title,
            COUNT(*)                                 AS txn_count,
            COUNT(DISTINCT t.accession_number)       AS filings
        FROM transactions   t
        JOIN filings        f  ON f.accession_number = t.accession_number
        JOIN filing_owners  fo ON fo.accession_number = t.accession_number
        JOIN reporting_owners o ON o.owner_cik = fo.owner_cik
        JOIN companies      c  ON c.cik = f.issuer_cik
        GROUP BY o.owner_cik, c.cik
        ORDER BY txn_count DESC
        LIMIT 10
        """,
        "Owners joined via filing_owners; no owner column exists on transactions.",
    ),
    (
        "What was actually traded under the FWONK ticker",
        # The finding that motivated the securities table: one ticker, many
        # securities. security_title comes from the transaction row; the
        # searched ticker is only provenance.
        """
        SELECT
            s.security_title,
            c.name                AS issuer_name,
            COUNT(*)              AS txns,
            ROUND(SUM(v.shares_num)) AS total_shares
        FROM v_transactions v
        JOIN transactions t ON t.accession_number = v.accession_number
                           AND t.line_number      = v.line_number
        JOIN securities   s ON s.security_id      = t.security_id
        JOIN companies    c ON c.cik              = s.issuer_cik
        WHERE v.searched_ticker = 'FWONK'
        GROUP BY s.security_id
        ORDER BY txns DESC
        LIMIT 12
        """,
        "One ticker -> 20 securities across 3 issuers.",
    ),
    (
        "Filings surfaced under one ticker but issued by another company",
        # The misattribution guard. issuer_matches_searched is a generated
        # column, so it cannot drift from the two CIKs it compares.
        """
        SELECT
            f.searched_ticker,
            c.name              AS actual_issuer,
            f.issuer_cik,
            COUNT(DISTINCT f.accession_number) AS filings
        FROM filings f
        JOIN companies c ON c.cik = f.issuer_cik
        WHERE f.issuer_matches_searched = 0
        GROUP BY f.searched_ticker, f.issuer_cik
        ORDER BY filings DESC
        """,
        "These would be misattributed if the schema keyed on ticker.",
    ),
    (
        "Price undisclosed vs zero, by transaction code",
        # NULL and 0 are different facts and the schema keeps them apart.
        """
        SELECT
            tc.code,
            tc.label,
            SUM(CASE WHEN t.price_per_share IS NULL THEN 1 ELSE 0 END)  AS undisclosed,
            SUM(CASE WHEN CAST(t.price_per_share AS REAL) = 0
                      AND t.price_per_share IS NOT NULL THEN 1 ELSE 0 END) AS zero_price,
            SUM(CASE WHEN CAST(t.price_per_share AS REAL) > 0 THEN 1 ELSE 0 END) AS priced
        FROM transactions t
        JOIN transaction_codes tc ON tc.code = t.transaction_code
        GROUP BY tc.code
        HAVING undisclosed > 0 OR zero_price > 0
        ORDER BY undisclosed DESC
        """,
        "NULL = not disclosed; 0 = no cash changed hands. Never conflated.",
    ),
    (
        "Current positions: the correct read of shares_owned_following",
        # Latest row per (owner, security, ownership form), never a SUM.
        """
        SELECT
            owner_name,
            issuer_name,
            security_title,
            direct_or_indirect AS ownership,
            COALESCE(nature_of_ownership, '')  AS nature,
            ROUND(shares_held) AS shares_held,
            as_of_filing_date,
            source
        FROM v_current_positions
        ORDER BY shares_held DESC
        LIMIT 10
        """,
        "Window function takes the latest statement, not a sum of balances.",
    ),
]


def render(conn: sqlite3.Connection, title: str, sql: str, note: str) -> None:
    rows = conn.execute(sql).fetchall()
    print("\n" + "=" * 100)
    print(title.upper())
    print(f"  {note}")
    print("=" * 100)
    if not rows:
        print("  (no rows)")
        return

    cols = rows[0].keys()
    widths = [
        min(34, max(len(c), max((len(str(r[c] if r[c] is not None else "")) for r in rows), default=0)))
        for c in cols
    ]
    print("  " + "  ".join(c[:w].ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        cells = []
        for c, w in zip(cols, widths):
            v = r[c]
            text = "" if v is None else (f"{v:,.0f}" if isinstance(v, float) and abs(v) >= 1000 else str(v))
            cells.append(text[:w].rjust(w) if isinstance(v, (int, float)) else text[:w].ljust(w))
        print("  " + "  ".join(cells))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=database.DEFAULT_DB_PATH)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"No database at {args.db}. Run scripts/03_load_database.py first.")
        return 1

    conn = database.connect(args.db)
    for title, sql, note in QUERIES:
        render(conn, title, sql, note)
    print()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
