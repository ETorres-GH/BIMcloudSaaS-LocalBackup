"""High level operations shared by the command line and the GUI."""

from __future__ import annotations

import contextlib
import logging
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

from bimcloud_backup.auth import Tokens, TokenStore, login, refresh
from bimcloud_backup.backup import (
    LIBRARY_TYPES,
    PROJECT_TYPES,
    BackupCancelled,
    BackupResult,
    ProgressCallback,
    run_backup,
)
from bimcloud_backup.blobserver import BlobDownloader
from bimcloud_backup.client import FOLDER_TYPE, ROOT_ID, ManagerClient
from bimcloud_backup.config import Config
from bimcloud_backup.errors import ApiError, AuthError, BimcloudError
from bimcloud_backup.exporter import Exporter
from bimcloud_backup.i18n import t
from bimcloud_backup.redaction import redact
from bimcloud_backup.state import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARNINGS,
    LastRun,
    save_last_run,
)

log = logging.getLogger("bimcloud_backup")


def sign_in(
    config: Config,
    store: TokenStore,
    open_browser: Callable[[str], object] = webbrowser.open,
) -> str:
    """Interactive browser login. Returns the BIMcloud username.

    `open_browser` gets the login page address; the login can be finished in any browser,
    even on another computer, since this program only polls BIMcloud for the result.
    """
    with requests.Session() as session:
        tokens = login(session, config.server_url, config.client_id, config.username, open_browser)
        store.save(config.server_url, tokens.refresh_token)
        client = ManagerClient(session, config.server_url, config.client_id, tokens)
        return _username(client)


def connect(session: requests.Session, config: Config, store: TokenStore) -> ManagerClient:
    refresh_token = store.load(config.server_url)
    if not refresh_token:
        raise AuthError(t("auth.no_stored_login"))

    def save(tokens: Tokens) -> None:
        try:
            store.save(config.server_url, tokens.refresh_token)
        except Exception as e:  # noqa: BLE001 - keyring backends raise many error types
            # The new token stays in memory, so this run goes on; only the next run may need a
            # new login. Only the error type is logged: a backend message may quote the token.
            log.warning(t("auth.store_failed", error=type(e).__name__))

    # Refresh tokens may rotate, so every new one has to be persisted.
    tokens = refresh(session, config.server_url, config.client_id, refresh_token)
    save(tokens)
    return ManagerClient(
        session, config.server_url, config.client_id, tokens, on_tokens_refreshed=save
    )


def signed_in_user(config: Config, store: TokenStore) -> str:
    """Username of the stored login; raises AuthError when there is none or it expired."""
    with requests.Session() as session:
        return _username(connect(session, config, store))


def backup_now(
    config: Config,
    store: TokenStore,
    cancel: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    """Run a backup and record its outcome for the GUI's status panel.

    `cancel` (the GUI's Cancel button) stops it safely; so does Ctrl+C on the command line.
    `progress` gets how far the run is, from the backup threads.
    """
    try:
        with requests.Session() as session:
            client = connect(session, config, store)
            downloader = BlobDownloader(session, client, _username(client))
            try:
                result = run_backup(
                    config,
                    client,
                    downloader,
                    exporter=Exporter(client, stall_seconds=config.export_stall_minutes * 60),
                    cancel=cancel,
                    progress=progress,
                )
            finally:
                downloader.close()
    except BackupCancelled:
        _record_cancelled(t("service.cancelled_by_user"))
        raise
    except KeyboardInterrupt:
        _record_cancelled(t("service.cancelled_ctrl_c"))
        raise
    except (BimcloudError, requests.RequestException, OSError) as e:
        message = redact(str(e))
        _record(LastRun(_now(), STATUS_FAILED, message=message))
        if message != str(e):
            # Connection errors quote the URL, session-id included: never show it.
            raise BimcloudError(message) from None
        raise
    _record(
        LastRun(
            _now(),
            STATUS_OK if result.ok and not result.pending else STATUS_WARNINGS,
            files=result.files,
            bytes=result.bytes,
            errors=len(result.errors),
            pending=len(result.pending),
            folder=str(result.folder or ""),
        )
    )
    return result


KIND_PROJECT = "project"
KIND_LIBRARY = "library"


@dataclass(frozen=True)
class FolderListing:
    """One level of the BIMcloud folder tree, for the folder picker."""

    # (name, path relative to the BIMcloud root), sorted by name.
    folders: list[tuple[str, str]]
    # Projects and libraries directly inside the listed folder.
    projects: int
    libraries: int
    # Those projects and libraries: (name, path relative to the root, kind), sorted by name.
    items: list[tuple[str, str, str]] = field(default_factory=list)
    # (projects, libraries) of each subfolder, counting everything below it, when known.
    totals: dict[str, tuple[int, int]] = field(default_factory=dict)


class FolderBrowser:
    """Lists the BIMcloud tree of folders, projects and libraries for the picker."""

    def __init__(self, client: ManagerClient):
        self._client = client
        root = client.get_resource(ROOT_ID) or {}
        self._root_path = root.get("$path", "Project Root")

    def tree(self, check: Callable[[], None] = lambda: None) -> dict[str, FolderListing]:
        """The whole tree of folders, projects and libraries at once, by folder path.

        Plain files are left out. Counts include everything below a folder.
        """
        resources = self._everything(check)
        folders: dict[str, list[tuple[str, str]]] = {"": []}
        items: dict[str, list[tuple[str, str, str]]] = {"": []}

        def add_folder(path: str) -> None:
            while path and path not in folders:
                folders[path], items[path] = [], []
                parent, _, name = path.rpartition("/")
                add_folder(parent)
                folders[parent].append((name, path))

        seen: set[object] = set()
        for resource in resources:
            # A resource listed twice (pages can shift while they are read) counts once.
            key = resource.get("id") or resource.get("$path")
            if key in seen:
                continue
            seen.add(key)
            path = self._relative(resource)
            kind = resource.get("type")
            if not path:
                continue
            if kind == FOLDER_TYPE:
                add_folder(path)
            elif kind in PROJECT_TYPES or kind in LIBRARY_TYPES:
                parent, _, name = path.rpartition("/")
                add_folder(parent)
                item_kind = KIND_PROJECT if kind in PROJECT_TYPES else KIND_LIBRARY
                items[parent].append((name, path, item_kind))

        totals = {
            path: [
                sum(1 for *_, k in found if k == KIND_PROJECT),
                sum(1 for *_, k in found if k == KIND_LIBRARY),
            ]
            for path, found in items.items()
        }
        # Deepest first, so each folder adds a finished total to its parent.
        for path in sorted(folders, key=lambda p: p.count("/"), reverse=True):
            if path:
                parent = path.rpartition("/")[0]
                totals[parent][0] += totals[path][0]
                totals[parent][1] += totals[path][1]
        listings = {}
        for path, subfolders in folders.items():
            subfolders.sort(key=lambda f: f[0].casefold())
            items[path].sort(key=lambda i: i[0].casefold())
            listings[path] = FolderListing(
                subfolders,
                totals[path][0],
                totals[path][1],
                items[path],
                {child: (totals[child][0], totals[child][1]) for _, child in subfolders},
            )
        return listings

    def _everything(self, check: Callable[[], None]) -> list[dict[str, Any]]:
        """Folders, projects and libraries in one listing, or folder by folder when this
        BIMcloud does not take that query (or answers it with something else)."""
        kinds = [FOLDER_TYPE, *sorted(PROJECT_TYPES), *sorted(LIBRARY_TYPES)]
        try:
            resources = self._client.find_resources({"$eq": {"type": kinds}}, check)
        except ApiError as e:
            problem = redact(str(e))
        else:
            wrong = [r for r in resources if not isinstance(r, dict) or r.get("type") not in kinds]
            if wrong:
                problem = t("service.listing_wrong_items", count=len(wrong))
            elif resources or not self._client.get_children(ROOT_ID):
                return resources
            else:
                # Nothing at all, but the root has something: the filter was not understood.
                problem = t("service.listing_empty")
        log.warning(t("service.listing_fallback", problem=problem))
        return self._folder_by_folder(check)

    def _folder_by_folder(self, check: Callable[[], None]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        pending = [ROOT_ID]
        while pending:
            check()
            for resource in self._client.get_children(pending.pop()):
                found.append(resource)
                if resource.get("type") == FOLDER_TYPE:
                    pending.append(resource["id"])
        return found

    def _relative(self, item: dict[str, Any]) -> str | None:
        """Path from the BIMcloud root; None for anything outside it, which the backup could
        not find again by that path."""
        prefix = self._root_path + "/"
        path = item.get("$path") or ""
        return path[len(prefix) :] if path.startswith(prefix) else None


def open_folder_browser(
    session: requests.Session, config: Config, store: TokenStore
) -> FolderBrowser:
    return FolderBrowser(connect(session, config, store))


def _record_cancelled(message: str) -> None:
    log.warning(t("service.cancelled", message=message))
    _record(LastRun(_now(), STATUS_CANCELLED, message=message))


def _record(run: LastRun) -> None:
    # The status file is a convenience; never let it break a backup.
    with contextlib.suppress(OSError):
        save_last_run(run)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _username(client: ManagerClient) -> str:
    user = client.get_user(client.user_id) or {}
    return user.get("username") or client.user_id
