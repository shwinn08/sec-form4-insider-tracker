"""Download raw Form 4 XML documents, with an on-disk cache.

Kept separate from parsing. Fetching is slow, rate-limited, and can
fail; parsing is fast, deterministic, and is what you'll iterate on. Keeping
the raw XML on disk means you can rewrite the parser twenty times without
re-hitting the SEC even once, which is both faster for you and the polite
thing to do.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import config
from .client import SECClient

log = logging.getLogger(__name__)

XML_CACHE_DIR = config.CACHE_DIR / "filings"


def cache_path_for(accession_number: str) -> Path:
    """One file per filing, named by accession number.

    Accession numbers are globally unique across EDGAR and contain no path
    separators, so they make safe filenames with no sanitising required.
    """
    return XML_CACHE_DIR / f"{accession_number}.xml"


def fetch_filing_xml(
    accession_number: str,
    url: str,
    client: SECClient,
    force_refresh: bool = False,
) -> str | None:
    """Return the raw XML for one filing, from cache when possible.

    Returns None if the document couldn't be fetched, rather than raising: a
    single dead URL shouldn't abort a run over hundreds of filings.
    """
    path = cache_path_for(accession_number)

    if path.exists() and not force_refresh:
        return path.read_text(encoding="utf-8")

    try:
        text = client.get(url).text
    except Exception as exc:
        log.error("Failed to fetch %s (%s): %s", accession_number, url, exc)
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def fetch_all(
    filings: list[dict], client: SECClient, force_refresh: bool = False
) -> dict[str, str]:
    """Fetch XML for many filings. Returns {accession_number: xml_text}.

    `filings` are the dicts from the enumeration output, so this step consumes
    step 1's artifact directly rather than re-querying EDGAR for the list.
    """
    XML_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    documents: dict[str, str] = {}
    cached = downloaded = failed = 0

    for i, filing in enumerate(filings, start=1):
        accession = filing["accession_number"]
        url = filing["raw_xml_url"]
        if not url:
            log.warning("%s has no raw_xml_url; skipping", accession)
            failed += 1
            continue

        was_cached = cache_path_for(accession).exists() and not force_refresh
        text = fetch_filing_xml(accession, url, client, force_refresh=force_refresh)

        if text is None:
            failed += 1
            continue

        documents[accession] = text
        if was_cached:
            cached += 1
        else:
            downloaded += 1
            # Only log progress for real network activity, since a fully cached run
            # would otherwise spam hundreds of lines.
            if downloaded % 50 == 0:
                log.info("Downloaded %d filings (%d/%d processed)", downloaded, i, len(filings))

    log.info(
        "Fetch complete: %d from cache, %d downloaded, %d failed",
        cached, downloaded, failed,
    )
    return documents
