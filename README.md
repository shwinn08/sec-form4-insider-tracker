# SEC Form 4 Insider Trading Scraper

Tracks insider transactions (SEC Form 4 filings) for a configurable list of
public companies, for later cleaning and loading into SQLite.

**Current status: Step 1 — filing enumeration.** This lists *what filings
exist*. It does not yet download or parse filing content.

## What a Form 4 is

When a corporate insider — an officer, director, or >10% shareholder — buys or
sells their company's stock, they must report it to the SEC on Form 4, generally
within two business days. Each filing is a small XML document describing the
transaction: who, what security, how many shares, at what price, and their
resulting holdings.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then edit it — see "User-Agent" below
```

## Usage

```bash
python scripts/01_enumerate_filings.py                       # 12 months, tickers from config
python scripts/01_enumerate_filings.py --months 6            # shorter window
python scripts/01_enumerate_filings.py --tickers AAPL MSFT   # ad-hoc ticker list
python scripts/01_enumerate_filings.py --refresh-tickers     # bypass the ticker-map cache
python scripts/01_enumerate_filings.py -v                    # debug logging
```

Output lands in `data/raw/` as a timestamped JSON + CSV pair.

## The User-Agent requirement

The SEC's fair-access policy requires every automated request to identify a real
contact person:

```
SEC_USER_AGENT="Jane Doe Form4Scraper jane@example.com"
```

This is not optional politeness. Requests sent with a default
`python-requests/2.x` header get a **403 with an HTML block page body** — which
is worse than an outright failure, because naive code parses the block page as
though it were data and produces silent garbage. Blocks apply at the IP level
and can persist. `config.get_user_agent()` refuses to run without a configured
address containing `@`, and `SECClient` raises a diagnostic error on any 403
rather than retrying.

## Rate limiting

SEC's published ceiling is 10 requests/second. This project runs at **5**, via a
monotonic-clock minimum-interval limiter in `client.RateLimiter`. Transient
failures (429, 5xx) retry with exponential backoff; 403 and 404 fail
immediately, since retrying won't fix either.

A full 8-ticker run costs 9 HTTP requests and finishes in about two seconds.

## Endpoints used

| Purpose | Endpoint |
|---|---|
| Ticker → CIK | `https://www.sec.gov/files/company_tickers.json` |
| CIK → filing list | `https://data.sec.gov/submissions/CIK##########.json` |
| Filing documents | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/` |

### Things that bite you

**CIK zero-padding is inconsistent across EDGAR.** The submissions API needs the
10-digit padded form (`0000320193`); the Archives directory path needs the
unpadded form (`320193`). `Company` carries both so callers never guess.

**Accession-number formatting is inconsistent too.** The Archives *directory* is
the dash-free form (`000032019325000073`); the index *filename* keeps the dashes
(`0000320193-25-000073-index.htm`).

**The submissions API stores filings column-wise.** `filings.recent` is a dict of
parallel arrays, not a list of records — index `i` of every array describes the
same filing. `_iter_filing_records()` zips them back together.

**`filings.recent` has a horizon.** It holds at least the last 12 months *or*
1000 filings, whichever is larger. For very heavy filers, 1000 filings may not
reach back a full year, so `fetch_form4_filings()` checks the oldest date in
`recent` and follows the `filings.files` overflow chunks if the window isn't
covered.

**`primaryDocument` points at a rendered view, not the data.** For Form 4 it's
usually `xslF345X06/form4.xml`, an XSL-transformed HTML page for humans. Drop
the `xsl*/` path segment to get the raw XML. Both are recorded —
`primary_document_url` and `raw_xml_url`. **Parse `raw_xml_url`.**

**Not every listed company files Form 4 at all.** Foreign private issuers are
exempt from Section 16 under Exchange Act Rule 3a12-3(b) — their insiders never
file Form 4. Ferrari (`RACE`) is one: 630 filings since 2015, all `6-K`/`20-F`,
zero Form 4s. An empty result there is correct, not a bug.

Because a zero can mean *wrong CIK*, *exempt issuer*, or *genuinely no trades* —
and those look identical from the count alone — `diagnose_empty_result()` names
the cause in the run summary and in the JSON's `empty_result_notes`.

**A ticker maps to an issuer, not to a share class.** `FWONK` is the Formula One
Group tracking stock, but the *issuer* is Liberty Media Corp. Its Form 4s cover
Liberty Media insiders across every tracking stock (FWONA/FWONK/LLYV…), not just
the Formula One series. Filing counts for tracking-stock tickers are therefore
issuer-wide; if you need per-series attribution you'll have to get it from the
security title inside each filing's XML.

**A ticker's CIK is not stable over time.** After a merger or holding-company
reorganization the ticker is reassigned to a new registrant while the filing
history stays with the predecessor, and `company_tickers.json` only ever shows
the current one. This is live right now for XOM: SEC maps it to ExxonMobil
Holdings Corp (CIK 2115436, reorganized July 2026), which has **zero** Form 4s,
while all 300+ live under CIK 34088. Without a fix you'd get an empty result and
no error. `config/cik_overrides.json` pins a ticker to a chosen CIK.

## Layout

```
config/
  tickers.txt          companies to track — one per line
  cik_overrides.json   pin a ticker to a specific CIK
src/sec_form4/
  config.py            URLs, paths, tuning knobs, User-Agent validation
  client.py            HTTP: required headers, rate limiting, retries
  tickers.py           ticker -> CIK resolution, with on-disk cache
  filings.py           submissions API -> filtered Form 4 records
  storage.py           JSON + CSV writers
scripts/
  01_enumerate_filings.py
data/
  cache/               company_tickers.json (7-day TTL)
  raw/                 enumeration output
  processed/           (step 3)
```

## Output schema

15 columns per filing: `ticker`, `company_name`, `cik`, `form_type`,
`is_amendment`, `accession_number`, `filing_date`, `report_date`,
`acceptance_datetime`, `primary_document`, `primary_doc_description`,
`filing_index_url`, `primary_document_url`, `raw_xml_url`, `size_bytes`.

`filing_date` is when it was submitted; `report_date` is when the transaction
happened. They differ, and for insider-trading analysis you usually care about
`report_date`. `acceptance_datetime` is Eastern Time, not UTC, despite the
trailing `Z`.

Both `4` and `4/A` are collected. A `4/A` is an amendment correcting an earlier
filing — dropping them loses corrections, but counting them naively
double-counts transactions. `is_amendment` flags them for the dedup decision at
load time.

## Next steps

1. Fetch and parse the Form 4 XML at `raw_xml_url` (transaction codes, share
   counts, prices, derivative vs. non-derivative holdings).
2. Design the SQLite schema and load.
