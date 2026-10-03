"""Errors raised when talking to BIMcloud."""

from __future__ import annotations

import requests

# HTTP status BIMcloud uses for its own application errors.
BIMCLOUD_ERROR_STATUS = 430


class BimcloudError(Exception):
    """Base class for every BIMcloud related failure."""


class AuthError(BimcloudError):
    """Login or token refresh failed; the user has to run `login` again."""


class ApiError(BimcloudError):
    """BIMcloud answered a request with an error."""

    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def is_invalid_token(response: requests.Response) -> bool:
    return response.status_code == 401 and _json(response).get("error") == "invalid_token"


def refuse_redirect(response: requests.Response) -> None:
    """Downloads never follow redirects: the address was checked, where it points to was not."""
    if response.is_redirect or 300 <= response.status_code < 400:
        response.close()
        raise BimcloudError(
            f"O servidor redirecionou o download (HTTP {response.status_code}); recusado"
        )


def raise_for_response(response: requests.Response) -> None:
    if response.ok:
        return
    body = _json(response)
    blob_error = body.get("data")
    if isinstance(blob_error, dict) and "error-code" in blob_error:
        # Blob Server errors: {"data": {"error-code": 4, "error-message": "..."}}
        raise ApiError(
            blob_error.get("error-message", "Erro desconhecido do servidor de arquivos"),
            code=blob_error.get("error-code"),
            status=response.status_code,
        )
    if response.status_code == BIMCLOUD_ERROR_STATUS:
        raise ApiError(
            body.get("error-message", "Erro desconhecido do BIMcloud"),
            code=body.get("error-code"),
            status=response.status_code,
        )
    if "error" in body:
        # OAuth2 style error: {"error": "...", "error_description": "..."}
        message = body.get("error_description") or body["error"]
        if response.status_code in (400, 401):
            raise AuthError(f"{body['error']}: {message}")
        raise ApiError(message, status=response.status_code)
    raise ApiError(
        f"HTTP {response.status_code} {response.reason or ''}".strip(),
        status=response.status_code,
    )


def _json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
