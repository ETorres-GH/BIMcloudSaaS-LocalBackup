"""Client for the BIMcloud Manager resource API."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from bimcloud_backup.auth import REQUEST_TIMEOUT_SECONDS, Tokens, refresh
from bimcloud_backup.errors import (
    ApiError,
    BimcloudError,
    is_invalid_token,
    raise_for_response,
    refuse_redirect,
)
from bimcloud_backup.urls import https_origin

API_ROOT = "management/client"
ROOT_ID = "projectRoot"
FOLDER_TYPE = "resourceGroup"
# The API caps every query at 1000 items, so folder contents are paginated.
PAGE_SIZE = 100
# A download may follow this many redirects, each one checked before it is used.
MAX_DOWNLOAD_REDIRECTS = 3

log = logging.getLogger("bimcloud_backup")


class ManagerClient:
    def __init__(
        self,
        session: requests.Session,
        server_url: str,
        client_id: str,
        tokens: Tokens,
        on_tokens_refreshed: Callable[[Tokens], None] | None = None,
        page_size: int = PAGE_SIZE,
    ):
        self._session = session
        self._server_url = server_url
        self._client_id = client_id
        self._tokens = tokens
        self._on_tokens_refreshed = on_tokens_refreshed
        self._page_size = page_size
        self._refresh_lock = threading.Lock()

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._request("GET", "get-user", params={"user-id": user_id})

    @property
    def server_url(self) -> str:
        return self._server_url

    @property
    def user_id(self) -> str:
        return self._tokens.user_id

    def get_resource(self, resource_id: str) -> dict[str, Any]:
        return self._request("GET", "get-resource", params={"resource-id": resource_id})

    def get_resource_by_path(self, path: str) -> dict[str, Any] | None:
        result = self._request("POST", "get-resources-by-criterion", json={"$eq": {"$path": path}})
        return result[0] if result else None

    def get_ticket(self, server_id: str) -> str:
        """Return a base64 ticket that authenticates a session on a Blob Server."""
        response = self._request(
            "POST",
            "ticket-generator/get-ticket",
            raw=True,
            json={
                "type": "freeTicket",
                "resources": [server_id],
                "format": "base64",
                "user-id": self.user_id,
            },
        )
        # Depending on the server version the ticket comes as plain text or a JSON string.
        return response.text.strip().strip('"')

    def get_children(self, parent_id: str) -> list[dict[str, Any]]:
        criterion = {"$eq": {"$parentId": parent_id}}
        children: list[dict[str, Any]] = []
        skip = 0
        while True:
            page = self._request(
                "POST",
                "get-resources-by-criterion",
                params={"sort-by": "name", "skip": skip, "limit": self._page_size},
                json=criterion,
            )
            children.extend(page)
            if len(page) < self._page_size:
                return children
            skip += self._page_size

    def find_resources(
        self,
        criterion: dict[str, Any],
        check: Callable[[], None] = lambda: None,
        sort_by: str = "id",
    ) -> list[dict[str, Any]]:
        """Every resource matching `criterion`, wherever it is, a page at a time.

        Pages are sorted by `sort_by`, by default the id: unique, so no resource moves from one
        page to another between two calls (sorting by name, equal names could). `check` runs
        between pages, so a long listing can be stopped.
        """
        found: list[dict[str, Any]] = []
        skip = 0
        while True:
            check()
            page = self._request(
                "POST",
                "get-resources-by-criterion",
                params={"sort-by": sort_by, "skip": skip, "limit": self._page_size},
                json=criterion,
            )
            if not isinstance(page, list):
                raise ApiError("O BIMcloud respondeu à listagem num formato inesperado")
            found.extend(page)
            if len(page) < self._page_size:
                return found
            skip += self._page_size

    def walk(self, parent_id: str = ROOT_ID) -> Iterator[dict[str, Any]]:
        """Yield every resource below `parent_id`, depth first."""
        for resource in self.get_children(parent_id):
            yield resource
            if resource.get("type") == FOLDER_TYPE:
                yield from self.walk(resource["id"])

    def request(self, method: str, endpoint: str, api_root: str = API_ROOT, **kwargs: Any) -> Any:
        """JSON call to `{server}/{api_root}/{endpoint}`, refreshing an expired token once."""
        return self._request(method, endpoint, api_root=api_root, **kwargs)

    def open_download(
        self,
        url: str,
        params: dict[str, str] | None = None,
        timeout: Any = None,
        trusted: Callable[[str], bool] | None = None,
    ) -> requests.Response:
        """Start a streamed GET of `url`; the caller must close the response.

        The access token goes only in the `access_token` query parameter: `download-backup`
        refuses a request that also has the Authorization header. The caller must make sure
        `url` belongs to BIMcloud; plain http is always refused.

        Redirects are refused unless `trusted` accepts the new address. Then up to
        MAX_DOWNLOAD_REDIRECTS are followed, each to an https address that `trusted` accepts,
        exactly as given: the access token is never added to them.
        """
        if not url.lower().startswith("https://"):
            raise BimcloudError("Download recusado: o endereço não usa https")

        def send() -> requests.Response:
            return self._session.get(
                url,
                params={**(params or {}), "access_token": self._tokens.access_token},
                stream=True,
                allow_redirects=False,
                timeout=timeout or REQUEST_TIMEOUT_SECONDS,
            )

        used = self._tokens
        response = send()
        if is_invalid_token(response):
            response.close()
            self._refresh_tokens(used)
            response = send()
        if trusted is not None:
            response = self._follow_redirects(response, url, trusted, timeout)
        refuse_redirect(response)
        if not response.ok:
            try:
                raise_for_response(response)
            finally:
                response.close()
        return response

    def _follow_redirects(
        self,
        response: requests.Response,
        url: str,
        trusted: Callable[[str], bool],
        timeout: Any,
    ) -> requests.Response:
        for _ in range(MAX_DOWNLOAD_REDIRECTS):
            if not (300 <= response.status_code < 400):
                return response
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise BimcloudError(
                    "O servidor redirecionou o download para um endereço sem https; recusado"
                )
            target = _redirect_target(urljoin(response.url or url, location))
            host = urlsplit(target).hostname or "?"
            if not trusted(target):
                raise BimcloudError(
                    f"O servidor redirecionou o download para {host}, que não pertence ao "
                    "BIMcloud; recusado"
                )
            if _same_ascii_origin(target, self._server_url):
                # The BIMcloud server itself: download-backup needs the login here, and only
                # one method at a time, so the Authorization header goes alone.
                log.info(
                    "Download redirecionado para %s (o próprio servidor; acesso enviado no "
                    "cabeçalho Authorization)",
                    host,
                )
                response = self._get_on_the_server(target, timeout)
            else:
                # Only the host: the query of a download address may carry a ticket.
                log.info("Download redirecionado para %s", host)
                response = self._get_without_credentials(target, timeout)
        if 300 <= response.status_code < 400:
            response.close()
            raise BimcloudError(
                f"O download foi redirecionado mais de {MAX_DOWNLOAD_REDIRECTS} vezes; recusado"
            )
        return response

    def _get_on_the_server(self, url: str, timeout: Any) -> requests.Response:
        """GET on the BIMcloud server with the access token in the Authorization header only."""
        used = self._tokens
        response = self._get_without_credentials(url, timeout, _Bearer(used.access_token))
        if is_invalid_token(response):
            response.close()
            self._refresh_tokens(used)
            response = self._get_without_credentials(
                url, timeout, _Bearer(self._tokens.access_token)
            )
        return response

    def _get_without_credentials(
        self, url: str, timeout: Any, auth: requests.auth.AuthBase | None = None
    ) -> requests.Response:
        """GET with nothing of ours (no Authorization, cookies or .netrc login) but `auth`.

        The request is prepared on its own, not through the session, so the session's cookies
        and headers stay out; proxies and certificates still come from the environment.
        """
        prepared = requests.Request("GET", url, auth=auth or _NoCredentials()).prepare()
        settings = self._session.merge_environment_settings(prepared.url, {}, True, None, None)
        return self._session.send(
            prepared,
            stream=True,
            allow_redirects=False,
            timeout=timeout or REQUEST_TIMEOUT_SECONDS,
            proxies=settings["proxies"],
            verify=settings["verify"],
            cert=settings["cert"],
        )

    def _request(
        self, method: str, endpoint: str, raw: bool = False, api_root: str = API_ROOT, **kwargs: Any
    ) -> Any:
        used = self._tokens
        response = self._send(method, endpoint, api_root, **kwargs)
        if is_invalid_token(response):
            self._refresh_tokens(used)
            response = self._send(method, endpoint, api_root, **kwargs)
        raise_for_response(response)
        if raw:
            return response
        return response.json() if response.content else None

    def _refresh_tokens(self, expired: Tokens) -> None:
        """Get a new access token, once, however many threads found the old one expired.

        BIMcloud may rotate the refresh token, so a second refresh with the old one would fail.
        """
        with self._refresh_lock:
            if self._tokens is not expired:
                return  # another thread already refreshed
            self._tokens = refresh(
                self._session, self._server_url, self._client_id, self._tokens.refresh_token
            )
            if self._on_tokens_refreshed:
                self._on_tokens_refreshed(self._tokens)

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.access_token}"}

    def _send(self, method: str, endpoint: str, api_root: str, **kwargs: Any) -> requests.Response:
        return self._session.request(
            method,
            f"{self._server_url}/{api_root}/{endpoint}",
            headers=self._auth_header(),
            timeout=REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )


def _same_ascii_origin(url: str, server_url: str) -> bool:
    """Same scheme, host and port, with plain ASCII hosts on both sides.

    International names are compared by different rules here and in requests ("faß" can be
    "fass" for one and "xn--fa-hia" for the other), so a host with any other character never
    gets the login, even when it may be followed without it.
    """
    origin, server = https_origin(url), https_origin(server_url)
    return origin is not None and origin == server and origin[0].isascii()


class _Bearer(requests.auth.AuthBase):
    """The access token as `Authorization: Bearer`, and nothing else."""

    def __init__(self, token: str):
        self._token = token

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Bearer {self._token}"
        return request


class _NoCredentials(requests.auth.AuthBase):
    """Makes sure a request goes out with no Authorization header at all."""

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers.pop("Authorization", None)
        return request


def _redirect_target(url: str) -> str:
    """The address a redirect points to, refused or cleaned of anything that is ours.

    A relative Location (even just "#parte") is resolved against the previous address, which
    carries our access token in the query: the token is taken out. A user and password in the
    address would make requests send them, so such an address is refused.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise BimcloudError(
            "O servidor redirecionou o download para um endereço sem https; recusado"
        )
    if parts.username is not None or parts.password is not None:
        raise BimcloudError(
            "O servidor redirecionou o download para um endereço com usuário e senha; recusado"
        )
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "access_token"
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
