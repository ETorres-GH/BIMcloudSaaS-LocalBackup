import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from bimcloud_backup.client import ManagerClient
from bimcloud_backup.errors import ApiError, BimcloudError
from tests.conftest import API, OAUTH, SERVER, token_response

CRITERION_URL = f"{API}/get-resources-by-criterion"


def make_client(tokens, **kwargs):
    return ManagerClient(requests.Session(), SERVER, "client", tokens, **kwargs)


def resource(id_, type_, path):
    return {"id": id_, "type": type_, "name": path.rsplit("/", 1)[-1], "$path": path}


def children_of(parent_id):
    """Match a get-resources-by-criterion call for the given parent."""

    def matcher(request):
        body = json.loads(request.body)
        ok = body == {"$eq": {"$parentId": parent_id}}
        return ok, "" if ok else f"parent != {parent_id}"

    return matcher


@responses.activate
def test_get_children_paginates(tokens):
    page1 = [resource(f"p{i}", "project", f"Project Root/p{i}") for i in range(2)]
    page2 = [resource("p2", "project", "Project Root/p2")]
    responses.post(CRITERION_URL, json=page1)
    responses.post(CRITERION_URL, json=page2)

    result = make_client(tokens, page_size=2).get_children("projectRoot")

    assert [r["id"] for r in result] == ["p0", "p1", "p2"]
    skips = [parse_qs(urlparse(c.request.url).query)["skip"] for c in responses.calls]
    assert skips == [["0"], ["2"]]
    assert responses.calls[0].request.headers["Authorization"] == "Bearer access-1"


@responses.activate
def test_walk_recurses_into_folders(tokens):
    folder = resource("f1", "resourceGroup", "Project Root/Obras")
    responses.post(
        CRITERION_URL,
        json=[folder, resource("lib", "library", "Project Root/Biblioteca")],
        match=[children_of("projectRoot")],
    )
    responses.post(
        CRITERION_URL,
        json=[resource("p1", "project", "Project Root/Obras/Casa")],
        match=[children_of("f1")],
    )

    paths = [r["$path"] for r in make_client(tokens).walk()]

    assert paths == ["Project Root/Obras", "Project Root/Obras/Casa", "Project Root/Biblioteca"]


@responses.activate
def test_expired_access_token_is_refreshed_and_saved(tokens):
    responses.get(f"{API}/get-user", status=401, json={"error": "invalid_token"})
    responses.post(f"{OAUTH}/token", json=token_response("access-2", "refresh-2"))
    responses.get(f"{API}/get-user", json={"username": "ana"})
    saved = []

    user = make_client(tokens, on_tokens_refreshed=saved.append).get_user("user-1")

    assert user == {"username": "ana"}
    assert [t.refresh_token for t in saved] == ["refresh-2"]
    assert responses.calls[2].request.headers["Authorization"] == "Bearer access-2"


@responses.activate
def test_bimcloud_error_is_raised(tokens):
    responses.get(
        f"{API}/get-resource",
        status=430,
        json={"error-code": 6, "error-message": "EntityNotFoundError: nada"},
    )
    with pytest.raises(ApiError, match="EntityNotFoundError") as info:
        make_client(tokens).get_resource("x")
    assert info.value.code == 6


@responses.activate
def test_find_resources_pages_through_everything_and_can_be_stopped(tokens):
    url = f"{API}/get-resources-by-criterion"
    responses.post(url, json=[{"id": "1"}, {"id": "2"}])
    responses.post(url, json=[{"id": "3"}])
    client = make_client(tokens, page_size=2)
    criterion = {"$eq": {"type": ["resourceGroup"]}}

    assert [r["id"] for r in client.find_resources(criterion)] == ["1", "2", "3"]
    assert json.loads(responses.calls[0].request.body) == criterion
    assert parse_qs(urlparse(responses.calls[1].request.url).query)["skip"] == ["2"]

    def stop():
        raise RuntimeError("cancelado")

    with pytest.raises(RuntimeError):
        client.find_resources(criterion, stop)

    # Sorted by id, unique: equal names cannot move an item from one page to another.
    assert parse_qs(urlparse(responses.calls[0].request.url).query)["sort-by"] == ["id"]


@responses.activate
def test_find_resources_refuses_an_answer_that_is_not_a_list(tokens):
    responses.post(f"{API}/get-resources-by-criterion", json={"erro": "x"})

    with pytest.raises(ApiError, match="formato inesperado"):
        make_client(tokens).find_resources({})


def test_open_download_refuses_plain_http(tokens):
    with pytest.raises(BimcloudError, match="https"):
        make_client(tokens).open_download("http://example.bimcloud.com/file")


@responses.activate
def test_open_download_does_not_follow_redirects(tokens):
    url = "https://example-data.bimcloud.com/file-manager-service/get-file"
    responses.get(url, status=302, headers={"Location": "http://evil.invalid/file"})

    with pytest.raises(BimcloudError, match="redirecionou"):
        make_client(tokens).open_download(url)

    assert len(responses.calls) == 1


@responses.activate
def test_token_is_refreshed_once_when_several_threads_find_it_expired(tokens):
    threads = 4
    # Every thread gets "expired" for the old token before any of them refreshes it.
    all_expired = threading.Barrier(threads, timeout=5)
    refreshes = []

    def get_user(request):
        if request.headers["Authorization"] == "Bearer access-1":
            all_expired.wait()
            return 401, {}, json.dumps({"error": "invalid_token"})
        return 200, {}, json.dumps({"id": "user-1"})

    def token(request):
        used = parse_qs(request.body)["refresh_token"][0]
        refreshes.append(used)
        if used != "refresh-1" or len(refreshes) > 1:
            # BIMcloud rotates the refresh token: reusing the old one fails.
            return 400, {}, json.dumps({"error": "invalid_grant"})
        time.sleep(0.05)  # a slow refresh, so the other threads arrive while it runs
        return 200, {}, json.dumps(token_response("access-2", "refresh-2"))

    responses.add_callback(responses.GET, f"{API}/get-user", callback=get_user)
    responses.add_callback(responses.POST, f"{OAUTH}/token", callback=token)
    saved = []
    client = make_client(tokens, on_tokens_refreshed=saved.append)

    with ThreadPoolExecutor(threads) as pool:
        users = list(pool.map(lambda _: client.get_user("user-1"), range(threads)))

    assert users == [{"id": "user-1"}] * threads
    assert refreshes == ["refresh-1"]
    assert [t.refresh_token for t in saved] == ["refresh-2"]
