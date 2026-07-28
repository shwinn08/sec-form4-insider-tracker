"""Write the enumerated filing list to disk as JSON and CSV.

Two formats on purpose:
  - JSON keeps the run metadata (when, what window, which tickers failed)
    alongside the data, which is what you want when you come back in a week
    and wonder what a file actually contains.
  - CSV is the format you can open in a spreadsheet and skim in ten seconds.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .filings import Filing, filing_to_dict

log = logging.getLogger(__name__)

# Column order for the CSV, taken from the dataclass definition so the two can
# never drift apart.
CSV_COLUMNS = [f.name for f in dataclass_fields(Filing)]


def write_json(filings: list[Filing], metadata: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "filing_count": len(filings),
        "filings": [filing_to_dict(f) for f in filings],
    }
    path.write_text(json.dumps(document, indent=2))
    log.info("Wrote %d filings to %s", len(filings), path)
    return path


def write_csv(filings: list[Filing], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" is required by the csv module on all platforms; without it you
    # get blank lines between rows on Windows.
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for filing in filings:
            writer.writerow(filing_to_dict(filing))
    log.info("Wrote %d filings to %s", len(filings), path)
    return path


def default_output_paths(stem: str = "form4_filings") -> tuple[Path, Path]:
    """Timestamped output paths, so a new run never silently overwrites the
    previous one — useful when you're tuning the lookback window."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        config.RAW_DIR / f"{stem}_{stamp}.json",
        config.RAW_DIR / f"{stem}_{stamp}.csv",
    )
