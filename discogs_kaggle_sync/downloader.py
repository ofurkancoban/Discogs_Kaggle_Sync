"""Streams a dump file to disk with resume-on-reconnect support.

Server-side batch jobs care about reliability over raw throughput, so this is a single
connection with byte-range resume, not the 8-way parallel downloader in the desktop apps.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MB
MAX_RETRIES = 10
RETRY_DELAY_SECONDS = 15


def download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)

    head = requests.head(url, timeout=30)
    head.raise_for_status()
    total_size = int(head.headers.get("Content-Length", 0))

    attempt = 0
    while True:
        attempt += 1
        downloaded = destination.stat().st_size if destination.exists() else 0
        if total_size and downloaded >= total_size:
            logger.info("Already fully downloaded: %s", destination.name)
            return destination

        headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 416:  # already complete
                    return destination
                r.raise_for_status()
                mode = "ab" if downloaded else "wb"
                with open(destination, mode) as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
            logger.info("Downloaded %s (%s bytes)", destination.name, destination.stat().st_size)
            return destination
        except requests.exceptions.RequestException as e:
            if attempt >= MAX_RETRIES:
                raise
            logger.warning(
                "Download error for %s (attempt %d/%d): %s. Retrying in %ds...",
                destination.name, attempt, MAX_RETRIES, e, RETRY_DELAY_SECONDS,
            )
            time.sleep(RETRY_DELAY_SECONDS)
