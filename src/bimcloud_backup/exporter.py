"""Export of Teamwork projects (.BIMProject) and libraries (.BIMLibrary), and download of the
backups the BIMcloud server keeps (such as .pln files).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from bimcloud_backup.blobserver import candidate_urls
from bimcloud_backup.client import ManagerClient
from bimcloud_backup.errors import ApiError, BimcloudError
from bimcloud_backup.i18n import t
from bimcloud_backup.redaction import redact
from bimcloud_backup.urls import https_origin, is_trusted_host

# Endpoints under management/latest; abort-job and get-job are under management/client.
MANAGER_ROOT = "management/latest"
PUBLIC_ROOT = "management/client"
EXPORT_PROJECT = "export-project"
EXPORT_LIBRARY = "export-library"
JOBS_BY_CRITERION = "get-jobs-by-criterion"
PUBLIC_GET_JOB = "get-job"
# POST abort-job?job-id=..., answers true/false.
PUBLIC_ABORT_JOB = "abort-job"
BACKUPS_BY_CRITERION = "get-resource-backups-by-criterion"
DOWNLOAD_BACKUP = "download-backup"

JOB_COMPLETED = "completed"
FINAL_JOB_STATUSES = frozenset({JOB_COMPLETED, "failed", "aborted", "abort failed"})
POLL_SECONDS = 3.0
JOB_TIMEOUT_SECONDS = 2 * 3600
# A job with no change of status or progress for this long is given up (config.toml can change it).
STALL_SECONDS = 20 * 60
# While a job is waited for, the log gets its status on every change and at least this often.
STATUS_LOG_SECONDS = 5 * 60
EXPORT_JOB_TYPES = ("exportProject", "exportLibrary")
RUNNING_JOB_STATUSES = ("starting", "running")

FORMAT_PREFIX = "_server.backup.format."
PLN_FORMATS = frozenset({f"{FORMAT_PREFIX}pln", f"{FORMAT_PREFIX}pln-automatic"})
BACKUP_DONE = "_server.backup.status.done"
BACKUP_PAGE_SIZE = 50
MAX_BACKUP_PAGES = 100

PROJECT_EXTENSION = re.compile(r"\.BIMProject\d*$", re.IGNORECASE)
LIBRARY_EXTENSION = re.compile(r"\.BIMLibrary$", re.IGNORECASE)
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in (*"123456789", "¹", "²", "³")}
    | {f"LPT{i}" for i in (*"123456789", "¹", "²", "³")}
)

CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT = (10, 300)

log = logging.getLogger("bimcloud_backup")


class ExportError(BimcloudError):
    """An export job or a download from BIMcloud failed."""


@dataclass(frozen=True)
class ExportedFile:
    path: Path
    size: int


Check = Callable[[], None]


def _no_check() -> None:
    pass


class Exporter:
    def __init__(
        self,
        client: ManagerClient,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = POLL_SECONDS,
        job_timeout_seconds: float = JOB_TIMEOUT_SECONDS,
        stall_seconds: float = STALL_SECONDS,
    ):
        self._client = client
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_seconds = poll_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._stall_seconds = stall_seconds
        self._public_get_job = False
        self._report: Callable[[str | None], None] = lambda _text: None

    def watch(self, report: Callable[[str | None], None]) -> None:
        """`report` gets, while an export waits for BIMcloud, what the job is doing (None after)."""
        self._report = report

    def running_exports(self) -> int:
        """Exports of this user still running on BIMcloud (an earlier backup, the web page...)."""
        jobs = self._call(
            "POST",
            JOBS_BY_CRITERION,
            json={
                "$and": [
                    {"$eq": {"jobType": list(EXPORT_JOB_TYPES)}},
                    {"$eq": {"status": list(RUNNING_JOB_STATUSES)}},
                ]
            },
        )
        user = self._client.user_id
        jobs = jobs if isinstance(jobs, list) else []
        return sum(
            1
            for job in jobs
            if isinstance(job, dict)
            and job.get("status") in RUNNING_JOB_STATUSES
            and user in (job.get("authorId"), (job.get("data") or {}).get("userId"))
        )

    def export_project(
        self, project_id: str, folder: Path, include_backups: bool = False, check: Check = _no_check
    ) -> ExportedFile:
        """Export a Teamwork project into `folder` as `<name>.BIMProject<version>`."""
        params = {"project-id": project_id, **self._export_options(include_backups)}
        return self._export(EXPORT_PROJECT, params, folder, PROJECT_EXTENSION, check)

    def export_library(
        self,
        library_id: str,
        folder: Path,
        include_backups: bool = False,
        check: Check = _no_check,
    ) -> ExportedFile:
        """Export a server library into `folder` as `<name>.BIMLibrary`."""
        params = {"library-id": library_id, **self._export_options(include_backups)}
        return self._export(EXPORT_LIBRARY, params, folder, LIBRARY_EXTENSION, check)

    def latest_backup(self, resource_id: str, formats: Collection[str]) -> dict[str, Any] | None:
        """Newest finished server backup of `resource_id` in one of `formats`, if any.

        Every page is read: pending backups and other formats may fill the first ones.
        """
        ready: list[dict[str, Any]] = []
        for page_number in range(MAX_BACKUP_PAGES):
            page = self._call(
                "POST",
                BACKUPS_BY_CRITERION,
                params={
                    "skip": page_number * BACKUP_PAGE_SIZE,
                    "limit": BACKUP_PAGE_SIZE,
                    "sort-by": "$time",
                    "sort-direction": "desc",
                },
                json={"ids": [resource_id], "criterion": {}},
            )
            page = page if isinstance(page, list) else []
            ready.extend(b for b in page if isinstance(b, dict) and _is_ready(b, formats))
            if len(page) < BACKUP_PAGE_SIZE:
                break
        else:
            log.warning(t("export.backups_truncated", pages=MAX_BACKUP_PAGES))
        return max(ready, key=lambda b: b["$time"], default=None)

    def download_backup(
        self, backup: dict[str, Any], target: Path, check: Check = _no_check
    ) -> int:
        """Download a server backup (from `latest_backup`) to `target`."""
        url = f"{self._client.server_url}/{MANAGER_ROOT}/{DOWNLOAD_BACKUP}"
        params = {"backup-id": backup["id"], "resource-id": backup["$resourceId"]}
        # BIMcloud answers with a redirect to where the file is; only BIMcloud addresses pass.
        trusted = self._trust(backup.get("$serverId"))
        return self._download(
            url, params, target, check, expected=backup.get("$fileSize"), trusted=trusted
        )

    # ------------------------------------------------------------------ jobs

    def _export_options(self, include_backups: bool) -> dict[str, str]:
        flag = "true" if include_backups else "false"
        return {
            "url-root": self._client.server_url,
            "include-automatic-backups": flag,
            "include-manual-backups": flag,
        }

    def _export(
        self,
        endpoint: str,
        params: dict[str, str],
        folder: Path,
        extension: re.Pattern[str],
        check: Check,
    ) -> ExportedFile:
        job = self._call("GET", endpoint, params=params)
        job_id = job.get("id") if isinstance(job, dict) else None
        if not job_id:
            raise ExportError(t("export.not_started"))
        job = self._wait(job_id, check)
        url = _job_property(job, "absoluteUrl")
        if not url:
            raise ExportError(t("export.no_url"))
        name = file_name_from_url(url)
        if not extension.search(name):
            raise ExportError(t("export.unexpected_file", name=name))
        # The download carries the access token, so it only goes to a BIMcloud address.
        data = job.get("data") if isinstance(job.get("data"), dict) else {}
        trusted = self._trust(data.get("modelServerId") or _job_property(job, "modelServerId"))
        origin = https_origin(url)
        if origin is None:
            raise ExportError(t("export.url_not_https"))
        if not trusted(url):
            raise ExportError(t("export.url_not_bimcloud", host=origin[0]))
        target = folder / name
        size = self._download(url, None, target, check, trusted=trusted)
        return ExportedFile(target, size)

    def _trust(self, server_id: object) -> Callable[[str], bool]:
        """Whether a download address belongs to BIMcloud: the server, its SaaS data server, or
        the exact https connectionUrls of the data server `server_id` (looked up once)."""
        origins: list[set[tuple[str, int]]] = []

        def trusted(url: str) -> bool:
            origin = https_origin(url)
            if origin is None:
                return False
            if is_trusted_host(origin[0], self._client.server_url):
                return True
            if not origins:
                origins.append(self._model_server_origins(server_id))
            return origin in origins[0]

        return trusted

    def _model_server_origins(self, server_id: object) -> set[tuple[str, int]]:
        """Exact https (host, port) pairs in the connectionUrls of a data server."""
        if not isinstance(server_id, str) or not server_id:
            return set()
        try:
            server = self._client.get_resource(server_id)
        except (BimcloudError, requests.RequestException) as e:
            log.warning(t("export.data_server_failed", error=redact(str(e))))
            return set()
        manager = urlparse(self._client.server_url)
        urls = candidate_urls(server or {}, manager.scheme, manager.hostname or "")
        return {origin for u in urls if (origin := https_origin(u)) is not None}

    def _wait(self, job_id: str, check: Check) -> dict[str, Any]:
        """Wait for the job, telling what it does. If the wait stops for any other reason than
        the job ending (cancel, a limit, no progress), the job is aborted on the server too,
        so it does not keep running there and hold up the next exports."""
        watcher = _JobWatcher(self._monotonic())
        try:
            while True:
                check()
                now = self._monotonic()
                if now - watcher.started > self._job_timeout_seconds:
                    minutes = self._job_timeout_seconds / 60
                    raise ExportError(t("export.timeout", minutes=f"{minutes:.0f}"))
                if now - watcher.changed > self._stall_seconds:
                    minutes = self._stall_seconds / 60
                    raise ExportError(t("export.stalled", minutes=f"{minutes:.0f}"))
                self._sleep(self._poll_seconds)
                job = self._get_job(job_id)
                status = job.get("status") if job else None
                if status in FINAL_JOB_STATUSES:
                    watcher.ended = True
                    if status == JOB_COMPLETED:
                        return job
                    detail = job.get("result")
                    message = t("export.final_status", status=repr(status))
                    raise ExportError(f"{message}: {detail}" if detail else message)
                self._report(watcher.update(job, self._monotonic()))
        except BaseException:
            if not watcher.ended:
                self._abort(job_id)
            raise
        finally:
            self._report(None)

    def _abort(self, job_id: str) -> None:
        """Ask BIMcloud to stop the job. Never raises: the backup is stopping anyway."""
        try:
            aborted = self._call(
                "POST", PUBLIC_ABORT_JOB, api_root=PUBLIC_ROOT, params={"job-id": job_id}
            )
        except BimcloudError as e:
            log.warning(t("export.abort_failed", error=e))
            return
        if aborted is True:
            log.info(t("export.aborted"))
        else:
            log.warning(t("export.not_aborted"))

    def _get_job(self, job_id: str) -> dict[str, Any] | None:
        """The job, or None while BIMcloud does not list it yet.

        Falls back to get-job when get-jobs-by-criterion does not exist on the server.
        """
        if not self._public_get_job:
            try:
                jobs = self._call("POST", JOBS_BY_CRITERION, json={"$eq": {"id": [job_id]}})
            except ApiError as e:
                if e.status != 404:
                    raise
                log.info("get-jobs-by-criterion not available; using get-job")
                self._public_get_job = True
            else:
                return next((j for j in jobs or [] if j.get("id") == job_id), None)
        return self._call("GET", PUBLIC_GET_JOB, api_root=PUBLIC_ROOT, params={"job-id": job_id})

    def _call(self, method: str, endpoint: str, api_root: str = MANAGER_ROOT, **kwargs: Any) -> Any:
        try:
            return self._client.request(method, endpoint, api_root=api_root, **kwargs)
        except requests.RequestException as e:
            raise ExportError(t("export.connection_failed", error=redact(str(e)))) from None

    # -------------------------------------------------------------- download

    def _download(
        self,
        url: str,
        params: dict[str, str] | None,
        target: Path,
        check: Check,
        expected: Any = None,
        trusted: Callable[[str], bool] | None = None,
    ) -> int:
        """Stream `url` to `target` through a temporary `.part` file renamed at the end."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ExportError(t("export.file_exists", name=target.name))
        partial = target.with_name(target.name + ".part")
        try:
            written = self._stream(url, params, partial, check, trusted)
            if isinstance(expected, int) and expected >= 0 and written != expected:
                raise ExportError(
                    t("download.incomplete_file", name=target.name, written=written, total=expected)
                )
            partial.replace(target)
        except requests.RequestException as e:
            partial.unlink(missing_ok=True)
            raise ExportError(
                t("export.download_failed", name=target.name, error=redact(str(e)))
            ) from None
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return written

    def _stream(
        self,
        url: str,
        params: dict[str, str] | None,
        partial: Path,
        check: Check,
        trusted: Callable[[str], bool] | None = None,
    ) -> int:
        response = self._client.open_download(
            url, params, timeout=DOWNLOAD_TIMEOUT, trusted=trusted
        )
        with response:
            length = response.headers.get("Content-Length")
            written = 0
            with partial.open("wb") as f:
                for chunk in response.iter_content(CHUNK_SIZE):
                    check()
                    f.write(chunk)
                    written += len(chunk)
        if length and length.isdigit() and int(length) != written:
            raise ExportError(t("download.incomplete", written=written, total=length))
        return written


class _JobWatcher:
    """What a waited-for job is doing, when it last changed, and when to log it."""

    def __init__(self, started: float):
        self.started = started
        self.changed = started
        self.ended = False
        self._key: tuple[Any, ...] | None = None
        self._logged = started

    def update(self, job: dict[str, Any] | None, now: float) -> str:
        progress = job.get("progress") if job else None
        progress = progress if isinstance(progress, dict) else {}
        key = (
            job.get("status") if job else None,
            progress.get("current"),
            progress.get("max"),
            progress.get("phase"),
            job.get("progressedOn") if job else None,
        )
        elapsed = now - self.started
        if key != self._key or now - self._logged >= STATUS_LOG_SECONDS:
            if key != self._key:
                self.changed = now
            self._key = key
            self._logged = now
            log.info(t("export.status", text=describe_job(job, elapsed)))
        return describe_job(job, elapsed, short=True)


def describe_job(job: dict[str, Any] | None, elapsed: float, short: bool = False) -> str:
    """What the job is doing: 'BIMcloud is preparing the file (step 1 of 3, 2 min)' for the
    log, or (`short`) 'preparing, step 1 of 3 · 2 min', which fits one line of the window."""
    status = job.get("status") if job else None
    if status in (None, "starting"):
        state = "queued"
    elif status == "aborting":
        state = "aborting"
    else:
        state = "preparing"
    details = []
    progress = job.get("progress") if job else None
    if isinstance(progress, dict):
        current, top = progress.get("current"), progress.get("max")
        if isinstance(current, int) and isinstance(top, int) and top > 0 and status == "running":
            details.append(t("export.step", number=min(current + 1, top), total=top))
        phase = progress.get("phase")
        if not short and isinstance(phase, str) and phase.strip():
            details.append(phase.strip())
    minutes = t("export.under_a_minute") if elapsed < 60 else f"{int(elapsed // 60)} min"
    if short:
        return " · ".join((", ".join((t(f"export.short.{state}"), *details)), minutes))
    return f"{t(f'export.long.{state}')} ({', '.join((*details, minutes))})"


def _is_ready(backup: dict[str, Any], formats: Collection[str]) -> bool:
    size, when = backup.get("$fileSize"), backup.get("$time")
    return (
        backup.get("$formatId") in formats
        and backup.get("$statusId") == BACKUP_DONE
        and isinstance(size, int)
        and size >= 0
        and isinstance(when, int)
        and bool(backup.get("id"))
        and bool(backup.get("$resourceId"))
    )


def _job_property(job: dict[str, Any], name: str) -> str | None:
    for prop in job.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name") == name:
            value = prop.get("value")
            return value if isinstance(value, str) else None
    return None


def file_name_from_url(url: str) -> str:
    """The `file-name` of an export download URL, made safe to use as a Windows file name."""
    names = parse_qs(urlparse(url).query).get("file-name")
    if not names:
        raise ExportError(t("export.url_without_name"))
    return safe_file_name(names[0])


def safe_file_name(name: str) -> str:
    """Keep only the last path component and drop characters Windows does not accept."""
    name = re.split(r"[\\/]", name)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        raise ExportError(t("export.invalid_file_name"))
    return avoid_reserved_name(name)


def avoid_reserved_name(name: str) -> str:
    """Prefix `_` to Windows device names (CON, NUL, COM1...), which stay reserved with an
    extension too: `NUL.txt` would be written to the null device."""
    if name.split(".")[0].strip().upper() in WINDOWS_RESERVED_NAMES:
        return f"_{name}"
    return name
