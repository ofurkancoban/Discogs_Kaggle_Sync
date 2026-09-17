"""Builds a Kaggle dataset-metadata.json (full "About Dataset" description, per-file
descriptions, provenance, license, per-column schema) and publishes a new dataset for
the month via the Kaggle CLI.

Each month gets its own dataset (matching the existing manually-published pattern:
"Discogs Data Dumps (April 2025)", "(June 2025)", etc.) rather than versioning one
ever-growing dataset. All copy below is the exact text previously written by hand for
these datasets, with the month/year and file format made dynamic.

Two separate CLI calls are needed to fully populate a dataset's "Pending Actions" list:

1. `kaggle datasets create` - reads title/id/licenses/subtitle/description/keywords/
   resources(schema) from dataset-metadata.json, plus auto-detects a sibling
   "dataset-cover-image.<ext>" file (see cover_art.py). Confirmed by reading
   `dataset_create_new()` in kaggle_api_extended.py.
2. `kaggle datasets metadata <ref> --update -p <folder>` - reads the *same* metadata
   file's `userSpecifiedSources` (-> the "Provenance / Sources" section) and
   `expectedUpdateFrequency`, which `create` does not apply. Confirmed by reading
   `dataset_metadata_update()` in the same file.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import subprocess
import time
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
        "catalog."
    )


def _subtitle(month_label: str) -> str:
    # Must be 20-80 characters (enforced by the API) - keep this in sync if the wording
    # changes, since a subtitle outside that range makes `datasets create` raise.
    text = f"Discogs' full {month_label} music catalog: artists, labels, masters, releases"
    assert 20 <= len(text) <= 80, f"subtitle length {len(text)} out of Kaggle's allowed 20-80 range"
    return text


def _provenance_sources(month_label: str) -> str:
    return (
        f"Sources: The Discogs Data Dumps ({month_label}) were sourced directly from the "
        "official Discogs Data Dumps web page. The original dataset was provided in XML.GZ "
        "format, which was then processed and converted into CSV format automatically.\n\n"
        "Collection Methodology: Since the data is sourced directly from Discogs' open "
        "database, it reflects real-world contributions from users worldwide, ensuring "
        "accuracy and depth across different music genres and formats."
    )


def dataset_slug_for(month: str) -> str:
    """"YYYY-MM" -> "discogs-data-dumps-<month-name>-<year>", matching the naming pattern
    of the prior manually-published datasets."""
    year, month_num = month.split("-")
    return f"discogs-data-dumps-{month_name[int(month_num)].lower()}-{year}"


def build_dataset_metadata(
    staging_dir: Path,
    owner_slug: str,
    month: str,  # "YYYY-MM"
    csv_files: dict[str, Path],  # content_type -> csv path, all inside staging_dir
) -> Path:
    """Writes dataset-metadata.json into staging_dir and returns its path."""
    year, month_num = month.split("-")
    month_label = f"{month_name[int(month_num)]} {year}"
    dataset_slug = dataset_slug_for(month)

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
    # DATASET_COVER_IMAGE_FILES in kaggle_api_extended.py) - listing it here too would
    # describe a "resource" that was never actually uploaded as one.

    metadata = {
        "title": f"Discogs Data Dumps ({month_label})",
        "id": f"{owner_slug}/{dataset_slug}",
        "subtitle": _subtitle(month_label),
        "licenses": [{"name": "CC0-1.0"}],
        "keywords": ["music", "software", "beginner", "tabular"],
        "description": _about_dataset(month_label),
        "isPrivate": False,
        "userSpecifiedSources": _provenance_sources(month_label),
        "expectedUpdateFrequency": "monthly",
        "resources": resources,
    }

    metadata_path = staging_dir / "dataset-metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata_path


def _kaggle_cmd() -> str:
    # Resolve the CLI next to the current interpreter (sys.executable) rather than trusting
    # PATH: when this script is invoked as `/path/to/venv/bin/python run_monthly_sync.py`
    # without activating the venv first, a bare "kaggle" isn't on PATH even though it's
    # installed right there in the venv's bin/ alongside python.
    kaggle_bin = Path(sys.executable).parent / "kaggle"
    return str(kaggle_bin) if kaggle_bin.exists() else "kaggle"


def delete_dataset(owner_slug: str, dataset_slug: str) -> None:
    """Permanently deletes an existing dataset. Only call this when the caller has
    explicit intent to replace it (e.g. a --replace-existing flag) - this cannot be undone
    and drops the dataset's view/download/vote history."""
    ref = f"{owner_slug}/{dataset_slug}"
    result = subprocess.run(
        [_kaggle_cmd(), "datasets", "delete", "-y", ref],
        capture_output=True, text=True,
    )
    logger.info("kaggle datasets delete stdout: %s", result.stdout.strip())
    if result.returncode != 0:
        logger.error("kaggle datasets delete stderr: %s", result.stderr.strip())
        raise RuntimeError(f"Kaggle delete failed for {ref} (exit {result.returncode})")


def publish_dataset(staging_dir: Path) -> None:
    """Runs `kaggle datasets create` for the metadata/CSVs staged in `staging_dir`."""
    result = subprocess.run(
        # -u/--public: the CLI defaults to creating datasets *private*, which doesn't match
        # every prior manually-published month (publicly visible, with view/download counts).
        [_kaggle_cmd(), "datasets", "create", "-p", str(staging_dir), "-r", "skip", "-u"],
        capture_output=True, text=True,
    )
    logger.info("kaggle datasets create stdout: %s", result.stdout.strip())
    if result.returncode != 0:
        logger.error("kaggle datasets create stderr: %s", result.stderr.strip())
        raise RuntimeError(f"Kaggle publish failed (exit {result.returncode})")


METADATA_UPDATE_MAX_RETRIES = 8
METADATA_UPDATE_RETRY_DELAY_SECONDS = 60

def _patched_upload_dataset_image_file(self, metadata_file_path, relative_image_file_path, quiet=False):
    """Replaces KaggleApi._upload_dataset_image_file, which hardcodes the header/thumbnail
    crop rectangles to a fixed top-left 560x280 / 280x280 region of whatever image is
    uploaded - completely independent of that image's actual size or content. Our cover
    image is a 1792x902 wide banner with its year/month text centered around y=480-810, so
    that hardcoded crop always landed on the plain background photo instead. This computes
    crops from the real image dimensions instead: the header as a near-full-image 2:1 slice
    (matches our banner's own aspect ratio almost exactly) and the thumbnail as a centered
    square, both of which include the actual branding/text.
    """
    import mimetypes
    import os
    from kaggle.api.kaggle_api_extended import ResumableUploadContext
    from kagglesdk.blobs.types.blob_api_service import ApiBlobType
    from kagglesdk.common.types.cropped_image_upload import CroppedImageRectangle, CroppedImageUpload
    from PIL import Image

    image_full_path = os.path.join(metadata_file_path, relative_image_file_path)
    with Image.open(image_full_path) as im:
        img_w, img_h = im.size

    header_h = min(img_h, img_w // 2)
    header_w = header_h * 2

    thumb_size = min(img_w, img_h)
    thumb_left = (img_w - thumb_size) // 2
    thumb_top = (img_h - thumb_size) // 2

    file_name = os.path.basename(image_full_path)
    content_type, _ = mimetypes.guess_type(file_name)
    with ResumableUploadContext() as upload_context:
        upload_file = self._upload_file(
            file_name, image_full_path, ApiBlobType.INBOX, upload_context, quiet,
            resources=None, content_type=content_type,
        )
        if not upload_file:
            raise ValueError("Error uploading image file: %s" % image_full_path)

        header_image_rect = CroppedImageRectangle()
        header_image_rect.title = "cover image"
        header_image_rect.top = 0
        header_image_rect.left = 0
        header_image_rect.width = header_w
        header_image_rect.height = header_h

        thumbnail_rect = CroppedImageRectangle()
        thumbnail_rect.title = "thumbnail"
        thumbnail_rect.top = thumb_top
        thumbnail_rect.left = thumb_left
        thumbnail_rect.width = thumb_size
        thumbnail_rect.height = thumb_size

        cropped_image_upload = CroppedImageUpload()
        cropped_image_upload.token = upload_file.token
        cropped_image_upload.crop_rectangles = [header_image_rect, thumbnail_rect]
        return cropped_image_upload


def update_dataset_settings(owner_slug: str, dataset_slug: str, staging_dir: Path) -> None:
    """Pushes the fields `datasets create` doesn't apply - userSpecifiedSources
    (Provenance/Sources), expectedUpdateFrequency, and a correctly-cropped cover
    image/thumbnail - by re-reading the same dataset-metadata.json against the
    now-existing dataset. Must run after publish_dataset(); the ref has to already
    exist for this call to succeed.

    Uses the kaggle Python API directly (not the `kaggle` CLI subprocess) so the
    cover image crop rectangles can be patched to match our actual image layout
    instead of the CLI's hardcoded top-left crop (see _patched_upload_dataset_image_file).

    Kaggle finishes creating a large dataset (the releases CSV alone can be 30GB)
    asynchronously after `datasets create` returns, and this call gets a transient
    403 Forbidden if it runs before that processing completes - so retry with a
    delay instead of treating the first failure as fatal.
    """
    import types
    from kaggle.api.kaggle_api_extended import KaggleApi

    ref = f"{owner_slug}/{dataset_slug}"
    api = KaggleApi()
    api.authenticate()
    api._upload_dataset_image_file = types.MethodType(_patched_upload_dataset_image_file, api)

    last_error: BaseException | None = None
    for attempt in range(1, METADATA_UPDATE_MAX_RETRIES + 1):
        try:
            api.dataset_metadata_update(ref, str(staging_dir))
            return
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001 - dataset_metadata_update calls exit(1) on
            # API-level errors (raising SystemExit, not Exception), so this must be broad to
            # actually retry instead of crashing the whole sync process on the first failure.
            last_error = e
            logger.warning(
                "dataset_metadata_update failed (attempt %d/%d): %s",
                attempt, METADATA_UPDATE_MAX_RETRIES, e,
            )
            if attempt < METADATA_UPDATE_MAX_RETRIES:
                time.sleep(METADATA_UPDATE_RETRY_DELAY_SECONDS)
    raise RuntimeError(f"Kaggle metadata update failed for {ref} after {METADATA_UPDATE_MAX_RETRIES} attempts") from last_error
