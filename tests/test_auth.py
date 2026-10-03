import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from bimcloud_backup.auth import login, pkce_pair, refresh
from bimcloud_backup.errors import AuthError
from tests.conftest import OAUTH, SERVER, token_response


def test_pkce_challenge_matches_verifier():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@responses.activate
def test_login_opens_browser_polls_and_exchanges_code():
    responses.get(f"{OAUTH}/get-authorization-code-by-state", json={"status": "pending"})
    responses.get(
        f"{OAUTH}/get-authorization-code-by-state", json={"status": "succeeded", "code": "abc"}
    )
    responses.post(f"{OAUTH}/token", json=token_response())
    opened = []

    tokens = login(
        requests.Session(),
        SERVER,
        "client",
        username="ana@escritorio.com",
        open_browser=opened.append,
        sleep=lambda _: None,
    )

    assert tokens.refresh_token == "refresh-1"
    query = parse_qs(urlparse(opened[0]).query)
    assert query["client_id"] == ["client"]
    assert query["username"] == ["ana@escritorio.com"]
    assert query["code_challenge_method"] == ["S256"]

    polled_state = parse_qs(urlparse(responses.calls[0].request.url).query)["state"]
    assert polled_state == query["state"]

    form = parse_qs(responses.calls[2].request.body)
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["abc"]
    verifier = form["code_verifier"][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert query["code_challenge"] == [expected.rstrip(b"=").decode()]


@responses.activate
def test_login_times_out():
    responses.get(f"{OAUTH}/get-authorization-code-by-state", json={"status": "pending"})
    with pytest.raises(AuthError, match="não foi concluído"):
        login(
            requests.Session(),
            SERVER,
            "client",
            open_browser=lambda _: None,
            sleep=lambda _: None,
            timeout_seconds=3,
        )


@responses.activate
def test_refresh_rejected_raises_auth_error():
    responses.post(
        f"{OAUTH}/token",
        status=400,
        json={"error": "invalid_grant", "error_description": "Refresh token expired"},
    )
    with pytest.raises(AuthError, match="invalid_grant"):
        refresh(requests.Session(), SERVER, "client", "old")


@responses.activate
def test_unexpected_token_response():
    responses.post(f"{OAUTH}/token", json={"access_token": "only"})
    with pytest.raises(AuthError, match="inesperada"):
        refresh(requests.Session(), SERVER, "client", "old")
