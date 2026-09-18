#!/usr/bin/env python3
"""Entry point for the monthly Discogs -> Kaggle sync.

Checks for a new dump month, downloads its 4 files, converts each to CSV, publishes a
new Kaggle dataset for the month, then cleans up local disk. Safe to run repeatedly
(via cron) - already-published months are skipped using state/published_months.json.

Usage:
    python run_monthly_sync.py [--work-dir /path/to/scratch] [--kaggle-owner USERNAME]
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from pathlib import Path

from discogs_kaggle_sync import converter, cover_art, downloader, kaggle_publish, logging_setup, notebook, scraper, state, web_metadata

logging_setup.configure_logging("sync")
logger = logging.getLogger("run_monthly_sync")

CONTENT_TYPES = ("artists", "labels", "masters", "releases")

# Multiplier applied to the compressed download size to estimate final disk usage:
# converter.py deletes each .xml.gz right after producing its .csv, so peak usage is
# roughly the sum of all final CSVs (XML decompresses to several times its gzipped size -
# releases.xml.gz -> releases.csv was ~3.2x on a real run) plus the one gz currently
# downloading, which this multiplier already covers with room to spare.
_DISK_ESTIMATE_MULTIPLIER = 5
_DISK_MIN_REQUIRED_BYTES = 15 * 1024**3  # floor for small/partial (--only-types) runs

_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGT]?B)")
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _parse_size_to_bytes(size_display: str) -> float:
    """"10.5 GB" -> bytes. Returns 0 if the text doesn't match the expected format."""
    m = _SIZE_RE.match(size_display.strip())
    if not m:
        return 0
    value, unit = m.groups()
    return float(value) * _SIZE_UNITS.get(unit, 1)


def _ensure_disk_space(work_dir: Path, dumps) -> None:
    """Fails fast with a clear message instead of dying mid-download with an OS-level
    'no space left on device' error hours into a multi-GB run."""
    compressed_total = sum(_parse_size_to_bytes(d.size_display) for d in dumps)
    required = max(compressed_total * _DISK_ESTIMATE_MULTIPLIER, _DISK_MIN_REQUIRED_BYTES)

    check_dir = work_dir if work_dir.exists() else work_dir.parent
    free = shutil.disk_usage(check_dir).free

    logger.info(
        "Disk space check: %.1f GB free, ~%.1f GB estimated needed (%.1f GB compressed x%d).",
        free / 1024**3, required / 1024**3, compressed_total / 1024**3, _DISK_ESTIMATE_MULTIPLIER,
    )
    if free < required:
        raise RuntimeError(
            f"Only {free / 1024**3:.1f} GB free at {check_dir}, but this run needs an "
            f"estimated {required / 1024**3:.1f} GB. Free up disk space before retrying."
        )


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
             "use this deliberately while iterating on one month - it permanently drops that "
             "dataset's view/download/vote history. Never combine with an unattended cron run.",
    )
    parser.add_argument(
        "--only-types", type=str, default=None,
        help="Debug only: comma-separated subset of artists,labels,masters,releases to "
             "process (e.g. 'labels'). Publishes a real dataset missing the other files - "
             "for exercising the automated publish mechanism cheaply, never for a real month.",
    )
    parser.add_argument(
        "--month", type=str, default=None,
        help="Target month (YYYY-MM). Defaults to the latest month on data.discogs.com.",
    )
    parser.add_argument(
        "--update-metadata-only", action="store_true",
        help="Skip downloading/converting CSVs; generate metadata (file info + column descriptors), "
             "update settings on Kaggle, and push/link the companion notebook for the target month.",
    )
    args = parser.parse_args()

    content_types = CONTENT_TYPES
    if args.only_types:
        content_types = tuple(t.strip() for t in args.only_types.split(","))
        logger.warning("--only-types set: restricting this run to %s (debug mode)", content_types)

    if args.month:
        month = args.month
        logger.info("Target month specified: %s", month)
        files = scraper.files_for_month(month)
        if not files:
            logger.error("No dump files found for %s on data.discogs.com", month)
            return 1
    else:
        logger.info("Checking for the latest Discogs dump month...")
        month, files = scraper.latest_month_files()
        logger.info("Latest month with data: %s (%d file(s))", month, len(files))

    if not args.update_metadata_only:
        published = state.load_published_months()
        if month in published and not args.force:
            logger.info("%s already published to Kaggle. Nothing to do.", month)
            return 0

    by_type = {f.content_type: f for f in files if f.content_type in content_types}
    missing = [t for t in content_types if t not in by_type]
    if missing:
        logger.error("Missing expected file type(s) for %s: %s", month, missing)
        return 1

    work_dir = args.work_dir / month
    staging_dir = work_dir / "staging"
    work_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    if not args.update_metadata_only:
        try:
            _ensure_disk_space(work_dir, by_type.values())
        except RuntimeError as e:
            logger.error("%s", e)
            return 1

    csv_files: dict[str, Path] = {}
    try:
        if not args.update_metadata_only:
            for content_type, dump in by_type.items():
                csv_name = Path(dump.filename).with_suffix("").with_suffix(".csv").name
                csv_path = staging_dir / csv_name

                if csv_path.exists():
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

                gz_path.unlink(missing_ok=True)

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
        else:
            # Metadata update only mode: construct dummy header files if missing to generate schema metadata
            for content_type, dump in by_type.items():
                csv_name = Path(dump.filename).with_suffix("").with_suffix(".csv").name
                csv_path = staging_dir / csv_name
                if not csv_path.exists():
                    descs = kaggle_publish._load_descriptions(content_type)
                    csv_path.write_text(",".join(descs.keys()) + "\n", encoding="utf-8")
                csv_files[content_type] = csv_path

            cover_path = staging_dir / "dataset-cover-image.png"
            if not cover_path.exists():
                logger.info("Generating cover image for %s...", month)
                cover_art.generate_cover_image(month, cover_path)

            logger.info("Building Kaggle dataset metadata...")
            kaggle_publish.build_dataset_metadata(staging_dir, args.kaggle_owner, month, csv_files)

        dataset_slug = kaggle_publish.dataset_slug_for(month)

        logger.info("Waiting for dataset to reach 'ready' status on Kaggle...")
        kaggle_publish.wait_for_dataset_ready(args.kaggle_owner, dataset_slug)

        logger.info("Updating provenance, file descriptions, column descriptors & settings...")
        kaggle_publish.update_dataset_settings(args.kaggle_owner, dataset_slug, staging_dir)

        logger.info("Filling file and column descriptions via web session...")
        try:
            import fill_descriptions
            fill_descriptions.fill_month(args.kaggle_owner, month, content_types)
            state.clear_descriptions_pending(month)
            logger.info("%s web descriptions filled.", month)
        except Exception as e:
            state.mark_descriptions_pending(month)
            logger.warning("Web description update for %s skipped/queued for later: %s", month, e)

        # Notebook goes last: it's the slowest step (has to actually finish running on
        # Kaggle, not just get pushed - can queue behind Kaggle's shared compute for a
        # long time) and the least likely to matter to anything else in this run, so
        # descriptions/provenance land first and a slow/failed notebook run doesn't crash
        # a publish that's otherwise complete - the usability score re-read below will
        # correctly reflect a missing notebook and keep local files in place either way.
        logger.info("Publishing companion starter notebook...")
        try:
            notebook_dir = work_dir / "notebook"
            notebook.write_notebook(
                notebook_dir, args.kaggle_owner, dataset_slug, month,
                {ctype: path.name for ctype, path in csv_files.items()},
            )
            notebook.push_notebook(notebook_dir)
            kernel_ref = f"{args.kaggle_owner}/{notebook.kernel_slug_for(dataset_slug)}"
            notebook.wait_for_run(kernel_ref)
        except Exception as e:
            logger.warning("Notebook publish/run for %s did not complete: %s", month, e)

        # The score fill_descriptions saw above predates the notebook existing, so it
        # can't reflect publicKernelScore yet - re-read the live rating now that
        # everything (descriptions, provenance, notebook) is actually in place.
        usability_score: float | None = None
        try:
            client = web_metadata.KaggleWebClient()
            basics = client.dataset_basics(args.kaggle_owner, dataset_slug)
            usability_score = client.usability_rating(basics["datasetId"]).get("score")
            logger.info("%s final usability rating: %s", month, usability_score)
        except Exception as e:
            logger.warning("Could not confirm final usability rating for %s: %s", month, e)

        # Only a perfect score means Kaggle's "Pending Actions" checklist is actually
        # clear - the documented API can report success (no exception above) while
        # provenance, notebook linkage, or descriptions are still silently unmet (see
        # web_metadata module docstring). Local files stay in place whenever that isn't
        # confirmed, so a retry has real data to work with instead of an unrecoverable gap.
        metadata_complete = usability_score is not None and usability_score >= 0.999

        if content_types == CONTENT_TYPES:
            state.mark_published(month)
        else:
            logger.warning("--only-types set: not marking %s as published (this was a partial debug run).", month)
        logger.info("Done: %s published to Kaggle.", month)
    except Exception:
        logger.exception(
            "Sync failed for %s. Leaving %s in place (not deleting) so a re-run can "
            "resume from here instead of redoing hours of conversion work.",
            month, work_dir,
        )
        return 1
    else:
        # Only reclaim disk once the dataset is verifiably complete (perfect usability
        # score) - these are multi-GB working sets, but the whole point of keeping them on
        # failure, an imperfect score, or with --keep-staging is so a retry is cheap and
        # nothing has to be re-downloaded/re-converted.
        if args.keep_staging:
            logger.info("--keep-staging set: leaving %s in place for fast re-publish iteration.", work_dir)
        elif not metadata_complete:
            logger.warning(
                "Leaving %s in place: usability score is %s, not a perfect 1.0 yet. "
                "Re-run (or `fill_descriptions.py --pending`) once that's resolved, then "
                "this will clean up on the next fully-complete run.",
                work_dir, usability_score,
            )
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
