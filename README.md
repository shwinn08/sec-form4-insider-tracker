# SEC Form 4 Insider Trading Scraper

A data pipeline that collects **insider stock transactions** — the trades made
by a company's own executives, directors, and major shareholders — directly from
the SEC's public filing system, and turns them into clean tabular records. US
law requires corporate insiders to disclose their trades within two business
days on a form called **Form 4**, and the SEC publishes every one of them for
free. The catch is that the raw data is designed for legal compliance, not
analysis: it's spread across two different APIs, indexed by an internal ID
rather than by ticker symbol, and full of fields that look straightforward but
quietly mean something other than what you'd assume. This project handles the
sourcing, the rate-limiting etiquette the SEC requires, and — most of the
actual work — the data-quality problems that make naive parsing produce
confidently wrong numbers.

**Why insider trading data?** It's a genuinely useful signal (executives buying
their own stock with cash is a meaningful vote of confidence) and it's a good
test of real data engineering: the source is authoritative and messy, the edge
cases are subtle rather than obvious, and nearly every trap fails *silently*
rather than throwing an error.

---

## Status

| Stage | State |
|---|---|
| 1. Enumerate filings | Complete |
| 2. Fetch + parse XML | Complete |
| 3. SQLite schema + load | In progress |
| 4. Analysis / reporting | Not started |

Current corpus: **11 tickers, 854 filings, 1,998 transactions** over a 12-month
window. All figures quoted in this README are measured from that corpus, not
estimated.

---

## What a Form 4 actually is

When someone with inside knowledge of a public company — an officer, a board
member, or anyone holding more than 10% of the shares — buys or sells that
company's stock, US securities law (Section 16 of the Exchange Act) requires
them to report it to the SEC, generally within two business days.

Each report is a small XML document listing who traded, what security, how many
shares, at what price, and what they held afterwards. Filings live in EDGAR, the
SEC's public filing database, and are free to access with no API key.

---

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

The SEC's fair-access policy requires automated traffic to identify a real
person who can be contacted if the scraper misbehaves. This is enforced, not
advisory.

What makes it worth calling out is the *failure mode*. A request sent with the
default `python-requests/2.x` header doesn't get a clean rejection — it gets
**HTTP 403 with an HTML block page as the response body**. Code that doesn't
check the status will happily hand that HTML to a parser and produce empty or
garbage records with no exception raised anywhere. A loud failure would be
safer than what actually happens.

This project therefore refuses to start without a configured contact address
containing `@` (`config.get_user_agent()`), and raises an explicit diagnostic on
any 403 instead of retrying it (`client.SECClient`), since retrying a rejected
identity never succeeds.

### Rate limiting

The SEC publishes a ceiling of 10 requests/second. This project runs at **5**,
enforced by a monotonic-clock minimum-interval limiter. Transient failures (429,
5xx) retry with exponential backoff; 403 and 404 fail immediately because
retrying fixes neither. Downloaded documents are cached to disk, so re-running
the parser costs zero additional requests.

---

## Running it

The two stages are separate scripts, run in order.

```bash
# Stage 1 — find out what filings exist          -> data/raw/
python scripts/01_enumerate_filings.py

# Stage 2 — download and parse those filings     -> data/processed/
python scripts/02_parse_filings.py
```

Useful flags:

```bash
python scripts/01_enumerate_filings.py --months 6           # shorter lookback
python scripts/01_enumerate_filings.py --tickers AAPL MSFT  # override ticker list
python scripts/01_enumerate_filings.py --refresh-tickers    # refresh CIK map cache

python scripts/02_parse_filings.py --limit 25 -v            # fast iteration
python scripts/02_parse_filings.py --force-refresh          # re-download XML
```

**Stage 1** resolves each ticker to the SEC's internal company ID (a **CIK**,
Central Index Key — EDGAR indexes by this, not by ticker), then lists that
company's Form 4 filings with dates, accession numbers, and document URLs. It
does not download filing content. Output: timestamped JSON + CSV.

**Stage 2** reads Stage 1's output, downloads each filing's raw XML (cached
under `data/cache/filings/`, one file per accession number), and parses it into
flat transaction rows. Output: 44-column JSON + CSV, plus a separate holdings
file.

Companies to track live in `config/tickers.txt`, one ticker per line.

---

## Project layout

```
config/
  tickers.txt              companies to track
  cik_overrides.json       pin a ticker to a specific CIK (see findings below)
src/sec_form4/
  config.py                endpoints, paths, tuning, User-Agent validation
  client.py                HTTP: required headers, rate limiting, retries
  tickers.py               ticker -> CIK resolution, cached
  filings.py               submissions API -> Form 4 filing list
  fetcher.py               XML download with on-disk cache
  parser.py                XML -> flat transaction records
  storage.py               JSON / CSV writers
scripts/
  01_enumerate_filings.py
  02_parse_filings.py
data/
  cache/                   CIK map + raw filing XML
  raw/                     stage 1 output (filing lists)
  processed/               stage 2 output (parsed transactions)
```

### Endpoints used

| Purpose | Endpoint |
|---|---|
| Ticker → CIK | `https://www.sec.gov/files/company_tickers.json` |
| CIK → filing list | `https://data.sec.gov/submissions/CIK##########.json` |
| Filing documents | `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/` |

---

# Data quality findings

This is the substance of the project. Every item below was found by inspecting
the real corpus, and every one of them fails **silently** — producing plausible
numbers rather than an error. They're written for someone who has never worked
with SEC data.

## 1. A company's filing feed contains filings that aren't about that company

**What you'd assume:** if you ask EDGAR for company X's filings, you get filings
about company X.

**What's actually true:** a Form 4 always involves *two* parties — the company
whose stock was traded (the **issuer**) and the insider who traded it (the
**reporting owner**). EDGAR indexes each filing under *both*. So when the
reporting owner is itself a corporation, that corporation's filing feed contains
Form 4s about **completely different companies**.

In this corpus, 14 filings (24 transactions) are affected:

- JPMorgan's feed returns **BlackRock municipal bond fund** filings — JPMorgan
  is a >10% holder of those funds, so it files Form 4s about *them*.
- Exxon's feed returns **ProPetro Holding Corp** (`PUMP`).
- Liberty Media's feed returns **Live Nation Entertainment** (`LYV`).

Loaded naively, you'd have BlackRock fund trades recorded as JPMorgan insider
activity.

**The fix:** the `<issuer>` block *inside the XML* is the only authoritative
statement of which company a filing concerns. The ticker you searched under is
provenance, not identity. The parser records both and flags every divergence via
`issuer_matches_searched`. **Never key a database table on the searched ticker.**

## 2. A ticker doesn't identify what was actually traded

**What you'd assume:** filings found under ticker `FWONK` describe trades in
`FWONK` stock.

**What's actually true:** a ticker maps to a *company*, but a company can issue
many different securities — multiple share classes, multiple series, and in the
case of **tracking stocks**, entirely separate stocks tracking different
business units under one corporate umbrella.

`FWONK` is the Liberty Media Formula One Group tracking stock. But the issuer is
Liberty Media Corp, which also issues Liberty Live series stock, and whose
insiders also hold options, restricted stock units, debentures, forward sale
contracts, and put options.

Of **232 transactions** found under `FWONK`, only **38 (16%)** are actually
Formula One securities. The rest span **19 distinct security titles**.

Worse — and this is the part that catches people — the XML contains a field
called `issuerTradingSymbol` that *looks* like the authoritative answer. It
reads `FWONK` on 221 of those 232 rows. It is **wrong 83% of the time** for
identifying the traded security, because it names the issuer's primary listing,
not the instrument in the transaction.

**The fix:** the `securityTitle` field on each individual transaction row is the
only reliable identifier of what changed hands. It's free text (e.g. `"Series C
Liberty Live Common Stock"`, `"Stock Option (Right to Buy) - LLYVK"`), which is
inconvenient, but it's correct.

## 3. Holdings and transactions are different kinds of record

Each filing has two tables (non-derivative and derivative), and each table can
contain two *structurally different* element types:

- `<nonDerivativeTransaction>` — something happened: has a date, a transaction
  code, a share count, a price.
- `<nonDerivativeHolding>` — a static statement of a position the insider holds:
  **no date, no code, no share count, no price.** It exists so the filer can
  disclose holdings that weren't traded.

They sit side by side in the same parent element, so a natural-looking XPath
like `.//nonDerivative*` scoops up both. This corpus has **807 holdings** against
1,998 transactions — parsing them together would inject 807 phantom trades with
null dates and zero shares into the transaction table.

**The fix:** parse them with separate element queries into separate outputs.
Holdings are written to their own file rather than discarded, since they're
legitimately useful for reconstructing positions.

## 4. Booleans have three encodings, and Python's `bool()` inverts one of them

Fields like `isDirector`, `isOfficer`, and `isTenPercentOwner` record the
insider's relationship to the company. Across the corpus's role fields they
appear three ways:

| Encoding | Occurrences |
|---|---|
| `1` / `0` | 2,493 |
| element absent entirely | 863 |
| `true` / `false` | 108 |

The danger is the third form. XML values arrive as **strings**, and in Python
every non-empty string is truthy:

```python
bool("false")   # True  ← not a typo
bool("0")       # True  ← also True
```

So `is_officer = bool(element.text)` returns `True` for an insider whose filing
explicitly states they are **not** an officer. The data is inverted, no error is
raised, and the resulting table looks entirely reasonable — you'd just have
directors misclassified as officers throughout.

Absence is a third case: roughly a quarter of role fields are simply missing,
which means "not this role" and must map to `False` rather than crashing or
producing `None`.

**The fix:** an explicit `_parse_bool()` that maps `{"1","true","y","yes"}` →
`True`, `{"0","false","n","no",""}` → `False`, absent → `False`, and logs
anything unrecognised instead of guessing.

## 5. Zero has five different spellings

XML has no numeric type — every value is text, and each filing agent's software
formats decimals differently. The price `0` appears as:

| Spelling | Count |
|---|---|
| `0` | 404 |
| `0.0000` | 240 |
| `0.000` | 9 |
| `0.0` | 8 |
| `0.00` | 3 |

**664 transactions** have a price of zero, but a string comparison against `'0'`
matches only 404 of them and **silently misses 260**. Group by the raw string
and one real value splits into five buckets. Sort as text and `'0.0000'` orders
before `'0.00'`.

**Separately: zero and missing are different facts.** A further **81**
transactions have *no price value at all* — the filer supplied a footnote
reference instead of a number. Compare:

- Price `0` on a grant (`A`) or gift (`G`): a real fact. No cash changed hands.
- Price absent on an option exercise (`M`): the price exists but wasn't
  disclosed numerically.

Defaulting missing prices to `0.0` — the obvious convenience — invents 81
free share transfers that never happened. The parser keeps price nullable and
carries a `price_is_disclosed` flag so the two stay distinguishable.

Values are parsed as `Decimal` and serialised as strings rather than floats, so
`184.90` survives as `184.90` instead of drifting to `184.9000000000001`. Note
also that **269 share counts are genuinely fractional** (401k and dividend
reinvestment plans produce partial shares) — an integer column truncates them.

## 6. One filing can report multiple insiders, and joining naively doubles the shares

Most filings name one reporting owner. **12 filings name two** — typically a
corporate entity plus an affiliated trust filing jointly.

Two failure modes, in opposite directions:

- Using `find()` (singular) instead of `findall()` silently **drops the second
  insider** entirely.
- Building a `transactions × owners` join **doubles the share counts** — because
  a joint filing reports each transaction *once, collectively*, not once per
  owner. Two owners × one 10,000-share sale becomes 20,000 shares sold.

This is the trap that matters for schema design: owners are **many-to-many**
with filings, so the relationship needs its own table. It cannot be flattened
into the transaction row without either losing data or inflating it.

**Current handling:** transaction rows are attributed to the first-listed owner,
with `num_reporting_owners` and a full `all_reporting_owners` roster carried
alongside so nothing is lost before the schema exists to hold it properly.

## 7. `shares_owned_following` is a running balance, not a position

The field `sharesOwnedFollowingTransaction` reads like "how much this person
owns." It isn't. It's the balance **after that specific transaction line**, for
**that specific ownership form** — and both qualifiers matter.

A single filing can contain several lines, each with its own successive balance,
*and* split holdings between **direct** ownership (shares in your own name) and
**indirect** ownership (shares held via a trust, a spouse, a family
partnership). These are separate pools that are reported separately.

One real example from the corpus — a single filing, single security:

| Line | Ownership | `shares_owned_following` |
|---|---|---|
| 1 | Direct | 230,278 |
| 2 | Direct | 224,619 |
| 3 | Indirect (*By Spouse*) | 135,027 |

Lines 1 and 2 are **sequential states of the same pool** — the balance before
and after another sale. Line 3 is a **different pool entirely**.

Summing gives 589,924, a number corresponding to nothing: it double-counts the
direct pool by adding an intermediate snapshot to a final one, then adds an
unrelated pool on top. 25 (filing, security) groups in this corpus mix direct
and indirect rows this way.

**The correct read:** take the **last** row per (owner, security, ownership
form), then decide deliberately whether direct and indirect should be summed for
your purpose. Never `SUM()` the column.

## 8. Most Form 4 rows are not trades

A transaction code identifies what kind of event occurred. Only two are
open-market trades:

| Code | Meaning | Count |
|---|---|---|
| `S` | Open-market **sale** | 819 |
| `A` | **Grant/award** from the company | 418 |
| `M` | Option/derivative **exercise** | 311 |
| `F` | Shares **withheld** to pay taxes | 228 |
| `J` | Other (footnote explains) | 137 |
| `G` | **Gift** | 55 |
| `P` | Open-market **purchase** | 28 |
| `D` | Disposition to the issuer | 2 |

**847 of 1,998 rows are actual market transactions; 1,151 are not.** Reporting
"insiders acquired 418,000 shares" from code `A` describes equity compensation
being granted, not anyone choosing to buy. The signal most people actually want
lives in the 28 `P` rows.

## 9. Three different dates, and they disagree

- `transaction_date` — when the trade happened *(use this)*
- `filing_date` — when it was submitted to the SEC
- `period_of_report` — filing-level, not row-level

`transaction_date` differs from `filing_date` in **1,729 of 1,998 rows**, and
from `period_of_report` in 419. Typical lag is 1–2 business days, matching the
statutory deadline — but the corpus maximum is **233 days**, so late filings
exist and any "insider activity this week" query keyed on `filing_date` will
mix in trades from months earlier.

## 10. Smaller EDGAR mechanics worth knowing

**A ticker's CIK is not permanent.** After a merger or holding-company
reorganisation, the ticker moves to a new legal entity while the filing history
stays with the predecessor — and the SEC's ticker→CIK file only ever reflects
the *current* registrant. Live example: `XOM` maps to ExxonMobil Holdings Corp
(reorganised July 2026), which has **zero** Form 4s, while 300+ sit under the
predecessor CIK. You get an empty result and no error.
`config/cik_overrides.json` pins a ticker to a chosen CIK.

**Not every listed company files Form 4 at all.** Foreign private issuers are
exempt from Section 16 (Rule 3a12-3(b)). Ferrari (`RACE`) has 630 filings since
2015 and not one Form 4. An empty result there is correct.

Because "zero filings" can mean *wrong CIK*, *exempt issuer*, or *genuinely no
trades* — indistinguishable from the count alone — the enumerator diagnoses the
cause rather than reporting a bare zero.

**CIK zero-padding is inconsistent.** The submissions API needs the 10-digit
padded form (`0000320193`); the archive path needs it unpadded (`320193`).
Accession numbers are similar: the directory drops the dashes, the index
filename keeps them. Mixing them up is the most common source of 404s.

**The submissions API stores filings column-wise** — parallel arrays where index
*i* of every array describes the same filing, not a list of objects.

**The obvious document URL is a rendered view, not the data.** The
`primaryDocument` field points at an XSL-transformed HTML page built for human
reading. Removing the `xsl*/` path segment yields the raw XML. Parsing the
rendered page means scraping HTML for data that's available structured.

**`(security_title, transaction_date, transaction_code)` is not unique.** 164
filings contain repeated triples — genuinely separate tranches or price points,
not duplicates. A line ordinal is required for a candidate key.

**Filings with no transactions are legitimate.** 7 filings report nothing at
all, each flagged `notSubjectToSection16` — "exit filings" from insiders who
resigned. One states so in its remarks.

---

## Output schema

Stage 2 emits 44 columns per transaction. The ones that carry the findings
above:

| Column | Why it exists |
|---|---|
| `issuer_cik`, `issuer_name` | Authoritative issuer, from the XML (finding 1) |
| `searched_ticker`, `searched_cik` | Provenance — which feed surfaced it |
| `issuer_matches_searched` | Flags misattributed filings |
| `filer_agent_cik` | From the accession prefix — the *transmitter*, not the issuer |
| `security_title` | What was actually traded (finding 2) |
| `transaction_code` | Trade vs grant vs tax withholding (finding 8) |
| `transaction_price_per_share` | Nullable; `NULL` ≠ `0` (finding 5) |
| `price_is_disclosed` | Distinguishes zero from undisclosed |
| `num_reporting_owners`, `all_reporting_owners` | Joint filings (finding 6) |
| `line_number` | Ordinal; required for uniqueness |
| `not_subject_to_section16` | Marks exit filings |

---

## What I'd add next

**Immediate — the database step:**

- SQLite schema with `(accession_number, table, line_number)` as the transaction
  key, a separate `reporting_owners` table for the many-to-many relationship
  (finding 6), and a `securities` table keyed on `(issuer_cik, security_title)`
  so tracking stocks resolve correctly (finding 2).
- `NUMERIC` columns for shares and prices, nullable prices, foreign keys on
  `issuer_cik` rather than ticker.
- Idempotent upserts keyed on accession number, so re-running never duplicates.

**Then:**

- **Form 4/A amendment handling.** Amendments correct earlier filings. The
  current 12-month window happens to contain none, so the dedup logic is
  written but unexercised — it needs a longer window to test against. Counting
  both an original and its amendment double-counts the transaction.
- **Footnote text extraction.** 81 transactions have a footnote in place of a
  price. The footnote often contains the actual figure in prose ("a weighted
  average price of $X"). Currently only footnote IDs are captured.
- **Incremental runs.** Re-fetch only filings newer than the last run rather
  than the whole window.
- **Automated tests against pinned fixtures.** The trap cases (multi-owner,
  holdings-only, undisclosed price, issuer mismatch) are each represented by a
  known accession number and would make good regression fixtures.
- **Backfill beyond 12 months**, which requires following the submissions API's
  overflow chunks — already implemented but only lightly exercised.
- **Enrichment with market prices** to compute whether insider purchases
  preceded returns — the actual analytical payoff, and the reason for insisting
  on `security_title` rather than ticker for the join.

---

## Notes

- All data is public and free. No API key or account is required.
- Nothing here constitutes investment advice.
- SEC EDGAR documentation: <https://www.sec.gov/edgar/sec-api-documentation>
