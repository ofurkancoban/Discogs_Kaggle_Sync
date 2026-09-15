"""Builds a Kaggle dataset-metadata.json (with per-column descriptions) and publishes
a new dataset for the month via the Kaggle CLI.

Each month gets its own dataset (matching the existing manually-published pattern:
"Discogs Data Dumps (April 2025)", "(June 2025)", etc.) rather than versioning one
ever-growing dataset.
"""
from __future__ import annotations

import csv
import json
import logging
import subprocess
from calendar import month_name
from pathlib import Path

logger = logging.getLogger(__name__)

MODULE_DIR = Path(__file__).parent
DESCRIPTIONS_DIR = MODULE_DIR / "column_descriptions"

FILE_DESCRIPTIONS = {
    "artists": (
        "Contains metadata on artists, including unique Discogs IDs, names, aliases, "
        "profile descriptions, and associated URLs. Useful for studying artist "
        "collaborations, discographies, and musical influences."
    ),
    "labels": (
        "Contains metadata on record labels, including unique Discogs IDs, names, "
        "parent/sublabel relationships, and associated URLs."
    ),
    "masters": (
        "Contains metadata on master releases (the abstract work behind one or more "
        "physical/digital pressings), including title, year, genres, styles, and credited artists."
    ),
    "releases": (
        "Contains detailed metadata on individual releases/pressings, including title, "
        "country, format, label, catalog number, tracklist, credited and extra artists, "
        "and identifiers such as barcodes and matrix numbers."
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

    metadata = {
        "title": f"Discogs Data Dumps ({month_label})",
        "id": f"{owner_slug}/{dataset_slug}",
        "licenses": [{"name": "CC0-1.0"}],
        "description": (
            f"The Discogs Data Dumps ({month_label}) provide a comprehensive archive of the "
            "Discogs music database, offering detailed metadata on releases, artists, labels, "
            "and masters. This dataset includes structured information on millions of vinyl "
            "records, CDs, digital releases, and more, making it an invaluable resource for "
            "music researchers, collectors, and developers.\n\n"
            "Sourced directly from the official Discogs Data Dumps and converted from XML.GZ "
            "to CSV. Published automatically on a monthly cadence."
        ),
        "resources": resources,
    }

    metadata_path = staging_dir / "dataset-metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata_path


def publish_dataset(staging_dir: Path) -> None:
    """Runs `kaggle datasets create` for the metadata/CSVs staged in `staging_dir`."""
    result = subprocess.run(
        ["kaggle", "datasets", "create", "-p", str(staging_dir), "-r", "skip"],
        capture_output=True, text=True,
    )
    logger.info("kaggle datasets create stdout: %s", result.stdout.strip())
    if result.returncode != 0:
        logger.error("kaggle datasets create stderr: %s", result.stderr.strip())
        raise RuntimeError(f"Kaggle publish failed (exit {result.returncode})")
