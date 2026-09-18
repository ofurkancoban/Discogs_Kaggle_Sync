"""Generates and publishes a starter Kaggle notebook for a month's dataset.

Kaggle's "Pending Actions" checklist asks every dataset to ship a companion notebook
("Provide an example of the data in use so other users can get started quickly"), file
descriptions ("Add file information"), and column descriptors ("Include column descriptors").
All three are automatically created and pushed upon dataset readiness.

The notebook is written as raw nbformat v4 JSON rather than via the nbformat package so
this stays dependency-free. It has to actually run on Kaggle's servers, so every read is
row-limited: releases.csv alone is ~30GB and would blow the kernel's memory otherwise.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from calendar import month_name
from pathlib import Path

logger = logging.getLogger(__name__)

# Kaggle mounts a dataset at /kaggle/input/<dataset-slug>/.
KAGGLE_INPUT_ROOT = "/kaggle/input"

# Every pandas read in the notebook is capped at this many rows. The point is a runnable
# example, not a full pass over tens of GB.
SAMPLE_ROWS = 50_000


def _markdown(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": source.splitlines(keepends=True)}


def _code(source: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


def kernel_slug_for(dataset_slug: str) -> str:
    return f"{dataset_slug}-starter"


def build_notebook(
    month: str,  # "YYYY-MM"
    dataset_slug: str,
    csv_names: dict[str, str],  # content_type -> csv file name
) -> dict:
    year, month_num = month.split("-")
    month_label = f"{month_name[int(month_num)]} {year}"
    data_dir = f"{KAGGLE_INPUT_ROOT}/{dataset_slug}"

    files_literal = json.dumps(csv_names, indent=4)

    cells = [
        _markdown(
            f"# Discogs Data Dumps ({month_label}) - Getting Started\n"
            "\n"
            "This notebook is a quick tour of the four CSV files in this dataset: what each one\n"
            "contains, how the columns are shaped, and a few starter aggregations you can build on.\n"
            "\n"
            "Discogs is a community-maintained database of music releases. Each monthly dump is a\n"
            "snapshot of four record types:\n"
            "\n"
            "- **artists** - performers and their aliases, groups, and name variations\n"
            "- **labels** - record labels, their sublabels, and parent labels\n"
            "- **masters** - abstract works that group together every pressing of the same release\n"
            "- **releases** - individual physical/digital pressings, with tracklists and credits\n"
            "\n"
            "Nested XML elements were flattened into columns, and repeated elements are stored as\n"
            "JSON-encoded lists inside a single cell, so a few columns need `json.loads` to unpack.\n"
        ),
        _code(
            "import glob\n"
            "import json\n"
            "import os\n"
            "\n"
            "import pandas as pd\n"
            "\n"
            f'EXPECTED_DATA_DIR = "{data_dir}"\n'
            f"FILES = {files_literal}\n"
            "\n"
            "# These files are large (releases.csv is tens of GB), so every read below is capped.\n"
            f"SAMPLE_ROWS = {SAMPLE_ROWS}\n"
            "\n"
            "\n"
            "def resolve_data_dir(preferred):\n"
            '    """Finds the mounted dataset directory.\n'
            "\n"
            "    Kaggle normally mounts an attached dataset at /kaggle/input/<slug>, but the mount\n"
            "    can be missing or named differently while a new version is still processing, so\n"
            "    fall back to whatever is actually present under /kaggle/input.\n"
            '    """\n'
            "    if os.path.isdir(preferred):\n"
            "        return preferred\n"
            f'    mounted = sorted(p for p in glob.glob("{KAGGLE_INPUT_ROOT}/*") if os.path.isdir(p))\n'
            "    if mounted:\n"
            '        print(f"Expected {preferred!r}; using {mounted[0]!r} instead.")\n'
            "        return mounted[0]\n"
            f'    print("No dataset is mounted under {KAGGLE_INPUT_ROOT} yet.")\n'
            "    return preferred\n"
            "\n"
            "\n"
            "DATA_DIR = resolve_data_dir(EXPECTED_DATA_DIR)\n"
            "\n"
            "if os.path.isdir(DATA_DIR):\n"
            "    for name in sorted(os.listdir(DATA_DIR)):\n"
            "        size_gb = os.path.getsize(os.path.join(DATA_DIR, name)) / 1e9\n"
            '        print(f"{name:<45} {size_gb:>8.2f} GB")\n'
        ),
        _markdown(
            "## Loading a sample\n"
            "\n"
            "`pd.read_csv(..., nrows=...)` reads only the first N rows, which keeps this notebook\n"
            "inside the kernel's memory limit. Drop `nrows` if you're working on a machine that can\n"
            "hold the full file.\n"
        ),
        _code(
            "def load_sample(content_type, nrows=SAMPLE_ROWS, **kwargs):\n"
            '    """Reads the first `nrows` rows of one of the dump files, or None if absent.\n'
            "\n"
            "    A month's dataset is published one file at a time, so a file can legitimately\n"
            "    be missing while the upload is still in progress. Returning None keeps the rest\n"
            "    of the notebook runnable instead of failing the whole kernel.\n"
            '    """\n'
            "    if content_type not in FILES:\n"
            '        print(f"{content_type}: not part of this dataset, skipping")\n'
            "        return None\n"
            "    path = os.path.join(DATA_DIR, FILES[content_type])\n"
            "    if not os.path.exists(path):\n"
            '        print(f"{content_type}: not present in this dataset yet, skipping")\n'
            "        return None\n"
            "    return pd.read_csv(path, nrows=nrows, low_memory=False, **kwargs)\n"
            "\n"
            "\n"
            'labels = load_sample("labels")\n'
            "if labels is not None:\n"
            "    print(labels.shape)\n"
            "    display(labels.head())\n"
        ),
        _code(
            "if labels is not None:\n"
            "    labels.info()\n"
        ),
        _markdown(
            "## Unpacking the JSON-encoded columns\n"
            "\n"
            "Where a record had several of the same child element (a label with multiple sublabels,\n"
            "a release with multiple artists), the values were serialised into a JSON list. Here's\n"
            "how to turn one of those back into real Python values.\n"
        ),
        _code(
            "def parse_json_cell(value):\n"
            '    """Turns a JSON-encoded list cell back into a list; leaves plain values alone."""\n'
            "    if pd.isna(value):\n"
            "        return []\n"
            "    try:\n"
            "        parsed = json.loads(value)\n"
            "    except (TypeError, ValueError):\n"
            "        return [value]\n"
            "    return parsed if isinstance(parsed, list) else [parsed]\n"
            "\n"
            "\n"
            "if labels is not None:\n"
            '    sublabels = labels["label_sublabels_label"].map(parse_json_cell)\n'
            "    labels_with_counts = labels.assign(sublabel_count=sublabels.str.len())\n"
            '    display(labels_with_counts.nlargest(10, "sublabel_count")[\n'
            '        ["labels_label_name", "sublabel_count"]\n'
            "    ])\n"
        ),
        _markdown(
            "## Releases: formats, countries and years\n"
            "\n"
            "`releases.csv` is the big one. The same row-capped read works here, and the columns\n"
            "below are a good starting point for most analyses.\n"
        ),
        _code(
            'releases = load_sample("releases")\n'
            "if releases is not None:\n"
            "    print(releases.shape)\n"
            '    display(releases[["releases_release_title", "releases_release_country",\n'
            '                      "releases_release_released", "release_formats_format_name"]].head(10))\n'
        ),
        _code(
            "if releases is not None:\n"
            '    top_countries = releases["releases_release_country"].value_counts().head(15)\n'
            '    top_countries.plot(kind="barh", figsize=(8, 6),\n'
            '                       title="Releases by country (sample)").invert_yaxis()\n'
        ),
        _code(
            "# `released` is free-form (full date, year-month, or just a year), so pull the year out.\n"
            "if releases is not None:\n"
            "    years = pd.to_numeric(\n"
            '        releases["releases_release_released"].astype(str).str.slice(0, 4), errors="coerce"\n'
            "    )\n"
            "    years = years[(years >= 1900) & (years <= 2030)]\n"
            "    years.value_counts().sort_index().plot(\n"
            '        figsize=(10, 4), title="Releases per year (sample)"\n'
            "    )\n"
        ),
        _markdown(
            "## Masters and artists\n"
            "\n"
            "`masters` groups every pressing of the same work, so it's the right table for questions\n"
            "about the work itself rather than a specific pressing. `artists` carries names, aliases\n"
            "and profiles.\n"
        ),
        _code(
            'masters = load_sample("masters")\n'
            'artists = load_sample("artists")\n'
            "\n"
            "if masters is not None:\n"
            '    print("masters:", masters.shape)\n'
            "    display(\n"
            '        masters["master_genres_genre"].map(parse_json_cell).explode().value_counts().head(15)\n'
            "    )\n"
            "if artists is not None:\n"
            '    print("artists:", artists.shape)\n'
            '    display(artists.head())\n'
        ),
        _markdown(
            "## Where to go next\n"
            "\n"
            "A few directions this dataset supports well:\n"
            "\n"
            "- **Catalogue growth** - how release counts shift by year, country and format\n"
            "- **Label networks** - parent/sublabel relationships as a graph\n"
            "- **Genre drift** - how style tags co-occur and change over decades\n"
            "- **Reissue patterns** - linking pressings back to their master release\n"
            "\n"
            "Every month is published as its own dataset, so you can compare snapshots over time.\n"
        ),
    ]

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.10.0"},
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def build_kernel_metadata(owner_slug: str, dataset_slug: str, month: str, notebook_filename: str) -> dict:
    year, month_num = month.split("-")
    month_label = f"{month_name[int(month_num)]} {year}"
    return {
        "id": f"{owner_slug}/{kernel_slug_for(dataset_slug)}",
        # Kaggle rejects the push unless the title slugifies to exactly the id above, so
        # this has to stay word-for-word in step with kernel_slug_for().
        "title": f"Discogs Data Dumps ({month_label}) Starter",
        "code_file": notebook_filename,
        "language": "python",
        "kernel_type": "notebook",
        "is_private": False,
        "enable_gpu": False,
        # No network access needed: everything comes from the attached dataset.
        "enable_internet": False,
        "dataset_sources": [f"{owner_slug}/{dataset_slug}"],
        "competition_sources": [],
        "kernel_sources": [],
    }


def write_notebook(
    out_dir: Path,
    owner_slug: str,
    dataset_slug: str,
    month: str,
    csv_names: dict[str, str],
) -> Path:
    """Writes the .ipynb and its kernel-metadata.json into `out_dir`, returning that dir."""
    out_dir.mkdir(parents=True, exist_ok=True)

    notebook_filename = f"{kernel_slug_for(dataset_slug)}.ipynb"
    notebook = build_notebook(month, dataset_slug, csv_names)
    (out_dir / notebook_filename).write_text(json.dumps(notebook, indent=1), encoding="utf-8")

    metadata = build_kernel_metadata(owner_slug, dataset_slug, month, notebook_filename)
    (out_dir / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return out_dir


def _kaggle_cmd() -> str:
    kaggle_bin = Path(sys.executable).parent / "kaggle"
    return str(kaggle_bin) if kaggle_bin.exists() else "kaggle"


def push_notebook(notebook_dir: Path) -> None:
    """Runs `kaggle kernels push` for the notebook staged in `notebook_dir`."""
    result = subprocess.run(
        [_kaggle_cmd(), "kernels", "push", "-p", str(notebook_dir)],
        capture_output=True, text=True,
    )
    logger.info("kaggle kernels push stdout: %s", result.stdout.strip())
    if result.returncode != 0:
        logger.error("kaggle kernels push stderr: %s", result.stderr.strip())
        raise RuntimeError(f"Kaggle notebook push failed (exit {result.returncode})")


def wait_for_run(
    kernel_ref: str,  # "owner_slug/kernel_slug"
    timeout_seconds: int = 3600,
    poll_interval: int = 15,
) -> None:
    """Blocks until the just-pushed notebook finishes executing on Kaggle.

    `kernels push` only queues a run; the dataset's "Publish a notebook" pending-action
    item only clears once that run actually finishes successfully, not merely on push.
    Raises on failure/timeout so a broken notebook is visible instead of silently leaving
    that checklist item unmet.

    900s wasn't enough for a real month's data (as opposed to the tiny labels-only
    dataset this was tuned against): the kernel was still RUNNING well past that, most
    likely queued behind Kaggle's shared compute rather than genuinely stuck - hence the
    more generous default, matching wait_for_dataset_ready's same finding.
    """
    from kaggle.api.kaggle_api_extended import KaggleApi
    from kagglesdk.kernels.types.kernels_enums import KernelWorkerStatus

    api = KaggleApi()
    api.authenticate()
    terminal = {
        KernelWorkerStatus.COMPLETE,
        KernelWorkerStatus.ERROR,
        KernelWorkerStatus.CANCEL_ACKNOWLEDGED,
    }
    start_time = time.time()
    logger.info("Waiting for notebook %s to finish running...", kernel_ref)
    while time.time() - start_time < timeout_seconds:
        try:
            result = api.kernels_status(kernel_ref)
        except Exception as e:
            logger.warning("Error querying kernel status for %s: %s", kernel_ref, e)
            time.sleep(poll_interval)
            continue
        if result.status in terminal:
            if result.status != KernelWorkerStatus.COMPLETE:
                raise RuntimeError(
                    f"Notebook {kernel_ref} finished with status {result.status.name}: "
                    f"{result.failure_message}"
                )
            logger.info("Notebook %s finished running successfully.", kernel_ref)
            return
        time.sleep(poll_interval)
    raise TimeoutError(f"Notebook {kernel_ref} did not finish running within {timeout_seconds} seconds")
