"""OAuth2 login (authorization code with PKCE) and storage of the refresh token."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import requests

from bimcloud_backup.errors import AuthError, raise_for_response
from bimcloud_backup.i18n import t

KEYRING_SERVICE = "BIMcloudSaaS-LocalBackup"
LOGIN_TIMEOUT_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 30

OAUTH_ROOT = "management/client/oauth2"


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    access_token_exp: float
    user_id: str

    @classmethod
    def from_response(cls, data: dict) -> Tokens:
        try:
            return cls(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                access_token_exp=float(data["access_token_exp"]),
                user_id=data["user_id"],
            )
        except (KeyError, TypeError, ValueError) as e:
            raise AuthError(t("auth.unexpected_token", error=e)) from e


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(
    server_url: str, client_id: str, state: str, code_challenge: str, username: str | None = None
) -> str:
    params = {
        "client_id": client_id,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    if username:
        params["username"] = username
    return f"{server_url}/{OAUTH_ROOT}/authorize?{urlencode(params)}"


def login(
    session: requests.Session,
    server_url: str,
    client_id: str,
    username: str | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: int = LOGIN_TIMEOUT_SECONDS,
) -> Tokens:
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    open_browser(authorization_url(server_url, client_id, state, challenge, username))
    code = _wait_for_authorization_code(session, server_url, state, sleep, timeout_seconds)
    return _request_tokens(
        session,
        server_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


def refresh(
    session: requests.Session, server_url: str, client_id: str, refresh_token: str
) -> Tokens:
    return _request_tokens(
        session,
        server_url,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )


def _wait_for_authorization_code(
    session: requests.Session,
    server_url: str,
    state: str,
    sleep: Callable[[float], None],
    timeout_seconds: int,
) -> str:
    url = f"{server_url}/{OAUTH_ROOT}/get-authorization-code-by-state"
    for _ in range(timeout_seconds):
        response = session.get(url, params={"state": state}, timeout=REQUEST_TIMEOUT_SECONDS)
        raise_for_response(response)
        result = response.json()
        if result.get("status") == "succeeded" and result.get("code"):
            return result["code"]
        sleep(1)
    raise AuthError(t("auth.login_timeout", minutes=timeout_seconds // 60))


def _request_tokens(session: requests.Session, server_url: str, form: dict) -> Tokens:
    response = session.post(
        f"{server_url}/{OAUTH_ROOT}/token", data=form, timeout=REQUEST_TIMEOUT_SECONDS
    )
    raise_for_response(response)
    return Tokens.from_response(response.json())


class TokenStore(Protocol):
    def load(self, server_url: str) -> str | None: ...
    def save(self, server_url: str, refresh_token: str) -> None: ...
    def delete(self, server_url: str) -> None: ...


class KeyringTokenStore:
    """Keeps the refresh token in the OS credential store (Windows Credential Manager)."""

    def load(self, server_url: str) -> str | None:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, server_url)

    def save(self, server_url: str, refresh_token: str) -> None:
        import keyring

        keyring.set_password(KEYRING_SERVICE, server_url, refresh_token)

    def delete(self, server_url: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(KEYRING_SERVICE, server_url)
