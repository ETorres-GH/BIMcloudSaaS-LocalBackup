import json
import logging
import shutil
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bimcloud_backup import __version__
from bimcloud_backup.backup import (
    QUEUED_PER_THREAD,
    BackupAborted,
    BackupCancelled,
    format_count,
    format_size,
    relative_path,
    run_backup,
)
from bimcloud_backup.config import Config
from bimcloud_backup.errors import BimcloudError
from bimcloud_backup.exporter import PLN_FORMATS, ExportedFile, ExportError
from bimcloud_backup.failures import KIND_LIBRARY
from bimcloud_backup.retention import backup_folder_name
from bimcloud_backup.state import MANIFEST_NAME, MANIFEST_TMP_NAME, ManifestFile, load_manifest

NOW = datetime(2026, 9, 27, 23, 0, 0)
ROOT = "Project Root"
MODIFIED = 1_727_467_200_000


def res(id_, type_, path, **extra):
    return {"id": id_, "type": type_, "$path": f"{ROOT}/{path}", **extra}


TREE = {
    "projectRoot": [
        res("f1", "resourceGroup", "Obras"),
        res("b1", "blob", "Leia-me.pdf", **{"$size": 3, "$modifiedDate": MODIFIED}),
        res("p1", "project", "Casa Modelo"),
        res("l1", "library", "Biblioteca Escritório"),
    ],
    "f1": [
        res(
            "b2",
            "blob",
            "Obras/planta.dwg",
            **{"$size": 5, "$modifiedDate": MODIFIED + 1},
        )
    ],
}


class FakeClient:
    def __init__(self, tree=TREE):
        self.tree = tree

    def get_resource(self, resource_id):
        return {"id": resource_id, "type": "resourceGroup", "$path": ROOT}

    def get_resource_by_path(self, path):
        for items in self.tree.values():
            for r in items:
                if r["$path"] == path:
                    return r
        return None

    def walk(self, parent_id="projectRoot"):
        for r in self.tree.get(parent_id, []):
            yield r
            if r["type"] == "resourceGroup":
                yield from self.walk(r["id"])


class FakeDownloader:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def download(self, blob, target, check=None):
        self.calls.append(blob["id"])
        if blob["id"] in self.fail:
            raise BimcloudError("sem conexão")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = b"x" * blob["$size"]
        target.write_bytes(data)
        return len(data)


def make_config(tmp_path, **kwargs):
    base = Config(
        server_url="https://example.bimcloud.com",
        backup_dir=tmp_path / "backups",
        min_free_space_gb=0,
    )
    return replace(base, **kwargs)


def old_backup(config, days):
    folder = config.backup_dir / backup_folder_name(NOW - timedelta(days=days))
    folder.mkdir(parents=True)
    return folder


def test_downloads_files_keeping_folder_structure(tmp_path):
    config = make_config(tmp_path)
    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW)

    assert result.ok
    assert result.folder == config.backup_dir / backup_folder_name(NOW)
    assert (result.folder / "Leia-me.pdf").read_bytes() == b"xxx"
    assert (result.folder / "Obras" / "planta.dwg").stat().st_size == 5
    assert (result.files, result.bytes) == (2, 8)
    assert result.pending == [f"{ROOT}/Casa Modelo", f"{ROOT}/Biblioteca Escritório"]
    manifest = json.loads((result.folder / ".backup.json").read_text(encoding="utf-8"))
    assert manifest["created_at"] == NOW.isoformat()
    assert manifest["version"] == __version__
    assert manifest["server_url"] == config.server_url
    assert manifest["source_path"] == [ROOT]
    assert manifest["by_type"] == {
        "resourceGroup": {"count": 1, "bytes": 0},
        "blob": {"count": 2, "bytes": 8},
    }
    assert manifest["files"] == [
        {"path": "Obras/planta.dwg", "$size": 5, "$modifiedDate": MODIFIED + 1},
        {"path": "Leia-me.pdf", "$size": 3, "$modifiedDate": MODIFIED},
    ]
    assert manifest["errors"] == []


def test_respects_what_to_copy(tmp_path):
    config = make_config(
        tmp_path, include_files=False, include_projects=False, include_libraries=True
    )
    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW)
    assert result.files == 0
    assert result.pending == [f"{ROOT}/Biblioteca Escritório"]


def test_source_folder_limits_the_backup_and_keeps_the_path_from_the_root(tmp_path):
    config = make_config(tmp_path, source_folders=("Obras",))
    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW)
    assert sorted(p.name for p in result.folder.iterdir()) == [MANIFEST_NAME, "Obras"]
    assert (result.folder / "Obras" / "planta.dwg").exists()
    assert load_manifest(result.folder).source_path == [f"{ROOT}/Obras"]


MULTI_TREE = {
    "projectRoot": [
        res("exemplos", "resourceGroup", "Pastas Exemplo"),
        res("obras", "resourceGroup", "Obras"),
        res("fora", "blob", "fora.pdf", **{"$size": 1, "$modifiedDate": MODIFIED}),
    ],
    "exemplos": [
        res("teste", "resourceGroup", "Pastas Exemplo/Projeto Teste"),
        res("other", "resourceGroup", "Pastas Exemplo/Outro"),
    ],
    "teste": [res("b1", "blob", "Pastas Exemplo/Projeto Teste/a.pdf", **{"$size": 2})],
    "other": [res("b2", "blob", "Pastas Exemplo/Outro/b.pdf", **{"$size": 2})],
    "obras": [
        res("vazia", "resourceGroup", "Obras/Vazia"),
        res("b3", "blob", "Obras/a.pdf", **{"$size": 3}),
    ],
}


def test_several_source_folders_keep_their_paths_and_never_collide(tmp_path):
    config = make_config(
        tmp_path, source_folders=("Obras/Vazia", "Pastas Exemplo/Projeto Teste", "Obras")
    )
    downloader = FakeDownloader()

    result = run_backup(config, FakeClient(MULTI_TREE), downloader, now=NOW)

    assert result.ok
    assert sorted(downloader.calls) == ["b1", "b3"]
    assert (result.folder / "Pastas Exemplo" / "Projeto Teste" / "a.pdf").read_bytes() == b"xx"
    assert (result.folder / "Obras" / "a.pdf").read_bytes() == b"xxx"
    assert (result.folder / "Obras" / "Vazia").is_dir()
    assert not (result.folder / "Pastas Exemplo" / "Outro").exists()
    assert not (result.folder / "fora.pdf").exists()
    assert load_manifest(result.folder).source_path == [
        f"{ROOT}/Obras",
        f"{ROOT}/Pastas Exemplo/Projeto Teste",
    ]


def test_missing_source_folder_is_an_error_but_the_others_are_copied(tmp_path, caplog):
    config = make_config(tmp_path, source_folders=("Nada", "Obras"))

    result = run_backup(config, FakeClient(MULTI_TREE), FakeDownloader(), now=NOW)

    assert result.errors == [f"{ROOT}/Nada: pasta de origem não encontrada no BIMcloud"]
    assert (result.folder / "Obras" / "a.pdf").exists()
    assert f"Falha: Pasta {ROOT}/Nada: o item não existe mais no BIMcloud" in caplog.text
    assert load_manifest(result.folder).source_path == [f"{ROOT}/Nada", f"{ROOT}/Obras"]


def test_incremental_needs_the_same_list_of_source_folders(tmp_path):
    tree = {
        "projectRoot": [res("obras", "resourceGroup", "Obras")],
        "obras": [res("b1", "blob", "Obras/a.pdf", **{"$size": 2, "$modifiedDate": MODIFIED})],
    }
    config = make_config(tmp_path, source_folders=("Obras",))
    run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW - timedelta(minutes=2))
    same = FakeDownloader()
    run_backup(config, FakeClient(tree), same, now=NOW - timedelta(minutes=1))
    other = FakeDownloader()
    run_backup(replace(config, source_folders=()), FakeClient(tree), other, now=NOW)

    assert same.calls == []
    assert other.calls == ["b1"]


def test_history_retention_removes_expired_backups(tmp_path):
    config = make_config(tmp_path, retention_days=30)
    recent, expired = old_backup(config, 5), old_backup(config, 40)

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW)

    assert result.removed == [expired]
    assert recent.exists() and not expired.exists()


def test_latest_versioning_keeps_only_new_backup(tmp_path):
    config = make_config(tmp_path, versioning="latest")
    old_backup(config, 1)
    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW)
    assert sorted(p.name for p in config.backup_dir.iterdir() if p.is_dir()) == [result.folder.name]


def test_failed_file_keeps_backup_but_skips_retention(tmp_path):
    config = make_config(tmp_path, retention_days=30)
    expired = old_backup(config, 40)

    result = run_backup(config, FakeClient(), FakeDownloader(fail={"b2"}), now=NOW)

    assert not result.ok
    assert result.errors == [f"{ROOT}/Obras/planta.dwg: sem conexão"]
    assert result.folder.exists()
    assert expired.exists()
    manifest = json.loads((result.folder / ".backup.json").read_text(encoding="utf-8"))
    assert manifest["errors"] == [f"{ROOT}/Obras/planta.dwg: sem conexão"]
    assert manifest["by_type"] == {
        "resourceGroup": {"count": 1, "bytes": 0},
        "blob": {"count": 1, "bytes": 3},
    }


def test_reserved_manifest_names_are_reported_and_skipped(tmp_path):
    tree = {
        "projectRoot": [
            res("b1", "blob", MANIFEST_NAME.upper(), **{"$size": 4, "$modifiedDate": MODIFIED}),
            res("b2", "blob", MANIFEST_TMP_NAME, **{"$size": 4, "$modifiedDate": MODIFIED}),
        ]
    }
    downloader = FakeDownloader()

    result = run_backup(make_config(tmp_path), FakeClient(tree), downloader, now=NOW)

    assert downloader.calls == []
    assert len(result.errors) == 2
    assert all("nome reservado" in error for error in result.errors)
    assert load_manifest(result.folder) is not None
    assert not (result.folder / MANIFEST_TMP_NAME).exists()


def incremental_tree(size=4, modified=MODIFIED):
    return {
        "projectRoot": [
            res("b1", "blob", "modelo.bin", **{"$size": size, "$modifiedDate": modified})
        ]
    }


def test_second_unchanged_backup_uses_hardlink_without_download(tmp_path):
    config = make_config(tmp_path)
    tree = incremental_tree()
    first_downloader = FakeDownloader()
    first = run_backup(config, FakeClient(tree), first_downloader, now=NOW - timedelta(minutes=1))
    second_downloader = FakeDownloader()
    second = run_backup(config, FakeClient(tree), second_downloader, now=NOW)

    old_file = first.folder / "modelo.bin"
    new_file = second.folder / "modelo.bin"
    assert first_downloader.calls == ["b1"]
    assert second_downloader.calls == []
    manifest = load_manifest(first.folder)
    assert manifest is not None
    assert manifest.server_url == config.server_url
    assert manifest.source_path == [ROOT]
    assert manifest.files == [ManifestFile("modelo.bin", 4, MODIFIED)]
    assert load_manifest(second.folder) is not None
    assert old_file.samefile(new_file)
    assert old_file.stat().st_ino == new_file.stat().st_ino
    assert new_file.stat().st_nlink >= 2


@pytest.mark.parametrize(
    "changed_tree",
    [
        incremental_tree(size=5),
        incremental_tree(modified=MODIFIED + 1),
        incremental_tree(modified=True),
    ],
)
def test_changed_file_is_downloaded_again(tmp_path, changed_tree):
    config = make_config(tmp_path)
    first = run_backup(
        config,
        FakeClient(incremental_tree()),
        FakeDownloader(),
        now=NOW - timedelta(minutes=1),
    )
    downloader = FakeDownloader()
    second = run_backup(config, FakeClient(changed_tree), downloader, now=NOW)

    assert downloader.calls == ["b1"]
    assert not (first.folder / "modelo.bin").samefile(second.folder / "modelo.bin")


def test_hardlink_failure_falls_back_to_local_copy(tmp_path, monkeypatch, caplog):
    config = make_config(tmp_path)
    tree = incremental_tree()
    first = run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW - timedelta(minutes=1))

    def fail_link(source, target):
        raise OSError("sem suporte")

    monkeypatch.setattr("bimcloud_backup.backup.os.link", fail_link)
    downloader = FakeDownloader()
    second = run_backup(config, FakeClient(tree), downloader, now=NOW)

    assert downloader.calls == []
    assert (second.folder / "modelo.bin").read_bytes() == b"xxxx"
    assert not (first.folder / "modelo.bin").samefile(second.folder / "modelo.bin")
    assert "tentando copiar" in caplog.text


def test_hardlink_and_copy_failures_fall_back_to_download(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    tree = incremental_tree()
    run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW - timedelta(minutes=1))

    def fail_local_reuse(*args):
        raise OSError("falha local")

    monkeypatch.setattr("bimcloud_backup.backup.os.link", fail_local_reuse)
    monkeypatch.setattr("bimcloud_backup.backup.shutil.copy2", fail_local_reuse)
    downloader = FakeDownloader()

    run_backup(config, FakeClient(tree), downloader, now=NOW)

    assert downloader.calls == ["b1"]


@pytest.mark.parametrize("manifest_state", ["missing", "corrupt"])
def test_missing_or_corrupt_previous_manifest_forces_download(tmp_path, manifest_state):
    config = make_config(tmp_path)
    tree = incremental_tree()
    first = run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW - timedelta(minutes=1))
    manifest_path = first.folder / MANIFEST_NAME
    if manifest_state == "missing":
        manifest_path.unlink()
    else:
        manifest_path.write_text("{inválido", encoding="utf-8")
    downloader = FakeDownloader()

    run_backup(config, FakeClient(tree), downloader, now=NOW)

    assert downloader.calls == ["b1"]


@pytest.mark.parametrize("previous_state", ["missing", "wrong-size"])
def test_missing_or_wrong_sized_previous_file_forces_download(tmp_path, previous_state):
    config = make_config(tmp_path)
    tree = incremental_tree()
    first = run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW - timedelta(minutes=1))
    previous_file = first.folder / "modelo.bin"
    if previous_state == "missing":
        previous_file.unlink()
    else:
        previous_file.write_bytes(b"tamanho incorreto")
    downloader = FakeDownloader()

    run_backup(config, FakeClient(tree), downloader, now=NOW)

    assert downloader.calls == ["b1"]


@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        ("server_url", "https://outro.bimcloud.com"),
        ("source_path", ["Project Root/Outra pasta"]),
    ],
)
def test_different_previous_origin_forces_download(tmp_path, field, different_value):
    config = make_config(tmp_path)
    tree = incremental_tree()
    first = run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW - timedelta(minutes=1))
    manifest_path = first.folder / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = different_value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    downloader = FakeDownloader()

    run_backup(config, FakeClient(tree), downloader, now=NOW)

    assert downloader.calls == ["b1"]


def test_retention_does_not_break_hardlinked_new_backup(tmp_path):
    config = make_config(tmp_path, versioning="latest")
    tree = incremental_tree()
    first = run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW - timedelta(minutes=1))
    second = run_backup(config, FakeClient(tree), FakeDownloader(), now=NOW)

    assert not first.folder.exists()
    assert (second.folder / "modelo.bin").read_bytes() == b"xxxx"
    assert (second.folder / "modelo.bin").stat().st_nlink == 1


def test_time_limit_aborts_and_cleans_up(tmp_path):
    config = make_config(tmp_path, max_duration_hours=1)
    clock = iter([0, 0, 7200, 7200, 7200])

    with pytest.raises(BackupAborted, match="Tempo máximo"):
        run_backup(config, FakeClient(), FakeDownloader(), now=NOW, monotonic=lambda: next(clock))

    assert list(config.backup_dir.iterdir()) == [config.backup_dir / ".backup.lock"]


def test_low_disk_space_aborts_before_starting(tmp_path):
    config = make_config(tmp_path, min_free_space_gb=10)
    with pytest.raises(BackupAborted, match="Espaço livre"):
        run_backup(config, FakeClient(), FakeDownloader(), now=NOW, free_space=lambda p: 1)


# ".incompleto-" is how earlier versions named it.
@pytest.mark.parametrize("prefix", [".incomplete-", ".incompleto-"])
def test_leftover_incomplete_folder_is_removed(tmp_path, prefix):
    config = make_config(tmp_path)
    leftover = config.backup_dir / f"{prefix}2026-09-01_230000"
    leftover.mkdir(parents=True)
    run_backup(config, FakeClient(), FakeDownloader(), now=NOW)
    assert not leftover.exists()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{ROOT}/Obras/planta.dwg", Path("Obras", "planta.dwg")),
        (f"{ROOT}/nome com ponto final.", Path("nome com ponto final")),
    ],
)
def test_relative_path(path, expected):
    assert relative_path(path, ROOT) == expected


@pytest.mark.parametrize("path", [f"{ROOT}/../fora", f"{ROOT}/a/./b", f"{ROOT}/x:y"])
def test_relative_path_rejects_unsafe_names(path):
    with pytest.raises(ValueError):
        relative_path(path, ROOT)


NAMES = {
    "p1": "Casa Modelo",
    "l1": "Biblioteca Escritório",
    "pa": "ProjetoA",
    "pc": "ProjetoC",
    "lb": "BibliotecaB",
}
PLN_BACKUP = {
    "id": "backup-1",
    "$resourceId": "p1",
    "$backupFileName": "Casa Modelo - 2026.09.27 16-00.pln",
    "$fileSize": 6,
    "$time": 1_790_000_000_000,
}


class FakeExporter:
    running = 0

    def __init__(self, backups=None, fail=()):
        self.backups = backups or {}
        self.fail = set(fail)
        self.report = None
        self.calls = []
        self.exporting = False

    def watch(self, report):
        self.report = report

    def running_exports(self):
        return self.running

    def export_project(self, project_id, folder, include_backups, check):
        self.calls.append(("bimproject", project_id, include_backups))
        return self._export(project_id, folder / f"{NAMES[project_id]}.BIMProject29", check)

    def export_library(self, library_id, folder, include_backups, check):
        self.calls.append(("bimlibrary", library_id) + ((True,) if include_backups else ()))
        return self._export(library_id, folder / f"{NAMES[library_id]}.BIMLibrary", check)

    def latest_backup(self, resource_id, formats):
        assert formats == PLN_FORMATS
        return self.backups.get(resource_id)

    def download_backup(self, backup, target, check):
        self.calls.append(("pln", backup["id"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"n" * backup["$fileSize"])
        return backup["$fileSize"]

    def _export(self, resource_id, target, check):
        self.exporting = True
        check()
        if resource_id in self.fail:
            raise ExportError(f"Falha ao baixar {target.name}: url?access_token=SEGREDO")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"e" * 7)
        return ExportedFile(target, 7)


def manifest_files(folder):
    return load_manifest(folder).files


def test_exports_projects_and_libraries_into_the_backup_tree(tmp_path):
    config = make_config(tmp_path, include_files=False)
    exporter = FakeExporter()

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert result.ok and result.pending == []
    assert exporter.calls == [("bimproject", "p1", False), ("bimlibrary", "l1")]
    assert (result.folder / "Casa Modelo.BIMProject29").read_bytes() == b"e" * 7
    assert (result.folder / "Biblioteca Escritório.BIMLibrary").exists()
    assert (result.files, result.bytes) == (2, 14)
    manifest = json.loads((result.folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["files"] == [
        {
            "path": "Casa Modelo.BIMProject29",
            "$size": 7,
            "$modifiedDate": None,
            "type": "bimproject",
        },
        {
            "path": "Biblioteca Escritório.BIMLibrary",
            "$size": 7,
            "$modifiedDate": None,
            "type": "bimlibrary",
        },
    ]
    assert manifest["by_type"]["bimproject"] == {"count": 1, "bytes": 7}
    assert manifest["by_type"]["bimlibrary"] == {"count": 1, "bytes": 7}


ITEM_TREE = {
    "projectRoot": [
        res("obras", "resourceGroup", "Obras"),
        res("bib", "resourceGroup", "Bibliotecas"),
    ],
    "obras": [
        res("pa", "project", "Obras/ProjetoA"),
        res("pc", "project", "Obras/ProjetoC"),
        res("b1", "blob", "Obras/memorial.pdf", **{"$size": 2}),
    ],
    "bib": [res("lb", "library", "Bibliotecas/BibliotecaB")],
}


def test_chosen_projects_and_libraries_keep_their_bimcloud_path(tmp_path):
    config = make_config(
        tmp_path, source_projects=("Obras/ProjetoA",), source_libraries=("Bibliotecas/BibliotecaB",)
    )
    exporter = FakeExporter()

    result = run_backup(config, FakeClient(ITEM_TREE), FakeDownloader(), now=NOW, exporter=exporter)

    assert result.ok
    # Only what was chosen: not ProjetoC nor the PDF next to ProjetoA.
    assert exporter.calls == [("bimproject", "pa", False), ("bimlibrary", "lb")]
    assert (result.folder / "Obras" / "ProjetoA.BIMProject29").exists()
    assert (result.folder / "Bibliotecas" / "BibliotecaB.BIMLibrary").exists()
    assert load_manifest(result.folder).source_path == [
        f"{ROOT}/Bibliotecas/BibliotecaB",
        f"{ROOT}/Obras/ProjetoA",
    ]


def test_a_folder_and_single_items_elsewhere_are_copied_together(tmp_path):
    config = make_config(
        tmp_path,
        source_folders=("Bibliotecas",),
        source_projects=("Obras/ProjetoC", "Bibliotecas/Dentro"),
    )
    exporter = FakeExporter()

    result = run_backup(config, FakeClient(ITEM_TREE), FakeDownloader(), now=NOW, exporter=exporter)

    assert result.ok
    assert exporter.calls == [("bimlibrary", "lb"), ("bimproject", "pc", False)]


def test_a_chosen_item_that_is_gone_is_an_error_and_the_rest_is_copied(tmp_path, caplog):
    config = make_config(
        tmp_path,
        source_projects=("Obras/ProjetoA", "Obras/Sumiu", "Bibliotecas/BibliotecaB"),
        source_libraries=("Obras/ProjetoC",),  # a project now, not a library
    )
    exporter = FakeExporter()

    result = run_backup(config, FakeClient(ITEM_TREE), FakeDownloader(), now=NOW, exporter=exporter)

    assert result.errors == [
        f"{ROOT}/Bibliotecas/BibliotecaB: não é mais um projeto no BIMcloud (agora é biblioteca)",
        f"{ROOT}/Obras/Sumiu: projeto não encontrado no BIMcloud",
        f"{ROOT}/Obras/ProjetoC: não é mais uma biblioteca no BIMcloud (agora é projeto)",
    ]
    assert exporter.calls == [("bimproject", "pa", False)]
    assert (result.folder / "Obras" / "ProjetoA.BIMProject29").exists()
    assert [f.kind for f in result.failures] == ["project", "project", "library"]
    assert f"Projeto {ROOT}/Obras/Sumiu" in caplog.text


@pytest.mark.parametrize(
    ("switch", "chosen", "message"),
    [
        (
            "include_projects",
            {"source_projects": ("Obras/ProjetoA",)},
            f"{ROOT}/Obras/ProjetoA: projeto marcado, mas Projetos está desligado",
        ),
        (
            "include_libraries",
            {"source_libraries": ("Bibliotecas/BibliotecaB",)},
            f"{ROOT}/Bibliotecas/BibliotecaB: biblioteca marcada, mas Bibliotecas está desligado",
        ),
    ],
)
def test_a_chosen_item_of_a_switched_off_kind_is_an_error_and_keeps_old_backups(
    tmp_path, switch, chosen, message
):
    config = make_config(tmp_path, versioning="latest", **{switch: False}, **chosen)
    previous = old_backup(config, 1)
    exporter = FakeExporter()

    result = run_backup(config, FakeClient(ITEM_TREE), FakeDownloader(), now=NOW, exporter=exporter)

    assert result.errors == [message]
    assert exporter.calls == []
    # Not a good backup: "keep only the last one" must not delete the previous one.
    assert previous.exists()


def test_the_export_status_reaches_the_progress_and_old_exports_are_reported(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="bimcloud_backup")
    reports = []

    class Watched(FakeExporter):
        running = 2

        def _export(self, resource_id, target, check):
            self.report("na fila do BIMcloud (1 min)")
            seen.append(reports[-1].detail)
            self.report(None)
            return super()._export(resource_id, target, check)

    seen = []
    run_backup(
        make_config(tmp_path, include_files=False),
        FakeClient(),
        FakeDownloader(),
        now=NOW,
        exporter=Watched(),
        progress=reports.append,
    )

    assert seen == ["na fila do BIMcloud (1 min)"] * 2
    assert reports[-1].detail is None
    assert "Já há 2 exportações suas em andamento no BIMcloud" in caplog.text


def test_exports_are_never_reused_from_the_previous_backup(tmp_path):
    config = make_config(tmp_path, include_files=False, include_libraries=False)
    run_backup(
        config,
        FakeClient(),
        FakeDownloader(),
        now=NOW - timedelta(minutes=1),
        exporter=FakeExporter(),
    )
    exporter = FakeExporter()

    run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert exporter.calls == [("bimproject", "p1", False)]


def test_include_backups_in_export_is_passed_on(tmp_path):
    config = make_config(tmp_path, include_libraries=False, include_backups_in_export=True)
    exporter = FakeExporter()
    run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)
    assert exporter.calls == [("bimproject", "p1", True)]


def test_pln_format_saves_the_latest_server_pln(tmp_path):
    config = make_config(tmp_path, include_libraries=False, projects_format="pln")
    exporter = FakeExporter(backups={"p1": PLN_BACKUP})

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert exporter.calls == [("pln", "backup-1")]
    pln = manifest_files(result.folder)[-1]
    assert pln == ManifestFile("Casa Modelo - 2026.09.27 16-00.pln", 6, PLN_BACKUP["$time"], "pln")
    assert (result.folder / pln.path).read_bytes() == b"n" * 6


def test_pln_format_without_a_ready_pln_exports_the_bimproject(tmp_path, caplog):
    config = make_config(tmp_path, include_libraries=False, projects_format="pln")
    exporter = FakeExporter()

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert result.ok
    assert exporter.calls == [("bimproject", "p1", False)]
    assert "exportando o .BIMProject no lugar" in caplog.text


def test_both_formats_save_pln_and_bimproject(tmp_path):
    config = make_config(tmp_path, include_libraries=False, projects_format="both")
    exporter = FakeExporter(backups={"p1": PLN_BACKUP})

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert exporter.calls == [("pln", "backup-1"), ("bimproject", "p1", False)]
    kinds = [f.kind for f in manifest_files(result.folder)]
    assert kinds.count("pln") == kinds.count("bimproject") == 1


def test_unchanged_server_pln_is_reused_from_the_previous_backup(tmp_path):
    config = make_config(
        tmp_path, include_libraries=False, include_files=False, projects_format="pln"
    )
    first = run_backup(
        config,
        FakeClient(),
        FakeDownloader(),
        now=NOW - timedelta(minutes=1),
        exporter=FakeExporter(backups={"p1": PLN_BACKUP}),
    )
    exporter = FakeExporter(backups={"p1": PLN_BACKUP})

    second = run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert exporter.calls == []
    name = PLN_BACKUP["$backupFileName"]
    assert (first.folder / name).samefile(second.folder / name)
    newer = {**PLN_BACKUP, "id": "backup-2", "$time": PLN_BACKUP["$time"] + 1}
    exporter = FakeExporter(backups={"p1": newer})
    run_backup(
        config, FakeClient(), FakeDownloader(), now=NOW + timedelta(minutes=1), exporter=exporter
    )
    assert exporter.calls == [("pln", "backup-2")]


def test_export_failure_is_recorded_without_secrets(tmp_path):
    config = make_config(tmp_path)
    expired = old_backup(config, 90)
    exporter = FakeExporter(fail={"p1"})

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert len(result.errors) == 1
    assert result.errors[0].startswith(f"{ROOT}/Casa Modelo: Falha ao baixar")
    assert "SEGREDO" not in result.errors[0]
    assert (result.folder / "Biblioteca Escritório.BIMLibrary").exists()
    assert expired.exists()


def test_time_limit_during_an_export_aborts_the_run(tmp_path):
    config = make_config(tmp_path, max_duration_hours=1)
    exporter = FakeExporter()

    with pytest.raises(BackupAborted, match="Tempo máximo"):
        run_backup(
            config,
            FakeClient(),
            FakeDownloader(),
            now=NOW,
            monotonic=lambda: 7200 if exporter.exporting else 0,
            exporter=exporter,
        )

    assert list(config.backup_dir.iterdir()) == [config.backup_dir / ".backup.lock"]


def test_file_errors_in_the_manifest_have_no_secrets(tmp_path):
    class LeakyDownloader(FakeDownloader):
        def download(self, blob, target, check=None):
            raise OSError("Max retries exceeded with url: /get-blob-content?session-id=S3GR3D0")

    config = make_config(tmp_path, include_projects=False, include_libraries=False)

    result = run_backup(config, FakeClient(), LeakyDownloader(), now=NOW)

    assert result.errors and all("S3GR3D0" not in e for e in result.errors)
    assert all("S3GR3D0" not in e for e in load_manifest(result.folder).errors)


def test_time_limit_is_checked_while_resolving_the_source_folders(tmp_path):
    config = make_config(tmp_path, max_duration_hours=1, source_folders=("A", "B", "C"))
    clock = iter([0, 0, 0, 7200, 7200])
    client = FakeClient(MULTI_TREE)
    lookups = []
    original = client.get_resource_by_path
    client.get_resource_by_path = lambda path: lookups.append(path) or original(path)

    with pytest.raises(BackupAborted, match="Tempo máximo"):
        run_backup(config, client, FakeDownloader(), now=NOW, monotonic=lambda: next(clock))

    assert lookups == [f"{ROOT}/A"]
    assert list(config.backup_dir.iterdir()) == [config.backup_dir / ".backup.lock"]


def test_time_limit_is_checked_after_resolving_even_if_every_folder_is_missing(tmp_path):
    config = make_config(tmp_path, max_duration_hours=1, source_folders=("Nada",))
    clock = iter([0, 0, 0, 7200])

    with pytest.raises(BackupAborted, match="Tempo máximo"):
        run_backup(
            config, FakeClient(MULTI_TREE), FakeDownloader(), now=NOW, monotonic=lambda: next(clock)
        )


def test_time_limit_during_a_file_download_aborts_the_run(tmp_path):
    class CheckingDownloader(FakeDownloader):
        downloading = False

        def download(self, blob, target, check=None):
            self.downloading = True
            check()
            return super().download(blob, target)

    config = make_config(tmp_path, max_duration_hours=1)
    downloader = CheckingDownloader()

    with pytest.raises(BackupAborted, match="Tempo máximo"):
        run_backup(
            config,
            FakeClient(),
            downloader,
            now=NOW,
            monotonic=lambda: 7200 if downloader.downloading else 0,
        )

    assert list(config.backup_dir.iterdir()) == [config.backup_dir / ".backup.lock"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{ROOT}/CON/nul.txt", Path("_CON", "_nul.txt")),
        (f"{ROOT}/Obras/COM1.pdf", Path("Obras", "_COM1.pdf")),
        (f"{ROOT}/Obras/lpt9", Path("Obras", "_lpt9")),
        (f"{ROOT}/CONSOLE/COM10.pdf", Path("CONSOLE", "COM10.pdf")),
    ],
)
def test_relative_path_avoids_windows_device_names(path, expected):
    assert relative_path(path, ROOT) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{ROOT}/COM¹/arquivo.pdf", Path("_COM¹", "arquivo.pdf")),
        (f"{ROOT}/Obras/LPT³/planta.dwg", Path("Obras", "_LPT³", "planta.dwg")),
        (f"{ROOT}/Obras/com².pln", Path("Obras", "_com².pln")),
        (f"{ROOT}/Obras/lpt¹", Path("Obras", "_lpt¹")),
    ],
)
def test_relative_path_avoids_superscript_device_names(path, expected):
    assert relative_path(path, ROOT) == expected


COLLIDING_TREE = {
    "projectRoot": [
        res("con", "resourceGroup", "CON"),
        res("_con", "resourceGroup", "_CON"),
        res("Obra", "resourceGroup", "Obra"),
        res("obra", "resourceGroup", "obra"),
        res("obra2", "resourceGroup", "obra (2)"),
        res("dados-pasta", "resourceGroup", "dados"),
        res("dados-arquivo", "blob", "Dados", **{"$size": 1, "$modifiedDate": MODIFIED}),
    ],
    "con": [res("c1", "blob", "CON/a.pdf", **{"$size": 1, "$modifiedDate": MODIFIED})],
    "_con": [res("c2", "blob", "_CON/a.pdf", **{"$size": 2, "$modifiedDate": MODIFIED})],
    "Obra": [
        res("f1", "blob", "Obra/Arquivo.pln", **{"$size": 3, "$modifiedDate": MODIFIED}),
        res("f2", "blob", "Obra/arquivo.PLN", **{"$size": 4, "$modifiedDate": MODIFIED}),
    ],
    "obra": [res("f3", "blob", "obra/Arquivo.pln", **{"$size": 5, "$modifiedDate": MODIFIED})],
    "obra2": [res("f4", "blob", "obra (2)/x.txt", **{"$size": 6, "$modifiedDate": MODIFIED})],
}


def test_names_that_collide_on_windows_get_a_numbered_suffix(tmp_path, caplog):
    config = make_config(tmp_path, include_projects=False, include_libraries=False)

    result = run_backup(config, FakeClient(COLLIDING_TREE), FakeDownloader(), now=NOW)

    assert result.ok
    paths = {f.path: f.size for f in load_manifest(result.folder).files}
    assert paths == {
        "_CON/a.pdf": 1,
        "_CON (2)/a.pdf": 2,
        "Obra/Arquivo.pln": 3,
        "Obra/arquivo (2).PLN": 4,
        "obra (2)/Arquivo.pln": 5,
        "obra (2) (2)/x.txt": 6,
        "Dados (2)": 1,
    }
    # No two manifest entries point to the same file on Windows.
    assert len({p.casefold() for p in paths}) == len(paths)
    for path, size in paths.items():
        assert (result.folder / path).stat().st_size == size
    assert "Nome repetido no Windows" in caplog.text


def test_numbered_names_are_the_same_in_the_next_run(tmp_path):
    config = make_config(tmp_path, include_projects=False, include_libraries=False)
    run_backup(config, FakeClient(COLLIDING_TREE), FakeDownloader(), now=NOW - timedelta(minutes=1))
    downloader = FakeDownloader()

    run_backup(config, FakeClient(COLLIDING_TREE), downloader, now=NOW)

    assert downloader.calls == []


def test_cancel_before_starting_keeps_everything(tmp_path):
    config = make_config(tmp_path, versioning="latest")
    previous = old_backup(config, 1)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(BackupCancelled):
        run_backup(config, FakeClient(), FakeDownloader(), now=NOW, cancel=cancel)

    assert set(config.backup_dir.iterdir()) == {previous, config.backup_dir / ".backup.lock"}


def test_cancel_during_a_download_stops_at_the_next_block(tmp_path):
    cancel = threading.Event()

    class CancellingDownloader(FakeDownloader):
        def download(self, blob, target, check=None):
            self.calls.append(blob["id"])
            cancel.set()  # the user clicks Cancel while this file is downloading
            check()
            raise AssertionError("o download deveria ter parado no bloco")

    # One download at a time, so exactly one file has started when Cancel is clicked.
    config = make_config(tmp_path, versioning="latest", parallel_downloads=1)
    previous = old_backup(config, 1)
    downloader = CancellingDownloader()

    with pytest.raises(BackupCancelled, match="cancelado"):
        run_backup(config, FakeClient(), downloader, now=NOW, cancel=cancel)

    assert len(downloader.calls) == 1
    # The incomplete folder is gone and the previous backup was not removed by retention.
    assert set(config.backup_dir.iterdir()) == {previous, config.backup_dir / ".backup.lock"}


def test_cancel_during_an_export_stops_it(tmp_path):
    cancel = threading.Event()

    class CancellingExporter(FakeExporter):
        def _export(self, resource_id, target, check):
            cancel.set()
            return super()._export(resource_id, target, check)

    config = make_config(tmp_path, include_files=False)

    with pytest.raises(BackupCancelled):
        run_backup(
            config,
            FakeClient(),
            FakeDownloader(),
            now=NOW,
            exporter=CancellingExporter(),
            cancel=cancel,
        )

    assert list(config.backup_dir.iterdir()) == [config.backup_dir / ".backup.lock"]


def test_a_new_backup_works_after_a_cancelled_one(tmp_path):
    config = make_config(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(BackupCancelled):
        run_backup(config, FakeClient(), FakeDownloader(), now=NOW, cancel=cancel)

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW + timedelta(minutes=1))

    assert result.ok


def test_cancel_right_after_the_last_item_is_still_honoured(tmp_path):
    """Cancel clicked while the last file finishes: nothing is made final or removed."""
    cancel = threading.Event()
    tree = {"projectRoot": [res("b1", "blob", "unico.pdf", **{"$size": 1})]}

    class LastDownloader(FakeDownloader):
        def download(self, blob, target, check=None):
            size = super().download(blob, target)
            cancel.set()  # after the last check of this file
            return size

    config = make_config(tmp_path, versioning="latest")
    previous = old_backup(config, 1)

    with pytest.raises(BackupCancelled):
        run_backup(config, FakeClient(tree), LastDownloader(), now=NOW, cancel=cancel)

    assert set(config.backup_dir.iterdir()) == {previous, config.backup_dir / ".backup.lock"}


def test_ctrl_c_during_retention_finishes_it_and_reports_a_completed_backup(
    tmp_path, monkeypatch, caplog
):
    config = make_config(tmp_path, retention_days=30)
    expired = [old_backup(config, 60), old_backup(config, 90)]
    deleted = []

    def interrupted_delete(paths):
        for number, path in enumerate(paths):
            shutil.rmtree(path)
            deleted.append(path)
            if number == 0:
                signal.raise_signal(signal.SIGINT)  # Ctrl+C after the first removal

    monkeypatch.setattr("bimcloud_backup.backup.delete_backups", interrupted_delete)

    result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW)

    assert result.ok and result.folder.exists()
    assert sorted(deleted) == sorted(expired)
    assert "Ctrl+C recebido durante a finalização" in caplog.text
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def many_blobs(count):
    return {
        "projectRoot": [
            res(f"b{i:02d}", "blob", f"arquivo-{i:02d}.pdf", **{"$size": i + 1})
            for i in range(count)
        ]
    }


class SlowDownloader(FakeDownloader):
    """Later files finish first, and the number of downloads at the same time is recorded."""

    def __init__(self, count):
        super().__init__()
        self.count = count
        self.running = 0
        self.most = 0
        self.threads = set()
        self.lock = threading.Lock()

    def download(self, blob, target, check=None):
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)
            self.threads.add(threading.current_thread().name)
        try:
            time.sleep(0.002 * (self.count - int(blob["id"][1:])))
            check()
            return super().download(blob, target)
        finally:
            with self.lock:
                self.running -= 1


def test_files_download_in_parallel_up_to_the_limit_with_a_stable_manifest(tmp_path):
    config = make_config(tmp_path, parallel_downloads=3)
    downloader = SlowDownloader(12)

    result = run_backup(config, FakeClient(many_blobs(12)), downloader, now=NOW)

    assert result.ok and result.files == 12
    assert 1 < downloader.most <= 3
    assert all(name.startswith("download") for name in downloader.threads)
    paths = [f.path for f in load_manifest(result.folder).files]
    assert paths == [f"arquivo-{i:02d}.pdf" for i in range(12)]  # listing order, not finish order


def test_one_download_at_a_time_when_configured(tmp_path):
    config = make_config(tmp_path, parallel_downloads=1)
    downloader = SlowDownloader(4)

    run_backup(config, FakeClient(many_blobs(4)), downloader, now=NOW)

    assert downloader.most == 1


def test_cancel_with_parallel_downloads_stops_all_and_leaves_nothing(tmp_path):
    cancel = threading.Event()

    class CancellingDownloader(SlowDownloader):
        def download(self, blob, target, check=None):
            if blob["id"] == "b03":
                cancel.set()
            return super().download(blob, target, check)

    config = make_config(tmp_path, versioning="latest", parallel_downloads=4)
    previous = old_backup(config, 1)

    with pytest.raises(BackupCancelled):
        run_backup(
            config, FakeClient(many_blobs(20)), CancellingDownloader(20), now=NOW, cancel=cancel
        )

    # Every download stopped before the incomplete folder was removed.
    assert set(config.backup_dir.iterdir()) == {previous, config.backup_dir / ".backup.lock"}


def test_a_failed_download_does_not_stop_the_others(tmp_path):
    config = make_config(tmp_path, parallel_downloads=3)

    result = run_backup(config, FakeClient(many_blobs(6)), FakeDownloader(fail={"b02"}), now=NOW)

    assert result.files == 5
    assert result.errors == [f"{ROOT}/arquivo-02.pdf: sem conexão"]


def test_exports_stay_in_one_thread_while_files_download_in_parallel(tmp_path):
    threads = []

    class RecordingExporter(FakeExporter):
        def _export(self, resource_id, target, check):
            threads.append(threading.current_thread())
            return super()._export(resource_id, target, check)

    config = make_config(tmp_path, parallel_downloads=4)

    run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=RecordingExporter())

    assert threads and all(t is threading.main_thread() for t in threads)


@pytest.fixture
def submitted(monkeypatch):
    """How many downloads the backup has handed to its download threads so far."""
    count = []

    class CountingPool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            count.append(1)
            return super().submit(*args, **kwargs)

    monkeypatch.setattr("bimcloud_backup.backup.ThreadPoolExecutor", CountingPool)
    return count


class GatedDownloader(FakeDownloader):
    """Every download waits for `gate`; `started` counts the downloads that began."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.started = threading.Semaphore(0)

    def download(self, blob, target, check=None):
        self.started.release()
        assert self.gate.wait(10)
        return super().download(blob, target)


def test_no_more_downloads_are_queued_than_the_limit(tmp_path, submitted):
    config = make_config(tmp_path, parallel_downloads=2)
    client = FakeClient(many_blobs(30))
    downloader = GatedDownloader()
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("result", run_backup(config, client, downloader, now=NOW))
    )
    worker.start()
    try:
        assert downloader.started.acquire(timeout=10) and downloader.started.acquire(timeout=10)
        time.sleep(0.3)  # time to queue more, if nothing held it back
        # 2 threads x 2 = 4 queued or running; the next one waits for a free slot.
        assert len(submitted) == 2 * QUEUED_PER_THREAD
    finally:
        downloader.gate.set()
        worker.join(10)

    result = outcome["result"]
    assert result.ok and result.files == 30
    paths = [f.path for f in load_manifest(result.folder).files]
    assert paths == [f"arquivo-{i:02d}.pdf" for i in range(30)]


def test_a_limit_hit_inside_a_download_stops_the_run_early(tmp_path, submitted):
    class FullDisk(FakeDownloader):
        def download(self, blob, target, check=None):
            raise BackupAborted("Espaço livre abaixo de 10 GB")

    config = make_config(tmp_path, parallel_downloads=2)

    with pytest.raises(BackupAborted, match="Espaço livre"):
        run_backup(config, FakeClient(many_blobs(50)), FullDisk(), now=NOW)

    assert len(submitted) < 50
    assert [p.name for p in config.backup_dir.iterdir()] == [".backup.lock"]


def test_progress_knows_the_totals_before_the_first_export(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="bimcloud_backup")
    config = make_config(tmp_path)
    reports = []

    class WatchingExporter(FakeExporter):
        def _export(self, resource_id, target, check):
            seen.append(reports[-1])
            return super()._export(resource_id, target, check)

    seen = []
    result = run_backup(
        config,
        FakeClient(),
        FakeDownloader(),
        now=NOW,
        exporter=WatchingExporter(),
        progress=reports.append,
    )

    assert result.ok
    assert reports[0].listing
    first = seen[0]
    assert (first.exports_done, first.exports_total) == (0, 2)
    assert (first.files_total, first.bytes_total) == (2, 8)
    assert first.current == f"{ROOT}/Casa Modelo"
    last = reports[-1]
    assert (last.exports_done, last.files_done, last.bytes_done) == (2, 2, 8)
    assert last.current is None
    assert "Para copiar: 2 projetos e bibliotecas e 2 arquivos (0,0 MB)" in caplog.text
    assert f"Projeto 1 de 2: {ROOT}/Casa Modelo" in caplog.text
    assert f"Biblioteca 2 de 2: {ROOT}/Biblioteca Escritório" in caplog.text


def test_progress_counts_parallel_downloads_and_logs_only_milestones(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="bimcloud_backup")
    config = make_config(tmp_path, parallel_downloads=4)
    reports = []
    lock = threading.Lock()

    def report(progress):
        with lock:
            reports.append(progress)

    result = run_backup(
        config, FakeClient(many_blobs(40)), SlowDownloader(40), now=NOW, progress=report
    )

    assert result.files == 40
    last = reports[-1]
    assert (last.files_done, last.files_total) == (40, 40)
    assert last.bytes_done == last.bytes_total == sum(range(1, 41))
    # Never going back, whatever order the downloads finish in.
    done = [r.files_done for r in reports]
    assert done == sorted(done)
    assert caplog.text.count("Arquivos: ") == 10
    assert "Arquivos: 40 de 40" in caplog.text


def test_progress_counts_the_projects_and_libraries_chosen_one_by_one(tmp_path):
    config = make_config(
        tmp_path,
        source_folders=("Bibliotecas",),
        source_projects=("Obras/ProjetoA", "Obras/ProjetoC"),
    )
    reports = []

    run_backup(
        config,
        FakeClient(ITEM_TREE),
        FakeDownloader(),
        now=NOW,
        exporter=FakeExporter(),
        progress=reports.append,
    )

    assert (reports[-1].exports_done, reports[-1].exports_total) == (3, 3)


def test_a_download_stopped_by_cancel_is_not_counted_as_done(tmp_path):
    cancel = threading.Event()
    reports = []

    class CancelledMidway(FakeDownloader):
        def download(self, blob, target, check=None):
            cancel.set()
            check()  # the next block: the run is cancelled
            return super().download(blob, target)

    with pytest.raises(BackupCancelled):
        run_backup(
            make_config(tmp_path),
            FakeClient(many_blobs(1)),
            CancelledMidway(),
            now=NOW,
            cancel=cancel,
            progress=reports.append,
        )

    assert reports[-1].files_total == 1
    assert (reports[-1].files_done, reports[-1].bytes_done) == (0, 0)


def test_files_that_fail_or_are_refused_still_count_as_done(tmp_path):
    tree = many_blobs(3)
    tree["projectRoot"].append(res("m", "blob", MANIFEST_NAME, **{"$size": 1}))
    reports = []

    result = run_backup(
        make_config(tmp_path),
        FakeClient(tree),
        FakeDownloader(fail={"b01"}),
        now=NOW,
        progress=reports.append,
    )

    assert len(result.errors) == 2
    assert (reports[-1].files_done, reports[-1].files_total) == (4, 4)


def test_progress_stops_with_a_cancelled_run(tmp_path):
    cancel = threading.Event()
    reports = []

    class CancelsAtTheThird(FakeDownloader):
        def download(self, blob, target, check=None):
            if len(self.calls) == 2:
                cancel.set()
            check()
            return super().download(blob, target)

    with pytest.raises(BackupCancelled):
        run_backup(
            make_config(tmp_path, parallel_downloads=1),
            FakeClient(many_blobs(20)),
            CancelsAtTheThird(),
            now=NOW,
            cancel=cancel,
            progress=reports.append,
        )

    assert reports[-1].files_done < 20


@pytest.mark.parametrize("value", [0, 9])
def test_parallel_downloads_outside_the_limits_is_refused(tmp_path, value):
    with pytest.raises(BimcloudError, match="parallel_downloads"):
        run_backup(make_config(tmp_path, parallel_downloads=value), FakeClient(), FakeDownloader())


@pytest.mark.parametrize(
    ("size", "expected"),
    [(19_999_000_000, "18,6 GB"), (512 * 1024**2, "512,0 MB"), (0, "0,0 MB")],
)
def test_summary_sizes_are_written_like_the_interface(size, expected):
    assert format_size(size) == expected


def test_summary_counts_use_thousands_dots():
    assert format_count(1284) == "1.284"


def test_library_snapshots_option_is_passed_on_separately(tmp_path):
    config = make_config(tmp_path, include_files=False, include_backups_in_library_export=True)
    exporter = FakeExporter()

    run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    assert exporter.calls == [("bimproject", "p1", False), ("bimlibrary", "l1", True)]


def test_failures_say_what_the_item_is_once_each_and_sum_up(tmp_path, caplog):
    config = make_config(tmp_path)
    exporter = FakeExporter(fail={"l1"})

    with caplog.at_level(logging.INFO, logger="bimcloud_backup"):
        result = run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    path = f"{ROOT}/Biblioteca Escritório"
    assert [(f.path, f.kind) for f in result.failures] == [(path, KIND_LIBRARY)]
    assert result.errors == [result.failures[0].text]
    plain = [r for r in caplog.records if r.getMessage().startswith("Falha: ")]
    assert [r.getMessage() for r in plain] == [
        f"Falha: Biblioteca {path}: Falha ao baixar Biblioteca Escritório.BIMLibrary: "
        "url?access_token=<REMOVIDO>"
    ]
    assert "SEGREDO" not in caplog.text
    assert "1 item falhou" in caplog.text and "1 itens" not in caplog.text
    manifest = load_manifest(result.folder)
    assert manifest.failures == result.failures


def test_a_translated_failure_keeps_the_technical_text_in_the_log_file(tmp_path, caplog):
    config = make_config(tmp_path, include_files=False, include_projects=False)
    exporter = FakeExporter()

    def failed_job(*args):
        raise ExportError("A exportação terminou com status 'failed': job failed with code 18")

    exporter.export_library = failed_job
    with caplog.at_level(logging.INFO, logger="bimcloud_backup"):
        run_backup(config, FakeClient(), FakeDownloader(), now=NOW, exporter=exporter)

    plain = next(r for r in caplog.records if r.getMessage().startswith("Falha: "))
    assert "erro 18 do servidor" in plain.getMessage()
    technical = [r for r in caplog.records if getattr(r, "technical", False)]
    assert len(technical) == 1 and "code 18" in technical[0].getMessage()
