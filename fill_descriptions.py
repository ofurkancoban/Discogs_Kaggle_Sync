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

from discogs_kaggle_sync import kaggle_publish, state, web_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fill_descriptions")

CONTENT_TYPES = ("artists", "labels", "masters", "releases")


def csv_name_for(month: str, content_type: str) -> str:
    year, month_num = month.split("-")
    return f"discogs_{year}{month_num}01_{content_type}.csv"


def fill_month(owner_slug: str, month: str, content_types: tuple[str, ...]) -> dict:
    """Writes one month's file and column descriptions, returning the usability rating."""
    file_descriptions: dict[str, str] = {}
    column_descriptions: dict[str, dict[str, str]] = {}
    for content_type in content_types:
        csv_name = csv_name_for(month, content_type)
        file_descriptions[csv_name] = kaggle_publish.FILE_DESCRIPTIONS.get(content_type, "")
        column_descriptions[csv_name] = kaggle_publish._load_descriptions(content_type)

    dataset_slug = kaggle_publish.dataset_slug_for(month)
    logger.info("Filling descriptions on %s/%s", owner_slug, dataset_slug)
    return web_metadata.apply_descriptions(
        owner_slug, dataset_slug, file_descriptions, column_descriptions,
    )


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
        "--pending", action="store_true",
        help="Fill every month queued by a run that had no usable session, then clear it "
             "from the queue. Safe to run from cron: a lapsed session logs a warning and "
             "leaves the queue for the next attempt.",
    )
    parser.add_argument(
        "--set-session", action="store_true",
        help="Read a browser 'Copy as cURL' command on stdin and write it to the session "
             "file. Use it when --check-session says the session has expired.",
    )
    args = parser.parse_args()

    content_types = (
        tuple(t.strip() for t in args.only_types.split(",")) if args.only_types else CONTENT_TYPES
    )

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

    if args.pending:
        if not args.kaggle_owner:
            parser.error("--kaggle-owner is required with --pending")
        months = state.load_pending_descriptions()
        if not months:
            logger.info("No months are waiting for descriptions.")
            return 0
        try:
            ok, detail = web_metadata.check_session()
        except web_metadata.KaggleWebSessionError as e:
            # Expected whenever the exported session has lapsed. Leave the queue alone and
            # exit cleanly so an unattended run does not look like a failure.
            logger.warning("%s", e)
            logger.warning("%d month(s) still waiting: %s", len(months), ", ".join(months))
            return 0
        if not ok:
            logger.warning("%s", detail)
            logger.warning("%d month(s) still waiting: %s", len(months), ", ".join(months))
            return 0

        logger.info("%s", detail)
        for month in months:
            try:
                rating = fill_month(args.kaggle_owner, month, content_types)
            except Exception:
                logger.exception("Could not fill %s; leaving it queued.", month)
                continue
            state.clear_descriptions_pending(month)
            logger.info("%s done. Usability: %s", month, rating.get("score"))
        return 0

    if not args.kaggle_owner or not args.month:
        parser.error("--kaggle-owner and --month are required unless checking or setting the session")

    try:
        ok, detail = web_metadata.check_session()
    except web_metadata.KaggleWebSessionError as e:
        ok, detail = False, str(e)
    if not ok:
        state.mark_descriptions_pending(args.month)
        logger.error("%s", detail)
        logger.error("Queued %s; a later run with a valid session will fill it.", args.month)
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

    try:
        rating = fill_month(args.kaggle_owner, args.month, content_types)
    except web_metadata.KaggleWebSessionError as e:
        state.mark_descriptions_pending(args.month)
        logger.error("%s", e)
        logger.error("Queued %s to be filled by a later run.", args.month)
        return 2

    state.clear_descriptions_pending(args.month)
    if rating:
        logger.info("Final usability rating: %s", rating)
    return 0


if __name__ == "__main__":
    sys.exit(main())
