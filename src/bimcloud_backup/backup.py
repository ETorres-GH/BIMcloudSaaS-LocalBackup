"""One backup run: walk BIMcloud, download everything into a dated folder, prune old ones.

Files go to `.incompleto-<data>` first; the folder gets its final name only when the run ends.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Protocol

from bimcloud_backup import __version__
from bimcloud_backup.client import FOLDER_TYPE, ROOT_ID, ManagerClient
from bimcloud_backup.config import (
    MAX_PARALLEL_DOWNLOADS,
    PROJECTS_BIMPROJECT,
    PROJECTS_BOTH,
    PROJECTS_PLN,
    VERSIONING_LATEST,
    Config,
    Selection,
    normalize_selection,
)
from bimcloud_backup.errors import BimcloudError
from bimcloud_backup.exporter import (
    PLN_FORMATS,
    ExportedFile,
    avoid_reserved_name,
    safe_file_name,
)
from bimcloud_backup.failures import (
    KIND_FILE,
    KIND_FOLDER,
    KIND_ITEM,
    KIND_LIBRARY,
    KIND_PROJECT,
    Failure,
    count_failed,
    explain,
    first_line,
    reason,
)
from bimcloud_backup.i18n import format_count, format_decimal, plural, t
from bimcloud_backup.redaction import redact
from bimcloud_backup.retention import (
    backup_folder_name,
    delete_backups,
    find_expired_backups,
    latest_backup,
)
from bimcloud_backup.state import (
    RESERVED_MANIFEST_NAMES,
    BackupManifest,
    ManifestFile,
    load_manifest,
    save_manifest,
)

BLOB_TYPE = "blob"
PROJECT_TYPES = {"project"}
LIBRARY_TYPES = {"library"}
INCOMPLETE_PREFIX = ".incompleto-"
LOCK_NAME = ".backup.lock"
GB = 1024**3

log = logging.getLogger("bimcloud_backup")


KIND_BIMPROJECT = "bimproject"
KIND_PLN = "pln"
KIND_BIMLIBRARY = "bimlibrary"


class Downloader(Protocol):
    def download(self, blob: dict[str, Any], target: Path, check: Callable[[], None]) -> int: ...


class ProjectExporter(Protocol):
    def export_project(
        self, project_id: str, folder: Path, include_backups: bool, check: Callable[[], None]
    ) -> ExportedFile: ...

    def export_library(
        self, library_id: str, folder: Path, include_backups: bool, check: Callable[[], None]
    ) -> ExportedFile: ...

    def latest_backup(self, resource_id: str, formats: frozenset[str]) -> dict[str, Any] | None: ...

    def download_backup(
        self, backup: dict[str, Any], target: Path, check: Callable[[], None]
    ) -> int: ...

    def watch(self, report: Callable[[str | None], None]) -> None: ...

    def running_exports(self) -> int: ...


class BackupAborted(BimcloudError):
    """The run was stopped before finishing (time limit, disk space...)."""


class BackupCancelled(BackupAborted):
    """The user asked to stop the backup."""


@dataclass
class BackupResult:
    folder: Path | None = None
    files: int = 0
    bytes: int = 0
    pending: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # The same failures as `errors`, with what each item was.
    failures: list[Failure] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    by_type: dict[str, dict[str, int]] = field(default_factory=dict)
    manifest_files: list[ManifestFile] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.folder is not None and not self.errors


# Downloads queued or running at a time, per download thread. The walk waits beyond this, so a
# big tree never piles up thousands of pending downloads in memory.
QUEUED_PER_THREAD = 2
# How often the walk, while waiting for a free slot, checks the limits and the cancel button.
QUEUE_WAIT_SECONDS = 0.5


@dataclass(frozen=True)
class Progress:
    """How far a run is. Totals are what this run will copy, known once the listing ends."""

    listing: bool = True
    exports_done: int = 0
    exports_total: int = 0
    # Project or library being exported now, as its BIMcloud path.
    current: str | None = None
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    # What BIMcloud is doing with that export: "queued on BIMcloud (2 min)".
    detail: str | None = None


ProgressCallback = Callable[[Progress], None]
# Files finish many times a second with parallel downloads; the window needs far fewer updates.
PROGRESS_INTERVAL_SECONDS = 0.25
# The log and the Activity get a line every tenth of the files, not one per file.
FILE_MILESTONES = 10


class _ProgressTracker:
    """Counts what is done, from the walk and from the download threads, and reports it."""

    def __init__(
        self, report: ProgressCallback | None, monotonic: Callable[[], float] = time.monotonic
    ):
        self._report = report
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._sent = 0.0
        self._milestone = 0
        self.state = Progress()
        self._send()

    def start(self, exports: int, files: int, size: int) -> None:
        with self._lock:
            self.state = replace(
                self.state,
                listing=False,
                exports_total=exports,
                files_total=files,
                bytes_total=size,
            )
            self._send()
        parts = []
        if exports:
            parts.append(plural("backup.to_copy.exports", exports))
        if files:
            parts.append(plural("backup.to_copy.files", files, size=format_size(size)))
        what = t("backup.to_copy.and").join(parts) if parts else t("backup.to_copy.nothing")
        log.info(t("backup.to_copy", what=what))

    def export_started(self, kind_label: str, path: str) -> None:
        with self._lock:
            self.state = replace(self.state, current=path)
            number, total = self.state.exports_done + 1, self.state.exports_total
            self._send()
        log.info(t("backup.export_started", kind=kind_label, number=number, total=total, path=path))

    def export_finished(self) -> None:
        with self._lock:
            self.state = replace(
                self.state, exports_done=self.state.exports_done + 1, current=None, detail=None
            )
            self._send()

    def export_detail(self, text: str | None) -> None:
        with self._lock:
            if text != self.state.detail:
                self.state = replace(self.state, detail=text)
                self._send()

    def file_finished(self, size: int) -> None:
        """Called from the download threads too."""
        with self._lock:
            s = self.state
            self.state = replace(s, files_done=s.files_done + 1, bytes_done=s.bytes_done + size)
            s = self.state
            finished = s.files_done == s.files_total
            if finished or self._monotonic() - self._sent >= PROGRESS_INTERVAL_SECONDS:
                self._send()
            milestone = s.files_done * FILE_MILESTONES // max(s.files_total, 1)
            if milestone > self._milestone and s.files_total >= FILE_MILESTONES:
                self._milestone = milestone
                log.info(
                    t(
                        "backup.files_progress",
                        done=format_count(s.files_done),
                        total=format_count(s.files_total),
                        bytes_done=format_size(s.bytes_done),
                        bytes_total=format_size(s.bytes_total),
                    )
                )

    def _send(self) -> None:
        self._sent = self._monotonic()
        if self._report is not None:
            self._report(self.state)


@dataclass
class _Run:
    """Everything the handling of one resource needs during a run."""

    config: Config
    downloader: Downloader
    exporter: ProjectExporter | None
    work: Path
    source_path: str
    result: BackupResult
    check: Callable[[], None]
    previous_folder: Path | None
    previous_files: dict[str, ManifestFile]
    pool: ThreadPoolExecutor | None = None
    # Set when the run stops for any reason: downloads in progress give up at their next block.
    stop: threading.Event = field(default_factory=threading.Event)
    paths: LocalPaths = field(init=False)
    # What each item produced, in the order BIMcloud listed them. Downloads finish in any order,
    # but applying outcomes in this order keeps the manifest and the error list deterministic.
    outcomes: list[Future[_Outcome] | _Outcome] = field(default_factory=list)
    # Downloads submitted and not finished yet.
    in_flight: set[Future[_Outcome]] = field(default_factory=set)
    progress: _ProgressTracker = field(default_factory=lambda: _ProgressTracker(None))
    submitted: int = 0

    def __post_init__(self) -> None:
        self.paths = LocalPaths(self.source_path)

    def add(self, outcome: _Outcome) -> None:
        self.outcomes.append(outcome)

    def submit_download(
        self, resource: dict[str, Any], path: str, target: Path, relative: Path
    ) -> None:
        """Queue one download, first waiting while too many are already queued or running."""
        assert self.pool is not None
        limit = QUEUED_PER_THREAD * self.config.parallel_downloads
        while len(self.in_flight) >= limit:
            done, self.in_flight = wait(
                self.in_flight, timeout=QUEUE_WAIT_SECONDS, return_when=FIRST_COMPLETED
            )
            for future in done:
                if future.exception() is not None:
                    future.result()  # a limit was hit inside a download: stop the run now
            self.check()
        future = self.pool.submit(_download_blob, resource, path, target, relative, self)
        size = _planned_size(resource)

        def finished(done: Future[_Outcome]) -> None:
            # Failures come back as outcomes; an exception means the run stopped (cancel or a
            # limit), and that download never finished.
            if not done.cancelled() and done.exception() is None:
                self.progress.file_finished(size)

        future.add_done_callback(finished)
        self.submitted += 1
        self.in_flight.add(future)
        self.outcomes.append(future)

    def worker_check(self) -> None:
        """The limits check used inside downloads, which also stops them when the run stops."""
        if self.stop.is_set():
            raise BackupAborted(t("backup.interrupted"))
        self.check()


@dataclass(frozen=True)
class _Outcome:
    """What handling one item added to the backup: a file, an error, or nothing."""

    file: ManifestFile | None = None
    failure: Failure | None = None
    kind: str | None = None

    @classmethod
    def failed(cls, path: str, kind: str, message: str) -> _Outcome:
        # The message also goes to .backup.json, which the log redaction does not cover.
        failure = Failure(path, kind, redact(message))
        log_failure(failure)
        return cls(failure=failure)


def log_failure(failure: Failure) -> None:
    """One line in plain words for the log and the activity box, then the technical text.

    The technical line is marked so the window leaves it out; the log file keeps it.
    """
    log.error(t("backup.failure", text=explain(failure)))
    if reason(failure.message) or first_line(failure.message) != failure.message.strip():
        log.info(t("backup.technical_detail", text=failure.text), extra={"technical": True})


def _failure_kind(resource_type: object) -> str:
    if resource_type in PROJECT_TYPES:
        return KIND_PROJECT
    if resource_type in LIBRARY_TYPES:
        return KIND_LIBRARY
    if resource_type == FOLDER_TYPE:
        return KIND_FOLDER
    if resource_type == BLOB_TYPE:
        return KIND_FILE
    return KIND_ITEM


class LocalPaths:
    """Unique local paths for one run.

    Windows ignores case and reserved names get a `_` prefix, so different BIMcloud names can
    land on the same local name ("Obra" and "obra", "CON" and "_CON"). The first one keeps it;
    the next ones get " (2)", " (3)"... before the extension. Folders are mapped too, so what
    is inside a renamed folder follows it. BIMcloud lists folders sorted by name, which keeps
    the choice the same from one run to the next (and the incremental working).
    """

    def __init__(self, root_path: str):
        self._root = root_path
        self._folders: dict[str, Path] = {}
        self._used: set[str] = set()

    def claim(self, resource_path: str, folder: bool) -> Path:
        """Local path (relative to the backup) for a BIMcloud folder or file."""
        if folder and resource_path in self._folders:
            return self._folders[resource_path]
        parent = self.parent_of(resource_path)
        name = _clean_component(resource_path.rpartition("/")[2])
        local = self._unique(parent, name, folder)
        if folder:
            self._folders[resource_path] = local
        return local

    def parent_of(self, resource_path: str) -> Path:
        """Local folder that holds `resource_path`, claiming its parents when needed."""
        parent = resource_path.rpartition("/")[0]
        if not parent or parent == self._root:
            return Path()
        return self.claim(parent, folder=True)

    def unique_file(self, folder: Path, name: str) -> Path:
        """Unique path for a file this program names itself (a server .pln)."""
        return self._unique(folder, name, folder=False)

    def reserve(self, local: Path) -> None:
        """Mark a file that was named by someone else (an export) as used."""
        self._used.add(_path_key(local))

    def _unique(self, parent: Path, name: str, folder: bool) -> Path:
        stem, extension = (name, "") if folder else _split_extension(name)
        candidate = parent / name
        number = 2
        while _path_key(candidate) in self._used:
            candidate = parent / f"{stem} ({number}){extension}"
            number += 1
        self._used.add(_path_key(candidate))
        if candidate.name != name:
            log.warning(
                t(
                    "backup.renamed",
                    name=(parent / name).as_posix(),
                    saved_as=candidate.as_posix(),
                )
            )
        return candidate


def _path_key(path: Path) -> str:
    return path.as_posix().casefold()


def _split_extension(name: str) -> tuple[str, str]:
    stem, dot, extension = name.rpartition(".")
    return (stem, f".{extension}") if dot and stem else (name, "")


def run_backup(
    config: Config,
    client: ManagerClient,
    downloader: Downloader,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    free_space: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
    exporter: ProjectExporter | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    """Run one backup. Without `exporter`, projects and libraries are only listed as pending.

    Setting `cancel` stops the run at the next check (between items and at every downloaded
    block), exactly like the time limit: the incomplete folder is deleted and no old backup is
    removed. `progress` is called, from any thread, as the run moves on.
    """
    root_dir = config.backup_dir
    root_dir.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(root_dir / LOCK_NAME):
        return _run(
            config,
            client,
            downloader,
            exporter,
            now or datetime.now(),
            monotonic,
            free_space,
            cancel,
            progress,
        )


def _run(
    config: Config,
    client: ManagerClient,
    downloader: Downloader,
    exporter: ProjectExporter | None,
    now: datetime,
    monotonic: Callable[[], float],
    free_space: Callable[[Path], int],
    cancel: threading.Event | None,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    started = monotonic()
    root_dir = config.backup_dir
    _remove_leftovers(root_dir)

    def check_limits() -> None:
        if cancel is not None and cancel.is_set():
            raise BackupCancelled(t("backup.cancelled"))
        if config.max_duration_hours and monotonic() - started > config.max_duration_hours * 3600:
            raise BackupAborted(t("backup.time_limit", hours=config.max_duration_hours))
        if config.min_free_space_gb and free_space(root_dir) < config.min_free_space_gb * GB:
            raise BackupAborted(t("backup.low_space", gb=config.min_free_space_gb, folder=root_dir))

    check_limits()
    if not 1 <= config.parallel_downloads <= MAX_PARALLEL_DOWNLOADS:
        raise BimcloudError(
            t("config.between", name="parallel_downloads", low=1, high=MAX_PARALLEL_DOWNLOADS)
        )
    sources = _resolve_sources(client, config.selection, check_limits)
    check_limits()
    source_paths = sources.manifest_paths
    previous_folder, previous_files = _incremental_base(root_dir, config.server_url, source_paths)
    name = backup_folder_name(now)
    work = root_dir / f"{INCOMPLETE_PREFIX}{name}"
    work.mkdir()
    result = BackupResult()
    # Local paths always start at the BIMcloud root, so two chosen folders never collide.
    run = _Run(
        config,
        downloader,
        exporter,
        work,
        sources.root_path,
        result,
        check_limits,
        previous_folder,
        previous_files,
        progress=_ProgressTracker(progress),
    )
    interrupt = _DeferredInterrupt()
    log.info(t("backup.started", sources=", ".join(source_paths), folder=root_dir / name))
    for path, kind, message in sources.missing:
        failure = Failure(f"{sources.root_path}/{path}", kind, message)
        log_failure(failure)
        result.failures.append(failure)
        result.errors.append(failure.text)

    # Only plain files are downloaded in parallel; exports stay one at a time in this thread
    # (the BIMcloud terms allow limits on usage, clause 12.12, so it is never flooded).
    run.pool = ThreadPoolExecutor(config.parallel_downloads, thread_name_prefix="download")
    try:
        # The whole listing comes first: the same calls the copy needs, now made up front, so
        # the totals are known before the first export.
        resources = []
        for source_id, source_path in sources.found:
            if source_id != ROOT_ID:
                # The chosen folder itself (and its parents) exists even when empty.
                with contextlib.suppress(ValueError):
                    (work / run.paths.claim(source_path, folder=True)).mkdir(
                        parents=True, exist_ok=True
                    )
            for resource in client.walk(source_id):
                check_limits()
                resources.append(resource)
        # Projects and libraries chosen one by one, outside the chosen folders, come last.
        chosen = {id(resource) for resource in sources.items}
        resources += sources.items
        _start_progress(resources, run)
        for resource in resources:
            check_limits()
            switched_off = _switched_off(resource, config) if id(resource) in chosen else None
            if switched_off:
                # An error, not a silent skip: a backup of nothing must never pass for a good
                # one, nor let the retention delete the last real backup.
                kind = _failure_kind(resource.get("type"))
                run.add(_Outcome.failed(resource["$path"], kind, switched_off))
            else:
                _handle_counted(resource, run)
        _collect(run)
        run.pool.shutdown(wait=True)
        # Last chance to cancel: from here on the backup is made final and old ones are
        # removed, and none of that is interrupted any more.
        if cancel is not None and cancel.is_set():
            raise BackupCancelled(t("backup.cancelled"))
        interrupt.defer()
        save_manifest(
            BackupManifest(
                created_at=now.isoformat(),
                version=__version__,
                server_url=config.server_url,
                source_path=source_paths,
                by_type=result.by_type,
                files=result.manifest_files,
                errors=result.errors,
                failures=result.failures,
            ),
            work,
        )
        final = root_dir / name
        work.rename(final)
        result.folder = final
    except BaseException:
        interrupt.restore()
        # Downloads still running stop at their next block; wait for them before deleting.
        run.stop.set()
        run.pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(work, ignore_errors=True)
        raise

    try:
        log.info(
            t(
                "backup.finished",
                files=format_count(result.files),
                size=format_size(result.bytes),
                folder=result.folder,
            )
        )
        if result.errors:
            log.warning(t("backup.retention_skipped", failed=count_failed(len(result.errors))))
        else:
            result.removed = _apply_retention(config, now)
    finally:
        interrupt.restore()
    if interrupt.received:
        log.warning(t("backup.late_interrupt"))
    return result


class _DeferredInterrupt:
    """Holds Ctrl+C back while the backup is made final and old backups are removed.

    Stopping in the middle would leave a finished backup reported as cancelled, or old
    backups deleted while the program says nothing was removed. Signals only reach the main
    thread, so in the GUI's worker thread there is nothing to hold back.
    """

    def __init__(self) -> None:
        self.received = False
        self._previous: Any = None
        self._active = False

    def defer(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        self._previous = signal.signal(signal.SIGINT, self._handle)
        self._active = True

    def restore(self) -> None:
        if self._active:
            signal.signal(signal.SIGINT, self._previous)
            self._active = False

    def _handle(self, signum: int, frame: Any) -> None:
        self.received = True


def _exported(resource: dict[str, Any], run: _Run) -> bool:
    """Whether this run exports (or downloads the .pln of) `resource`."""
    kind = resource.get("type")
    if run.exporter is None:
        return False
    return (kind in PROJECT_TYPES and run.config.include_projects) or (
        kind in LIBRARY_TYPES and run.config.include_libraries
    )


def _copied_file(resource: dict[str, Any], run: _Run) -> bool:
    return resource.get("type") == BLOB_TYPE and run.config.include_files


def _planned_size(resource: dict[str, Any]) -> int:
    size = resource.get("$size")
    return size if isinstance(size, int) and not isinstance(size, bool) else 0


def _start_progress(resources: list[dict[str, Any]], run: _Run) -> None:
    files = [r for r in resources if _copied_file(r, run)]
    exports = sum(1 for r in resources if _exported(r, run))
    run.progress.start(exports, len(files), sum(_planned_size(r) for r in files))
    if exports and run.exporter is not None:
        run.exporter.watch(run.progress.export_detail)
        _warn_about_running_exports(run.exporter)


def _warn_about_running_exports(exporter: ProjectExporter) -> None:
    """Exports still running on BIMcloud (a cancelled backup, the web page) go first."""
    try:
        running = exporter.running_exports()
    except (BimcloudError, OSError) as e:
        log.debug(t("backup.running_exports_unknown", error=redact(str(e))))
        return
    if running:
        log.warning(plural("backup.running_exports", running))


def _handle_counted(resource: dict[str, Any], run: _Run) -> None:
    """Handle one resource and count it as done, even when it fails or is skipped."""
    if _exported(resource, run):
        label = t("kind.project" if resource.get("type") in PROJECT_TYPES else "kind.library")
        run.progress.export_started(label, resource.get("$path", resource.get("name", "?")))
        _handle(resource, run)
        run.progress.export_finished()
    elif _copied_file(resource, run):
        submitted = run.submitted
        _handle(resource, run)
        if run.submitted == submitted:  # refused before downloading: done all the same
            run.progress.file_finished(_planned_size(resource))
    else:
        _handle(resource, run)


def _handle(resource: dict[str, Any], run: _Run) -> None:
    config, work, result = run.config, run.work, run.result
    kind = resource.get("type")
    path = resource.get("$path", resource.get("name", "?"))
    try:
        if kind in (FOLDER_TYPE, BLOB_TYPE):
            if kind == BLOB_TYPE and not config.include_files:
                return
            target = work / run.paths.claim(path, folder=kind == FOLDER_TYPE)
        else:
            # Projects and libraries are not written under their own name: their export goes
            # into the folder that holds them.
            target = work / run.paths.parent_of(path)
    except ValueError as e:
        run.add(_Outcome.failed(path, _failure_kind(kind), t("backup.path_ignored", reason=e)))
        return

    if kind == FOLDER_TYPE:
        if _reject_reserved_manifest_path(target.relative_to(work), path, KIND_FOLDER, run):
            return
        _count_resource(result, kind, resource)
        target.mkdir(parents=True, exist_ok=True)
    elif kind == BLOB_TYPE:
        relative = target.relative_to(work)
        if _reject_reserved_manifest_path(relative, path, KIND_FILE, run):
            return
        run.submit_download(resource, path, target, relative)
    elif kind in PROJECT_TYPES or kind in LIBRARY_TYPES:
        wanted = config.include_projects if kind in PROJECT_TYPES else config.include_libraries
        if not wanted:
            return
        exporter = run.exporter
        if exporter is None:
            result.pending.append(path)
            log.warning(t("backup.export_unavailable", kind=kind, path=path))
        elif kind in PROJECT_TYPES:
            _save_project(resource, path, target, exporter, run)
        else:
            _record_export(
                path,
                KIND_BIMLIBRARY,
                lambda: exporter.export_library(
                    resource["id"], target, config.include_backups_in_library_export, run.check
                ),
                run,
            )
    else:
        log.debug(t("backup.type_ignored", kind=kind, path=path))


def _save_project(
    resource: dict[str, Any], path: str, folder: Path, exporter: ProjectExporter, run: _Run
) -> None:
    """Save a Teamwork project as .BIMProject, as the latest server .pln, or both."""
    config = run.config
    fmt = config.projects_format
    export = fmt in (PROJECTS_BIMPROJECT, PROJECTS_BOTH)
    if fmt in (PROJECTS_PLN, PROJECTS_BOTH) and not _save_latest_pln(
        resource, path, folder, exporter, run
    ):
        if fmt == PROJECTS_PLN:
            log.warning(t("backup.no_pln_export", path=path))
            export = True
        else:
            log.warning(t("backup.no_pln", path=path))
    if export:
        _record_export(
            path,
            KIND_BIMPROJECT,
            lambda: exporter.export_project(
                resource["id"], folder, config.include_backups_in_export, run.check
            ),
            run,
        )


def _save_latest_pln(
    resource: dict[str, Any], path: str, folder: Path, exporter: ProjectExporter, run: _Run
) -> bool:
    """Download the newest finished .pln the server made; False when there is none."""
    try:
        backup = exporter.latest_backup(resource["id"], PLN_FORMATS)
    except BackupAborted:
        raise
    except (BimcloudError, OSError) as e:
        run.add(_Outcome.failed(path, KIND_PROJECT, t("backup.server_backups_failed", error=e)))
        return False
    if backup is None:
        return False
    name = _pln_file_name(backup, folder.name)
    target = run.work / run.paths.unique_file(folder.relative_to(run.work), name)
    # A server backup never changes, so the one already saved last time is reused locally.
    metadata = {"$size": backup["$fileSize"], "$modifiedDate": backup["$time"]}
    relative = target.relative_to(run.work).as_posix()
    previous = run.previous_files.get(relative)
    source = _BackupDownload(exporter, backup, run.check)

    def download() -> ExportedFile:
        if (
            run.previous_folder is not None
            and previous is not None
            and previous.kind == KIND_PLN
            and same_file(metadata, previous)
        ):
            size = _reuse_file(run.previous_folder / relative, target, metadata, source, run.check)
        else:
            size = source.download(metadata, target, run.check)
        return ExportedFile(target, size)

    log.info(t("backup.downloading_pln", path=path))
    _record_export(path, KIND_PLN, download, run, modified_date=backup["$time"])
    return True


def _pln_file_name(backup: dict[str, Any], project_name: str) -> str:
    try:
        name = safe_file_name(backup.get("$backupFileName") or "")
    except BimcloudError:
        name = f"{project_name}.pln"
    return name if name.lower().endswith(".pln") else f"{name}.pln"


class _BackupDownload:
    """Adapts a server backup download to the `Downloader` protocol used by `_reuse_file`."""

    def __init__(
        self, exporter: ProjectExporter, backup: dict[str, Any], check: Callable[[], None]
    ):
        self._exporter = exporter
        self._backup = backup
        self._check = check

    def download(self, blob: dict[str, Any], target: Path, check: Callable[[], None]) -> int:
        # A failed local copy may have left part of the file behind.
        target.unlink(missing_ok=True)
        return self._exporter.download_backup(self._backup, target, self._check)


def _record_export(
    path: str,
    kind: str,
    action: Callable[[], ExportedFile],
    run: _Run,
    modified_date: int | None = None,
) -> None:
    try:
        exported = action()
    except BackupAborted:
        raise
    except (BimcloudError, OSError) as e:
        failure_kind = KIND_LIBRARY if kind == KIND_BIMLIBRARY else KIND_PROJECT
        run.add(_Outcome.failed(path, failure_kind, str(e)))
        return
    run.paths.reserve(exported.path.relative_to(run.work))
    relative = exported.path.relative_to(run.work).as_posix()
    file = ManifestFile(relative, exported.size, modified_date, kind)
    run.add(_Outcome(file=file, kind=kind))
    log.info(t("backup.saved", path=relative, size=format_decimal(exported.size / 1024**2)))


def _download_blob(
    resource: dict[str, Any], path: str, target: Path, relative: Path, run: _Run
) -> _Outcome:
    """Runs in a download thread: fetch one file (or reuse it) and say what it added."""
    manifest_path = relative.as_posix()
    try:
        previous = run.previous_files.get(manifest_path)
        if run.previous_folder is not None and previous and same_file(resource, previous):
            size = _reuse_file(
                run.previous_folder / Path(manifest_path),
                target,
                resource,
                run.downloader,
                run.worker_check,
            )
        else:
            size = run.downloader.download(resource, target, run.worker_check)
    except BackupAborted:
        raise
    except (BimcloudError, OSError) as e:
        return _Outcome.failed(path, KIND_FILE, str(e))
    log.debug(t("backup.included", path=path, size=size))
    return _Outcome(
        file=ManifestFile(manifest_path, size, _modified_date(resource)), kind=BLOB_TYPE
    )


def _collect(run: _Run) -> None:
    """Wait for the downloads and apply every outcome, in the order BIMcloud listed the items."""
    result = run.result
    for item in run.outcomes:
        outcome = item.result() if isinstance(item, Future) else item
        if outcome.failure is not None and outcome.failure not in result.failures:
            result.failures.append(outcome.failure)
            result.errors.append(outcome.failure.text)
        if outcome.file is not None:
            result.files += 1
            result.bytes += outcome.file.size
            totals = result.by_type.setdefault(outcome.kind or BLOB_TYPE, {"count": 0, "bytes": 0})
            totals["count"] += 1
            totals["bytes"] += outcome.file.size
            result.manifest_files.append(outcome.file)
    run.outcomes.clear()
    run.in_flight.clear()


def same_file(resource: dict[str, Any], previous: ManifestFile) -> bool:
    """Compare BIMcloud metadata in one place so real API differences are easy to adjust."""
    size = resource.get("$size")
    modified_date = resource.get("$modifiedDate")
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and isinstance(modified_date, int)
        and not isinstance(modified_date, bool)
        and size == previous.size
        and modified_date == previous.modified_date
    )


def _modified_date(resource: dict[str, Any]) -> int | None:
    value = resource.get("$modifiedDate")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _reject_reserved_manifest_path(
    relative: Path, resource_path: str, kind: str, run: _Run
) -> bool:
    if relative.parts[0].casefold() not in RESERVED_MANIFEST_NAMES:
        return False
    message = t("backup.path_ignored", reason=t("backup.reserved_name"))
    run.add(_Outcome.failed(resource_path, kind, message))
    return True


def _incremental_base(
    root_dir: Path, server_url: str, source_path: list[str]
) -> tuple[Path | None, dict[str, ManifestFile]]:
    folder = latest_backup(root_dir)
    if folder is None:
        return None, {}
    manifest = load_manifest(folder)
    if manifest is None:
        log.warning(t("backup.previous_manifest_invalid", folder=folder))
        return None, {}
    if manifest.server_url != server_url or manifest.source_path != source_path:
        log.info(t("backup.previous_source_differs"))
        return None, {}
    return folder, {item.path: item for item in manifest.files}


def _reuse_file(
    source: Path,
    target: Path,
    resource: dict[str, Any],
    downloader: Downloader,
    check: Callable[[], None],
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if source.stat().st_size != resource["$size"]:
            raise OSError("local size differs from the manifest")
    except OSError as source_error:
        log.warning(t("backup.previous_file_unavailable", path=target, error=source_error))
        return downloader.download(resource, target, check)
    try:
        os.link(source, target)
        log.debug(t("backup.hardlinked", path=target))
        return resource["$size"]
    except OSError as link_error:
        log.warning(t("backup.hardlink_failed", path=target, error=link_error))
    try:
        shutil.copy2(source, target)
        log.debug(t("backup.copied_locally", path=target))
        return resource["$size"]
    except OSError as copy_error:
        log.warning(t("backup.local_copy_failed", path=target, error=copy_error))
        return downloader.download(resource, target, check)


def format_size(size: int) -> str:
    """Bytes as the interface shows them: "18.6 GB", "512.0 MB" ("18,6 GB" in Portuguese)."""
    value, unit = (size / 1024**3, "GB") if size >= 1024**3 else (size / 1024**2, "MB")
    return f"{format_decimal(value)} {unit}"


def _count_resource(result: BackupResult, kind: str, resource: dict[str, Any]) -> None:
    totals = result.by_type.setdefault(kind, {"count": 0, "bytes": 0})
    totals["count"] += 1
    size = resource.get("$size", 0)
    if isinstance(size, int) and not isinstance(size, bool):
        totals["bytes"] += size


@dataclass(frozen=True)
class _Sources:
    root_path: str
    # (resource id, BIMcloud path) of every chosen folder that exists.
    found: list[tuple[str, str]]
    # (path from the root, failure kind, message) of what was chosen and BIMcloud no longer has.
    missing: list[tuple[str, str, str]]
    # Chosen projects and libraries that exist, as BIMcloud lists them.
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def manifest_paths(self) -> list[str]:
        """Sorted BIMcloud paths of what was asked for, found or not."""
        if not self.found and not self.missing and not self.items:
            return [self.root_path]
        paths = [path for _, path in self.found]
        paths += [item["$path"] for item in self.items]
        paths += [f"{self.root_path}/{path}" for path, _, _ in self.missing]
        return sorted(paths)


# How the type of a resource is named in messages: "is no longer a folder (now a project)".
TYPE_KEYS = {FOLDER_TYPE: "folder", BLOB_TYPE: "file"}
TYPE_KEYS.update(dict.fromkeys(PROJECT_TYPES, "project"))
TYPE_KEYS.update(dict.fromkeys(LIBRARY_TYPES, "library"))


def _switched_off(resource: dict[str, Any], config: Config) -> str | None:
    """Why a chosen project or library will not be copied, when its kind is switched off."""
    kind = resource.get("type")
    if kind in PROJECT_TYPES and not config.include_projects:
        return t("backup.projects_off")
    if kind in LIBRARY_TYPES and not config.include_libraries:
        return t("backup.libraries_off")
    return None


def _wrong_type(expected: str, found: dict[str, Any]) -> str:
    was = t(f"backup.no_longer.{TYPE_KEYS.get(expected, 'item')}")
    now = t(f"backup.type.{TYPE_KEYS.get(found.get('type'), 'other')}")
    return t("backup.wrong_type", was=was, now=now)


def _resolve_sources(
    client: ManagerClient, selection: Selection, check: Callable[[], None]
) -> _Sources:
    root = client.get_resource(ROOT_ID)
    root_path = root.get("$path", "Project Root")
    selection = normalize_selection(selection.folders, selection.projects, selection.libraries)
    if selection.everything:
        return _Sources(root_path, [(ROOT_ID, root_path)], [])
    sources = _Sources(root_path, [], [])
    wanted = [
        *((folder, (FOLDER_TYPE,), t("backup.missing_folder")) for folder in selection.folders),
        *((project, PROJECT_TYPES, t("backup.missing_project")) for project in selection.projects),
        *((library, LIBRARY_TYPES, t("backup.missing_library")) for library in selection.libraries),
    ]
    for relative, types, missing in wanted:
        kind = _failure_kind(next(iter(types)))
        # Many folders or a slow BIMcloud must not get past the time limit.
        check()
        path = f"{root_path}/{relative}"
        resource = client.get_resource_by_path(path)
        if not resource:
            sources.missing.append((relative, kind, missing))
        elif resource.get("type") not in types:
            sources.missing.append((relative, kind, _wrong_type(next(iter(types)), resource)))
        elif resource["type"] == FOLDER_TYPE:
            sources.found.append((resource["id"], resource.get("$path", path)))
        else:
            sources.items.append({**resource, "$path": resource.get("$path", path)})
    return sources


def relative_path(resource_path: str, source_path: str) -> Path:
    """Local relative path of a resource, refusing anything that could escape the backup."""
    prefix = source_path.rstrip("/") + "/"
    rel = resource_path[len(prefix) :] if resource_path.startswith(prefix) else resource_path
    return Path(*(_clean_component(part) for part in rel.split("/")))


def _clean_component(part: str) -> str:
    """One safe local name: no path tricks, no forbidden characters, no Windows device name."""
    # Windows silently drops trailing dots and spaces.
    clean = part.strip().rstrip(".")
    if not clean or clean in (".", ".."):
        raise ValueError(t("backup.invalid_name"))
    if any(c in clean for c in '<>:"\\|?*') or any(ord(c) < 32 for c in clean):
        raise ValueError(t("backup.invalid_characters"))
    return avoid_reserved_name(clean)


def backup_folder_lock(backup_dir: Path) -> contextlib.AbstractContextManager[None]:
    """The lock a backup holds on its destination; `prune --apply` takes it too."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    return _exclusive_lock(backup_dir / LOCK_NAME)


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Fail fast when another backup (scheduled or manual) is already running."""
    f = path.open("a+")
    try:
        try:
            _lock_file(f)
        except OSError as e:
            raise BackupAborted(t("backup.already_running")) from e
        yield
    finally:
        f.close()


def _lock_file(f: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _remove_leftovers(root_dir: Path) -> None:
    """Delete incomplete folders left behind by a run that was killed."""
    for entry in root_dir.glob(f"{INCOMPLETE_PREFIX}*"):
        if entry.is_dir():
            log.info(t("backup.removing_incomplete", name=entry.name))
            shutil.rmtree(entry, ignore_errors=True)


def _apply_retention(config: Config, now: datetime) -> list[Path]:
    if config.versioning == VERSIONING_LATEST:
        expired = find_expired_backups(config.backup_dir, 0, 1, now)
    else:
        expired = find_expired_backups(
            config.backup_dir, config.retention_days, config.min_backups_to_keep, now
        )
    if expired:
        log.info(plural("backup.removing_old", len(expired)))
        delete_backups(expired)
    return expired
