"""Backup folder naming and retention policy.

Only folders whose name matches BACKUP_DIR_FORMAT exactly are ever touched.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

BACKUP_DIR_FORMAT = "%Y-%m-%d_%H%M%S"


def backup_folder_name(moment: datetime) -> str:
    return moment.strftime(BACKUP_DIR_FORMAT)


def list_backups(backup_root: Path) -> list[tuple[datetime, Path]]:
    """Return (creation time, path) of every backup folder, newest first."""
    if not backup_root.is_dir():
        return []
    backups = []
    for entry in backup_root.iterdir():
        if not entry.is_dir():
            continue
        try:
            created = datetime.strptime(entry.name, BACKUP_DIR_FORMAT)
        except ValueError:
            continue
        backups.append((created, entry))
    backups.sort(reverse=True)
    return backups


def latest_backup(backup_root: Path) -> Path | None:
    """Return the newest completed backup, never an incomplete working folder."""
    backups = list_backups(backup_root)
    return backups[0][1] if backups else None


def find_expired_backups(
    backup_root: Path, retention_days: int, min_keep: int, now: datetime
) -> list[Path]:
    """Return backups older than `retention_days`, oldest first.

    The `min_keep` most recent backups are never returned, even when expired,
    so repeated backup failures cannot wipe out the last good copies.
    """
    cutoff = now - timedelta(days=retention_days)
    candidates = list_backups(backup_root)[min_keep:]
    return [path for created, path in reversed(candidates) if created < cutoff]


def delete_backups(paths: list[Path]) -> None:
    for path in paths:
        shutil.rmtree(path)
