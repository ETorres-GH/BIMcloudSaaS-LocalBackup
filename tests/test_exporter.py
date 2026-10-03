import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from bimcloud_backup.client import ManagerClient
from bimcloud_backup.errors import BimcloudError
from bimcloud_backup.exporter import (
    BACKUP_DONE,
    BACKUP_PAGE_SIZE,
    MAX_BACKUP_PAGES,
    PLN_FORMATS,
    Exporter,
    ExportError,
    describe_job,
    file_name_from_url,
    https_origin,
    is_trusted_host,
    safe_file_name,
)
from tests.conftest import token_response

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "exportacao"
# Same server as the fixtures (data.rootUrl), whose data server is bimcloud-data.example.invalid.
SERVER = "https://bimcloud.example.invalid"
OAUTH = f"{SERVER}/management/client/oauth2"
LATEST = f"{SERVER}/management/latest"
JOBS_URL = f"{LATEST}/get-jobs-by-criterion"
PROJECT_JOB = "22222222-2222-2222-2222-222222222222"
PROJECT_ID = "44444444-4444-4444-4444-444444444444"
DATA_URL = "https://bimcloud-data.example.invalid/file-manager-service/get-file"
BACKUP_FORMAT = "_server.backup.format.bimproject-automatic"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def query(call):
    return parse_qs(urlparse(call.request.url).query)


def make_exporter(tokens, **kwargs):
    client = ManagerClient(requests.Session(), SERVER, "client", tokens)
    sleeps = []
    exporter = Exporter(client, sleep=sleeps.append, **kwargs)
    return exporter, sleeps


def mock_data_server():
    """The fixtures' server is not BIMcloud SaaS, so its data server is trusted only because
    it is listed in the connectionUrls of the job's model server."""
    responses.get(
        f"{SERVER}/management/client/get-resource",
        json={"id": "ms1", "connectionUrls": ["$protocol//bimcloud-data.example.invalid"]},
    )


def mock_project_export(*job_polls, data_server=True):
    responses.get(f"{LATEST}/export-project", json=fixture("export-project-starting.json"))
    for poll in job_polls:
        responses.post(JOBS_URL, json=poll)
    if data_server:
        mock_data_server()


@responses.activate
def test_export_project_follows_the_job_and_downloads_the_file(tokens, tmp_path):
    mock_project_export(
        [],
        fixture("jobs-export-project-running.json"),
        fixture("jobs-export-project-completed.json"),
    )
    responses.get(DATA_URL, body=b"bimproject")
    exporter, sleeps = make_exporter(tokens)

    exported = exporter.export_project(PROJECT_ID, tmp_path / "Pasta")

    assert exported.path == tmp_path / "Pasta" / "ProjetoExemplo.BIMProject29"
    assert exported.path.read_bytes() == b"bimproject"
    assert exported.size == 10
    assert list(exported.path.parent.iterdir()) == [exported.path]
    assert sleeps == [3.0, 3.0, 3.0]
    start = query(responses.calls[0])
    assert start == {
        "project-id": [PROJECT_ID],
        "url-root": [SERVER],
        "include-automatic-backups": ["false"],
        "include-manual-backups": ["false"],
    }
    assert json.loads(responses.calls[1].request.body) == {"$eq": {"id": [PROJECT_JOB]}}
    download = query(responses.calls[-1])
    assert download["file-name"] == ["ProjetoExemplo.BIMProject29"]
    assert download["access_token"] == ["access-1"]
    # Only one authentication method: the query token, never the Authorization header too.
    assert "Authorization" not in responses.calls[-1].request.headers


@responses.activate
def test_include_backups_sets_both_flags(tokens, tmp_path):
    mock_project_export(fixture("jobs-export-project-completed.json"))
    responses.get(DATA_URL, body=b"x")
    exporter, _ = make_exporter(tokens)

    exporter.export_project(PROJECT_ID, tmp_path, include_backups=True)

    start = query(responses.calls[0])
    assert start["include-automatic-backups"] == ["true"]
    assert start["include-manual-backups"] == ["true"]


@responses.activate
def test_export_library(tokens, tmp_path):
    responses.get(f"{LATEST}/export-library", json=fixture("export-library-starting.json"))
    responses.post(JOBS_URL, json=fixture("jobs-export-library-completed.json"))
    mock_data_server()
    responses.get(DATA_URL, body=b"lib")
    exporter, _ = make_exporter(tokens)

    exported = exporter.export_library("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", tmp_path)

    assert exported.path.name == "BibliotecaExemplo.libpack.BIMLibrary"
    assert query(responses.calls[0])["library-id"] == ["BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"]


@responses.activate
def test_failed_job_raises_without_leaving_files(tokens, tmp_path):
    failed = fixture("jobs-export-project-completed.json")
    failed[0].update(status="failed", result="Model server error", properties=[])
    mock_project_export(failed)
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="'failed': Model server error"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert list(tmp_path.iterdir()) == []


@responses.activate
def test_job_that_never_ends_times_out(tokens, tmp_path):
    responses.get(f"{LATEST}/export-project", json=fixture("export-project-starting.json"))
    responses.post(JOBS_URL, json=fixture("jobs-export-project-running.json"))
    clock = iter(range(0, 10_000, 60))
    exporter, _ = make_exporter(tokens, monotonic=lambda: next(clock), job_timeout_seconds=300)

    with pytest.raises(ExportError, match="não terminou em 5 minutos"):
        exporter.export_project(PROJECT_ID, tmp_path)


ABORT_URL = f"{SERVER}/management/client/abort-job"


def job_with(status, current=0, top=3, phase="", progressed=1):
    job = fixture("jobs-export-project-running.json")[0]
    job.update(status=status, progressedOn=progressed)
    job["progress"] = {"min": 0, "current": current, "max": top, "phase": phase}
    return [job]


@pytest.mark.parametrize(
    ("job", "elapsed", "text"),
    [
        (None, 10, "na fila do BIMcloud (menos de 1 min)"),
        (job_with("starting")[0], 70, "na fila do BIMcloud (1 min)"),
        (
            job_with("running", 0)[0],
            125,
            "o BIMcloud está preparando o arquivo (etapa 1 de 3, 2 min)",
        ),
        (
            job_with("running", 2, phase="Compactando")[0],
            600,
            "o BIMcloud está preparando o arquivo (etapa 3 de 3, Compactando, 10 min)",
        ),
    ],
)
def test_describe_job(job, elapsed, text):
    assert describe_job(job, elapsed) == text


def test_describe_job_short_fits_one_line():
    job = job_with("running", 0, phase="Compactando")[0]
    assert describe_job(job, 725, short=True) == "preparando, etapa 1 de 3 · 12 min"


@responses.activate
def test_the_wait_reports_the_job_and_logs_only_its_changes(tokens, tmp_path, caplog):
    caplog.set_level("INFO", logger="bimcloud_backup")
    mock_project_export(
        job_with("starting"),
        job_with("starting"),
        job_with("running", 0, progressed=2),
        job_with("running", 1, progressed=3),
        fixture("jobs-export-project-completed.json"),
    )
    responses.get(DATA_URL, body=b"conteudo")
    reports = []
    exporter, _ = make_exporter(tokens)
    exporter.watch(reports.append)

    exporter.export_project(PROJECT_ID, tmp_path)

    assert reports[0] == "na fila · menos de 1 min"
    assert "preparando, etapa 2 de 3 · menos de 1 min" in reports
    assert "Exportação: o BIMcloud está preparando o arquivo (etapa 2 de 3" in caplog.text
    assert reports[-1] is None
    assert caplog.text.count("Exportação: ") == 3  # the second "starting" changed nothing
    assert not [c for c in responses.calls if "abort-job" in c.request.url]


@responses.activate
def test_cancelling_the_wait_aborts_the_job_on_the_server(tokens, tmp_path, caplog):
    caplog.set_level("INFO", logger="bimcloud_backup")
    mock_project_export(job_with("running"), data_server=False)
    responses.post(ABORT_URL, json=True)
    exporter, _ = make_exporter(tokens)
    checks = []

    def check():
        checks.append(1)
        if len(checks) == 2:
            raise RuntimeError("cancelado")

    with pytest.raises(RuntimeError, match="cancelado"):
        exporter.export_project(PROJECT_ID, tmp_path, check=check)

    aborts = [c for c in responses.calls if "abort-job" in c.request.url]
    assert len(aborts) == 1 and query(aborts[0])["job-id"] == [PROJECT_JOB]
    assert aborts[0].request.method == "POST"
    assert "Exportação interrompida no BIMcloud" in caplog.text


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({"json": False}, "O BIMcloud não interrompeu a exportação"),
        (
            {"status": 430, "json": {"error-code": 6, "error-message": "EntityNotFoundError"}},
            "Não foi possível interromper a exportação",
        ),
    ],
)
@responses.activate
def test_a_failed_abort_is_only_a_warning(tokens, tmp_path, caplog, answer, message):
    mock_project_export(job_with("running"), data_server=False)
    responses.post(ABORT_URL, **answer)
    clock = iter(range(0, 10_000, 60))
    exporter, _ = make_exporter(tokens, monotonic=lambda: next(clock), job_timeout_seconds=120)

    with pytest.raises(ExportError, match="não terminou em 2 minutos"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert message in caplog.text


@responses.activate
def test_a_job_with_no_progress_is_aborted_and_becomes_an_error(tokens, tmp_path):
    mock_project_export(job_with("running", 1), data_server=False)
    responses.post(ABORT_URL, json=True)
    clock = iter(range(0, 100_000, 60))
    exporter, _ = make_exporter(tokens, monotonic=lambda: next(clock), stall_seconds=20 * 60)

    with pytest.raises(ExportError, match="20 minutos parada no BIMcloud"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert [c for c in responses.calls if "abort-job" in c.request.url]


@responses.activate
def test_a_job_that_failed_on_the_server_is_not_aborted(tokens, tmp_path):
    failed = {**fixture("jobs-create-library-backup-failed.json")[0], "id": PROJECT_JOB}
    mock_project_export([failed], data_server=False)
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="failed"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert not [c for c in responses.calls if "abort-job" in c.request.url]


@responses.activate
def test_running_exports_counts_only_this_users_jobs(tokens):
    mine = job_with("running")[0]
    mine["data"]["userId"] = tokens.user_id
    other = {**job_with("starting")[0], "authorId": "outra-pessoa", "data": {"userId": "outra"}}
    finished = {**mine, "status": "completed"}
    responses.post(JOBS_URL, json=[mine, other, finished])
    exporter, _ = make_exporter(tokens)

    assert exporter.running_exports() == 1
    criterion = json.loads(responses.calls[0].request.body)
    assert {"$eq": {"jobType": ["exportProject", "exportLibrary"]}} in criterion["$and"]


@responses.activate
def test_expired_token_while_polling_and_downloading_is_refreshed(tokens, tmp_path):
    expired = fixture("token-expired-401.json")
    mock_project_export()
    responses.post(JOBS_URL, status=401, json=expired)
    responses.post(f"{OAUTH}/token", json=token_response("access-2", "refresh-2"))
    responses.post(JOBS_URL, json=fixture("jobs-export-project-completed.json"))
    responses.get(DATA_URL, status=401, json=expired)
    responses.post(f"{OAUTH}/token", json=token_response("access-3", "refresh-3"))
    responses.get(DATA_URL, body=b"ok")
    exporter, _ = make_exporter(tokens)

    exported = exporter.export_project(PROJECT_ID, tmp_path)

    assert exported.path.read_bytes() == b"ok"
    assert responses.calls[3].request.headers["Authorization"] == "Bearer access-2"
    assert query(responses.calls[-1])["access_token"] == ["access-3"]
    downloads = [c for c in responses.calls if "get-file" in c.request.url]
    assert len(downloads) == 2
    assert all("Authorization" not in c.request.headers for c in downloads)


@responses.activate
def test_public_get_job_is_used_when_the_criterion_endpoint_is_missing(tokens, tmp_path):
    mock_project_export()
    responses.post(JOBS_URL, status=404)
    responses.get(
        f"{SERVER}/management/client/get-job",
        json=fixture("jobs-export-project-completed.json")[0],
    )
    responses.get(DATA_URL, body=b"x")
    exporter, _ = make_exporter(tokens)

    exporter.export_project(PROJECT_ID, tmp_path)

    assert query(responses.calls[2]) == {"job-id": [PROJECT_JOB]}


@responses.activate
def test_limit_check_stops_the_download_and_removes_the_partial_file(tokens, tmp_path):
    mock_project_export(fixture("jobs-export-project-completed.json"))
    responses.get(DATA_URL, body=b"x" * 10)
    exporter, _ = make_exporter(tokens)
    calls = []

    def check():
        # First call: while polling the job; second: the first downloaded chunk.
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("limite")

    with pytest.raises(RuntimeError):
        exporter.export_project(PROJECT_ID, tmp_path, check=check)

    assert list(tmp_path.iterdir()) == []


@responses.activate
def test_connection_errors_do_not_expose_tokens(tokens, tmp_path):
    mock_project_export(fixture("jobs-export-project-completed.json"))
    responses.get(
        DATA_URL,
        body=requests.ConnectionError(
            "Max retries exceeded with url: /file-manager-service/get-file"
            "?session-id=SEGREDO1&access_token=SEGREDO2"
        ),
    )
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError) as error:
        exporter.export_project(PROJECT_ID, tmp_path)

    assert "SEGREDO" not in str(error.value)
    assert "session-id=<REMOVIDO>" in str(error.value)


@responses.activate
def test_existing_file_is_not_overwritten(tokens, tmp_path):
    mock_project_export(fixture("jobs-export-project-completed.json"))
    (tmp_path / "ProjetoExemplo.BIMProject29").write_bytes(b"old")
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="Já existe"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert (tmp_path / "ProjetoExemplo.BIMProject29").read_bytes() == b"old"


@responses.activate
def test_latest_backup_skips_pending_backups(tokens):
    url = f"{LATEST}/get-resource-backups-by-criterion"
    responses.post(url, json=fixture("resource-backups.json"))
    responses.post(url, json=fixture("resource-backups.json"))
    exporter, _ = make_exporter(tokens)

    assert exporter.latest_backup(PROJECT_ID, PLN_FORMATS) is None
    backup = exporter.latest_backup(PROJECT_ID, {BACKUP_FORMAT})

    assert backup["$fileSize"] == 5435493
    assert json.loads(responses.calls[0].request.body) == {"ids": [PROJECT_ID], "criterion": {}}
    assert query(responses.calls[0])["sort-by"] == ["$time"]


@responses.activate
def test_latest_backup_picks_the_newest_done_pln(tokens):
    done = fixture("resource-backups.json")[1]
    done.update({"$statusId": "_server.backup.status.done", "$fileSize": 7, "$time": 100})
    newer = {**done, "id": "newer", "$time": 200}
    responses.post(f"{LATEST}/get-resource-backups-by-criterion", json=[done, newer])
    exporter, _ = make_exporter(tokens)

    assert exporter.latest_backup(PROJECT_ID, PLN_FORMATS)["id"] == "newer"


@responses.activate
def test_download_backup_checks_the_size(tokens, tmp_path):
    backup = fixture("resource-backups.json")[0]
    responses.get(f"{LATEST}/download-backup", body=b"12345")
    responses.get(f"{LATEST}/download-backup", body=b"short")
    exporter, _ = make_exporter(tokens)

    good = {**backup, "$fileSize": 5}
    assert exporter.download_backup(good, tmp_path / "a.BIMProject") == 5
    params = query(responses.calls[0])
    assert params["backup-id"] == [backup["id"]]
    assert params["resource-id"] == [PROJECT_ID]
    assert params["access_token"] == ["access-1"]
    assert "Authorization" not in responses.calls[0].request.headers

    with pytest.raises(ExportError, match="incompleto"):
        exporter.download_backup(backup, tmp_path / "b.BIMProject")
    assert not (tmp_path / "b.BIMProject").exists()
    assert not (tmp_path / "b.BIMProject.part").exists()


FILE_HOST = "https://bimcloud.example.invalid/blob-store/arquivo"


@responses.activate
def test_download_backup_follows_a_redirect_to_bimcloud_without_the_token(tokens, tmp_path, caplog):
    caplog.set_level("INFO", logger="bimcloud_backup")
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 5}
    responses.get(
        f"{LATEST}/download-backup",
        status=302,
        headers={"Location": f"{FILE_HOST}?ticket=SEGREDO"},
    )
    responses.get(FILE_HOST, body=b"12345")
    exporter, _ = make_exporter(tokens)

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 5

    followed = responses.calls[1].request
    # The address as BIMcloud gave it, no token added to it. It is the server itself, so the
    # login goes once, in the header.
    assert query(responses.calls[1]) == {"ticket": ["SEGREDO"]}
    assert followed.headers["Authorization"] == "Bearer access-1"
    assert "Download redirecionado para bimcloud.example.invalid" in caplog.text
    assert "SEGREDO" not in caplog.text


@responses.activate
def test_download_backup_follows_a_redirect_to_the_data_server_connection_urls(tokens, tmp_path):
    data_host = "https://dados.example.invalid:24443/blob"
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 3, "$serverId": "ms1"}
    responses.get(f"{LATEST}/download-backup", status=302, headers={"Location": data_host})
    responses.get(
        f"{SERVER}/management/client/get-resource",
        json={"id": "ms1", "connectionUrls": ["$protocol//dados.example.invalid:24443"]},
    )
    responses.get(data_host, body=b"abc")
    exporter, _ = make_exporter(tokens)

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 3


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("https://outro.example.invalid/arquivo", "outro.example.invalid, que não pertence"),
        ("http://bimcloud.example.invalid/arquivo", "sem https"),
        ("", "sem https"),
    ],
)
@responses.activate
def test_download_backup_refuses_a_redirect_outside_bimcloud(tokens, tmp_path, location, message):
    backup = fixture("resource-backups.json")[0]
    responses.get(f"{LATEST}/download-backup", status=302, headers={"Location": location})
    exporter, _ = make_exporter(tokens)

    with pytest.raises(BimcloudError, match=message):
        exporter.download_backup(backup, tmp_path / "a.pln")

    # Nothing was requested from the refused address.
    assert all("arquivo" not in call.request.url for call in responses.calls)
    assert not (tmp_path / "a.pln.part").exists()


@pytest.mark.parametrize("location", ["#parte", "?pagina=2", "/blob-store/arquivo"])
@responses.activate
def test_a_relative_redirect_never_takes_the_token_along(tokens, tmp_path, location):
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 3}
    responses.get(f"{LATEST}/download-backup", status=302, headers={"Location": location})
    responses.get(f"{LATEST}/download-backup", body=b"abc")
    responses.get(FILE_HOST, body=b"abc")
    exporter, _ = make_exporter(tokens)
    exporter._client._session.cookies.set("sessao", "SEGREDO")

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 3

    followed = responses.calls[-1].request
    assert "access_token" not in followed.url and "access-1" not in followed.url
    # Relative addresses are on the server itself: the login only in the header.
    assert followed.headers["Authorization"] == "Bearer access-1"
    assert "Cookie" not in followed.headers


@responses.activate
def test_a_redirect_with_user_and_password_is_refused(tokens, tmp_path):
    backup = fixture("resource-backups.json")[0]
    responses.get(
        f"{LATEST}/download-backup",
        status=302,
        headers={"Location": "https://u:p@bimcloud.example.invalid/blob-store/arquivo"},
    )
    exporter, _ = make_exporter(tokens)

    with pytest.raises(BimcloudError, match="usuário e senha"):
        exporter.download_backup(backup, tmp_path / "a.pln")

    assert len(responses.calls) == 1


@responses.activate
def test_a_redirect_to_the_server_itself_sends_the_login_in_the_header_only(
    tokens, tmp_path, caplog
):
    caplog.set_level("INFO", logger="bimcloud_backup")
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 3}
    responses.get(
        f"{LATEST}/download-backup",
        status=302,
        headers={"Location": f"{FILE_HOST}?ticket=1"},
    )

    def only_the_header(request):
        if request.headers.get("Authorization") != "Bearer access-1":
            return 430, {}, json.dumps({"error-code": 9, "error-message": "AccessDeniedError"})
        if "access_token" in request.url:
            return 400, {}, json.dumps({"error": "only one authentication method is allowed"})
        return 200, {}, b"abc"

    responses.add_callback(responses.GET, FILE_HOST, callback=only_the_header)
    exporter, _ = make_exporter(tokens)
    exporter._client._session.cookies.set("sessao", "SEGREDO")

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 3
    followed = responses.calls[-1].request
    assert "Cookie" not in followed.headers
    assert "cabeçalho Authorization" in caplog.text


@responses.activate
def test_a_redirect_to_the_server_refreshes_an_expired_token_once(tokens, tmp_path):
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 3}
    responses.get(f"{LATEST}/download-backup", status=302, headers={"Location": FILE_HOST})
    responses.get(FILE_HOST, status=401, json=fixture("token-expired-401.json"))
    responses.post(f"{OAUTH}/token", json=token_response("access-2", "refresh-2"))
    responses.get(FILE_HOST, body=b"abc")
    exporter, _ = make_exporter(tokens)

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 3
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer access-2"


@responses.activate
def test_a_redirect_to_another_port_of_the_server_gets_no_login(tokens, tmp_path):
    other_port = "https://bimcloud.example.invalid:8443/blob-store/arquivo"
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 3, "$serverId": "ms1"}
    responses.get(f"{LATEST}/download-backup", status=302, headers={"Location": other_port})
    responses.get(
        f"{SERVER}/management/client/get-resource",
        json={"id": "ms1", "connectionUrls": ["$protocol//bimcloud.example.invalid:8443"]},
    )
    responses.get(other_port, body=b"abc")
    exporter, _ = make_exporter(tokens)

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 3
    assert "Authorization" not in responses.calls[-1].request.headers


ASCII_IDN = "https://xn--fa-hia.example"


@pytest.mark.parametrize(
    ("server", "location"),
    [
        # Unicode on either side: the login never goes, whatever each library makes of it.
        ("https://fa\u00df.example", f"{ASCII_IDN}/blob-store/arquivo"),
        ("https://fa\u00df.example", "https://fass.example/blob-store/arquivo"),
        (ASCII_IDN, "https://fass.example/blob-store/arquivo"),
    ],
)
@responses.activate
def test_an_international_host_never_gets_the_login(tokens, tmp_path, server, location):
    client = ManagerClient(requests.Session(), server, "client", tokens)
    exporter = Exporter(client, sleep=lambda _s: None)
    backup = fixture("resource-backups.json")[0]
    responses.get(
        f"{ASCII_IDN}/management/latest/download-backup",
        status=302,
        headers={"Location": location},
    )
    responses.get(f"{ASCII_IDN}/blob-store/arquivo", body=b"abc")
    responses.get("https://fass.example/blob-store/arquivo", body=b"abc")

    with pytest.raises(BimcloudError, match="não pertence ao BIMcloud"):
        exporter.download_backup(backup, tmp_path / "a.pln")

    assert not any("blob-store" in call.request.url for call in responses.calls)


@responses.activate
def test_the_same_ascii_server_in_punycode_gets_the_login(tokens, tmp_path):
    client = ManagerClient(requests.Session(), ASCII_IDN, "client", tokens)
    exporter = Exporter(client, sleep=lambda _s: None)
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 3}
    responses.get(
        f"{ASCII_IDN}/management/latest/download-backup",
        status=302,
        headers={"Location": f"{ASCII_IDN.upper()}/blob-store/arquivo"},
    )
    responses.get(f"{ASCII_IDN}/blob-store/arquivo", body=b"abc")

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 3
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer access-1"


@responses.activate
def test_download_backup_stops_a_redirect_loop(tokens, tmp_path):
    backup = fixture("resource-backups.json")[0]
    responses.get(f"{LATEST}/download-backup", status=302, headers={"Location": FILE_HOST})
    responses.get(FILE_HOST, status=302, headers={"Location": FILE_HOST})
    exporter, _ = make_exporter(tokens)

    with pytest.raises(BimcloudError, match="mais de 3 vezes"):
        exporter.download_backup(backup, tmp_path / "a.pln")

    assert len(responses.calls) == 4


def test_file_name_from_url():
    url = fixture("jobs-export-project-completed.json")[0]["properties"][3]["value"]
    assert file_name_from_url(url) == "ProjetoExemplo.BIMProject29"
    with pytest.raises(ExportError):
        file_name_from_url("https://x.invalid/get-file?session-id=1")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Casa.pln", "Casa.pln"),
        ("..\\..\\Windows\\evil.pln", "evil.pln"),
        ("a/b:c?.pln", "b_c_.pln"),
        ("Obra. ", "Obra"),
    ],
)
def test_safe_file_name(name, expected):
    assert safe_file_name(name) == expected


@pytest.mark.parametrize("name", ["", "..", "dir/"])
def test_safe_file_name_rejects_empty_names(name):
    with pytest.raises(ExportError):
        safe_file_name(name)


def completed_job_with_url(url):
    job = fixture("jobs-export-project-completed.json")
    for prop in job[0]["properties"]:
        if prop["name"] == "absoluteUrl":
            prop["value"] = url
    return job


@pytest.mark.parametrize(
    "url",
    [
        "http://bimcloud-data.example.invalid/file-manager-service/get-file?file-name=P.BIMProject29",
        "https://files.attacker.invalid/get-file?file-name=P.BIMProject29",
        "https://bimcloud.example.invalid.attacker.invalid/get-file?file-name=P.BIMProject29",
        # Another tenant under the same parent domain.
        "https://attacker.example.invalid/get-file?file-name=P.BIMProject29",
        "https://bimcloud-datax.example.invalid/get-file?file-name=P.BIMProject29",
        # Right host of the connectionUrls, wrong port.
        "https://bimcloud.example.invalid.attacker.invalid:22000/get-file?file-name=P.BIMProject29",
    ],
)
@responses.activate
def test_token_is_never_sent_to_an_untrusted_download_url(tokens, tmp_path, url):
    mock_project_export(completed_job_with_url(url), data_server=False)
    responses.get(
        f"{SERVER}/management/client/get-resource",
        json={"id": "ms1", "connectionUrls": ["$protocol//$hostname:22000"]},
    )
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="Endereço de download recusado"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert not any("get-file" in c.request.url for c in responses.calls)
    assert list(tmp_path.iterdir()) == []


@responses.activate
def test_download_url_of_the_model_server_is_trusted(tokens, tmp_path):
    url = "https://files.other.invalid:22000/file-manager-service/get-file?file-name=P.BIMProject29"
    mock_project_export(completed_job_with_url(url), data_server=False)
    responses.get(
        f"{SERVER}/management/client/get-resource",
        json={"id": "ms1", "connectionUrls": ["$protocol//files.other.invalid:22000"]},
    )
    responses.get(url.split("?")[0], body=b"ok")
    exporter, _ = make_exporter(tokens)

    assert exporter.export_project(PROJECT_ID, tmp_path).path.name == "P.BIMProject29"
    lookup = next(c for c in responses.calls if "get-resource" in c.request.url)
    assert query(lookup) == {"resource-id": ["11111111-1111-1111-1111-111111111111"]}


@responses.activate
def test_model_server_url_must_match_host_and_port_exactly(tokens, tmp_path):
    url = "https://files.other.invalid:443/file-manager-service/get-file?file-name=P.BIMProject29"
    mock_project_export(completed_job_with_url(url), data_server=False)
    responses.get(
        f"{SERVER}/management/client/get-resource",
        json={"id": "ms1", "connectionUrls": ["$protocol//files.other.invalid:22000"]},
    )
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="files.other.invalid não pertence"):
        exporter.export_project(PROJECT_ID, tmp_path)


SERVER_SAAS = "https://escritorio.bimcloud.com"


@pytest.mark.parametrize(
    ("host", "server", "trusted"),
    [
        # (a) the server itself; (b) its "-data" sibling, the pattern seen on BIMcloud SaaS.
        ("escritorio.bimcloud.com", SERVER_SAAS, True),
        ("escritorio-data.bimcloud.com", SERVER_SAAS, True),
        ("ESCRITORIO-Data.BIMcloud.com", SERVER_SAAS, True),
        ("escritorio-data.bimcloud.com.", SERVER_SAAS, True),
        ("escritorio-data.bimcloud.com", "https://Escritorio.BIMcloud.com.:443", True),
        # Nothing else, however similar.
        ("attacker.bimcloud.com", SERVER_SAAS, False),
        ("attacker-data.bimcloud.com", SERVER_SAAS, False),
        ("escritorio-datax.bimcloud.com", SERVER_SAAS, False),
        ("xescritorio-data.bimcloud.com", SERVER_SAAS, False),
        ("escritorio-data.bimcloud.com.evil.com", SERVER_SAAS, False),
        ("escritorio.bimcloud.com.evil.com", SERVER_SAAS, False),
        ("data.escritorio.bimcloud.com", SERVER_SAAS, False),
        ("bimcloud.com", SERVER_SAAS, False),
        ("outro.com.br", "https://escritorio.com.br", False),
        ("qualquer.com.br", "https://escritorio.com.br", False),
        ("escritorio-data.com.br", "https://escritorio.com.br", False),
        ("bimcloud-data.com", "https://bimcloud.com", False),
        ("escritorio-data.co.uk", "https://escritorio.co.uk", False),
        # The "-data" rule is only for BIMcloud SaaS (SAAS_DOMAINS), with one label before it.
        ("tenant-data.github.io", "https://tenant.github.io", False),
        ("tenant-data.appspot.com", "https://tenant.appspot.com", False),
        ("a-data.b.bimcloud.com", "https://a.b.bimcloud.com", False),
        ("a.b-data.bimcloud.com", "https://a.b.bimcloud.com", False),
        ("bimcloud-data.example.invalid", "https://bimcloud.example.invalid", False),
        ("bimcloud-data.escritorio.com.br", "https://bimcloud.escritorio.com.br", False),
        # Every other server trusts exactly itself.
        ("tenant.github.io", "https://tenant.github.io", True),
        ("bimcloud.escritorio.com.br", "https://bimcloud.escritorio.com.br", True),
        ("", SERVER_SAAS, False),
    ],
)
def test_is_trusted_host(host, server, trusted):
    assert is_trusted_host(host, server) is trusted


@pytest.mark.parametrize(
    ("host", "server"),
    [
        ("fass.example", "https://fa\u00df.example"),
        ("xn--fa-hia.example", "https://fa\u00df.example"),
        ("fa\u00df.example", "https://xn--fa-hia.example"),
        ("fa\u00df.example", "https://fass.example"),
    ],
)
def test_international_hosts_are_never_taken_for_another_spelling(host, server):
    assert not is_trusted_host(host, server)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Files.Example.invalid./x", ("files.example.invalid", 443)),
        ("https://files.example.invalid:22000/x", ("files.example.invalid", 22000)),
        ("http://files.example.invalid/x", None),
        ("https://files.example.invalid:porta/x", None),
        ("https:///x", None),
    ],
)
def test_https_origin(url, origin):
    assert https_origin(url) == origin


@responses.activate
def test_unexpected_file_extension_fails_without_downloading(tokens, tmp_path):
    url = f"{DATA_URL}?session-id=0&file-name=ProjetoExemplo.zip"
    mock_project_export(completed_job_with_url(url))
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="arquivo inesperado: ProjetoExemplo.zip"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert not any("get-file" in c.request.url for c in responses.calls)


@responses.activate
def test_library_export_must_be_a_bimlibrary(tokens, tmp_path):
    job = fixture("jobs-export-library-completed.json")
    for prop in job[0]["properties"]:
        if prop["name"] == "absoluteUrl":
            prop["value"] = f"{DATA_URL}?file-name=BibliotecaExemplo.BIMProject29"
    responses.get(f"{LATEST}/export-library", json=fixture("export-library-starting.json"))
    responses.post(JOBS_URL, json=job)
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="arquivo inesperado"):
        exporter.export_library("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", tmp_path)


@responses.activate
def test_latest_backup_reads_every_page(tokens):
    url = f"{LATEST}/get-resource-backups-by-criterion"
    pending = fixture("resource-backups.json")[1]
    done = {**pending, "id": "pln-antigo", "$statusId": BACKUP_DONE, "$fileSize": 9, "$time": 5}
    responses.post(url, json=[pending] * BACKUP_PAGE_SIZE)
    responses.post(url, json=[done])
    exporter, _ = make_exporter(tokens)

    assert exporter.latest_backup(PROJECT_ID, PLN_FORMATS)["id"] == "pln-antigo"
    assert [query(c)["skip"] for c in responses.calls] == [["0"], [str(BACKUP_PAGE_SIZE)]]


@responses.activate
def test_latest_backup_stops_after_the_page_limit(tokens, caplog):
    url = f"{LATEST}/get-resource-backups-by-criterion"
    pending = fixture("resource-backups.json")[1]
    responses.post(url, json=[pending] * BACKUP_PAGE_SIZE)
    exporter, _ = make_exporter(tokens)

    assert exporter.latest_backup(PROJECT_ID, PLN_FORMATS) is None
    assert len(responses.calls) == MAX_BACKUP_PAGES
    assert "truncada" in caplog.text


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CON", "_CON"),
        ("nul.BIMProject29", "_nul.BIMProject29"),
        ("COM1.pln", "_COM1.pln"),
        ("Lpt9.BIMLibrary", "_Lpt9.BIMLibrary"),
        ("CONSOLE.pln", "CONSOLE.pln"),
        ("COM10.pln", "COM10.pln"),
    ],
)
def test_safe_file_name_avoids_windows_device_names(name, expected):
    assert safe_file_name(name) == expected


@responses.activate
def test_outside_saas_the_data_server_needs_the_connection_urls(tokens, tmp_path):
    mock_project_export(fixture("jobs-export-project-completed.json"), data_server=False)
    responses.get(f"{SERVER}/management/client/get-resource", json={"id": "ms1"})
    exporter, _ = make_exporter(tokens)

    with pytest.raises(ExportError, match="bimcloud-data.example.invalid não pertence"):
        exporter.export_project(PROJECT_ID, tmp_path)

    assert not any("get-file" in c.request.url for c in responses.calls)


@responses.activate
def test_download_backup_rejecting_two_auth_methods_is_not_triggered(tokens, tmp_path):
    """download-backup answers 400 when the header and the query token come together."""

    def only_one_method(request):
        if "Authorization" in request.headers:
            body = {"error": "invalid_request", "error_description": "only one authentication"}
            return 400, {}, json.dumps(body)
        return 200, {}, b"12345"

    responses.add_callback(responses.GET, f"{LATEST}/download-backup", callback=only_one_method)
    exporter, _ = make_exporter(tokens)
    backup = {**fixture("resource-backups.json")[0], "$fileSize": 5}

    assert exporter.download_backup(backup, tmp_path / "a.pln") == 5


@pytest.mark.parametrize("name", ["COM¹", "com².pln", "LPT³.BIMProject29", "lpt¹.txt"])
def test_safe_file_name_avoids_superscript_device_names(name):
    assert safe_file_name(name) == f"_{name}"


@responses.activate
def test_library_export_can_include_the_server_snapshots(tokens, tmp_path):
    responses.get(f"{LATEST}/export-library", json=fixture("export-library-starting.json"))
    responses.post(JOBS_URL, json=fixture("jobs-export-library-completed.json"))
    mock_data_server()
    responses.get(DATA_URL, body=b"lib")
    exporter, _ = make_exporter(tokens)

    exporter.export_library("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", tmp_path, include_backups=True)

    start = query(responses.calls[0])
    assert start["include-automatic-backups"] == ["true"]
    assert start["include-manual-backups"] == ["true"]
