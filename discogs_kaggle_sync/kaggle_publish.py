"""Builds a Kaggle dataset-metadata.json (full "About Dataset" description, per-file
descriptions, provenance, license, per-column schema) and publishes a new dataset for
the month via the Kaggle CLI.

Each month gets its own dataset (matching the existing manually-published pattern:
"Discogs Data Dumps (April 2025)", "(June 2025)", etc.) rather than versioning one
ever-growing dataset. All copy below is the exact text previously written by hand for
these datasets, with the month/year and file format made dynamic.

Note: `description`, `licenses`, `keywords`, and per-resource `description`/`schema` are
documented, API-settable fields. Kaggle's web UI also has a "cover image" upload and an
"Expected Update Frequency" setting for a dataset; neither is confirmed to be settable
through the public create/version API, so those may still need a one-time manual check
after the first automated publish. The generated cover image is included as a dataset
file regardless (see cover_art.py), so it's available even if it can't be wired up as
the card thumbnail automatically.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import subprocess
from calendar import month_name
from pathlib import Path

logger = logging.getLogger(__name__)

MODULE_DIR = Path(__file__).parent
DESCRIPTIONS_DIR = MODULE_DIR / "column_descriptions"

FILE_DESCRIPTIONS = {
    "artists": (
        "• Contains metadata on artists, including unique Discogs IDs, names, aliases, "
        "profile descriptions, and associated URLs.\n"
        "• Useful for studying artist collaborations, discographies, and musical influences."
    ),
    "labels": (
        "• Includes record label information such as label names, parent labels, "
        "catalog numbers, and URLs.\n"
        "• Ideal for understanding label affiliations and discographies."
    ),
    "masters": (
        "• Represents master releases that group together different versions of a "
        "release (e.g., different formats, editions, and reissues).\n"
        "• Helps in tracking variations of a single album across different releases."
    ),
    "releases": (
        "• The largest and most detailed file, containing metadata on individual "
        "releases, including tracklists, formats, barcode information, and release dates.\n"
        "• Essential for in-depth music cataloging, marketplace analysis, and historical "
        "music research."
    ),
}


def _load_descriptions(content_type: str) -> dict[str, str]:
    path = DESCRIPTIONS_DIR / f"{content_type}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_header(csv_path: Path) -> list[str]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f))


def _about_dataset(month_label: str) -> str:
    return (
        f"The Discogs Data Dumps ({month_label}) provide a comprehensive archive of the "
        "Discogs music database, offering detailed metadata on releases, artists, labels, "
        "and masters. This dataset includes structured information on millions of vinyl "
        "records, CDs, digital releases, and more, making it an invaluable resource for "
        "music researchers, collectors, and developers.\n\n"
        "The archive is available in CSV format and contains various data files, including "
        "release details, artist discographies, label catalogs, and user-generated "
        "contributions. It is regularly updated and serves as a foundation for building "
        "applications, analyzing music trends, and exploring Discogs' extensive music "
        "catalog.\n\n"
        "## Provenance\n\n"
        f"**Sources**: The Discogs Data Dumps ({month_label}) were sourced directly from "
        "the official Discogs Data Dumps web page. The original dataset was provided in "
        "XML.GZ format, which was then processed and converted into CSV format "
        "automatically.\n\n"
        "**Collection Methodology**: Since the data is sourced directly from Discogs' open "
        "database, it reflects real-world contributions from users worldwide, ensuring "
        "accuracy and depth across different music genres and formats."
    )


def build_dataset_metadata(
    staging_dir: Path,
    owner_slug: str,
    month: str,  # "YYYY-MM"
    csv_files: dict[str, Path],  # content_type -> csv path, all inside staging_dir
) -> Path:
    """Writes dataset-metadata.json into staging_dir and returns its path."""
    year, month_num = month.split("-")
    month_label = f"{month_name[int(month_num)]} {year}"
    dataset_slug = f"discogs-data-dumps-{month_name[int(month_num)].lower()}-{year}"

    resources = []
    for content_type, csv_path in csv_files.items():
        descriptions = _load_descriptions(content_type)
        header = _csv_header(csv_path)

        missing = [col for col in header if col not in descriptions]
        if missing:
            logger.warning(
                "%s: %d column(s) have no description on file and will get a generic one: %s",
                content_type, len(missing), ", ".join(missing[:10]) + ("..." if len(missing) > 10 else ""),
            )

        fields = [
            {
                "name": col,
                "description": descriptions.get(col, f"Auto-generated field: {col}"),
                "type": "string",
            }
            for col in header
        ]

        resources.append({
            "path": csv_path.name,
            "description": FILE_DESCRIPTIONS.get(content_type, ""),
            "schema": {"fields": fields},
        })

    # No resources entry for the cover image: a file named exactly
    # "dataset-cover-image.<ext>" is auto-detected by the kaggle CLI and uploaded through
    # a separate cover-image code path, not as a regular data resource (see
    # DATASET_COVER_IMAGE_FILES in kaggle_api_extended.py) — listing it here too would
    # describe a "resource" that was never actually uploaded as one.

    metadata = {
        "title": f"Discogs Data Dumps ({month_label})",
        "id": f"{owner_slug}/{dataset_slug}",
        "licenses": [{"name": "CC0-1.0"}],
        "keywords": ["music"],
        "description": _about_dataset(month_label),
        "resources": resources,
    }

    metadata_path = staging_dir / "dataset-metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata_path


def publish_dataset(staging_dir: Path) -> None:
    """Runs `kaggle datasets create` for the metadata/CSVs staged in `staging_dir`."""
    # Resolve the CLI next to the current interpreter (sys.executable) rather than trusting
    # PATH: when this script is invoked as `/path/to/venv/bin/python run_monthly_sync.py`
    # without activating the venv first, a bare "kaggle" isn't on PATH even though it's
    # installed right there in the venv's bin/ alongside python.
    kaggle_bin = Path(sys.executable).parent / "kaggle"
    kaggle_cmd = str(kaggle_bin) if kaggle_bin.exists() else "kaggle"

    result = subprocess.run(
        # -u/--public: the CLI defaults to creating datasets *private*, which doesn't match
        # every prior manually-published month (publicly visible, with view/download counts).
        [kaggle_cmd, "datasets", "create", "-p", str(staging_dir), "-r", "skip", "-u"],
        capture_output=True, text=True,
    )
    logger.info("kaggle datasets create stdout: %s", result.stdout.strip())
    if result.returncode != 0:
        logger.error("kaggle datasets create stderr: %s", result.stderr.strip())
        raise RuntimeError(f"Kaggle publish failed (exit {result.returncode})")
