#!/usr/bin/env python3
"""Fills a month's file and column descriptions on Kaggle.

These are the two fields Kaggle's public API accepts and then discards (see
discogs_kaggle_sync/web_metadata.py), so this drives the same endpoint the web UI uses
and therefore needs a browser session rather than the kaggle.json API token.

Run it after the month's dataset has finished processing:

    python fill_descriptions.py --kaggle-owner ofurkancoban --month 2026-09
"""
from __future__ import annotations

import argparse
import logging
import sys

from discogs_kaggle_sync import kaggle_publish, web_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fill_descriptions")

CONTENT_TYPES = ("artists", "labels", "masters", "releases")


def csv_name_for(month: str, content_type: str) -> str:
    year, month_num = month.split("-")
    return f"discogs_{year}{month_num}01_{content_type}.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kaggle-owner", help="Kaggle username that owns the dataset.")
    parser.add_argument("--month", help="Month to fill, as YYYY-MM.")
    parser.add_argument(
        "--only-types", default=None,
        help="Comma-separated subset of artists,labels,masters,releases. Defaults to all "
             "four; files the dataset does not contain are skipped either way.",
    )
    parser.add_argument(
        "--check-session", action="store_true",
        help="Report whether the exported browser session still works, and roughly how "
             "long it has left, then exit.",
    )
    parser.add_argument(
        "--set-session", action="store_true",
        help="Read a browser 'Copy as cURL' command on stdin and write it to the session "
             "file. Use it when --check-session says the session has expired.",
    )
    args = parser.parse_args()

    if args.set_session:
        try:
            path = web_metadata.save_session_from_curl(sys.stdin.read())
        except web_metadata.KaggleWebSessionError as e:
            logger.error("%s", e)
            return 2
        logger.info("Saved session to %s", path)
        ok, detail = web_metadata.check_session()
        logger.info("%s", detail)
        return 0 if ok else 2

    if args.check_session:
        try:
            ok, detail = web_metadata.check_session()
        except web_metadata.KaggleWebSessionError as e:
            logger.error("%s", e)
            return 2
        logger.info("%s", detail)
        return 0 if ok else 2

    if not args.kaggle_owner or not args.month:
        parser.error("--kaggle-owner and --month are required unless checking or setting the session")

    try:
        ok, detail = web_metadata.check_session()
    except web_metadata.KaggleWebSessionError as e:
        logger.error("%s", e)
        return 2
    if not ok:
        logger.error("%s", detail)
        return 2
    logger.info("%s", detail)

    content_types = (
        tuple(t.strip() for t in args.only_types.split(",")) if args.only_types else CONTENT_TYPES
    )

    file_descriptions: dict[str, str] = {}
    column_descriptions: dict[str, dict[str, str]] = {}
    for content_type in content_types:
        csv_name = csv_name_for(args.month, content_type)
        file_descriptions[csv_name] = kaggle_publish.FILE_DESCRIPTIONS.get(content_type, "")
        column_descriptions[csv_name] = kaggle_publish._load_descriptions(content_type)

    dataset_slug = kaggle_publish.dataset_slug_for(args.month)
    logger.info("Filling descriptions on %s/%s", args.kaggle_owner, dataset_slug)

    try:
        rating = web_metadata.apply_descriptions(
            args.kaggle_owner, dataset_slug, file_descriptions, column_descriptions,
        )
    except web_metadata.KaggleWebSessionError as e:
        logger.error("%s", e)
        return 2

    if rating:
        logger.info("Final usability rating: %s", rating)
    return 0


if __name__ == "__main__":
    sys.exit(main())
