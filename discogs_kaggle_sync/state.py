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
