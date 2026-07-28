-- SQLite schema for SEC Form 4 insider transaction data.
--
-- Design principle: every constraint here exists because the parsing step found
-- a specific way the raw data misleads you. Comments name the finding, so the
-- reasoning survives without the README.
--
-- Two SQLite-specific behaviours drive the column types:
--
--   1. There is no DECIMAL type, and type affinity actively destroys precision.
--      A column declared NUMERIC or REAL converts '184.90' to the float 184.9
--      on insert. TEXT affinity stores the string verbatim, so decimals are
--      stored as TEXT and cast at query time (see the views at the bottom).
--
--   2. There is no BOOLEAN type, and SQLite is dynamically typed: a column with
--      INTEGER affinity will happily store the *string* 'false', because
--      'false' is not convertible to a number. The CHECK constraints below are
--      therefore the real enforcement, not the type declaration.

PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------------
-- companies: issuers, keyed by CIK.
--
-- Keyed on CIK rather than ticker because a ticker is not a stable identifier:
-- it is reassigned to a new legal entity after a reorganisation (XOM moved to a
-- new holdco while its filing history stayed with the predecessor). There is
-- deliberately no ticker column — one company can have several (tracking
-- stocks) and the mapping changes over time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    cik   INTEGER PRIMARY KEY,
    name  TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- filings: one row per Form 4 document.
--
-- FINDING: EDGAR indexes a Form 4 under BOTH the issuer and the reporting
-- owner, so a company's filing feed returns filings about other companies
-- (JPMorgan's feed carries BlackRock fund filings). Three CIKs are recorded and
-- only one of them — issuer_cik, taken from the XML <issuer> block — is
-- foreign-keyed to companies. searched_cik is provenance, never identity.
-- filer_agent_cik (from the accession prefix) is the transmitting agent and is
-- intentionally not a foreign key, because agents are not issuers.
--
-- issuer_matches_searched is GENERATED rather than stored by the loader: a
-- derived value that can drift from its inputs is a bug waiting to happen.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS filings (
    accession_number          TEXT PRIMARY KEY,
    issuer_cik                INTEGER NOT NULL REFERENCES companies(cik),
    searched_ticker           TEXT    NOT NULL,
    searched_cik              INTEGER NOT NULL,
    filer_agent_cik           INTEGER,
    filing_date               TEXT    NOT NULL,
    period_of_report          TEXT,
    document_type             TEXT    NOT NULL,
    schema_version            TEXT,
    not_subject_to_section16  INTEGER NOT NULL DEFAULT 0
                                CHECK (not_subject_to_section16 IN (0, 1)),
    raw_xml_url               TEXT,
    issuer_matches_searched   INTEGER GENERATED ALWAYS AS
                                (CASE WHEN issuer_cik = searched_cik THEN 1 ELSE 0 END)
                                STORED
);


-- ---------------------------------------------------------------------------
-- reporting_owners: the insiders themselves.
--
-- owner_cik is TEXT, not INTEGER: it is a zero-padded identifier ('0001818224')
-- and the padding is how it appears everywhere else in EDGAR. Storing it as an
-- integer strips the zeros and forces reconstruction on every join.
--
-- Roles are NOT stored here — see filing_owners.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reporting_owners (
    owner_cik   TEXT PRIMARY KEY,
    owner_name  TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- filing_owners: the many-to-many between filings and insiders.
--
-- FINDING: 12 filings name two reporting owners, and a joint filing reports
-- each transaction ONCE, COLLECTIVELY — not once per owner. Attaching an owner
-- column to `transactions` would make a naive join double the share counts.
-- Instead transactions carry no owner at all: they belong to a filing, and the
-- filing has N owners. Summing shares per insider therefore requires joining
-- through this table and consciously deciding what to do about joint filings.
-- The schema turns a silent double-count into a question you have to answer.
--
-- Roles live here rather than on reporting_owners because they are properties
-- of a filing: the same person can be a director at one company and a 10%
-- owner at another, and officer titles change over time.
--
-- FINDING: role booleans appear in the source as 1/0, true/false, and absent.
-- 'false' is a truthy string in Python, so naive coercion silently inverts the
-- flag. The CHECK (x IN (0,1)) constraints below reject anything that isn't a
-- real boolean, turning that failure loud at the database boundary.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS filing_owners (
    accession_number      TEXT    NOT NULL REFERENCES filings(accession_number)
                                    ON DELETE CASCADE,
    owner_cik             TEXT    NOT NULL REFERENCES reporting_owners(owner_cik),
    owner_order           INTEGER NOT NULL,
    is_director           INTEGER NOT NULL CHECK (is_director          IN (0, 1)),
    is_officer            INTEGER NOT NULL CHECK (is_officer           IN (0, 1)),
    is_ten_percent_owner  INTEGER NOT NULL CHECK (is_ten_percent_owner IN (0, 1)),
    is_other              INTEGER NOT NULL CHECK (is_other             IN (0, 1)),
    officer_title         TEXT,
    other_text            TEXT,
    PRIMARY KEY (accession_number, owner_cik)
);


-- ---------------------------------------------------------------------------
-- securities: what was actually traded.
--
-- FINDING: a ticker identifies a company, not an instrument. Liberty Media
-- alone issues 19 distinct securities across its tracking stocks, and the XML's
-- own issuerTradingSymbol field reads 'FWONK' on rows that are actually Liberty
-- Live stock — wrong 83% of the time. security_title from the transaction row
-- is the only reliable identifier.
--
-- There is deliberately NO ticker and NO issuer_trading_symbol column anywhere
-- in this schema. Removing the misleading field entirely is safer than storing
-- one that people will reach for by reflex.
--
-- Scoped by issuer because 'Common Stock' means something different for each
-- company. Underlying securities of derivatives populate this same table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS securities (
    security_id     INTEGER PRIMARY KEY,
    issuer_cik      INTEGER NOT NULL REFERENCES companies(cik),
    security_title  TEXT    NOT NULL,
    UNIQUE (issuer_cik, security_title)
);


-- ---------------------------------------------------------------------------
-- transaction_codes: lookup that encodes what each Form 4 code means.
--
-- FINDING: only 847 of 1,998 rows are open-market trades. The rest are grants
-- (A), option exercises (M), tax withholding (F), gifts (G) and other (J).
-- "Insiders acquired 418,000 shares" from code A describes equity compensation
-- being handed out, not anyone choosing to buy.
--
-- Making this a table rather than a CHECK constraint means an analyst who has
-- never read a Form 4 can write `WHERE is_open_market = 1` without first
-- learning the conventions — and an unrecognised code fails the insert instead
-- of quietly landing in an aggregate.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_codes (
    code            TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    is_open_market  INTEGER NOT NULL CHECK (is_open_market IN (0, 1))
);


-- ---------------------------------------------------------------------------
-- transactions: one row per reported transaction line.
--
-- FINDING: 164 filings contain repeated (security_title, transaction_date,
-- transaction_code) triples — genuinely separate tranches or price points, not
-- duplicates. line_number is the only thing that distinguishes them, which is
-- why it is part of the primary key. Without it a dedup would silently discard
-- real transactions.
--
-- FINDING: price_per_share is nullable and the two states are distinct:
--     NULL  -> the filer disclosed a footnote instead of a number (81 rows)
--     '0'   -> a real price of zero; no cash changed hands (664 rows)
-- Defaulting NULL to 0 invents 81 free share transfers. NULL also propagates
-- correctly through AVG(), which skips undisclosed rows rather than dragging
-- the average toward zero. The CHECK guards against an empty string sneaking in
-- as an ambiguous third state.
--
-- FINDING: 269 share counts are genuinely fractional (401k / dividend
-- reinvestment plans). These columns are TEXT to preserve the exact decimal as
-- filed; an INTEGER column truncates and a REAL column rounds.
--
-- Note there is no owner column here — see filing_owners.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    accession_number             TEXT    NOT NULL REFERENCES filings(accession_number)
                                           ON DELETE CASCADE,
    line_number                  INTEGER NOT NULL,
    table_type                   TEXT    NOT NULL
                                   CHECK (table_type IN ('non-derivative', 'derivative')),
    security_id                  INTEGER NOT NULL REFERENCES securities(security_id),

    transaction_date             TEXT    NOT NULL,
    deemed_execution_date        TEXT,
    transaction_code             TEXT    NOT NULL REFERENCES transaction_codes(code),
    equity_swap_involved         INTEGER NOT NULL DEFAULT 0
                                   CHECK (equity_swap_involved IN (0, 1)),

    shares                       TEXT,
    price_per_share              TEXT
                                   CHECK (price_per_share IS NULL OR price_per_share <> ''),
    acquired_disposed_code       TEXT    CHECK (acquired_disposed_code IN ('A', 'D')),
    shares_owned_following       TEXT,
    direct_or_indirect           TEXT    CHECK (direct_or_indirect IN ('D', 'I')),
    nature_of_ownership          TEXT,

    -- derivative-only columns; NULL on non-derivative rows
    conversion_or_exercise_price TEXT,
    exercise_date                TEXT,
    expiration_date              TEXT,
    underlying_security_id       INTEGER REFERENCES securities(security_id),
    underlying_security_shares   TEXT,

    footnote_ids                 TEXT,

    PRIMARY KEY (accession_number, line_number)
);


-- ---------------------------------------------------------------------------
-- holdings: positions reported without a transaction.
--
-- FINDING: <nonDerivativeHolding> elements sit beside transactions in the same
-- parent, but have NO date, NO transaction code, NO share count and NO price.
-- They state a position the insider holds but did not trade. 807 of them exist
-- in this corpus.
--
-- The important thing about this table is what it LACKS. There is no
-- transaction_date, transaction_code, shares or price_per_share column, because
-- those facts do not exist in the source. Forcing holdings into `transactions`
-- would create 807 rows with four permanently-NULL columns that every future
-- query has to remember to exclude — and eventually one won't.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS holdings (
    accession_number        TEXT    NOT NULL REFERENCES filings(accession_number)
                                      ON DELETE CASCADE,
    holding_number          INTEGER NOT NULL,
    table_type              TEXT    NOT NULL
                              CHECK (table_type IN ('non-derivative', 'derivative')),
    security_id             INTEGER NOT NULL REFERENCES securities(security_id),
    shares_owned_following  TEXT,
    direct_or_indirect      TEXT    CHECK (direct_or_indirect IN ('D', 'I')),
    nature_of_ownership     TEXT,
    underlying_security_id  INTEGER REFERENCES securities(security_id),
    footnote_ids            TEXT,
    PRIMARY KEY (accession_number, holding_number)
);


-- ---------------------------------------------------------------------------
-- Indexes for the joins the analysis views rely on.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_filings_issuer      ON filings(issuer_cik);
CREATE INDEX IF NOT EXISTS idx_filings_date        ON filings(filing_date);
CREATE INDEX IF NOT EXISTS idx_txn_date            ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_txn_security        ON transactions(security_id);
CREATE INDEX IF NOT EXISTS idx_txn_code            ON transactions(transaction_code);
CREATE INDEX IF NOT EXISTS idx_filing_owners_owner ON filing_owners(owner_cik);
CREATE INDEX IF NOT EXISTS idx_securities_issuer   ON securities(issuer_cik);
CREATE INDEX IF NOT EXISTS idx_holdings_security   ON holdings(security_id);


-- ---------------------------------------------------------------------------
-- v_transactions: the analysis view.
--
-- Base tables stay exact (TEXT decimals); casting to REAL happens here, once,
-- so no one writes CAST by hand and no one accidentally sums strings.
--
-- signed_shares applies the acquired/disposed direction so that summing gives
-- net share flow. transaction_value is NULL whenever the price was not
-- disclosed, which is correct: an undisclosed price means unknown value, not
-- zero value.
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_transactions AS
SELECT
    t.accession_number,
    t.line_number,
    t.table_type,
    f.filing_date,
    t.transaction_date,
    f.searched_ticker,
    f.issuer_matches_searched,
    c.cik                                   AS issuer_cik,
    c.name                                  AS issuer_name,
    s.security_title,
    t.transaction_code,
    tc.label                                AS transaction_label,
    tc.is_open_market,
    t.acquired_disposed_code,
    CAST(t.shares AS REAL)                  AS shares_num,
    CAST(t.price_per_share AS REAL)         AS price_num,
    (t.price_per_share IS NOT NULL)         AS price_is_disclosed,
    CASE t.acquired_disposed_code
        WHEN 'A' THEN  CAST(t.shares AS REAL)
        WHEN 'D' THEN -CAST(t.shares AS REAL)
    END                                     AS signed_shares,
    CASE WHEN t.price_per_share IS NULL THEN NULL
         ELSE CAST(t.shares AS REAL) * CAST(t.price_per_share AS REAL)
    END                                     AS transaction_value,
    t.direct_or_indirect,
    t.nature_of_ownership,
    us.security_title                       AS underlying_security_title,
    t.footnote_ids
FROM transactions t
JOIN filings           f  ON f.accession_number = t.accession_number
JOIN companies         c  ON c.cik             = f.issuer_cik
JOIN securities        s  ON s.security_id     = t.security_id
JOIN transaction_codes tc ON tc.code           = t.transaction_code
LEFT JOIN securities   us ON us.security_id    = t.underlying_security_id;


-- ---------------------------------------------------------------------------
-- v_current_positions: the correct read of shares_owned_following.
--
-- FINDING: shares_owned_following is a running balance after a specific
-- transaction line, for a specific ownership form — not a position. One filing
-- showed 230,278 (direct), 224,619 (direct) and 135,027 (indirect) for the same
-- security: the first two are successive states of one pool, the third is a
-- different pool entirely. Summing them produces a number corresponding to
-- nothing.
--
-- The correct read is the LATEST row per (owner, security, ownership form).
-- This view does that with a window function, drawing on both transactions and
-- holdings, since a position can be last stated by either.
--
-- Rows are attributed per owner via filing_owners, so a joint filing yields a
-- position row for each named owner. That is an attribution choice, not a fact
-- in the source — treat multi-owner positions accordingly.
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_current_positions AS
WITH stated AS (
    -- position statements attached to a transaction
    SELECT fo.owner_cik, t.security_id, t.direct_or_indirect, t.nature_of_ownership,
           f.filing_date, t.line_number AS seq, t.shares_owned_following, 'transaction' AS source
    FROM transactions t
    JOIN filings       f  ON f.accession_number = t.accession_number
    JOIN filing_owners fo ON fo.accession_number = t.accession_number
    WHERE t.shares_owned_following IS NOT NULL

    UNION ALL

    -- position statements with no transaction behind them
    SELECT fo.owner_cik, h.security_id, h.direct_or_indirect, h.nature_of_ownership,
           f.filing_date, h.holding_number AS seq, h.shares_owned_following, 'holding' AS source
    FROM holdings h
    JOIN filings       f  ON f.accession_number = h.accession_number
    JOIN filing_owners fo ON fo.accession_number = h.accession_number
    WHERE h.shares_owned_following IS NOT NULL
),
ranked AS (
    SELECT stated.*,
           ROW_NUMBER() OVER (
               PARTITION BY owner_cik, security_id, direct_or_indirect
               ORDER BY filing_date DESC, seq DESC
           ) AS rn
    FROM stated
)
SELECT
    r.owner_cik,
    o.owner_name,
    c.cik                          AS issuer_cik,
    c.name                         AS issuer_name,
    s.security_title,
    r.direct_or_indirect,
    r.nature_of_ownership,
    CAST(r.shares_owned_following AS REAL) AS shares_held,
    r.filing_date                  AS as_of_filing_date,
    r.source
FROM ranked r
JOIN reporting_owners o ON o.owner_cik  = r.owner_cik
JOIN securities       s ON s.security_id = r.security_id
JOIN companies        c ON c.cik         = s.issuer_cik
WHERE r.rn = 1;
