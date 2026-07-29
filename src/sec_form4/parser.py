"""Parse Form 4 ownership XML into flat transaction records.

Design notes worth reading before you trust the output:

* The XML's <issuer> block is the only authoritative statement of what company
  a filing is about. The ticker we searched under is not. See
  `issuer_matches_searched`.

* Every leaf value in this schema is wrapped: <transactionShares><value>N.
  That wrapper exists because a field can carry a <footnoteId> *instead of* a
  <value> when the filer discloses a footnote rather than a number. So a
  missing <value> means "not disclosed", which is different from zero.

* Holdings are not transactions. <nonDerivativeHolding> / <derivativeHolding>
  describe a position with no transaction attached (no date, no code, no share
  count). They're parsed separately, never mixed into the transaction stream.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)

# Boolean fields arrive in three different encodings across the corpus:
# "1"/"0" (most filings), "true"/"false" (some filer agents), and the element
# being absent entirely (863 across the four role fields). Absent means "not
# this role".
# This matters enormously: bool("false") is True in Python, so a naive
# truthiness check silently inverts the flag on every string-form filing.
_TRUE_STRINGS = {"1", "true", "y", "yes"}
_FALSE_STRINGS = {"0", "false", "n", "no", ""}


def _text(element: ET.Element | None, path: str) -> str | None:
    """Return stripped text at `path`, or None if absent/blank."""
    if element is None:
        return None
    found = element.find(path)
    if found is None or found.text is None:
        return None
    stripped = found.text.strip()
    return stripped or None


def _parse_bool(raw: str | None) -> bool:
    """Normalise the three boolean encodings described above."""
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUE_STRINGS:
        return True
    if value in _FALSE_STRINGS:
        return False
    log.warning("Unrecognised boolean value %r; treating as False", raw)
    return False


def _parse_decimal(raw: str | None) -> Decimal | None:
    """Parse a numeric value, preserving exact decimal representation.

    Decimal, not float: these are share counts and money. 269 share counts in
    the corpus have a non-zero fractional part (DRIP/401k plans), and float
    can't represent 184.90 exactly. None means the filer didn't disclose a
    number, which is not the same as 0.00. See `price_is_disclosed`.
    """
    if raw is None:
        return None
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        log.warning("Could not parse numeric value %r", raw)
        return None


def _footnote_ids(element: ET.Element | None) -> str:
    """Collect footnote ids referenced anywhere under `element`.

    Kept because a footnote is often the only explanation for a missing price
    or a 'J' (other) transaction code, and without it those rows are unreadable.
    """
    if element is None:
        return ""
    ids = [fn.get("id", "") for fn in element.iter("footnoteId")]
    return ";".join(sorted({i for i in ids if i}))


@dataclass(frozen=True)
class ReportingOwner:
    """One insider named on a filing. A filing can name several."""

    rpt_owner_cik: str
    rpt_owner_name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str
    other_text: str

    @property
    def roles(self) -> str:
        """Compact human-readable role summary, e.g. 'officer+director'."""
        parts = []
        if self.is_director:
            parts.append("director")
        if self.is_officer:
            parts.append("officer")
        if self.is_ten_percent_owner:
            parts.append("10%-owner")
        if self.is_other:
            parts.append("other")
        return "+".join(parts) or "unspecified"


@dataclass(frozen=True)
class Transaction:
    """One flattened transaction line, ready for inspection or loading."""

    # --- provenance ---------------------------------------------------------
    accession_number: str
    searched_ticker: str        # the ticker whose feed surfaced this filing
    searched_cik: int           # the CIK whose feed surfaced this filing
    filer_agent_cik: int        # from the accession prefix, not the issuer
    filing_date: str
    raw_xml_url: str

    # --- issuer (authoritative, from the XML) -------------------------------
    issuer_cik: int
    issuer_name: str
    issuer_trading_symbol: str
    issuer_matches_searched: bool

    # --- document -----------------------------------------------------------
    document_type: str
    schema_version: str
    period_of_report: str
    not_subject_to_section16: bool

    # --- reporting owner ----------------------------------------------------
    rpt_owner_cik: str
    rpt_owner_name: str
    rpt_owner_roles: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str
    num_reporting_owners: int
    all_reporting_owners: str

    # --- the transaction ----------------------------------------------------
    table: str                  # "non-derivative" | "derivative"
    line_number: int            # ordinal within the filing; see note below
    security_title: str         # what was ACTUALLY traded
    transaction_date: str
    deemed_execution_date: str
    transaction_code: str
    equity_swap_involved: bool
    transaction_shares: Decimal | None
    transaction_price_per_share: Decimal | None
    price_is_disclosed: bool
    acquired_disposed_code: str  # "A" acquired | "D" disposed
    shares_owned_following: Decimal | None
    direct_or_indirect: str      # "D" direct | "I" indirect
    nature_of_ownership: str

    # --- derivative-only ----------------------------------------------------
    conversion_or_exercise_price: Decimal | None
    exercise_date: str
    expiration_date: str
    underlying_security_title: str
    underlying_security_shares: Decimal | None

    footnote_ids: str


@dataclass(frozen=True)
class Holding:
    """A position with no transaction attached. Deliberately kept out of the
    transaction stream: it has no date, code, share count, or price, so it
    would appear as a phantom zero-share trade if merged."""

    accession_number: str
    holding_number: int         # ordinal within filing; natural key with accession
    searched_ticker: str
    issuer_cik: int
    issuer_name: str
    rpt_owner_cik: str
    rpt_owner_name: str
    table: str
    security_title: str
    shares_owned_following: Decimal | None
    direct_or_indirect: str
    nature_of_ownership: str
    underlying_security_title: str
    footnote_ids: str


@dataclass(frozen=True)
class FilingRecord:
    """Filing-level facts, emitted once per document.

    A filing is a first-class entity, not something to be inferred from its
    transaction rows: two filings in this corpus report a holding and no
    transaction at all, so deriving filings from transactions loses them.
    """

    accession_number: str
    searched_ticker: str
    searched_cik: int
    filer_agent_cik: int
    filing_date: str
    period_of_report: str
    document_type: str
    schema_version: str
    not_subject_to_section16: bool
    raw_xml_url: str
    issuer_cik: int
    issuer_name: str
    issuer_matches_searched: bool
    num_reporting_owners: int


@dataclass(frozen=True)
class FilingOwner:
    """One (filing, owner) pair with that owner's roles on that filing.

    Emitted per owner rather than folded into the transaction rows, so
    secondary owners on joint filings keep their own role flags instead of
    being reduced to a name in a joined string.
    """

    accession_number: str
    owner_order: int
    owner_cik: str
    owner_name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str
    other_text: str


@dataclass
class ParseResult:
    filing: FilingRecord | None = None
    owners: list[FilingOwner] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_owners(root: ET.Element) -> list[ReportingOwner]:
    """Extract every reporting owner.

    findall, not find: 12 filings in the corpus name two insiders (typically an
    entity and an affiliated trust filing jointly). Taking only the first would
    silently drop the second.
    """
    owners: list[ReportingOwner] = []
    for node in root.findall("reportingOwner"):
        rel = node.find("reportingOwnerRelationship")
        owners.append(
            ReportingOwner(
                rpt_owner_cik=_text(node, "reportingOwnerId/rptOwnerCik") or "",
                rpt_owner_name=_text(node, "reportingOwnerId/rptOwnerName") or "",
                is_director=_parse_bool(_text(rel, "isDirector")),
                is_officer=_parse_bool(_text(rel, "isOfficer")),
                is_ten_percent_owner=_parse_bool(_text(rel, "isTenPercentOwner")),
                is_other=_parse_bool(_text(rel, "isOther")),
                officer_title=_text(rel, "officerTitle") or "",
                other_text=_text(rel, "otherText") or "",
            )
        )
    return owners


def _filer_agent_cik(accession_number: str) -> int:
    """The accession prefix is the CIK of whoever *transmitted* the filing,
    usually a filing agent, sometimes the company itself. It is never reliably
    the issuer. Recorded so the distinction can't get lost downstream."""
    try:
        return int(accession_number.split("-")[0])
    except (ValueError, IndexError):
        return 0


def parse_form4(xml_text: str, enum_row: dict) -> ParseResult:
    """Parse one Form 4 document into transactions + holdings.

    `enum_row` is the record from step 1's enumeration output, which supplies
    provenance (which ticker's feed this came from) that the XML itself does
    not contain.
    """
    result = ParseResult()
    accession = enum_row["accession_number"]

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        result.warnings.append(f"{accession}: XML parse error: {exc}")
        return result

    # --- issuer: the authoritative identity of the filing --------------------
    issuer_cik_raw = _text(root, "issuer/issuerCik") or "0"
    issuer_cik = int(issuer_cik_raw)
    searched_cik = int(enum_row["cik"])
    matches = issuer_cik == searched_cik
    if not matches:
        # This is the "filing indexed under a party who isn't the issuer" case:
        # EDGAR lists a Form 4 under both the issuer's and the insider's CIK, so
        # a company's feed also returns filings where it is merely a 10% owner
        # of some *other* company.
        result.warnings.append(
            f"{accession}: issuer CIK {issuer_cik} != searched CIK {searched_cik} "
            f"({enum_row['ticker']}), filing is about "
            f"{_text(root, 'issuer/issuerName')!r}"
        )

    common = {
        "accession_number": accession,
        "searched_ticker": enum_row["ticker"],
        "searched_cik": searched_cik,
        "filer_agent_cik": _filer_agent_cik(accession),
        "filing_date": enum_row["filing_date"],
        "raw_xml_url": enum_row["raw_xml_url"],
        "issuer_cik": issuer_cik,
        "issuer_name": _text(root, "issuer/issuerName") or "",
        "issuer_trading_symbol": _text(root, "issuer/issuerTradingSymbol") or "",
        "issuer_matches_searched": matches,
        "document_type": _text(root, "documentType") or "",
        "schema_version": _text(root, "schemaVersion") or "",
        "period_of_report": _text(root, "periodOfReport") or "",
        # Set when the filer has ceased to be a Section 16 insider (resignation,
        # reorganisation). Such "exit filings" legitimately report no
        # transactions at all, so an empty filing is expected rather than a failure.
        "not_subject_to_section16": _parse_bool(_text(root, "notSubjectToSection16")),
    }

    owners = _parse_owners(root)
    if not owners:
        result.warnings.append(f"{accession}: no reportingOwner found")
        owners = [ReportingOwner("", "", False, False, False, False, "", "")]

    # A filing with multiple owners reports transactions *jointly*, so the shares
    # are not per-owner. Emitting one row per (transaction x owner) would
    # multiply the share counts, so we attribute rows to the first owner and
    # carry the full roster alongside. The owners file keeps the complete
    # many-to-many relationship for when you normalise into SQLite.
    primary = owners[0]
    roster = "; ".join(f"{o.rpt_owner_name} ({o.rpt_owner_cik})" for o in owners)

    result.filing = FilingRecord(
        accession_number=accession,
        searched_ticker=enum_row["ticker"],
        searched_cik=searched_cik,
        filer_agent_cik=common["filer_agent_cik"],
        filing_date=enum_row["filing_date"],
        period_of_report=common["period_of_report"],
        document_type=common["document_type"],
        schema_version=common["schema_version"],
        not_subject_to_section16=common["not_subject_to_section16"],
        raw_xml_url=enum_row["raw_xml_url"],
        issuer_cik=issuer_cik,
        issuer_name=common["issuer_name"],
        issuer_matches_searched=matches,
        num_reporting_owners=len(owners),
    )
    result.owners = [
        FilingOwner(
            accession_number=accession,
            owner_order=i,
            owner_cik=o.rpt_owner_cik,
            owner_name=o.rpt_owner_name,
            is_director=o.is_director,
            is_officer=o.is_officer,
            is_ten_percent_owner=o.is_ten_percent_owner,
            is_other=o.is_other,
            officer_title=o.officer_title,
            other_text=o.other_text,
        )
        for i, o in enumerate(owners, start=1)
        if o.rpt_owner_cik
    ]

    owner_fields = {
        "rpt_owner_cik": primary.rpt_owner_cik,
        "rpt_owner_name": primary.rpt_owner_name,
        "rpt_owner_roles": primary.roles,
        "is_director": primary.is_director,
        "is_officer": primary.is_officer,
        "is_ten_percent_owner": primary.is_ten_percent_owner,
        "is_other": primary.is_other,
        "officer_title": primary.officer_title,
        "num_reporting_owners": len(owners),
        "all_reporting_owners": roster,
    }

    # --- transactions --------------------------------------------------------
    # line_number is a stable ordinal within the filing. It's required, not
    # cosmetic: 164 filings contain two or more rows with an identical
    # (security_title, transaction_date, transaction_code) triple. These are
    # separate tranches or price points, not duplicates. Without an ordinal
    # there is no key that distinguishes them.
    line_number = 0
    holding_number = 0

    for table_name, txn_tag, hold_tag in (
        ("non-derivative", "nonDerivativeTransaction", "nonDerivativeHolding"),
        ("derivative", "derivativeTransaction", "derivativeHolding"),
    ):
        table_el = root.find(
            "nonDerivativeTable" if table_name == "non-derivative" else "derivativeTable"
        )
        if table_el is None:
            continue

        for node in table_el.findall(txn_tag):
            line_number += 1

            # Distinguish "disclosed as zero" from "not disclosed at all".
            # Both are common and they mean completely different things:
            # a $0 price on a grant (code A) or gift (G) is a real fact,
            # meaning no cash changed hands, whereas an absent price means the filer
            # pointed at a footnote instead of giving a number.
            price_node = node.find("transactionAmounts/transactionPricePerShare")
            price_value = _text(price_node, "value")
            price_disclosed = price_value is not None

            result.transactions.append(
                Transaction(
                    **common,
                    **owner_fields,
                    table=table_name,
                    line_number=line_number,
                    # The security actually traded. Never infer this from the
                    # ticker: one issuer can have several classes/series, and
                    # issuerTradingSymbol names only one of them.
                    security_title=_text(node, "securityTitle/value") or "",
                    transaction_date=_text(node, "transactionDate/value") or "",
                    deemed_execution_date=_text(node, "deemedExecutionDate/value") or "",
                    transaction_code=_text(node, "transactionCoding/transactionCode") or "",
                    equity_swap_involved=_parse_bool(
                        _text(node, "transactionCoding/equitySwapInvolved")
                    ),
                    transaction_shares=_parse_decimal(
                        _text(node, "transactionAmounts/transactionShares/value")
                    ),
                    transaction_price_per_share=_parse_decimal(price_value),
                    price_is_disclosed=price_disclosed,
                    acquired_disposed_code=_text(
                        node, "transactionAmounts/transactionAcquiredDisposedCode/value"
                    ) or "",
                    shares_owned_following=_parse_decimal(
                        _text(node, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
                    ),
                    direct_or_indirect=_text(
                        node, "ownershipNature/directOrIndirectOwnership/value"
                    ) or "",
                    nature_of_ownership=_text(
                        node, "ownershipNature/natureOfOwnership/value"
                    ) or "",
                    conversion_or_exercise_price=_parse_decimal(
                        _text(node, "conversionOrExercisePrice/value")
                    ),
                    exercise_date=_text(node, "exerciseDate/value") or "",
                    expiration_date=_text(node, "expirationDate/value") or "",
                    underlying_security_title=_text(
                        node, "underlyingSecurity/underlyingSecurityTitle/value"
                    ) or "",
                    underlying_security_shares=_parse_decimal(
                        _text(node, "underlyingSecurity/underlyingSecurityShares/value")
                    ),
                    footnote_ids=_footnote_ids(node),
                )
            )

        for node in table_el.findall(hold_tag):
            holding_number += 1
            result.holdings.append(
                Holding(
                    accession_number=accession,
                    holding_number=holding_number,
                    searched_ticker=enum_row["ticker"],
                    issuer_cik=issuer_cik,
                    issuer_name=common["issuer_name"],
                    rpt_owner_cik=primary.rpt_owner_cik,
                    rpt_owner_name=primary.rpt_owner_name,
                    table=table_name,
                    security_title=_text(node, "securityTitle/value") or "",
                    shares_owned_following=_parse_decimal(
                        _text(node, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
                    ),
                    direct_or_indirect=_text(
                        node, "ownershipNature/directOrIndirectOwnership/value"
                    ) or "",
                    nature_of_ownership=_text(
                        node, "ownershipNature/natureOfOwnership/value"
                    ) or "",
                    underlying_security_title=_text(
                        node, "underlyingSecurity/underlyingSecurityTitle/value"
                    ) or "",
                    footnote_ids=_footnote_ids(node),
                )
            )

    if not result.transactions and not result.holdings:
        if common["not_subject_to_section16"]:
            # Expected: the insider is reporting that they've exited Section 16
            # status. Recorded at info level so it doesn't read as a defect.
            log.info("%s: exit filing (notSubjectToSection16), no transactions", accession)
        else:
            result.warnings.append(
                f"{accession}: no transactions or holdings found "
                f"and notSubjectToSection16 is not set; investigate"
            )

    return result


def to_row(obj) -> dict:
    """Dataclass -> plain dict, with Decimals rendered as strings.

    Decimals become strings rather than floats so exact values survive the trip
    through JSON/CSV: 184.90 stays "184.90" instead of becoming 184.9 or
    184.90000000000001. None stays None, so "not disclosed" remains
    distinguishable from 0.
    """
    row = asdict(obj)
    return {
        k: (str(v) if isinstance(v, Decimal) else v)
        for k, v in row.items()
    }
