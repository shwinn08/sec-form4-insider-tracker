"""Write the enumerated filing list to disk as JSON and CSV.

Two formats:
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


def write_dicts_json(
    rows: list[dict], metadata: dict, path: Path, key: str = "records"
) -> Path:
    """Write already-flattened dict rows (from the parser) with run metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        f"{key}_count": len(rows),
        key: rows,
    }
    path.write_text(json.dumps(document, indent=2))
    log.info("Wrote %d %s to %s", len(rows), key, path)
    return path


def write_dicts_csv(rows: list[dict], path: Path) -> Path:
    """Write dict rows to CSV, taking column order from the first row.

    All rows come from one dataclass, so their keys are identical and ordered;
    an empty input still produces a valid (headerless) file rather than
    crashing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not rows:
            log.warning("No rows to write to %s", path)
            return path
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d rows to %s", len(rows), path)
    return path


def default_output_paths(stem: str = "form4_filings") -> tuple[Path, Path]:
    """Timestamped output paths, so a new run never silently overwrites the
    previous one, which helps when tuning the lookback window."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        config.RAW_DIR / f"{stem}_{stamp}.json",
        config.RAW_DIR / f"{stem}_{stamp}.csv",
    )
