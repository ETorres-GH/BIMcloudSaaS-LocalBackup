import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from bimcloud_backup.blobserver import BlobDownloader, candidate_urls
from bimcloud_backup.client import ManagerClient
from bimcloud_backup.errors import ApiError, BimcloudError
from tests.conftest import API, SERVER

BLOB_SERVER = "https://example.bimcloud.com:22000"
SERVER_RESOURCE = {
    "id": "ms1",
    "name": "model1",
    "connectionUrls": ["$protocol//10.0.0.5:22000", "$protocol//$hostname:22000"],
}
SECURE_URL = "https://example.bimcloud.com:22000"
BLOB = {"id": "b1", "modelServerId": "ms1", "$path": "Project Root/a.pdf", "$size": 4}


def downloader(tokens, sleeps=None):
    session = requests.Session()
    client = ManagerClient(session, SERVER, "client", tokens)
    return BlobDownloader(
        session, client, "ana", sleep=(sleeps if sleeps is not None else []).append
    )


def mock_server_setup():
    responses.get(f"{API}/get-resource", json=SERVER_RESOURCE)
    responses.get(
        "https://10.0.0.5:22000/application-server-service/get-runtime-id",
        body=requests.ConnectionError(),
    )
    responses.get(f"{BLOB_SERVER}/application-server-service/get-runtime-id", json={})
    responses.post(f"{API}/ticket-generator/get-ticket", body="VElDS0VU")
    responses.post(f"{BLOB_SERVER}/session-service/1.0/create-session", json={"data": {"id": "s1"}})


def test_candidate_urls_expand_placeholders():
    assert candidate_urls(SERVER_RESOURCE, "https", "example.bimcloud.com") == [
        "https://10.0.0.5:22000",
        BLOB_SERVER,
    ]


@responses.activate
def test_download_opens_session_and_writes_file(tokens, tmp_path):
    mock_server_setup()
    responses.get(f"{BLOB_SERVER}/blob-store-service/1.0/get-blob-content", body=b"data")
    target = tmp_path / "sub" / "a.pdf"

    assert downloader(tokens).download(BLOB, target) == 4

    assert target.read_bytes() == b"data"
    session_call = next(c for c in responses.calls if "create-session" in c.request.url)
    assert json.loads(session_call.request.body)["data"] == {
        "username": "ana",
        "ticket": "VElDS0VU",
    }
    download_call = responses.calls[-1].request.url
    assert "session-id=s1" in download_call and "blob-id=b1" in download_call


@responses.activate
def test_expired_session_is_reopened(tokens, tmp_path):
    mock_server_setup()
    url = f"{BLOB_SERVER}/blob-store-service/1.0/get-blob-content"
    responses.get(url, status=430, json={"data": {"error-code": 11, "error-message": "gone"}})
    responses.get(url, body=b"data")

    assert downloader(tokens).download(BLOB, tmp_path / "a.pdf") == 4
    sessions = [c for c in responses.calls if "create-session" in c.request.url]
    assert len(sessions) == 2


@responses.activate
def test_size_mismatch_is_an_error(tokens, tmp_path):
    mock_server_setup()
    responses.get(f"{BLOB_SERVER}/blob-store-service/1.0/get-blob-content", body=b"da")
    target = tmp_path / "a.pdf"

    with pytest.raises(BimcloudError, match="incompleto"):
        downloader(tokens).download(BLOB, target)

    assert list(tmp_path.iterdir()) == []


@responses.activate
def test_plain_http_connection_urls_never_receive_the_ticket(tokens, tmp_path):
    server = {"id": "ms1", "connectionUrls": ["http://ms.svc.cluster.local:22000", SECURE_URL]}
    responses.get(f"{API}/get-resource", json=server)
    responses.get(f"{SECURE_URL}/application-server-service/get-runtime-id", json={})
    responses.post(f"{API}/ticket-generator/get-ticket", body="VElDS0VU")
    responses.post(f"{SECURE_URL}/session-service/1.0/create-session", json={"data": {"id": "s1"}})
    responses.get(f"{SECURE_URL}/blob-store-service/1.0/get-blob-content", body=b"data")

    assert downloader(tokens).download(BLOB, tmp_path / "a.pdf") == 4
    assert not any(c.request.url.startswith("http://") for c in responses.calls)


@responses.activate
def test_server_with_only_http_urls_is_refused_before_asking_for_a_ticket(tokens, tmp_path):
    server = {"id": "ms1", "connectionUrls": ["http://ms.svc.cluster.local:22000"]}
    responses.get(f"{API}/get-resource", json=server)

    with pytest.raises(BimcloudError, match="https"):
        downloader(tokens).download(BLOB, tmp_path / "a.pdf")

    assert [c.request.url.split("?")[0] for c in responses.calls] == [f"{API}/get-resource"]


@responses.activate
def test_redirecting_runtime_id_is_not_a_reachable_server(tokens, tmp_path):
    server = {"id": "ms1", "connectionUrls": [SECURE_URL]}
    responses.get(f"{API}/get-resource", json=server)
    responses.get(
        f"{SECURE_URL}/application-server-service/get-runtime-id",
        status=302,
        headers={"Location": "https://evil.invalid/"},
    )

    with pytest.raises(BimcloudError, match="não está acessível"):
        downloader(tokens).download(BLOB, tmp_path / "a.pdf")
    assert not any("evil.invalid" in c.request.url for c in responses.calls)


@responses.activate
def test_blob_download_does_not_follow_redirects(tokens, tmp_path):
    mock_server_setup()
    responses.get(
        f"{BLOB_SERVER}/blob-store-service/1.0/get-blob-content",
        status=302,
        headers={"Location": "https://evil.invalid/file"},
    )

    with pytest.raises(BimcloudError, match="redirecionou"):
        downloader(tokens).download(BLOB, tmp_path / "a.pdf")

    assert not any("evil.invalid" in c.request.url for c in responses.calls)
    assert list(tmp_path.iterdir()) == []


@responses.activate
def test_limit_check_runs_for_every_block_and_removes_the_partial_file(tokens, tmp_path):
    mock_server_setup()
    responses.get(f"{BLOB_SERVER}/blob-store-service/1.0/get-blob-content", body=b"data")
    calls = []

    def check():
        calls.append(1)
        raise RuntimeError("espaço livre abaixo do mínimo")

    with pytest.raises(RuntimeError):
        downloader(tokens).download(BLOB, tmp_path / "a.pdf", check)

    assert calls == [1]
    assert list(tmp_path.iterdir()) == []


@responses.activate
def test_failure_in_the_middle_of_a_download_removes_the_partial_file(
    tokens, tmp_path, monkeypatch
):
    mock_server_setup()
    responses.get(f"{BLOB_SERVER}/blob-store-service/1.0/get-blob-content", body=b"data")

    def broken(self, chunk_size=1, decode_unicode=False):
        yield b"da"
        raise requests.ConnectionError("conexão caiu")

    monkeypatch.setattr(requests.Response, "iter_content", broken)

    with pytest.raises(requests.ConnectionError):
        downloader(tokens).download(BLOB, tmp_path / "a.pdf")

    assert list(tmp_path.iterdir()) == []


CONTENT_URL = f"{BLOB_SERVER}/blob-store-service/1.0/get-blob-content"


@responses.activate
def test_network_error_is_retried_with_growing_waits(tokens, tmp_path):
    mock_server_setup()
    responses.get(CONTENT_URL, body=requests.ConnectionError("conexão caiu"))
    responses.get(CONTENT_URL, status=503)
    responses.get(CONTENT_URL, body=b"data")
    sleeps = []

    assert downloader(tokens, sleeps).download(BLOB, tmp_path / "a.pdf") == 4

    assert sleeps == [1.0, 2.0]
    assert (tmp_path / "a.pdf").read_bytes() == b"data"


@responses.activate
def test_retries_give_up_after_three_attempts(tokens, tmp_path):
    mock_server_setup()
    responses.get(CONTENT_URL, status=502)
    sleeps = []

    with pytest.raises(ApiError):
        downloader(tokens, sleeps).download(BLOB, tmp_path / "a.pdf")

    assert sleeps == [1.0, 2.0]
    assert len([c for c in responses.calls if "get-blob-content" in c.request.url]) == 3
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {}),
        (403, {}),
        (430, {"error-code": 12, "error-message": "sem permissão"}),
    ],
)
@responses.activate
def test_client_errors_are_not_retried(tokens, tmp_path, status, body):
    mock_server_setup()
    responses.get(CONTENT_URL, status=status, json=body)
    sleeps = []

    with pytest.raises(BimcloudError):
        downloader(tokens, sleeps).download(BLOB, tmp_path / "a.pdf")

    assert sleeps == []


@responses.activate
def test_cancel_during_the_wait_stops_the_retries(tokens, tmp_path):
    mock_server_setup()
    responses.get(CONTENT_URL, status=503)
    cancelled = []

    def check():
        if cancelled:
            raise RuntimeError("cancelado")

    sleeps = []
    blob_downloader = downloader(tokens, sleeps)
    blob_downloader._sleep = lambda seconds: (sleeps.append(seconds), cancelled.append(True))

    with pytest.raises(RuntimeError, match="cancelado"):
        blob_downloader.download(BLOB, tmp_path / "a.pdf", check)

    assert sleeps == [1.0]
    assert len([c for c in responses.calls if "get-blob-content" in c.request.url]) == 1


@responses.activate
def test_threads_share_one_session_and_reopen_it_once(tokens, tmp_path):
    mock_server_setup()
    responses.get(CONTENT_URL, body=b"data")
    blob_downloader = downloader(tokens)
    blobs = [{**BLOB, "id": f"b{i}", "$path": f"Project Root/{i}.pdf"} for i in range(8)]

    with ThreadPoolExecutor(4) as pool:
        sizes = list(pool.map(lambda b: blob_downloader.download(b, tmp_path / b["id"]), blobs))

    assert sizes == [4] * 8
    sessions = [c for c in responses.calls if "create-session" in c.request.url]
    assert len(sessions) == 1


@responses.activate
def test_a_session_expired_for_several_threads_is_reopened_once(tokens, tmp_path):
    threads = 4
    mock_server_setup()
    opened = []

    def create_session(request):
        opened.append(f"s{len(opened) + 1}")
        return 200, {}, json.dumps({"data": {"id": opened[-1]}})

    # Every thread is downloading with the first session when it expires.
    all_expired = threading.Barrier(threads, timeout=5)

    def content(request):
        if parse_qs(urlparse(request.url).query)["session-id"] == ["s1"]:
            all_expired.wait()
            return 430, {}, json.dumps({"data": {"error-code": 11, "error-message": "gone"}})
        return 200, {}, b"data"

    responses.remove(responses.POST, f"{BLOB_SERVER}/session-service/1.0/create-session")
    responses.add_callback(
        responses.POST, f"{BLOB_SERVER}/session-service/1.0/create-session", callback=create_session
    )
    responses.add_callback(responses.GET, CONTENT_URL, callback=content)
    blob_downloader = downloader(tokens)
    blobs = [{**BLOB, "id": f"b{i}", "$path": f"Project Root/{i}.pdf"} for i in range(threads)]

    with ThreadPoolExecutor(threads) as pool:
        sizes = list(pool.map(lambda b: blob_downloader.download(b, tmp_path / b["id"]), blobs))

    assert sizes == [4] * threads
    # One session at the start and one replacement: the threads that noticed the expiry later
    # did not drop the new session.
    assert opened == ["s1", "s2"]
