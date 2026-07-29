# SEC Form 4 Insider Trading Scraper

A data pipeline that collects insider stock transactions (trades made by a
company's own executives, directors, and major shareholders) from the SEC's
public filing system and turns them into clean tabular records. US law requires
corporate insiders to disclose their trades within two business days on a form
called Form 4, and the SEC publishes all of them for free.

The raw data is built for legal compliance rather than analysis. It's spread
across two APIs, indexed by an internal ID instead of a ticker symbol, and
contains fields that look straightforward but mean something other than what
you'd assume. This project handles the sourcing, the rate limiting the SEC
requires, and the data quality problems that make naive parsing produce
confidently wrong numbers.

Insider trading data is a useful signal (an executive buying their own stock
with cash is a meaningful vote of confidence) and a good test of data
engineering practice: the source is authoritative and messy, the edge cases are
subtle, and almost every trap fails silently instead of raising an error.

## Status

| Stage | State |
|---|---|
| 1. Enumerate filings | Complete |
| 2. Fetch and parse XML | Complete |
| 3. SQLite schema and load | Complete |
| 4. Analysis / reporting | Complete |

Current corpus: 11 tickers, 854 filings, 1,998 transactions over a 12 month
window. Every figure in this README is measured from that corpus.

## What a Form 4 is

When someone with inside knowledge of a public company (an officer, a board
member, or anyone holding more than 10% of the shares) buys or sells that
company's stock, Section 16 of the Exchange Act requires them to report it to
the SEC, generally within two business days.

Each report is a small XML document listing who traded, what security, how many
shares, at what price, and what they held afterwards. Filings live in EDGAR, the
SEC's public filing database, and need no API key.

## Setup

Requires Python 3.9+ (developed on 3.13).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Then edit `.env` and set a real contact string:

```
SEC_USER_AGENT="Jane Doe Form4Scraper jane@example.com"
```

### Why the User-Agent is mandatory

The SEC's fair access policy requires automated traffic to identify a real
person who can be contacted if the scraper misbehaves. This is enforced.

The failure mode is what makes it worth calling out. A request sent with the
default `python-requests/2.x` header doesn't get a clean rejection. It gets HTTP
403 with an HTML block page as the response body. Code that doesn't check the
status will hand that HTML to a parser and produce empty or garbage records
without raising anything.

So the project refuses to start without a configured contact address containing
`@` (`config.get_user_agent()`), and raises a diagnostic on any 403 rather than
retrying it (`client.SECClient`). Retrying a rejected identity never succeeds.

### Rate limiting

The SEC publishes a ceiling of 10 requests/second. This project runs at 5,
enforced by a monotonic clock minimum interval limiter. Transient failures (429,
5xx) retry with exponential backoff. 403 and 404 fail immediately because
retrying fixes neither. Downloaded documents are cached to disk, so re-running
the parser costs no additional requests.

## Running it

The stages are separate scripts, run in order.

```bash
# Stage 1: find out what filings exist        -> data/raw/
python scripts/01_enumerate_filings.py

# Stage 2: download and parse those filings   -> data/processed/
python scripts/02_parse_filings.py

# Stage 3: build SQLite and load              -> data/form4.db
python scripts/03_load_database.py --rebuild

# Stage 4: schema demonstration queries
python scripts/04_example_queries.py

# Stage 5: headline analysis, also writes a Markdown report
python scripts/05_analysis.py
```

Useful flags:

```bash
python scripts/01_enumerate_filings.py --months 6           # shorter lookback
python scripts/01_enumerate_filings.py --tickers AAPL MSFT  # override ticker list
python scripts/01_enumerate_filings.py --refresh-tickers    # refresh CIK map cache

python scripts/02_parse_filings.py --limit 25 -v            # fast iteration
python scripts/02_parse_filings.py --force-refresh          # re-download XML
```

Stage 1 resolves each ticker to the SEC's internal company ID (a CIK, or Central
Index Key, which is how EDGAR indexes filings), then lists that company's Form 4
filings with dates, accession numbers, and document URLs. It doesn't download
filing content. Output is timestamped JSON and CSV.

Stage 2 reads Stage 1's output, downloads each filing's raw XML (cached under
`data/cache/filings/`, one file per accession number), and parses it into flat
transaction rows. Output is a 44 column JSON and CSV, plus a separate holdings
file.

Companies to track live in `config/tickers.txt`, one ticker per line.

## Project layout

```
config/
  tickers.txt              companies to track
  cik_overrides.json       pin a ticker to a specific CIK (see findings below)
src/sec_form4/
  config.py                endpoints, paths, tuning, User-Agent validation
  client.py                HTTP: required headers, rate limiting, retries
  tickers.py               ticker to CIK resolution, cached
  filings.py               submissions API to Form 4 filing list
  fetcher.py               XML download with on-disk cache
  parser.py                XML to flat transaction records
  database.py              schema creation and loading
  schema.sql               table definitions and views
  storage.py               JSON / CSV writers
scripts/
  01_enumerate_filings.py
  02_parse_filings.py
  03_load_database.py
  04_example_queries.py
  05_analysis.py
data/
  cache/                   CIK map and raw filing XML
  raw/                     stage 1 output (filing lists)
  processed/               stage 2 output and analysis report
  form4.db                 SQLite database
```

### Endpoints used

| Purpose | Endpoint |
|---|---|
| Ticker to CIK | `https://www.sec.gov/files/company_tickers.json` |
| CIK to filing list | `https://data.sec.gov/submissions/CIK##########.json` |
| Filing documents | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/` |

# Data quality findings

Everything below was found by inspecting the real corpus. All of these fail
silently, producing plausible numbers rather than an error, which is what makes
them worth documenting. Written for someone who hasn't worked with SEC data.

## 1. A company's filing feed contains filings that aren't about that company

You'd assume that asking EDGAR for company X's filings returns filings about
company X.

A Form 4 always involves two parties: the company whose stock was traded (the
issuer) and the insider who traded it (the reporting owner). EDGAR indexes each
filing under both. So when the reporting owner is itself a corporation, that
corporation's filing feed contains Form 4s about entirely different companies.

In this corpus, 14 filings (24 transactions) are affected:

- JPMorgan's feed returns BlackRock municipal bond fund filings. JPMorgan is a
  >10% holder of those funds, so it files Form 4s about them.
- Exxon's feed returns ProPetro Holding Corp (`PUMP`).
- Liberty Media's feed returns Live Nation Entertainment (`LYV`).

Loaded naively, BlackRock fund trades end up recorded as JPMorgan insider
activity.

The fix: the `<issuer>` block inside the XML is the only authoritative statement
of which company a filing concerns. The ticker you searched under is provenance,
not identity. The parser records both and flags every divergence via
`issuer_matches_searched`. Don't key a database table on the searched ticker.

## 2. A ticker doesn't identify what was traded

You'd assume filings found under ticker `FWONK` describe trades in `FWONK`
stock.

A ticker maps to a company, but a company can issue many securities: multiple
share classes, multiple series, and in the case of tracking stocks, separate
stocks tracking different business units under one corporate umbrella.

`FWONK` is the Liberty Media Formula One Group tracking stock. The issuer is
Liberty Media Corp, which also issues Liberty Live series stock, and whose
insiders hold options, restricted stock units, debentures, forward sale
contracts, and put options.

The search returns 232 transactions across 20 distinct securities. Only 38 of
them (16%) are Formula One common stock, covering 2 of those 20. Counting every
Formula One linked instrument, including options and restricted stock units,
raises it to 66 transactions across 5 securities. The other 15 securities have
nothing to do with Formula One.

The XML also contains a field called `issuerTradingSymbol` that looks like the
authoritative answer. It reads `FWONK` on 221 of those 232 rows, and is wrong
83% of the time for identifying the traded security, because it names the
issuer's primary listing rather than the instrument in the transaction.

The fix: the `securityTitle` field on each transaction row is the only reliable
identifier of what changed hands. It's free text (`"Series C Liberty Live Common
Stock"`, `"Stock Option (Right to Buy) - LLYVK"`), which is inconvenient but
correct.

## 3. Holdings and transactions are different kinds of record

Each filing has two tables (non-derivative and derivative), and each table can
contain two structurally different element types:

- `<nonDerivativeTransaction>`: something happened. Has a date, a transaction
  code, a share count, a price.
- `<nonDerivativeHolding>`: a static statement of a position the insider holds.
  No date, no code, no share count, no price. It exists so the filer can
  disclose holdings that weren't traded.

They sit side by side in the same parent element, so a natural looking XPath
like `.//nonDerivative*` picks up both. This corpus has 807 holdings against
1,998 transactions, so parsing them together would inject 807 phantom trades
with null dates and zero shares into the transaction table.

The fix: separate element queries into separate outputs. Holdings are written to
their own file rather than discarded, since they're useful for reconstructing
positions.

## 4. Booleans have three encodings, and `bool()` inverts one of them

Fields like `isDirector`, `isOfficer`, and `isTenPercentOwner` record the
insider's relationship to the company. Across the corpus's role fields they
appear three ways:

| Encoding | Occurrences |
|---|---|
| `1` / `0` | 2,493 |
| element absent entirely | 863 |
| `true` / `false` | 108 |

The third form is the dangerous one. XML values arrive as strings, and in Python
every non-empty string is truthy:

```python
bool("false")   # True, not a typo
bool("0")       # True as well
```

So `is_officer = bool(element.text)` returns `True` for an insider whose filing
explicitly states they are not an officer. The data is inverted, nothing raises,
and the resulting table looks reasonable. You'd just have directors
misclassified as officers throughout.

Absence is the third case. Roughly a quarter of role fields are missing, which
means "not this role" and should map to `False` rather than crashing or
producing `None`.

The fix: an explicit `_parse_bool()` mapping `{"1","true","y","yes"}` to `True`,
`{"0","false","n","no",""}` to `False`, absent to `False`, and logging anything
unrecognised instead of guessing.

## 5. Zero has five different spellings

XML has no numeric type. Every value is text, and each filing agent's software
formats decimals differently. The price `0` appears as:

| Spelling | Count |
|---|---|
| `0` | 404 |
| `0.0000` | 240 |
| `0.000` | 9 |
| `0.0` | 8 |
| `0.00` | 3 |

664 transactions have a price of zero, but a string comparison against `'0'`
matches only 404 of them and misses 260. Group by the raw string and one real
value splits into five buckets. Sort as text and `'0.0000'` orders before
`'0.00'`.

Zero and missing are also different facts. A further 81 transactions have no
price value at all, because the filer supplied a footnote reference instead of a
number:

- Price `0` on a grant (`A`) or gift (`G`) is a real fact. No cash changed
  hands.
- Price absent on an option exercise (`M`) means the price exists but wasn't
  disclosed numerically.

Defaulting missing prices to `0.0`, which is the convenient move, invents 81
free share transfers that never happened. The parser keeps price nullable and
carries a `price_is_disclosed` flag.

Values are parsed as `Decimal` and serialised as strings rather than floats, so
`184.90` survives as `184.90` instead of drifting to `184.9000000000001`. 269
share counts are fractional (401k and dividend reinvestment plans produce
partial shares), so an integer column truncates them.

## 6. One filing can report multiple insiders

Most filings name one reporting owner. 12 filings name two, typically a
corporate entity plus an affiliated trust filing jointly.

There are two failure modes, in opposite directions:

- Using `find()` instead of `findall()` drops the second insider entirely.
- Building a `transactions x owners` join doubles the share counts, because a
  joint filing reports each transaction once, collectively, not once per owner.
  Two owners against one 10,000 share sale becomes 20,000 shares sold.

This is the trap that matters most for schema design. Owners are many-to-many
with filings, so the relationship needs its own table. It can't be flattened
into the transaction row without either losing data or inflating it.

Current handling: transaction rows are attributed to the first listed owner,
with `num_reporting_owners` and a full `all_reporting_owners` roster carried
alongside.

## 7. `shares_owned_following` is a running balance, not a position

The field `sharesOwnedFollowingTransaction` reads like "how much this person
owns". It's the balance after that specific transaction line, for that specific
ownership form, and both qualifiers matter.

A single filing can contain several lines, each with its own successive balance,
and can split holdings between direct ownership (shares in your own name) and
indirect ownership (shares held via a trust, a spouse, a family partnership).
These are separate pools reported separately.

One example from the corpus, a single filing and a single security:

| Line | Ownership | `shares_owned_following` |
|---|---|---|
| 1 | Direct | 230,278 |
| 2 | Direct | 224,619 |
| 3 | Indirect (By Spouse) | 135,027 |

Lines 1 and 2 are sequential states of the same pool, the balance before and
after another sale. Line 3 is a different pool.

Summing gives 589,924, which corresponds to nothing: it double counts the direct
pool by adding an intermediate snapshot to a final one, then adds an unrelated
pool on top. 25 (filing, security) groups in this corpus mix direct and indirect
rows this way.

The correct read is the last row per (owner, security, ownership form), then a
deliberate decision about whether direct and indirect should be combined for
your purpose. Don't `SUM()` the column.

## 8. Most Form 4 rows are not trades

A transaction code identifies what kind of event occurred. Only two are open
market trades:

| Code | Meaning | Count |
|---|---|---|
| `S` | Open-market sale | 819 |
| `A` | Grant or award from the company | 418 |
| `M` | Option or derivative exercise | 311 |
| `F` | Shares withheld to pay taxes | 228 |
| `J` | Other (footnote explains) | 137 |
| `G` | Gift | 55 |
| `P` | Open-market purchase | 28 |
| `D` | Disposition to the issuer | 2 |

847 of 1,998 rows are market transactions. The other 1,151 are not. Reporting
"insiders acquired 418,000 shares" from code `A` describes equity compensation
being granted, not anyone choosing to buy. The signal most people want lives in
the 28 `P` rows.

## 9. Three different dates, and they disagree

- `transaction_date`: when the trade happened. Use this one.
- `filing_date`: when it was submitted to the SEC.
- `period_of_report`: filing level, not row level.

`transaction_date` differs from `filing_date` in 1,729 of 1,998 rows, and from
`period_of_report` in 419. Typical lag is one to two business days, matching the
statutory deadline, but the corpus maximum is 233 days. Late filings exist, so
any "insider activity this week" query keyed on `filing_date` will mix in trades
from months earlier.

## 10. Smaller EDGAR mechanics worth knowing

A ticker's CIK is not permanent. After a merger or holding company
reorganisation, the ticker moves to a new legal entity while the filing history
stays with the predecessor, and the SEC's ticker to CIK file only reflects the
current registrant. Live example: `XOM` maps to ExxonMobil Holdings Corp
(reorganised July 2026), which has zero Form 4s, while 300+ sit under the
predecessor CIK. You get an empty result and no error.
`config/cik_overrides.json` pins a ticker to a chosen CIK.

Not every listed company files Form 4 at all. Foreign private issuers are exempt
from Section 16 under Rule 3a12-3(b). Ferrari (`RACE`) has 630 filings since
2015 and not one Form 4. An empty result there is correct.

Because "zero filings" can mean a wrong CIK, an exempt issuer, or genuinely no
trades, and those are indistinguishable from the count alone, the enumerator
diagnoses the cause rather than reporting a bare zero.

CIK zero padding is inconsistent. The submissions API needs the 10 digit padded
form (`0000320193`); the archive path needs it unpadded (`320193`). Accession
numbers are similar: the directory drops the dashes, the index filename keeps
them. Mixing them up is the most common source of 404s.

The submissions API stores filings column-wise, as parallel arrays where index
*i* of every array describes the same filing, rather than a list of objects.

The obvious document URL is a rendered view, not the data. The `primaryDocument`
field points at an XSL transformed HTML page built for human reading. Removing
the `xsl*/` path segment yields the raw XML. Parsing the rendered page means
scraping HTML for data that's already available structured.

`(security_title, transaction_date, transaction_code)` is not unique. 164 filings
contain repeated triples, which are separate tranches or price points rather
than duplicates. A line ordinal is required for a candidate key.

Filings with no transactions are legitimate. 7 filings report nothing at all,
each flagged `notSubjectToSection16`. These are exit filings from insiders who
resigned, and one says so in its remarks.

## 11. Security titles are free text, and spelled inconsistently

Found by querying the loaded database rather than during parsing.
`security_title` is the only reliable identifier of a traded instrument (finding
2), but it isn't a clean one. Different filing agents spell the same security
differently for the same issuer:

| Issuer | Title as filed | Transactions |
|---|---|---|
| NVIDIA | `Common Stock` | 413 |
| NVIDIA | `Common` | 153 |
| Walmart | `Common` | 289 |
| Walmart | `Common Stock` | 2 |
| Sky Quarry | `Common Stock, par value $0.0001` | 1 |

The `securities` table would otherwise split one real security across several
rows, and per-security aggregates would undercount.

This is resolved with a curated alias table rather than a normalisation rule.
Collapsing `Common` into `Common Stock` is safe, but the same rule applied to
Liberty Media would merge `Series C Common Stock` with `Series C Liberty Live
Common Stock`, which are different securities. A string heuristic that silently
merges distinct instruments is worse than the duplication it fixes.

`security_aliases` holds three reviewed mappings, each carrying its
justification:

| Issuer | Filed as | Canonical | Evidence |
|---|---|---|---|
| NVIDIA | `Common` | `Common Stock` | Non-overlapping filer agents (4 vs 14, none shared), same date range, single common class |
| Walmart | `Common Stock` | `Common` | Agents `1579299` and `1502438` each filed both spellings themselves |
| BlackRock MuniHoldings | doubled-space variant | single-spaced form | Identical text apart from doubled spaces; same agent, same issuer |

Three candidates were examined and rejected:

- Goodyear `2022 Plan Restricted Stock Units` vs `Restricted Stock Units`. The
  footnotes settle it. The generic title covers RSUs *"accrued, pursuant to an
  election by the reporting person, to the Retainer Deferral Account"*, which is
  director deferred compensation, while the other is explicitly *"an RSU grant
  under the 2022 Performance Plan."* The generic bucket also holds both retainer
  accruals and plan vestings, so it can't map cleanly anywhere.
- Pfizer `Phantom Stock Units` vs `Phantom Stock Units SSP`. SSP is the
  Supplemental Savings Plan, used by exactly one insider.
- Liberty Media `Series C Common Stock`. Ambiguous and time dependent, since it
  appears only after Liberty Live split off into its own issuer. Assigning it to
  either series would reallocate real transactions.

Nothing is destructive. `securities.security_title` keeps the exact value as
filed and is never rewritten. The alias resolves in `v_securities_canonical`, so
every canonical aggregate can be audited against the unmerged data. The loader
fails loudly if a curated mapping stops resolving, and rejects alias chains.

Result: 57 securities resolve to 54 canonical.

# The database

Stage 3 loads everything into SQLite (`data/form4.db`, roughly 900 KB).

| Table | Rows | Purpose |
|---|---|---|
| `companies` | 24 | Issuers, keyed by CIK rather than ticker |
| `filings` | 854 | One row per Form 4 document |
| `reporting_owners` | 192 | The insiders |
| `filing_owners` | 866 | Many-to-many, with per-filing roles |
| `securities` | 57 | `(issuer_cik, security_title)` |
| `security_aliases` | 3 | Curated canonical mappings |
| `transaction_codes` | 19 | Lookup, with `is_open_market` |
| `transactions` | 1,998 | One row per transaction line |
| `holdings` | 807 | Positions with no transaction |

Three views: `v_transactions` (casts decimals, exposes `signed_shares` and
`transaction_value`), `v_securities_canonical` (alias resolution), and
`v_current_positions` (the correct read of `shares_owned_following`).

### How the schema encodes each finding

- Issuer misattribution: three separate CIK columns on `filings`, with a foreign
  key on `issuer_cik` only. `issuer_matches_searched` is a generated column, so
  it can't drift from the values it compares. 15 filings flagged.
- Ticker is not security: `securities` is keyed on `(issuer_cik,
  security_title)`. There's no ticker or `issuer_trading_symbol` column anywhere
  in the schema, since removing the misleading field is safer than storing one
  people reach for by reflex.
- Holdings vs transactions: separate tables. `holdings` has no
  `transaction_date`, `transaction_code`, `shares` or `price_per_share` column,
  because those facts don't exist in the source.
- Booleans: `INTEGER` with `CHECK (x IN (0,1))`. SQLite is dynamically typed and
  will store the string `'false'` in an INTEGER column, so the CHECK is the
  actual enforcement. Verified by inserting `'false'` and getting an
  `IntegrityError`.
- Zero vs undisclosed price: `NULL` for undisclosed (81 rows), the value as
  filed for a real zero (664). `NULL` also makes `AVG()` skip undisclosed rows
  instead of dragging the average toward zero. A `CHECK` blocks empty strings
  from becoming an ambiguous third state.
- Fractional shares and precision: all decimals stored as `TEXT`. Declaring them
  `NUMERIC` or `REAL` would make SQLite convert `'184.90'` to the float `184.9`
  on insert, undoing the parser's exactness. Casting happens once, in the views.
- Repeat tranches: `line_number` is part of the transactions primary key.
- Joint filings: `transactions` has no owner column. A transaction belongs to a
  filing, and a filing has N owners via `filing_owners`. You can't sum shares per
  insider without joining through it and deciding what to do about the 12 joint
  filings, which turns a silent double count into an explicit question.
- Grants aren't purchases: `transaction_codes.is_open_market` lets someone who
  has never read a Form 4 write `WHERE is_open_market = 1`.

# Sample queries and findings

Real output from `scripts/05_analysis.py` against the loaded database: 854
filings, 1,998 transactions, 24 issuers. The script is re-runnable and writes a
Markdown report to `data/processed/analysis_report.md`. All security grouping
uses the canonical security, never the searched ticker.

## Net open-market insider activity by company

Codes `P` and `S` only. Grants, option exercises and tax withholding are
excluded, since they're compensation mechanics rather than investment decisions.

| Company | Txns | Shares bought | Shares sold | Bought $M | Sold $M |
|---|---|---|---|---|---|
| Walmart | 128 | 0 | 31,315,447 | 0.00 | 3,597 |
| NVIDIA | 497 | 0 | 10,411,638 | 0.00 | 1,941 |
| ProPetro Holding | 1 | 0 | 16,600,000 | 0.00 | 276.56 |
| Apple | 31 | 0 | 750,748 | 0.00 | 199.00 |
| Tesla | 94 | 2,568,732 | 407,882 | 999.96 | 162.34 |
| JPMorgan Chase | 42 | 0 | 414,953 | 0.00 | 127.40 |
| Microsoft | 21 | 8,842 | 248,080 | 3.44 | 122.71 |
| Liberty Media | 20 | 0 | 747,677 | 0.00 | 49.79 |
| Exxon Mobil | 8 | 0 | 19,618 | 0.00 | 2.63 |
| Goodyear | 1 | 100,000 | 0 | 0.75 | 0.00 |

Ten of twelve companies show zero open-market insider buying over twelve months.

## Insiders almost never buy

Across 11 companies and a full year, open-market purchases came from four
people:

| Insider | Company | Date | Tranches | Shares | $M |
|---|---|---|---|---|---|
| Elon Musk | Tesla | 2025-09-12 | 25 | 2,568,732 | 999.96 |
| John W Stanton | Microsoft | 2026-02-18 | 1 | 5,000 | 1.99 |
| Bradford L Smith | Microsoft | 2025-04-23 | 1 | 3,842 | 1.45 |
| Jason J Winkler | Goodyear | 2025-11-14 | 1 | 100,000 | 0.75 |

99.6% of the $1.004 billion is one person on one day. Excluding Musk's Tesla
purchase, insider buying across eleven large-cap companies for a year totals
$4.2 million, against roughly $6.5 billion of selling.

This is the clearest argument for the `is_open_market` flag. The raw data
contains 418 `A` (grant) transactions that a naive query reads as acquisitions,
making insiders look like enthusiastic buyers when they were being handed
equity.

## Top insiders by transaction count and dollar value

| Insider | Company | Role | Txns | Filings |
|---|---|---|---|---|
| Jen Hsun Huang | NVIDIA | President and CEO | 306 | 27 |
| Colette Kress | NVIDIA | EVP & CFO | 143 | 12 |
| Walton Family Holdings Trust | Walmart | | 70 | 23 |
| Kathleen Wilson-Thompson | Tesla | Director | 44 | 3 |
| Renee L Wilm | Liberty Media | Chief Legal/Admin Officer | 33 | 7 |

By open-market dollar value, restricted to single-owner filings so joint filings
can't double count:

| Insider | Company | Sold $M |
|---|---|---|
| Walton Family Holdings Trust | Walmart | 3,523 |
| Jen Hsun Huang | NVIDIA | 782.04 |
| Mark A Stevens | NVIDIA | 699.59 |
| Exxon Mobil Corp | ProPetro Holding | 276.56 |
| Ajay K Puri | NVIDIA | 183.00 |
| Arthur D Levinson | Apple | 107.63 |

Row four is worth a look: the insider is Exxon Mobil Corp and the company is
ProPetro Holding, a corporation selling down a stake in another company it held
more than 10% of. That row surfaces in an Exxon ticker search and says nothing
about Exxon insider sentiment. It's finding 1 showing up in a headline number.

## Tracking stocks: what a FWONK ticker search returns

| Security | Actual issuer | Formula One? | Txns |
|---|---|---|---|
| Stock Option (Right to Buy) - LLYVK | Liberty Media Corp | no | 61 |
| Series C Liberty Live Common Stock | Liberty Media Corp | no | 56 |
| Series C Liberty Formula One Common Stock | Liberty Media Corp | yes | 37 |
| Restricted Stock Units - LLYVK | Liberty Media Corp | no | 16 |
| Stock Option (Right to Buy) - FWONK | Liberty Media Corp | yes | 14 |
| Restricted Stock Units-FWONK | Liberty Media Corp | yes | 13 |
| Series A Liberty Live Common Stock | Liberty Media Corp | no | 11 |
| Series C Common Stock | Liberty Media Corp | no | 4 |
| Common Stock | Live Nation Entertainment | no | 2 |
| Series C Liberty Live Group Common Stock | Liberty Live Holdings | no | 2 |
| Forward sale contract (obligation to sell) | Live Nation Entertainment | no | 1 |
| 2.375% Exch. Sr. Debentures due 2053 | Live Nation Entertainment | no | 1 |

Only 66 of 232 transactions (28.4%) are Formula One instruments, and that counts
generously by including FWONK-linked options and RSUs rather than just common
stock. The search spans 20 distinct securities across 3 issuers, and includes a
bond and a forward sale contract.

A pipeline that labelled every row `FWONK`, which is the obvious approach and
what the XML's own `issuerTradingSymbol` field would tell you, would be wrong on
71.6% of the data with no error raised anywhere.

## Code `G` gifts are frequently not gifts

`G` is defined as a bona fide gift, which reads like charitable giving. The
largest rows say otherwise:

| Insider | Company | Date | Shares | Ownership | Held via |
|---|---|---|---|---|---|
| Jen Hsun Huang | NVIDIA | 2026-03-18 | 58,962,602 | I | By Irrevocable Remainder Trust |
| Jen Hsun Huang | NVIDIA | 2026-03-18 | 29,481,301 | I | By Grantor Retained Annuity Trust |
| Jen Hsun Huang | NVIDIA | 2026-03-18 | 29,481,301 | I | By Grantor Retained Annuity Trust |
| A Brooke Seawell | NVIDIA | 2025-08-29 | 1,200,000 | I | By Administrative Trust |
| Tench Coxe | NVIDIA | 2025-09-08 | 1,000,000 | I | By Trust |

The three largest, 117.9 million shares on a single day, are footnoted as *"a
transfer of shares by The Lori Lynn Huang 2016 Annuity Trust II Agreement (the
'Grantor Retained Annuity Trust 1') to The Huang Irrevocable Remainder Trust."*
That's estate planning moving stock between the insider's own vehicles, not
stock leaving their control. Every large `G` row is flagged `I` for indirect.

Treating code `G` as philanthropy, or as any change in beneficial ownership,
would be wrong. This one only surfaced at the analysis stage rather than during
parsing.

## Section 16 deadline outliers

Insiders must file within two business days.

| Insider | Company | Transaction | Filed | Days | Code |
|---|---|---|---|---|---|
| Bradford L Smith | Microsoft | 2025-04-23 | 2025-12-12 | 233 | `P` |
| Renee L Wilm | Liberty Media | 2026-02-04 | 2026-02-23 | 19 | `A` |
| Brian J Wendling | Liberty Media | 2026-02-04 | 2026-02-23 | 19 | `A` |
| Amy Coleman | Microsoft | 2025-09-15 | 2025-10-03 | 18 | `A` |

The Microsoft outlier is 233 days, and it's a purchase, the most signal bearing
transaction type there is. It also explains why the analysis reports a
transaction date range starting 2025-04-23, four months before the 12 month
filing window opens: a late enough filing drags a much older transaction into
the dataset. Any query treating `filing_date` as a proxy for when something
happened inherits that error.

## Output schema

Stage 2 emits 44 columns per transaction. The ones carrying the findings above:

| Column | Why it exists |
|---|---|
| `issuer_cik`, `issuer_name` | Authoritative issuer, from the XML (finding 1) |
| `searched_ticker`, `searched_cik` | Provenance: which feed surfaced it |
| `issuer_matches_searched` | Flags misattributed filings |
| `filer_agent_cik` | From the accession prefix: the transmitter, not the issuer |
| `security_title` | What was actually traded (finding 2) |
| `transaction_code` | Trade vs grant vs tax withholding (finding 8) |
| `transaction_price_per_share` | Nullable, and `NULL` is not `0` (finding 5) |
| `price_is_disclosed` | Distinguishes zero from undisclosed |
| `num_reporting_owners`, `all_reporting_owners` | Joint filings (finding 6) |
| `line_number` | Ordinal, required for uniqueness |
| `not_subject_to_section16` | Marks exit filings |

## What I'd add next

Footnote text extraction is the highest value gap, and the analysis stage is
what proved it. Three separate findings bottomed out in footnote prose the
pipeline doesn't capture:

- 81 transactions carry a footnote instead of a price, and the footnote often
  contains the figure ("a weighted average price of $X").
- Code `G` gifts can only be distinguished from trust-to-trust transfers by
  reading the footnote.
- The Goodyear alias decision required reading footnotes by hand.

Only footnote IDs are stored today. Extracting the text would turn three manual
investigations into queryable columns, and is a prerequisite for classifying `J`
("other", 137 transactions) at all.

After that:

- A 10b5-1 flag. Filings indicate whether a trade was made under a pre-arranged
  trading plan, via the `aff10b5One` element and footnote text. Pre-planned
  sales carry far less signal than discretionary ones, and the dataset currently
  can't separate them, which affects how the "insiders almost never buy" result
  should be read.
- Per-owner share attribution for joint filings. The schema makes the ambiguity
  explicit, and the dollar value query sidesteps it by restricting to
  single-owner filings, but those 12 filings need a documented rule rather than
  an exclusion.
- Form 4/A amendment handling. Amendments correct earlier filings. The current
  12 month window contains none, so the dedup logic is written but unexercised
  and needs a longer window to test against. Counting both an original and its
  amendment double counts the transaction.
- Incremental runs, re-fetching only filings newer than the last run rather than
  the whole window.
- Automated tests against pinned fixtures. The trap cases (multi-owner,
  holdings-only, undisclosed price, issuer mismatch) each have a known accession
  number and would make good regression fixtures.
- Backfill beyond 12 months, which requires following the submissions API's
  overflow chunks. Already implemented but only lightly exercised.
- Enrichment with market prices, to check whether insider purchases preceded
  returns. That's the analytical payoff, and the reason for insisting on
  `security_title` rather than ticker for the join.

## Notes

- All data is public and free. No API key or account required.
- Nothing here constitutes investment advice.
- SEC EDGAR documentation: <https://www.sec.gov/edgar/sec-api-documentation>
