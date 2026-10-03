"""Downloads of files (blobs) from the BIMcloud Blob Server."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from bimcloud_backup.client import ManagerClient
from bimcloud_backup.errors import ApiError, BimcloudError, raise_for_response, refuse_redirect
from bimcloud_backup.i18n import t
from bimcloud_backup.redaction import redact
from bimcloud_backup.urls import https_origin

log = logging.getLogger("bimcloud_backup")

SESSION_CONTENT_TYPE = (
    "application/vnd.graphisoft.teamwork.session-service-1.0.authentication-request-1.0+json"
)
# Blob Server error codes that mean "open a new session and try again".
EXPIRED_SESSION_CODES = {4, 11}
# Network errors and 5xx answers get up to 3 attempts, waiting 1 s and then 2 s.
RETRY_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 1.0
CHUNK_SIZE = 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 120


class TransientDownloadError(BimcloudError):
    """The download broke off in a way that another attempt may fix."""


class BlobDownloader:
    """Downloads files; safe to use from several threads at once.

    All threads share one Blob Server session per server: it is opened once (under a lock) and,
    when it expires, reopened once, however many downloads notice it at the same time.
    """

    def __init__(
        self,
        session: requests.Session,
        manager: ManagerClient,
        username: str,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._session = session
        self._manager = manager
        self._username = username
        self._sleep = sleep
        self._server_urls: dict[str, str] = {}
        self._sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    def download(
        self, blob: dict[str, Any], target: Path, check: Callable[[], None] = lambda: None
    ) -> int:
        """Download `blob` to `target` and return the number of bytes written.

        `check` runs before every block (the backup's time and free space limits).
        """
        server_id = blob.get("modelServerId")
        if not server_id:
            raise BimcloudError(t("download.no_server", path=blob.get("$path")))
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return self._fetch_in_session(server_id, blob, target, check)
            except (requests.ConnectionError, requests.Timeout, TransientDownloadError) as e:
                error: Exception = e
            except requests.exceptions.ChunkedEncodingError as e:
                error = e
            except ApiError as e:
                # 5xx is the server having a bad moment; 4xx and BIMcloud's own 430 are not.
                if e.status is None or e.status < 500:
                    raise
                error = e
            if attempt == RETRY_ATTEMPTS:
                raise error
            wait = RETRY_WAIT_SECONDS * 2 ** (attempt - 1)
            log.warning(
                t(
                    "download.retry",
                    path=blob.get("$path"),
                    error=redact(str(error)),
                    seconds=f"{wait:.0f}",
                )
            )
            # A cancel or a time limit must not wait for the retries.
            check()
            self._sleep(wait)
            check()
        raise AssertionError("unreachable")

    def _fetch_in_session(
        self, server_id: str, blob: dict[str, Any], target: Path, check: Callable[[], None]
    ) -> int:
        session_id = self._session_id(server_id)
        try:
            return self._fetch(server_id, session_id, blob, target, check)
        except ApiError as e:
            if e.code not in EXPIRED_SESSION_CODES:
                raise
            self._forget_session(server_id, session_id)
            return self._fetch(server_id, self._session_id(server_id), blob, target, check)

    def close(self) -> None:
        for server_id, session_id in self._sessions.items():
            # If closing fails the session simply expires on its own.
            with contextlib.suppress(requests.RequestException):
                self._session.post(
                    f"{self._server_urls[server_id]}/session-service/1.0/close-session",
                    params={"session-id": session_id},
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
        self._sessions.clear()

    def _fetch(
        self,
        server_id: str,
        session_id: str,
        blob: dict[str, Any],
        target: Path,
        check: Callable[[], None],
    ) -> int:
        url = f"{self._server_urls[server_id]}/blob-store-service/1.0/get-blob-content"
        partial = target.with_name(target.name + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            written = self._stream(url, session_id, blob, partial, check)
            expected = blob.get("$size")
            if isinstance(expected, int) and expected != written:
                raise TransientDownloadError(
                    t(
                        "download.incomplete_file",
                        name=blob.get("$path"),
                        written=written,
                        total=expected,
                    )
                )
            partial.replace(target)
        except BaseException:
            # A failed or stopped download never leaves a partial file in the backup.
            partial.unlink(missing_ok=True)
            raise
        return written

    def _stream(
        self,
        url: str,
        session_id: str,
        blob: dict[str, Any],
        partial: Path,
        check: Callable[[], None],
    ) -> int:
        with self._session.get(
            url,
            params={"session-id": session_id, "blob-id": blob["id"]},
            stream=True,
            allow_redirects=False,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        ) as response:
            refuse_redirect(response)
            raise_for_response(response)
            written = 0
            with partial.open("wb") as f:
                for chunk in response.iter_content(CHUNK_SIZE):
                    check()
                    f.write(chunk)
                    written += len(chunk)
        return written

    def _session_id(self, server_id: str) -> str:
        with self._lock:
            if server_id not in self._sessions:
                self._sessions[server_id] = self._open_session(server_id)
            return self._sessions[server_id]

    def _forget_session(self, server_id: str, session_id: str) -> None:
        """Drop an expired session, unless another thread already replaced it."""
        with self._lock:
            if self._sessions.get(server_id) == session_id:
                del self._sessions[server_id]

    def _open_session(self, server_id: str) -> str:
        server = self._manager.get_resource(server_id)
        base_url = self._server_urls.get(server_id) or self._find_reachable_url(server)
        self._server_urls[server_id] = base_url
        ticket = self._manager.get_ticket(server_id)
        response = self._session.post(
            f"{base_url}/session-service/1.0/create-session",
            json={
                "data-content-type": SESSION_CONTENT_TYPE,
                "data": {"username": self._username, "ticket": ticket},
            },
            headers={"Content-Type": SESSION_CONTENT_TYPE},
            timeout=CONNECT_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        refuse_redirect(response)
        raise_for_response(response)
        return response.json()["data"]["id"]

    def _find_reachable_url(self, server: dict[str, Any]) -> str:
        """First https connection URL of the model server that answers.

        The session is opened with the user name and a ticket, so plain http addresses (like the
        internal `http://...cluster.local` one BIMcloud SaaS lists) are never tried.
        """
        manager = urlparse(self._manager.server_url)
        for url in candidate_urls(server, manager.scheme, manager.hostname or ""):
            if https_origin(url) is None:
                continue
            try:
                response = self._session.get(
                    f"{url}/application-server-service/get-runtime-id",
                    timeout=CONNECT_TIMEOUT_SECONDS,
                    allow_redirects=False,
                )
            except requests.RequestException:
                continue
            if response.ok and not response.is_redirect and response.status_code < 300:
                return url
        raise BimcloudError(t("download.server_unreachable", name=server.get("name", "?")))


def candidate_urls(server: dict[str, Any], scheme: str, hostname: str) -> list[str]:
    """Expand the $protocol/$hostname placeholders of a model server's connection URLs."""
    urls = []
    for url in server.get("connectionUrls") or []:
        url = url.replace("$protocol", f"{scheme}:").replace("$hostname", hostname)
        urls.append(url.rstrip("/"))
    return urls
