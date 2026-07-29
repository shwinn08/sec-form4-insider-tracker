"""Central place for constants and settings.

Keeping URLs, paths, and tuning knobs here (instead of scattered through the
modules that use them) means there is exactly one place to look when something
needs to change.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve the project root from this file's location, so scripts work no matter
# which directory you run them from.
#   config.py -> sec_form4/ -> src/ -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"      # downloaded artifacts we don't want to re-fetch
RAW_DIR = DATA_DIR / "raw"          # scraper output, pre-cleaning
PROCESSED_DIR = DATA_DIR / "processed"  # parsed records and the analysis report
CONFIG_DIR = PROJECT_ROOT / "config"

TICKERS_FILE = CONFIG_DIR / "tickers.txt"
CIK_OVERRIDES_FILE = CONFIG_DIR / "cik_overrides.json"

# Read .env into the environment. override=False means a real environment
# variable (e.g. exported in your shell or set in CI) wins over the file.
load_dotenv(PROJECT_ROOT / ".env", override=False)


# --- SEC endpoints ----------------------------------------------------------
# Note the two different hosts. This is not a typo: the static ticker file lives
# on www.sec.gov, but the JSON submissions API lives on data.sec.gov.

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik_padded}.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"


# --- Politeness -------------------------------------------------------------
# The SEC's published ceiling is 10 requests/second. We run at half that: the
# whole job is a handful of requests, so the extra seconds cost nothing and a
# block costs a lot.
REQUESTS_PER_SECOND = 5.0

# Retry policy for transient failures (429 rate-limited, 5xx server errors).
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0  # wait 2s, 4s, 8s, ...
REQUEST_TIMEOUT_SECONDS = 30

# How far back to look for filings, unless overridden on the command line.
DEFAULT_LOOKBACK_MONTHS = 12

# Form types we care about. "4/A" is an amended Form 4. Insiders file these to
# correct earlier reports, so they matter, but you want them labelled.
FORM_4_TYPES = ("4", "4/A")


def get_user_agent() -> str:
    """Return the SEC User-Agent string, or fail loudly if it isn't configured.

    We refuse to run without this rather than silently sending a default
    'python-requests/2.x' header, because the SEC answers those with a 403 HTML
    block page. A 403 with a body is worse than a crash: naive code parses the
    block page as if it were data.
    """
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT is not set.\n"
            "Copy .env.example to .env and put your real name and email in it, e.g.\n"
            '  SEC_USER_AGENT="Jane Doe Form4Scraper jane@example.com"\n'
            "The SEC requires automated requests to identify a contact person."
        )
    if "@" not in ua:
        raise RuntimeError(
            f"SEC_USER_AGENT={ua!r} has no email address in it. "
            "The SEC expects a contact address so they can reach you."
        )
    return ua
