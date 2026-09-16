#!/usr/bin/env python3
"""Entry point for the monthly Discogs -> Kaggle sync.

Checks for a new dump month, downloads its 4 files, converts each to CSV, publishes a
new Kaggle dataset for the month, then cleans up local disk. Safe to run repeatedly
(via cron) — already-published months are skipped using state/published_months.json.

Usage:
    python run_monthly_sync.py [--work-dir /path/to/scratch] [--kaggle-owner USERNAME]
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from discogs_kaggle_sync import converter, cover_art, downloader, kaggle_publish, scraper, state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("run_monthly_sync")

CONTENT_TYPES = ("artists", "labels", "masters", "releases")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path("./work"), help="Scratch directory for downloads/conversion.")
    parser.add_argument("--kaggle-owner", type=str, required=True, help="Kaggle username that owns the published datasets.")
    parser.add_argument("--force", action="store_true", help="Re-process even if this month was already published.")
    parser.add_argument(
        "--keep-staging", action="store_true",
        help="Don't delete the staged CSVs/cover image after a successful publish. Use while "
             "iterating on metadata/cover art/dataset settings: combine with --force on later "
             "runs to re-publish in seconds instead of redoing hours of conversion.",
    )
    parser.add_argument(
        "--replace-existing", action="store_true",
        help="Delete the month's existing Kaggle dataset (if any) before publishing. Only "
             "use this deliberately while iterating on one month — it permanently drops that "
             "dataset's view/download/vote history. Never combine with an unattended cron run.",
    )
    args = parser.parse_args()

    logger.info("Checking for the latest Discogs dump month...")
    month, files = scraper.latest_month_files()
    logger.info("Latest month with data: %s (%d file(s))", month, len(files))

    published = state.load_published_months()
    if month in published and not args.force:
        logger.info("%s already published to Kaggle. Nothing to do.", month)
        return 0

    by_type = {f.content_type: f for f in files if f.content_type in CONTENT_TYPES}
    missing = [t for t in CONTENT_TYPES if t not in by_type]
    if missing:
        logger.error("Missing expected file type(s) for %s: %s", month, missing)
        return 1

    work_dir = args.work_dir / month
    staging_dir = work_dir / "staging"
    work_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    csv_files: dict[str, Path] = {}
    try:
        for content_type, dump in by_type.items():
            csv_name = Path(dump.filename).with_suffix("").with_suffix(".csv").name
            csv_path = staging_dir / csv_name

            if csv_path.exists():
                # Resuming after a failure past this point (e.g. the publish step) — the
                # CSV conversion is the expensive part (hours for releases), so a retry
                # must not redo it just because a later step failed.
                logger.info("%s already converted, reusing %s", dump.filename, csv_path.name)
                csv_files[content_type] = csv_path
                continue

            gz_path = work_dir / dump.filename
            logger.info("Downloading %s (%s)...", dump.filename, dump.size_display)
            downloader.download(dump.url, gz_path)

            logger.info("Converting %s -> %s ...", dump.filename, csv_name)

            def progress(step: int, total: int, _name=dump.filename) -> None:
                if total and step % max(1, total // 10) == 0:
                    logger.info("  %s: %d%%", _name, int(step / total * 100))

            converter.convert_dump(gz_path, content_type, csv_path, progress_cb=progress)
            csv_files[content_type] = csv_path

            # Free disk immediately: the compressed dump isn't needed once its CSV exists.
            gz_path.unlink(missing_ok=True)

        # Must be named exactly "dataset-cover-image.<ext>" — the kaggle CLI auto-detects
        # this specific filename as a sibling of dataset-metadata.json and uploads it as
        # the dataset's actual cover image (not just a regular file in the listing).
        # Regenerated every run (unlike the CSVs) so a --keep-staging iteration loop that's
        # tweaking cover_art.py picks up each change instead of reusing a stale image.
        cover_path = staging_dir / "dataset-cover-image.png"
        logger.info("Generating cover image for %s...", month)
        cover_art.generate_cover_image(month, cover_path)

        logger.info("Building Kaggle dataset metadata...")
        kaggle_publish.build_dataset_metadata(staging_dir, args.kaggle_owner, month, csv_files)

        if args.replace_existing:
            dataset_slug = kaggle_publish.dataset_slug_for(month)
            logger.info("--replace-existing set: deleting %s/%s before re-publishing...", args.kaggle_owner, dataset_slug)
            try:
                kaggle_publish.delete_dataset(args.kaggle_owner, dataset_slug)
            except RuntimeError as e:
                logger.warning("Delete failed (dataset may not exist yet, continuing): %s", e)

        logger.info("Publishing to Kaggle...")
        kaggle_publish.publish_dataset(staging_dir)

        state.mark_published(month)
        logger.info("Done: %s published to Kaggle.", month)
    except Exception:
        logger.exception(
            "Sync failed for %s. Leaving %s in place (not deleting) so a re-run can "
            "resume from here instead of redoing hours of conversion work.",
            month, work_dir,
        )
        return 1
    else:
        # Only reclaim disk on success — these are multi-GB working sets, but the whole
        # point of keeping them on failure (or with --keep-staging) is so a retry is cheap.
        if not args.keep_staging:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            logger.info("--keep-staging set: leaving %s in place for fast re-publish iteration.", work_dir)
        return 0


if __name__ == "__main__":
    sys.exit(main())
