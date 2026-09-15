"""Discovers available Discogs data dump months and files.

Ported from DiscogsGUI's main.py (list_directories_from_s3 / list_files_in_directory),
with the Tkinter/pandas dependencies removed since this runs headless on a server.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

import requests

BASE_URL = "https://data.discogs.com/"
DATA_PREFIX = "data/"


@dataclass
class DumpFile:
    key: str
    filename: str
    content_type: str  # artists | labels | masters | releases | checksum | unknown
    size_display: str
    last_modified: str
    url: str

    @property
    def date(self) -> datetime | None:
        m = re.search(r"discogs_(\d{8})_", self.key)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y%m%d")
        except ValueError:
            return None

    @property
    def month(self) -> str:
        dt = self.date
        return dt.strftime("%Y-%m") if dt else ""


def classify(filename: str) -> str:
    lower = filename.lower()
    if "checksum" in lower:
        return "checksum"
    if "artist" in lower:
        return "artists"
    if "master" in lower:
        return "masters"
    if "label" in lower:
        return "labels"
    if "release" in lower:
        return "releases"
    return "unknown"


def list_year_directories() -> list[str]:
    """Returns year-prefix directories, e.g. ['data/2025/', 'data/2026/']."""
    url = BASE_URL + "?prefix=" + DATA_PREFIX
    r = requests.get(url, timeout=30)
    r.raise_for_status()

    dirs: list[str] = []
    for match in re.findall(r'href="\?prefix=([^"]+)"', r.text):
        decoded = urllib.parse.unquote(match)
        if decoded.startswith(DATA_PREFIX) and decoded != DATA_PREFIX and decoded not in dirs:
            dirs.append(decoded)
    return sorted(dirs)


def list_files_in_directory(directory_prefix: str) -> list[DumpFile]:
    """Lists files (with size/date/url) within a year directory prefix."""
    url = BASE_URL + "?prefix=" + directory_prefix
    r = requests.get(url, timeout=30)
    r.raise_for_status()

    pattern = (
        r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+([\d.]+\s+[KMG]?B)\s+'
        r'<a href="\?download=([^"]+)">([^<]+)</a>'
    )
    files = []
    for last_modified, size_hr, key_encoded, filename in re.findall(pattern, r.text):
        key = urllib.parse.unquote(key_encoded)
        files.append(
            DumpFile(
                key=key,
                filename=filename,
                content_type=classify(filename),
                size_display=size_hr,
                last_modified=last_modified,
                url=BASE_URL + "?download=" + key_encoded,
            )
        )
    return files


def latest_month_files() -> tuple[str, list[DumpFile]]:
    """Returns (month, files) for the most recent month that has dump files."""
    years = list_year_directories()
    if not years:
        raise RuntimeError("No year directories found on data.discogs.com")

    # Years are listed oldest-first; walk backwards to find the newest month with data.
    for year_prefix in reversed(years):
        files = [f for f in list_files_in_directory(year_prefix) if f.content_type != "checksum"]
        if not files:
            continue
        months = sorted({f.month for f in files if f.month}, reverse=True)
        if not months:
            continue
        latest_month = months[0]
        return latest_month, [f for f in files if f.month == latest_month]

    raise RuntimeError("No dump files found in any year directory")
