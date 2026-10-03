"""The backups kept in the destination folder, summarised for the interface."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bimcloud_backup.backup import INCOMPLETE_PREFIX
from bimcloud_backup.retention import BACKUP_DIR_FORMAT, list_backups
from bimcloud_backup.state import MANIFEST_NAME, BackupManifest, load_manifest

STATUS_OK = "ok"
STATUS_WARNINGS = "warnings"
STATUS_NO_DETAILS = "no_details"
STATUS_INCOMPLETE = "incomplete"

# File kinds in the manifest and how the interface names them. Plain files are counted as
# FILES, which the interface names in the chosen language.
KIND_LABELS = {"bimproject": ".BIMProject", "pln": ".pln", "bimlibrary": ".BIMLibrary"}
FILES = "files"


@dataclass(frozen=True)
class HistoryEntry:
    folder: Path
    when: datetime
    status: str
    size: int | None = None
    files: int | None = None
    # Exported or downloaded files per kind: ".BIMProject", ".pln", ".BIMLibrary" or FILES.
    counts: tuple[tuple[str, int], ...] = ()
    errors: int = 0


def read_history(
    backup_dir: Path, cache: dict[str, tuple[float, HistoryEntry]] | None = None
) -> list[HistoryEntry]:
    """Every backup in `backup_dir`, newest first, plus folders left by interrupted runs.

    `cache` (folder path -> (manifest mtime, entry)) avoids reading big manifests again on
    every refresh of the window.
    """
    entries = [_incomplete(folder) for folder in _incomplete_folders(backup_dir)]
    entries = [e for e in entries if e is not None]
    for created, folder in list_backups(backup_dir):
        entries.append(_completed(folder, created, cache))
    entries.sort(key=lambda e: e.when, reverse=True)
    return entries


def _completed(
    folder: Path, created: datetime, cache: dict[str, tuple[float, HistoryEntry]] | None
) -> HistoryEntry:
    try:
        mtime = (folder / MANIFEST_NAME).stat().st_mtime
    except OSError:
        return HistoryEntry(folder, created, STATUS_NO_DETAILS)
    key = str(folder)
    if cache is not None and key in cache and cache[key][0] == mtime:
        return cache[key][1]
    manifest = load_manifest(folder)
    entry = (
        HistoryEntry(folder, created, STATUS_NO_DETAILS)
        if manifest is None
        else summarise(folder, created, manifest)
    )
    if cache is not None:
        cache[key] = (mtime, entry)
    return entry


def summarise(folder: Path, created: datetime, manifest: BackupManifest) -> HistoryEntry:
    kinds = Counter(KIND_LABELS.get(f.kind or "", FILES) for f in manifest.files)
    order = [*KIND_LABELS.values(), FILES]
    counts = tuple((label, kinds[label]) for label in order if kinds[label])
    return HistoryEntry(
        folder,
        created,
        STATUS_WARNINGS if manifest.errors else STATUS_OK,
        size=sum(f.size for f in manifest.files),
        files=len(manifest.files),
        counts=counts,
        errors=len(manifest.errors),
    )


def _incomplete_folders(backup_dir: Path) -> list[Path]:
    try:
        return [p for p in backup_dir.glob(f"{INCOMPLETE_PREFIX}*") if p.is_dir()]
    except OSError:
        return []


def _incomplete(folder: Path) -> HistoryEntry | None:
    try:
        when = datetime.strptime(folder.name[len(INCOMPLETE_PREFIX) :], BACKUP_DIR_FORMAT)
    except ValueError:
        return None
    return HistoryEntry(folder, when, STATUS_INCOMPLETE)
