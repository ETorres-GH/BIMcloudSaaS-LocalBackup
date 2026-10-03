"""Logging to a daily file in the user's local app data folder."""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from bimcloud_backup.paths import log_dir
from bimcloud_backup.redaction import redact

LOGGER_NAME = "bimcloud_backup"
FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


class RedactingFormatter(logging.Formatter):
    """Last line of defense: no token, session id or ticket ever reaches the log file."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging(verbose: bool, directory: Path | None = None) -> Path:
    """Send log records to logs/backup-AAAA-MM-DD.log (and stderr when there is one)."""
    directory = directory or log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    logger = get_logger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(logger.handlers):
        if getattr(handler, "_bimcloud_backup", False):
            logger.removeHandler(handler)
            handler.close()

    file_handler = logging.FileHandler(
        directory / f"backup-{date.today():%Y-%m-%d}.log", encoding="utf-8"
    )
    handlers: list[logging.Handler] = [file_handler]
    # Windowed executables have no stderr.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(RedactingFormatter(FORMAT))
        handler._bimcloud_backup = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return directory
