"""Streams a .xml.gz dump straight into chunked, well-formed XML files, then converts
those chunks to a single CSV via a two-pass (discover columns, then write rows) approach.

Ported from DiscogsGUI's main.py (chunk_xml_by_type / update_columns_from_chunk /
write_chunk_to_csv / convert_chunked_files_to_csv), with one deliberate change: chunking
reads directly from the gzip stream instead of a fully-decompressed .xml file on disk.
Discogs' releases dump is ~10GB compressed and unpacks to tens of GB - never materializing
that full decompressed copy roughly halves peak disk usage, which matters a lot on a VPS
with finite disk.
"""
from __future__ import annotations

import csv
import gzip
import json
import logging
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

RECORDS_PER_CHUNK = 10_000

_INVALID_CHARS = re.compile(r"[^\x09\x0A\x0D\x20-퟿-�]")
_BARE_AMPERSAND = re.compile(r"&(?![a-zA-Z0-9#]+;)")


def sanitize_line(line: str) -> str:
    """Strips invalid XML characters and escapes bare '&' outside the current line."""
    line = _INVALID_CHARS.sub("", line)
    line = _BARE_AMPERSAND.sub("&amp;", line)
    return line


def content_type_from_filename(filename: str) -> str:
    lower = filename.lower()
    for ctype in ("artists", "labels", "masters", "releases"):
        if ctype[:-1] in lower:
            return ctype
    raise ValueError(f"Cannot determine content type from filename: {filename}")


def chunk_gz_dump(gz_path: Path, content_type: str, chunk_dir: Path) -> int:
    """Streams `gz_path` (never fully decompressed to disk) into `chunk_dir/chunk_NNNNNN.xml`
    files, `RECORDS_PER_CHUNK` records each. Returns the number of chunks created."""
    record_tag = content_type[:-1]
    start_pat = re.compile(rf"<{record_tag}\b", re.IGNORECASE)
    end_pat = re.compile(rf"</{record_tag}>", re.IGNORECASE)

    chunk_dir.mkdir(parents=True, exist_ok=True)

    chunk_count = 0
    record_count = 0
    inside_record = False
    buffer_lines: list[str] = []
    current_chunk_file = None

    def open_new_chunk():
        nonlocal chunk_count, current_chunk_file, record_count
        chunk_count += 1
        path = chunk_dir / f"chunk_{chunk_count:06d}.xml"
        current_chunk_file = open(path, "w", encoding="utf-8")
        current_chunk_file.write(f'<?xml version="1.0" encoding="utf-8"?>\n<{content_type}>\n')
        record_count = 0

    def close_chunk():
        nonlocal current_chunk_file
        if current_chunk_file:
            current_chunk_file.write(f"</{content_type}>")
            current_chunk_file.close()
            current_chunk_file = None

    open_new_chunk()

    with gzip.open(gz_path, "rt", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = sanitize_line(raw_line)
            if not inside_record:
                if start_pat.search(line):
                    inside_record = True
                    buffer_lines = [line]
            else:
                buffer_lines.append(line)
                if end_pat.search(line):
                    current_chunk_file.write("".join(buffer_lines) + "\n")
                    record_count += 1
                    inside_record = False
                    buffer_lines = []
                    if record_count >= RECORDS_PER_CHUNK:
                        close_chunk()
                        open_new_chunk()
    close_chunk()

    logger.info("Chunked %s into %d chunk(s) in %s", gz_path.name, chunk_count, chunk_dir)
    return chunk_count


def _flatten_tag(path: list[str], attr: str | None = None) -> str:
    depth = len(path)
    if depth >= 3:
        base = "_".join(path[-3:])
    elif depth == 2:
        base = "_".join(path[-2:])
    else:
        base = path[-1]
    return f"{base}_{attr}" if attr else base


def _discover_columns(chunk_file: Path, record_tag: str, columns: set[str]) -> None:
    path: list[str] = []
    for event, elem in ET.iterparse(str(chunk_file), events=("start", "end")):
        if event == "start":
            path.append(elem.tag)
            for attr in elem.attrib:
                columns.add(_flatten_tag(path, attr))
        else:
            if elem.text and not elem.text.isspace():
                columns.add(_flatten_tag(path))
            path.pop()
            elem.clear()


def _write_chunk_rows(chunk_file: Path, writer: "csv.DictWriter", columns: list[str], record_tag: str) -> None:
    path: list[str] = []
    record: dict[str, object] = {}

    def record_value(tag_name: str, value: str) -> None:
        if tag_name in record:
            existing = record[tag_name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                record[tag_name] = [existing, value]
        else:
            record[tag_name] = value

    for event, elem in ET.iterparse(str(chunk_file), events=("start", "end")):
        if event == "start":
            path.append(elem.tag)
            for attr, value in elem.attrib.items():
                record_value(_flatten_tag(path, attr), value)
        else:
            if elem.text and not elem.text.isspace():
                record_value(_flatten_tag(path), elem.text.strip())

            if elem.tag == record_tag:
                row = {}
                for col in columns:
                    value = record.get(col)
                    row[col] = json.dumps(value) if isinstance(value, list) else value
                writer.writerow(row)
                record.clear()

            path.pop()
            elem.clear()


def convert_dump(
    gz_path: Path,
    content_type: str,
    output_csv: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Full pipeline: chunk the .gz, discover columns, write the CSV, clean up chunks."""
    chunk_dir = gz_path.parent / f"chunked_{content_type}"
    record_tag = content_type[:-1]

    chunk_count = chunk_gz_dump(gz_path, content_type, chunk_dir)
    if chunk_count == 0:
        logger.warning("No records found in %s", gz_path.name)
        return

    chunk_files = sorted(chunk_dir.glob("chunk_*.xml"))
    total_steps = chunk_count * 2
    step = 0

    columns: set[str] = set()
    for cf in chunk_files:
        _discover_columns(cf, record_tag, columns)
        step += 1
        if progress_cb:
            progress_cb(step, total_steps)
    ordered_columns = sorted(columns)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ordered_columns)
        writer.writeheader()
        for cf in chunk_files:
            _write_chunk_rows(cf, writer, ordered_columns, record_tag)
            step += 1
            if progress_cb:
                progress_cb(step, total_steps)

    shutil.rmtree(chunk_dir, ignore_errors=True)
    logger.info("Converted %s -> %s (%d columns)", gz_path.name, output_csv.name, len(ordered_columns))
