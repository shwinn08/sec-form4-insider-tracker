#!/usr/bin/env python3
"""Step 5: the headline analysis, run against the loaded database.

Re-runnable: every number in the README's "Sample Queries & Findings" section is
produced here, and the script also writes a Markdown report to
data/processed/analysis_report.md so results can be diffed between runs.

All security-level grouping uses canonical_security_id (the curated alias
mapping), never the raw filed title and never the searched ticker.

Usage:
    python scripts/05_analysis.py
    python scripts/05_analysis.py --db data/form4.db --no-report
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_form4 import config, database  # noqa: E402


# Every insider named on a filing, resolved through the join table. Used by the
# per-owner queries below. A joint filing credits each named owner, which is
# right for "activity" but would double-count shares. The share-weighted
# queries below are restricted to single-owner filings for that reason.
OWNER_JOIN = """
    JOIN filing_owners    fo ON fo.accession_number = v.accession_number
    JOIN reporting_owners o  ON o.owner_cik         = fo.owner_cik
"""

ANALYSES: list[tuple[str, str, str]] = [
    (
        "Net open-market insider activity by company",
        """
        SELECT
            issuer_name                                                        AS company,
            COUNT(*)                                                           AS txns,
            ROUND(SUM(CASE WHEN transaction_code='P' THEN shares_num ELSE 0 END))  AS shares_bought,
            ROUND(SUM(CASE WHEN transaction_code='S' THEN shares_num ELSE 0 END))  AS shares_sold,
            ROUND(SUM(signed_shares))                                          AS net_shares,
            ROUND(SUM(CASE WHEN transaction_code='P'
                           THEN transaction_value ELSE 0 END)/1e6, 2)          AS bought_usd_m,
            ROUND(SUM(CASE WHEN transaction_code='S'
                           THEN transaction_value ELSE 0 END)/1e6, 2)          AS sold_usd_m
        FROM v_transactions
        WHERE is_open_market = 1
        GROUP BY issuer_cik
        ORDER BY sold_usd_m DESC
        """,
        "Only codes P and S. Grants, option exercises and tax withholding are excluded "
        "because they are compensation mechanics, not investment decisions.",
    ),
    (
        "Top insiders by transaction count",
        f"""
        SELECT
            o.owner_name                                   AS insider,
            c.name                                         AS company,
            COALESCE(MAX(fo.officer_title), CASE WHEN MAX(fo.is_director)=1
                     THEN 'Director' ELSE '' END)          AS role,
            COUNT(*)                                       AS txns,
            COUNT(DISTINCT v.accession_number)             AS filings,
            SUM(CASE WHEN v.is_open_market=1 THEN 1 ELSE 0 END) AS open_market
        FROM v_transactions v
        {OWNER_JOIN}
        JOIN companies c ON c.cik = v.issuer_cik
        GROUP BY o.owner_cik, c.cik
        ORDER BY txns DESC
        LIMIT 12
        """,
        "Insiders reached via filing_owners; transactions carry no owner column.",
    ),
    (
        "Top insiders by open-market dollar value",
        f"""
        SELECT
            o.owner_name                                              AS insider,
            c.name                                                    AS company,
            ROUND(SUM(CASE WHEN v.transaction_code='S'
                           THEN v.transaction_value ELSE 0 END)/1e6, 2) AS sold_usd_m,
            ROUND(SUM(CASE WHEN v.transaction_code='P'
                           THEN v.transaction_value ELSE 0 END)/1e6, 2) AS bought_usd_m,
            COUNT(*)                                                  AS txns
        FROM v_transactions v
        {OWNER_JOIN}
        JOIN companies c ON c.cik = v.issuer_cik
        WHERE v.is_open_market = 1
          AND v.accession_number IN (
                SELECT accession_number FROM filing_owners
                GROUP BY accession_number HAVING COUNT(*) = 1)
        GROUP BY o.owner_cik, c.cik
        ORDER BY sold_usd_m DESC
        LIMIT 12
        """,
        "Restricted to single-owner filings: a joint filing reports transactions "
        "collectively, so attributing their dollar value per owner would double-count.",
    ),
    (
        "Tracking stocks: what the FWONK ticker search actually returned",
        """
        SELECT
            v.canonical_security_title                       AS security,
            v.issuer_name                                    AS actual_issuer,
            CASE WHEN v.canonical_security_title LIKE '%Formula One%'
                   OR v.canonical_security_title LIKE '%FWON%'
                 THEN 'yes' ELSE 'NO' END                    AS is_formula_one,
            COUNT(*)                                         AS txns,
            ROUND(SUM(v.shares_num))                         AS shares
        FROM v_transactions v
        WHERE v.searched_ticker = 'FWONK'
        GROUP BY v.issuer_cik, v.canonical_security_id
        ORDER BY txns DESC
        """,
        "Searching the FWONK ticker returns Liberty Live securities, Live Nation and "
        "debentures. Only the 'yes' rows are actually Formula One.",
    ),
    (
        "How wrong the naive ticker assumption would have been",
        """
        SELECT
            searched_ticker                                              AS ticker,
            COUNT(*)                                                     AS total_txns,
            SUM(CASE WHEN canonical_security_title LIKE '%Formula One%'
                      OR canonical_security_title LIKE '%FWON%'
                     THEN 1 ELSE 0 END)                                  AS actually_formula_one,
            COUNT(DISTINCT canonical_security_id)                        AS distinct_securities,
            COUNT(DISTINCT issuer_cik)                                   AS distinct_issuers,
            ROUND(100.0 * SUM(CASE WHEN canonical_security_title LIKE '%Formula One%'
                                    OR canonical_security_title LIKE '%FWON%'
                                   THEN 1 ELSE 0 END) / COUNT(*), 1)     AS pct_correct
        FROM v_transactions
        WHERE searched_ticker = 'FWONK'
        """,
        "Treating every FWONK-sourced row as a Formula One trade would be wrong "
        "for the large majority of them.",
    ),
    (
        "Every open-market purchase in the window",
        f"""
        SELECT
            o.owner_name                          AS insider,
            c.name                                AS company,
            MIN(v.transaction_date)               AS first_date,
            MAX(v.transaction_date)               AS last_date,
            COUNT(*)                              AS tranches,
            ROUND(SUM(v.shares_num))              AS shares,
            ROUND(SUM(v.transaction_value)/1e6, 2) AS usd_m
        FROM v_transactions v
        {OWNER_JOIN}
        JOIN companies c ON c.cik = v.issuer_cik
        WHERE v.transaction_code = 'P'
        GROUP BY o.owner_cik, c.cik
        ORDER BY usd_m DESC
        """,
        "Insiders buying their own stock with cash is the signal most people want "
        "from Form 4 data. In 12 months across 11 companies, this is all of it.",
    ),
    (
        "Code 'G' gifts that are actually transfers between the insider's own trusts",
        f"""
        SELECT
            o.owner_name                    AS insider,
            c.name                          AS company,
            v.transaction_date              AS date,
            ROUND(v.shares_num)             AS shares,
            v.direct_or_indirect            AS ownership,
            COALESCE(t.nature_of_ownership, '') AS held_via
        FROM v_transactions v
        JOIN transactions t ON t.accession_number = v.accession_number
                           AND t.line_number      = v.line_number
        {OWNER_JOIN}
        JOIN companies c ON c.cik = v.issuer_cik
        WHERE v.transaction_code = 'G'
        ORDER BY v.shares_num DESC
        LIMIT 8
        """,
        "Code G is labelled 'bona fide gift', but covers estate-planning transfers "
        "between an insider's own vehicles. Note every large row is indirect (I).",
    ),
    (
        "Section 16 filing-deadline outliers",
        f"""
        SELECT
            o.owner_name    AS insider,
            c.name          AS company,
            v.transaction_date,
            v.filing_date,
            CAST(julianday(v.filing_date) - julianday(v.transaction_date) AS INT) AS days_late,
            v.transaction_code AS code,
            tc.label        AS what
        FROM v_transactions v
        {OWNER_JOIN}
        JOIN companies c ON c.cik = v.issuer_cik
        JOIN transaction_codes tc ON tc.code = v.transaction_code
        WHERE days_late > 14
        GROUP BY v.accession_number
        ORDER BY days_late DESC
        LIMIT 8
        """,
        "Section 16 requires filing within two business days of the transaction.",
    ),
    (
        "Effect of the curated security alias table",
        """
        SELECT
            c.name                              AS company,
            vsc.canonical_title                 AS canonical_security,
            GROUP_CONCAT(DISTINCT s.security_title) AS filed_as,
            COUNT(t.line_number)                AS txns_after_merge
        FROM securities s
        JOIN v_securities_canonical vsc ON vsc.security_id = s.security_id
        JOIN companies c ON c.cik = s.issuer_cik
        LEFT JOIN transactions t ON t.security_id = s.security_id
        WHERE vsc.canonical_security_id IN (SELECT canonical_security_id
                                            FROM v_securities_canonical
                                            WHERE is_aliased = 1)
        GROUP BY vsc.canonical_security_id
        """,
        "Three curated merges. securities.security_title is never rewritten; "
        "the alias resolves in views only, so aggregates stay auditable.",
    ),
]


def run(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return conn.execute(sql).fetchall()


def fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}" if abs(value) < 1000 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def print_table(title: str, note: str, rows: list[sqlite3.Row]) -> None:
    print("\n" + "=" * 104)
    print(title.upper())
    print(f"  {note}")
    print("=" * 104)
    if not rows:
        print("  (no rows)")
        return
    cols = list(rows[0].keys())
    widths = [
        # 42 rather than 30: security titles are distinguished by their suffix
        # ('... - LLYVK' vs '... - FWONK'), so truncating too early hides the
        # exact distinction these tables exist to show.
        min(42, max(len(c), max((len(fmt(r[c])) for r in rows), default=0)))
        for c in cols
    ]
    print("  " + "  ".join(c[:w].ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        cells = [
            fmt(r[c])[:w].rjust(w) if isinstance(r[c], (int, float)) else fmt(r[c])[:w].ljust(w)
            for c, w in zip(cols, widths)
        ]
        print("  " + "  ".join(cells))


def markdown_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "_(no rows)_\n"
    cols = list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |",
           "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(fmt(r[c]) for c in cols) + " |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=database.DEFAULT_DB_PATH)
    ap.add_argument("--no-report", action="store_true", help="Skip the Markdown report")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"No database at {args.db}. Run scripts/03_load_database.py first.")
        return 1

    conn = database.connect(args.db)

    scope = conn.execute("""
        SELECT COUNT(DISTINCT f.accession_number) filings,
               COUNT(t.line_number) txns,
               MIN(t.transaction_date) d0, MAX(t.transaction_date) d1,
               COUNT(DISTINCT f.issuer_cik) issuers
        FROM filings f LEFT JOIN transactions t ON t.accession_number = f.accession_number
    """).fetchone()

    header = (f"{scope['filings']} filings, {scope['txns']} transactions, "
              f"{scope['issuers']} issuers, {scope['d0']} to {scope['d1']}")
    print("\n" + "#" * 104)
    print(f"# FORM 4 INSIDER TRANSACTION ANALYSIS: {header}")
    print("#" * 104)

    sections = []
    for title, sql, note in ANALYSES:
        rows = run(conn, sql)
        print_table(title, note, rows)
        sections.append((title, note, rows))

    if not args.no_report:
        report = [
            "# Form 4 Insider Transaction Analysis",
            "",
            f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. {header}._",
            "",
            "Produced by `scripts/05_analysis.py`. All security grouping uses the "
            "curated canonical security, never the searched ticker.",
            "",
        ]
        for title, note, rows in sections:
            report += [f"## {title}", "", f"_{note}_", "", markdown_table(rows), ""]
        path = config.PROCESSED_DIR / "analysis_report.md"
        path.write_text("\n".join(report))
        print(f"\n\nMarkdown report written to {path}\n")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
