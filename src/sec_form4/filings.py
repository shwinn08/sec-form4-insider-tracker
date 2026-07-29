"""Enumerate a company's Form 4 filings from the EDGAR submissions API.

This module lists *what filings exist*. It does not download or
parse filing content. That's the next step of the project.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import asdict, dataclass, replace
from datetime import date

from . import config
from .client import SECClient
from .tickers import Company

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Filing:
    """One Form 4 filing. Flat by design, so it maps cleanly to a CSV row
    (and, later, to a SQL table)."""

    ticker: str
    company_name: str
    cik: int
    form_type: str            # "4" or "4/A"
    is_amendment: bool
    accession_number: str     # "0000320193-25-000073"
    filing_date: str          # date the filing was submitted
    report_date: str          # date of the transaction being reported
    acceptance_datetime: str  # timestamp EDGAR accepted it (note: ET, not UTC)
    primary_document: str
    primary_doc_description: str
    filing_index_url: str     # lists every file in the submission
    primary_document_url: str  # SEC's rendered, human-readable view
    raw_xml_url: str          # the machine-readable original; parse this one
    size_bytes: int


def months_ago(months: int, today: date | None = None) -> date:
    """Return the date `months` calendar months before today.

    Doing this properly rather than subtracting `months * 30` days keeps the
    window aligned to calendar months, which is what "last 12 months" means to
    a human. The min() call clamps for short months: 12 months before
    2025-03-31 is 2024-03-31, but 1 month before 2025-03-31 is 2025-02-28.
    """
    today = today or date.today()
    total = (today.year * 12 + today.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _accession_no_dashes(accession_number: str) -> str:
    """'0000320193-25-000073' -> '000032019325000073'.

    The Archives *directory* name uses the dash-free form while the filing
    *index file* name keeps the dashes. Getting this backwards is the classic
    source of 404s on EDGAR archive URLs.
    """
    return accession_number.replace("-", "")


def _build_urls(
    cik: int, accession_number: str, primary_document: str
) -> tuple[str, str, str]:
    """Construct (filing_index_url, primary_document_url, raw_xml_url).

    Note the CIK here is *unpadded*, the opposite of what the submissions API
    wanted. That asymmetry is an EDGAR quirk, not a mistake.

    For Form 4, `primaryDocument` usually points at an XSL-rendered path like
    "xslF345X06/form4.xml". That URL serves SEC's human-readable HTML view of
    the filing, not the underlying data. Removing the "xsl*/" path segment
    yields the raw XML original, which is what you want to parse.
    """
    bare = _accession_no_dashes(accession_number)
    folder = f"{config.ARCHIVES_BASE}/{cik}/{bare}"
    index_url = f"{folder}/{accession_number}-index.htm"

    if not primary_document:
        return index_url, "", ""

    doc_url = f"{folder}/{primary_document}"
    # Strip the rendering stylesheet directory, if present.
    raw_url = re.sub(r"/xsl[^/]+/", "/", doc_url)
    return index_url, doc_url, raw_url


def _iter_filing_records(block: dict):
    """Zip EDGAR's parallel arrays back into per-filing dicts.

    The API stores filings column-wise:
        {"form": ["10-Q", "4"], "filingDate": ["2025-05-02", "2025-05-05"]}
    Index i of every array describes the same filing, so we walk the indices
    and gather across arrays. `.get(field, [])` plus a length guard keeps us
    safe if the SEC ever adds a field that isn't present on older records.
    """
    accessions = block.get("accessionNumber", [])
    fields = (
        "form", "filingDate", "reportDate", "acceptanceDateTime",
        "primaryDocument", "primaryDocDescription", "size",
    )

    for i, accession in enumerate(accessions):
        record = {"accessionNumber": accession}
        for field in fields:
            values = block.get(field, [])
            record[field] = values[i] if i < len(values) else ""
        yield record


def _records_to_filings(records, company: Company, since: date) -> list[Filing]:
    """Filter records down to Form 4s inside the date window."""
    out: list[Filing] = []

    for rec in records:
        form = (rec.get("form") or "").strip()
        if form not in config.FORM_4_TYPES:
            continue

        filing_date_str = rec.get("filingDate") or ""
        if not filing_date_str:
            continue
        # EDGAR dates are ISO-8601 ("2025-05-05"), so fromisoformat is safe and
        # gives us a real date object to compare against the window.
        if date.fromisoformat(filing_date_str) < since:
            continue

        accession = rec["accessionNumber"]
        primary_doc = rec.get("primaryDocument") or ""
        index_url, doc_url, raw_url = _build_urls(company.cik, accession, primary_doc)

        out.append(
            Filing(
                ticker=company.ticker,
                company_name=company.name,
                cik=company.cik,
                form_type=form,
                is_amendment=form.endswith("/A"),
                accession_number=accession,
                filing_date=filing_date_str,
                report_date=rec.get("reportDate") or "",
                acceptance_datetime=rec.get("acceptanceDateTime") or "",
                primary_document=primary_doc,
                primary_doc_description=rec.get("primaryDocDescription") or "",
                filing_index_url=index_url,
                primary_document_url=doc_url,
                raw_xml_url=raw_url,
                size_bytes=int(rec.get("size") or 0),
            )
        )

    return out


# Form types that only a foreign private issuer files. An FPI is exempt from
# Section 16 under Exchange Act Rule 3a12-3(b), so its insiders never file
# Form 4, so an empty result for one of these is correct rather than a bug.
FOREIGN_ISSUER_FORMS = {"20-F", "6-K", "40-F", "F-1", "F-3", "F-4"}


def diagnose_empty_result(payload: dict, since: date) -> str:
    """Explain why a company yielded no Form 4s.

    An empty result has several very different causes, and they are impossible
    to tell apart from the count alone:
      - the CIK is wrong (ticker reassigned after a reorg) -> fixable
      - the company is a foreign private issuer -> no Form 4s will ever exist
      - the insiders did not trade in this window -> genuinely empty

    Silent zeros are the most dangerous output this scraper can produce, so we
    name the cause. Uses only the payload we already fetched, so no extra request.
    """
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])

    form4_dates = [
        d for f, d in zip(forms, dates) if f in config.FORM_4_TYPES
    ]

    if form4_dates:
        return (
            f"has Form 4 history but none since {since} "
            f"(most recent: {max(form4_dates)})"
        )

    if FOREIGN_ISSUER_FORMS & set(forms):
        return (
            "appears to be a foreign private issuer (files "
            f"{', '.join(sorted(FOREIGN_ISSUER_FORMS & set(forms)))}); "
            "exempt from Section 16, so no Form 4s exist"
        )

    if not forms:
        return "no filings at all under this CIK, likely the wrong CIK"

    return (
        f"no Form 4 in {len(forms)} recent filings "
        f"(forms present: {', '.join(sorted(set(forms))[:6])}). Check the CIK"
    )


def _oldest_filing_date(block: dict) -> date | None:
    """Oldest filingDate in a block. Used to decide whether we need to page
    further back into the company's history."""
    dates = [d for d in block.get("filingDate", []) if d]
    return date.fromisoformat(min(dates)) if dates else None


def fetch_form4_filings(
    company: Company, client: SECClient, since: date
) -> tuple[list[Filing], str | None]:
    """Fetch every Form 4 for one company filed on or after `since`.

    Returns (filings, note). `note` is None on a normal result, or a
    human-readable explanation when the result is empty. See
    diagnose_empty_result().

    One request per company in the normal case. `filings.recent` is guaranteed
    to hold at least the last 12 months *or* the last 1000 filings, whichever
    is larger, so for a 6-12 month window it is almost always sufficient. The
    exception is a very heavy filer whose 1000 most recent filings don't reach
    back a full year; for those we follow the `filings.files` overflow chunks.
    """
    url = config.SUBMISSIONS_URL.format(cik_padded=company.cik_padded)
    log.info("Fetching submissions for %s (CIK %s)", company.ticker, company.cik_padded)
    payload = client.get_json(url)

    # The submissions payload carries the registrant's official name. Prefer it
    # over whatever we had from the ticker file: it's authoritative, and for a
    # CIK pinned via cik_overrides.json it's the only real name we have.
    official_name = payload.get("name")
    if official_name:
        company = replace(company, name=official_name)

    filings_section = payload.get("filings", {})
    recent = filings_section.get("recent", {})

    results = _records_to_filings(_iter_filing_records(recent), company, since)

    # Do we need older data? Only if `recent` bottoms out *after* our window
    # starts, meaning there could be in-window filings we haven't seen.
    oldest = _oldest_filing_date(recent)
    if oldest is not None and oldest > since:
        log.info(
            "%s: 'recent' only reaches back to %s, need %s, checking older chunks",
            company.ticker, oldest, since,
        )
        for extra in filings_section.get("files", []):
            # Each chunk advertises its own date range; skip any that ends
            # before our window starts rather than downloading it blindly.
            chunk_end = extra.get("filingTo")
            if chunk_end and date.fromisoformat(chunk_end) < since:
                continue
            chunk_url = f"https://data.sec.gov/submissions/{extra['name']}"
            log.info("  fetching overflow chunk %s", extra["name"])
            chunk = client.get_json(chunk_url)
            results.extend(
                _records_to_filings(_iter_filing_records(chunk), company, since)
            )

    # Newest first, the order you want when eyeballing the output.
    results.sort(key=lambda f: (f.filing_date, f.accession_number), reverse=True)

    if not results:
        note = diagnose_empty_result(payload, since)
        log.warning("%s: 0 Form 4 filings, %s", company.ticker, note)
        return results, note

    log.info("%s: found %d Form 4 filings since %s", company.ticker, len(results), since)
    return results, None


def filing_to_dict(filing: Filing) -> dict:
    """Filing -> plain dict, for JSON/CSV writing."""
    return asdict(filing)
