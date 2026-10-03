import threading

import pytest
import requests
import responses

from bimcloud_backup import service
from bimcloud_backup.backup import BackupCancelled
from bimcloud_backup.config import Config
from bimcloud_backup.errors import ApiError, AuthError, BimcloudError
from bimcloud_backup.state import STATUS_CANCELLED
from tests.conftest import OAUTH, MemoryTokenStore, token_response

SECRET_URL = "https://data.example.invalid/get-file?session-id=S3GR3D0&access_token=T0K3N"


@pytest.fixture
def recorded(monkeypatch):
    runs = []
    monkeypatch.setattr(service, "save_last_run", runs.append)
    return runs


def config(tmp_path):
    return Config(server_url="https://example.bimcloud.com", backup_dir=tmp_path)


def fail_with(monkeypatch, error):
    def connect(session, config, store):
        raise error

    monkeypatch.setattr(service, "connect", connect)


def test_failure_with_secrets_is_raised_and_recorded_without_them(tmp_path, monkeypatch, recorded):
    fail_with(monkeypatch, requests.ConnectionError(f"Max retries exceeded with url: {SECRET_URL}"))

    with pytest.raises(BimcloudError) as error:
        service.backup_now(config(tmp_path), MemoryTokenStore())

    for text in (str(error.value), recorded[0].message):
        assert "S3GR3D0" not in text and "T0K3N" not in text
        assert "session-id=<REMOVIDO>" in text


def test_failure_without_secrets_keeps_its_type(tmp_path, monkeypatch, recorded):
    fail_with(monkeypatch, AuthError("nenhum acesso guardado neste computador"))

    with pytest.raises(AuthError):
        service.backup_now(config(tmp_path), MemoryTokenStore())

    assert recorded[0].message == "nenhum acesso guardado neste computador"


class TreeClient:
    tree = {
        "projectRoot": [
            {"id": "f2", "type": "resourceGroup", "name": "Obras", "$path": "Project Root/Obras"},
            {
                "id": "f1",
                "type": "resourceGroup",
                "name": "Pastas Exemplo",
                "$path": "Project Root/Pastas Exemplo",
            },
            {"id": "p0", "type": "project", "$path": "Project Root/Solto"},
        ],
        "f1": [
            {
                "id": "f3",
                "type": "resourceGroup",
                "name": "Projeto Teste",
                "$path": "Project Root/Pastas Exemplo/Projeto Teste",
            },
            {"id": "p1", "type": "project", "$path": "Project Root/Pastas Exemplo/A"},
            {"id": "p2", "type": "project", "$path": "Project Root/Pastas Exemplo/B"},
            {"id": "l1", "type": "library", "$path": "Project Root/Pastas Exemplo/L"},
            {"id": "b1", "type": "blob", "$path": "Project Root/Pastas Exemplo/x.pdf"},
        ],
    }

    def get_resource(self, resource_id):
        return {"id": resource_id, "$path": "Project Root"}

    def get_children(self, parent_id):
        return self.tree.get(parent_id, [])


def flat(tree):
    return [r for items in tree.values() for r in items]


class FlatClient(TreeClient):
    """Answers the single listing of the whole tree, as get-resources-by-criterion does."""

    def __init__(self):
        self.criteria = []

    def find_resources(self, criterion, check=lambda: None):
        self.criteria.append(criterion)
        check()
        return flat(self.tree)


def test_the_whole_tree_comes_in_one_listing_with_counts_of_everything_below():
    client = FlatClient()
    tree = service.FolderBrowser(client).tree()

    assert client.criteria == [{"$eq": {"type": ["resourceGroup", "project", "library"]}}]
    root = tree[""]
    assert root.folders == [("Obras", "Obras"), ("Pastas Exemplo", "Pastas Exemplo")]
    assert root.items == [("Solto", "Solto", "project")]
    # The root counts everything; each subfolder too, before it is ever opened.
    assert (root.projects, root.libraries) == (3, 1)
    assert root.totals == {"Obras": (0, 0), "Pastas Exemplo": (2, 1)}
    level = tree["Pastas Exemplo"]
    assert level.folders == [("Projeto Teste", "Pastas Exemplo/Projeto Teste")]
    assert [name for name, *_ in level.items] == ["A", "B", "L"]
    assert tree["Pastas Exemplo/Projeto Teste"].folders == []


def test_an_item_whose_folder_was_not_listed_still_has_a_place():
    client = FlatClient()
    client.tree = {
        "x": [
            {"id": "p9", "type": "project", "$path": "Project Root/Arquivo/2024/ProjetoA"},
        ]
    }

    tree = service.FolderBrowser(client).tree()

    assert tree[""].folders == [("Arquivo", "Arquivo")]
    assert tree["Arquivo"].folders == [("2024", "Arquivo/2024")]
    assert tree["Arquivo/2024"].items == [("ProjetoA", "Arquivo/2024/ProjetoA", "project")]
    assert tree[""].totals == {"Arquivo": (1, 0)}


def test_an_item_listed_twice_counts_once():
    client = FlatClient()
    project = {"id": "p1", "type": "project", "$path": "Project Root/Obras/ProjetoA"}
    client.tree = {
        "x": [
            {"id": "f1", "type": "resourceGroup", "$path": "Project Root/Obras"},
            project,
            dict(project),  # the same project again, on the next page
        ]
    }

    tree = service.FolderBrowser(client).tree()

    assert tree["Obras"].items == [("ProjetoA", "Obras/ProjetoA", "project")]
    assert tree[""].totals == {"Obras": (1, 0)}


def test_what_is_outside_the_root_is_left_out():
    client = FlatClient()
    client.tree = {
        "x": [
            {"id": "f1", "type": "resourceGroup", "$path": "Project Root/Obras"},
            {"id": "p1", "type": "project", "$path": "Outra Raiz/ProjetoA"},
            {"id": "p2", "type": "project", "name": "ProjetoB"},  # no path at all
        ]
    }

    tree = service.FolderBrowser(client).tree()

    assert tree[""].items == [] and tree[""].projects == 0


class RefusingClient(TreeClient):
    """A BIMcloud that does not take the one-query listing, or answers it wrongly."""

    def __init__(self, answer):
        self.answer = answer
        self.folders_listed = []

    def find_resources(self, criterion, check=lambda: None):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def get_children(self, parent_id):
        self.folders_listed.append(parent_id)
        return super().get_children(parent_id)


@pytest.mark.parametrize(
    "answer",
    [
        ApiError("BadRequest: critério inválido", status=400),
        # The filter ignored: plain files came along, so the pages cannot be trusted either.
        [{"id": "b1", "type": "blob", "$path": "Project Root/leia-me.pdf"}],
    ],
)
def test_without_the_one_query_listing_it_goes_folder_by_folder(answer, caplog):
    client = RefusingClient(answer)

    tree = service.FolderBrowser(client).tree()

    assert "listando pasta a pasta" in caplog.text
    assert client.folders_listed[0] == "projectRoot"
    assert (tree[""].projects, tree[""].libraries) == (3, 1)
    assert tree[""].totals["Pastas Exemplo"] == (2, 1)


def test_an_empty_answer_is_checked_against_the_root(caplog):
    client = RefusingClient([])

    tree = service.FolderBrowser(client).tree()

    assert "nenhum item na resposta" in caplog.text
    assert (tree[""].projects, tree[""].libraries) == (3, 1)


def test_an_empty_bimcloud_stays_empty():
    client = RefusingClient([])
    client.tree = {}

    tree = service.FolderBrowser(client).tree()

    assert tree[""].folders == [] and tree[""].items == []
    assert client.folders_listed == ["projectRoot"]  # only the check of the root


def test_cancelling_the_listing_does_not_fall_back(tmp_path):
    client = RefusingClient(BimcloudError("Carregamento cancelado"))

    with pytest.raises(BimcloudError, match="cancelado"):
        service.FolderBrowser(client).tree()

    assert client.folders_listed == []


def test_thousands_of_items_are_built_quickly():
    client = FlatClient()
    client.tree = {
        "x": [
            {"id": f"f{i}", "type": "resourceGroup", "$path": f"Project Root/Obra {i:03d}"}
            for i in range(300)
        ]
        + [
            {"id": f"p{i}", "type": "project", "$path": f"Project Root/Obra {i % 300:03d}/P{i}"}
            for i in range(6000)
        ]
    }

    tree = service.FolderBrowser(client).tree()

    assert (tree[""].projects, len(tree[""].folders)) == (6000, 300)
    assert tree["Obra 007"].projects == 20


class LockedVault(MemoryTokenStore):
    """Credential store that can be read but refuses to save (a locked vault)."""

    def save(self, server_url, refresh_token):
        raise RuntimeError(f"cofre bloqueado; recusado: {refresh_token}")


@responses.activate
def test_failing_to_save_the_renewed_token_does_not_stop_the_backup(tmp_path, caplog):
    responses.post(f"{OAUTH}/token", json=token_response("access-2", "refresh-NOVO"))
    store = LockedVault("refresh-1")

    with requests.Session() as session:
        client = service.connect(session, config(tmp_path), store)

    assert client.user_id == "user-1"
    assert "Não foi possível guardar o acesso renovado" in caplog.text
    # The error text of this fake store quotes the token: it must still not reach the log.
    assert "refresh-NOVO" not in caplog.text


def test_cancelled_backup_is_recorded_as_cancelled(tmp_path, monkeypatch, recorded):
    fail_with(monkeypatch, BackupCancelled("Backup cancelado"))

    with pytest.raises(BackupCancelled):
        service.backup_now(config(tmp_path), MemoryTokenStore(), cancel=threading.Event())

    assert recorded[0].status == STATUS_CANCELLED
    assert recorded[0].message == "Cancelado pelo usuário"


def test_ctrl_c_is_recorded_as_cancelled(tmp_path, monkeypatch, recorded):
    fail_with(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        service.backup_now(config(tmp_path), MemoryTokenStore())

    assert recorded[0].status == STATUS_CANCELLED
    assert "Ctrl+C" in recorded[0].message
