"""Shared logging setup with built-in rotation.

Both entry points used to call `logging.basicConfig()` and rely on the invoking shell
(cron's `>> logs/sync.log 2>&1`, or pm2's `out_file`/`error_file`) to capture output -
which appends forever and never gets cleaned up on its own. This configures a rotating
file handler directly in Python, so `logs/sync.log` stays bounded regardless of how the
script is invoked, plus a console handler so cron/pm2's own capture still shows output.
"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path(__file__).parent.parent / "logs"

# One file per day, two weeks of history - enough to debug a recent run without letting
# logs/ grow without bound over months of daily cron invocations.
BACKUP_DAYS = 14


def configure_logging(name: str, log_dir: Path = DEFAULT_LOG_DIR) -> None:
    root = logging.getLogger()
    if root.handlers:
        # Already configured - e.g. fill_descriptions.py gets `import`ed mid-run by
        # run_monthly_sync.py, which already set up logging. Reconfiguring here would
        # duplicate every handler and print each log line twice.
        return

    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = TimedRotatingFileHandler(
        log_dir / f"{name}.log", when="midnight", backupCount=BACKUP_DAYS, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
