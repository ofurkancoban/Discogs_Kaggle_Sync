"""Tracks which months have already been published, so a re-run (or a cron overlap)
never re-downloads/re-publishes the same month twice.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE_PATH = Path(__file__).parent.parent / "state" / "published_months.json"


def load_published_months(state_path: Path = DEFAULT_STATE_PATH) -> set[str]:
    if not state_path.exists():
        return set()
    return set(json.loads(state_path.read_text(encoding="utf-8")))


def mark_published(month: str, state_path: Path = DEFAULT_STATE_PATH) -> None:
    months = load_published_months(state_path)
    months.add(month)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(months), indent=2), encoding="utf-8")


# File and column descriptions cannot be written with the API token; they need a browser
# session, and Kaggle expires those in a couple of weeks (see web_metadata). A month whose
# descriptions could not be written is queued here instead of failing the publish, so the
# next run that finds a valid session fills in everything outstanding on its own.
DEFAULT_PENDING_PATH = Path(__file__).parent.parent / "state" / "pending_descriptions.json"


def load_pending_descriptions(state_path: Path = DEFAULT_PENDING_PATH) -> list[str]:
    if not state_path.exists():
        return []
    return sorted(set(json.loads(state_path.read_text(encoding="utf-8"))))


def _write_pending(months: set[str], state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(months), indent=2), encoding="utf-8")


def mark_descriptions_pending(month: str, state_path: Path = DEFAULT_PENDING_PATH) -> None:
    months = set(load_pending_descriptions(state_path))
    months.add(month)
    _write_pending(months, state_path)


def clear_descriptions_pending(month: str, state_path: Path = DEFAULT_PENDING_PATH) -> None:
    months = set(load_pending_descriptions(state_path))
    months.discard(month)
    _write_pending(months, state_path)
