"""Build the SQLite database and load parsed Form 4 records into it.

The load is idempotent: every table has a real primary key, so re-running
against the same input replaces rows rather than duplicating them. That matters
because you will re-run this while iterating on the parser.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = config.DATA_DIR / "form4.db"

# Form 4 transaction codes. is_open_market marks the only two that represent
# someone choosing to trade on the market. Everything else is compensation,
# tax mechanics, or a transfer.
TRANSACTION_CODES = [
    ("P", "Open-market or private purchase", 1),
    ("S", "Open-market or private sale", 1),
    ("A", "Grant, award or other acquisition from the issuer", 0),
    ("D", "Disposition to the issuer", 0),
    ("F", "Shares withheld by issuer to satisfy tax withholding", 0),
    ("M", "Exercise or conversion of a derivative security", 0),
    ("G", "Bona fide gift", 0),
    ("J", "Other acquisition or disposition (see footnote)", 0),
    ("C", "Conversion of a derivative security", 0),
    ("E", "Expiration of a short derivative position", 0),
    ("H", "Expiration (or cancellation) of a long derivative position", 0),
    ("I", "Discretionary transaction", 0),
    ("K", "Transaction in equity swap or similar instrument", 0),
    ("L", "Small acquisition", 0),
    ("U", "Disposition pursuant to a tender of shares", 0),
    ("V", "Transaction voluntarily reported earlier than required", 0),
    ("W", "Acquisition or disposition by will or laws of descent", 0),
    ("X", "Exercise of an in-the-money or at-the-money derivative", 0),
    ("Z", "Deposit into or withdrawal from a voting trust", 0),
]


# Curated security title aliases: (issuer_cik, title_as_filed, canonical_title, note).
#
# Keyed on (issuer_cik, title) rather than security_id because security_id is a
# surrogate assigned at load time, and hardcoded ids would point at the wrong
# securities after a rebuild.
#
# Every entry was reviewed individually against filer-agent overlap, date ranges
# and filing footnotes. Not included, with reasons:
#   - Goodyear '2022 Plan Restricted Stock Units' vs 'Restricted Stock Units':
#     the generic title includes director RSUs accrued to a Retainer Deferral
#     Account, a different instrument from a 2022 Performance Plan grant.
#   - Pfizer 'Phantom Stock Units' vs 'Phantom Stock Units SSP': SSP is the
#     Supplemental Savings Plan, used by a single insider.
#   - Liberty Media 'Series C Common Stock': ambiguous and time-dependent.
#     It appears only after Liberty Live split off into its own issuer, so
#     assigning it to either series would reallocate real transactions.
SECURITY_ALIASES: list[tuple[int, str, str, str]] = [
    (
        1045810,  # NVIDIA
        "Common",
        "Common Stock",
        "Same security. The two spellings come from non-overlapping sets of "
        "filing agents (4 vs 14, none shared) over the same date range; NVIDIA "
        "has a single class of common stock.",
    ),
    (
        104169,  # Walmart
        "Common Stock",
        "Common",
        "Same security. Filing agents 1579299 and 1502438 each used BOTH "
        "spellings on different dates; Walmart has a single class of common "
        "stock. Canonical is 'Common', that issuer's dominant spelling (289 vs 2).",
    ),
    (
        1034665,  # BlackRock MuniHoldings Fund
        "Series W-7 Variable  Rate Muni Term  Preferred Shares",
        "Series W-7 Variable Rate Muni Term Preferred Shares",
        "Identical title apart from doubled spaces, same filing agent and same "
        "issuer. A typo, not a distinct series.",
    ),
]


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced.

    SQLite ships with foreign key enforcement OFF by default for backwards
    compatibility, and it is a per-connection setting. Without this PRAGMA every
    REFERENCES clause in the schema is decorative.
    """
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql and seed the transaction-code lookup."""
    conn.executescript(SCHEMA_FILE.read_text())
    conn.executemany(
        "INSERT INTO transaction_codes (code, label, is_open_market) VALUES (?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET label = excluded.label, "
        "is_open_market = excluded.is_open_market",
        TRANSACTION_CODES,
    )
    conn.commit()
    log.info("Schema applied to %s", conn)


def _none_if_blank(value):
    """Convert the parser's empty-string placeholders back to SQL NULL.

    The flat JSON/CSV output uses '' for absent text because CSV has no null.
    In a database that distinction is available and worth keeping: '' and NULL
    should not both mean "missing".
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _security_id(conn: sqlite3.Connection, cache: dict, issuer_cik: int, title: str):
    """Resolve (issuer_cik, security_title) to a surrogate id, inserting if new.

    Cached in memory because the same handful of securities recur across
    thousands of rows.
    """
    title = (title or "").strip()
    if not title:
        return None
    key = (issuer_cik, title)
    if key in cache:
        return cache[key]

    row = conn.execute(
        "SELECT security_id FROM securities WHERE issuer_cik = ? AND security_title = ?",
        (issuer_cik, title),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO securities (issuer_cik, security_title) VALUES (?, ?)",
            (issuer_cik, title),
        )
        security_id = cur.lastrowid
    else:
        security_id = row["security_id"]

    cache[key] = security_id
    return security_id


def _as_bool_int(value) -> int:
    """Coerce a CSV/JSON boolean to a strict 0/1 for a CHECK-constrained column.

    CSV has no boolean type, so values arrive as the strings 'True'/'False'.
    This is the exact trap the schema guards against, since 'False' is truthy in
    Python, so the conversion is explicit rather than a bool() call.
    """
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None:
        return 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return 1
    if text in ("0", "false", "no", ""):
        return 0
    raise ValueError(f"Unrecognised boolean value {value!r}")


def load(
    conn: sqlite3.Connection,
    filing_records: list[dict],
    filing_owners: list[dict],
    transactions: list[dict],
    holdings: list[dict],
) -> dict[str, int]:
    """Load parsed records. Returns row counts per table.

    Order matters: companies before filings, filings before their children, and
    securities before anything referencing them, because foreign keys are on.
    """
    counts: dict[str, int] = {}
    sec_cache: dict[tuple, int] = {}

    # --- companies ----------------------------------------------------------
    # Taken from the filing records, which carry the issuer straight from the
    # XML <issuer> block. 23 companies for 11 tickers, because misattributed
    # filings reference different issuers.
    companies = {int(r["issuer_cik"]): r["issuer_name"] for r in filing_records}
    for row in holdings:
        companies.setdefault(int(row["issuer_cik"]), row["issuer_name"])
    conn.executemany(
        "INSERT INTO companies (cik, name) VALUES (?, ?) "
        "ON CONFLICT(cik) DO UPDATE SET name = excluded.name",
        sorted(companies.items()),
    )
    counts["companies"] = len(companies)

    # --- filings ------------------------------------------------------------
    # One row per document, from the parser's filing-level output. Deriving
    # these from transaction rows instead would drop the two filings that
    # report only a holding and no transaction.
    filings = {
        r["accession_number"]: (
            r["accession_number"],
            int(r["issuer_cik"]),
            r["searched_ticker"],
            int(r["searched_cik"]),
            int(r["filer_agent_cik"] or 0),
            r["filing_date"],
            _none_if_blank(r["period_of_report"]),
            r["document_type"],
            _none_if_blank(r["schema_version"]),
            _as_bool_int(r["not_subject_to_section16"]),
            _none_if_blank(r["raw_xml_url"]),
        )
        for r in filing_records
    }
    conn.executemany(
        "INSERT INTO filings (accession_number, issuer_cik, searched_ticker, "
        "searched_cik, filer_agent_cik, filing_date, period_of_report, "
        "document_type, schema_version, not_subject_to_section16, raw_xml_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(accession_number) DO UPDATE SET "
        "  issuer_cik = excluded.issuer_cik, searched_ticker = excluded.searched_ticker",
        list(filings.values()),
    )
    counts["filings"] = len(filings)

    # --- reporting owners + the join table ----------------------------------
    # Straight from the parser's per-owner output, so every owner on a joint
    # filing keeps its own role flags. (An earlier version reconstructed these
    # by splitting a "Name (CIK); Name (CIK)" string, which both lost the
    # secondary owner's roles and would have broken on any name containing a
    # parenthesis.)
    owners = {r["owner_cik"]: r["owner_name"] for r in filing_owners}
    filing_owner_rows = [
        (
            r["accession_number"],
            r["owner_cik"],
            int(r["owner_order"]),
            _as_bool_int(r["is_director"]),
            _as_bool_int(r["is_officer"]),
            _as_bool_int(r["is_ten_percent_owner"]),
            _as_bool_int(r["is_other"]),
            _none_if_blank(r["officer_title"]),
            _none_if_blank(r["other_text"]),
        )
        for r in filing_owners
    ]

    conn.executemany(
        "INSERT INTO reporting_owners (owner_cik, owner_name) VALUES (?, ?) "
        "ON CONFLICT(owner_cik) DO UPDATE SET owner_name = excluded.owner_name",
        sorted(owners.items()),
    )
    counts["reporting_owners"] = len(owners)

    # De-duplicate: every transaction row in a filing repeats the same roster.
    unique_filing_owners = {(r[0], r[1]): r for r in filing_owner_rows}
    conn.executemany(
        "INSERT INTO filing_owners (accession_number, owner_cik, owner_order, "
        "is_director, is_officer, is_ten_percent_owner, is_other, officer_title, other_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(accession_number, owner_cik) DO UPDATE SET "
        "  owner_order = excluded.owner_order, is_director = excluded.is_director, "
        "  is_officer = excluded.is_officer, "
        "  is_ten_percent_owner = excluded.is_ten_percent_owner, "
        "  is_other = excluded.is_other, officer_title = excluded.officer_title",
        list(unique_filing_owners.values()),
    )
    counts["filing_owners"] = len(unique_filing_owners)

    # --- transactions -------------------------------------------------------
    txn_rows = []
    for row in transactions:
        issuer_cik = int(row["issuer_cik"])
        txn_rows.append(
            (
                row["accession_number"],
                int(row["line_number"]),
                row["table"],
                _security_id(conn, sec_cache, issuer_cik, row["security_title"]),
                row["transaction_date"],
                _none_if_blank(row["deemed_execution_date"]),
                row["transaction_code"],
                _as_bool_int(row["equity_swap_involved"]),
                _none_if_blank(row["transaction_shares"]),
                # Stays NULL when undisclosed, '0' when the price is zero.
                _none_if_blank(row["transaction_price_per_share"]),
                _none_if_blank(row["acquired_disposed_code"]),
                _none_if_blank(row["shares_owned_following"]),
                _none_if_blank(row["direct_or_indirect"]),
                _none_if_blank(row["nature_of_ownership"]),
                _none_if_blank(row["conversion_or_exercise_price"]),
                _none_if_blank(row["exercise_date"]),
                _none_if_blank(row["expiration_date"]),
                _security_id(conn, sec_cache, issuer_cik, row["underlying_security_title"]),
                _none_if_blank(row["underlying_security_shares"]),
                _none_if_blank(row["footnote_ids"]),
            )
        )
    conn.executemany(
        "INSERT INTO transactions (accession_number, line_number, table_type, "
        "security_id, transaction_date, deemed_execution_date, transaction_code, "
        "equity_swap_involved, shares, price_per_share, acquired_disposed_code, "
        "shares_owned_following, direct_or_indirect, nature_of_ownership, "
        "conversion_or_exercise_price, exercise_date, expiration_date, "
        "underlying_security_id, underlying_security_shares, footnote_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(accession_number, line_number) DO UPDATE SET "
        "  shares = excluded.shares, price_per_share = excluded.price_per_share",
        txn_rows,
    )
    counts["transactions"] = len(txn_rows)

    # --- holdings -----------------------------------------------------------
    hold_rows = []
    for row in holdings:
        issuer_cik = int(row["issuer_cik"])
        hold_rows.append(
            (
                row["accession_number"],
                int(row["holding_number"]),
                row["table"],
                _security_id(conn, sec_cache, issuer_cik, row["security_title"]),
                _none_if_blank(row["shares_owned_following"]),
                _none_if_blank(row["direct_or_indirect"]),
                _none_if_blank(row["nature_of_ownership"]),
                _security_id(conn, sec_cache, issuer_cik, row["underlying_security_title"]),
                _none_if_blank(row["footnote_ids"]),
            )
        )
    conn.executemany(
        "INSERT INTO holdings (accession_number, holding_number, table_type, "
        "security_id, shares_owned_following, direct_or_indirect, "
        "nature_of_ownership, underlying_security_id, footnote_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(accession_number, holding_number) DO UPDATE SET "
        "  shares_owned_following = excluded.shares_owned_following",
        hold_rows,
    )
    counts["holdings"] = len(hold_rows)

    counts["securities"] = conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]
    counts["security_aliases"] = apply_security_aliases(conn)
    conn.commit()
    return counts


def apply_security_aliases(conn: sqlite3.Connection) -> int:
    """Insert the curated alias rows, resolving titles to security ids.

    Fails loudly rather than silently skipping: an alias whose title no longer
    matches anything would quietly stop merging, and the resulting aggregates
    would look plausible. If a mapping cannot be resolved, that is a signal the
    curation needs revisiting, not something to swallow.
    """
    resolved = 0
    for issuer_cik, filed_title, canonical_title, note in SECURITY_ALIASES:
        rows = {
            r["security_title"]: r["security_id"]
            for r in conn.execute(
                "SELECT security_id, security_title FROM securities "
                "WHERE issuer_cik = ? AND security_title IN (?, ?)",
                (issuer_cik, filed_title, canonical_title),
            )
        }
        src = rows.get(filed_title)
        dst = rows.get(canonical_title)

        if src is None or dst is None:
            missing = filed_title if src is None else canonical_title
            log.warning(
                "Alias not applied for issuer %s: no security titled %r. "
                "The curated alias list may be stale.",
                issuer_cik, missing,
            )
            continue

        conn.execute(
            "INSERT INTO security_aliases (security_id, canonical_security_id, note) "
            "VALUES (?, ?, ?) ON CONFLICT(security_id) DO UPDATE SET "
            "canonical_security_id = excluded.canonical_security_id, note = excluded.note",
            (src, dst, note),
        )
        resolved += 1

    # Reject alias chains (A -> B where B -> C). The canonical view resolves a
    # single hop only, so a chain would leave rows pointing at a non-canonical
    # target and quietly split an aggregate.
    chained = conn.execute(
        "SELECT a.security_id FROM security_aliases a "
        "JOIN security_aliases b ON b.security_id = a.canonical_security_id"
    ).fetchall()
    if chained:
        raise ValueError(
            f"Alias chain detected for security_id(s) {[r[0] for r in chained]}. "
            "Point every alias directly at its final canonical security."
        )

    log.info("Applied %d of %d curated security aliases", resolved, len(SECURITY_ALIASES))
    return resolved


def _read_csv(path: Path) -> list[dict]:
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_from_files(
    conn: sqlite3.Connection,
    transactions_json: Path,
    holdings_csv: Path,
    filing_records_csv: Path,
    filing_owners_csv: Path,
) -> dict[str, int]:
    """Load from the step-2 output artifacts."""
    transactions = json.loads(transactions_json.read_text())["transactions"]
    holdings = _read_csv(holdings_csv)
    filing_records = _read_csv(filing_records_csv)
    filing_owners = _read_csv(filing_owners_csv)
    log.info(
        "Loading %d filings, %d owners, %d transactions, %d holdings",
        len(filing_records), len(filing_owners), len(transactions), len(holdings),
    )
    return load(conn, filing_records, filing_owners, transactions, holdings)
