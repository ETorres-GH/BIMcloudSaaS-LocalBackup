from datetime import datetime

from bimcloud_backup import history
from bimcloud_backup.retention import backup_folder_name
from bimcloud_backup.state import MANIFEST_NAME, BackupManifest, ManifestFile, save_manifest

WHEN = datetime(2026, 9, 28, 13, 56, 0)


def make_backup(root, when, files=(), errors=(), manifest=True):
    folder = root / backup_folder_name(when)
    folder.mkdir(parents=True)
    if manifest:
        save_manifest(
            BackupManifest(
                when.isoformat(),
                "1",
                "https://example.invalid",
                ["Project Root"],
                files=list(files),
                errors=list(errors),
            ),
            folder,
        )
    return folder


FILES = [
    ManifestFile("Obras/Casa.BIMProject29", 100, None, "bimproject"),
    ManifestFile("Obras/Casa - 2026.pln", 50, 1, "pln"),
    ManifestFile("Bib.BIMLibrary", 30, None, "bimlibrary"),
    ManifestFile("Obras/planta.pdf", 5, 1),
    ManifestFile("Obras/memorial.pdf", 7, 1),
]


def test_backups_are_summarised_newest_first(tmp_path):
    older = make_backup(tmp_path, WHEN.replace(day=27), files=FILES[3:])
    newer = make_backup(tmp_path, WHEN, files=FILES, errors=["Obras/x.pdf: sem conexão"])

    entries = history.read_history(tmp_path)

    assert [e.folder for e in entries] == [newer, older]
    assert entries[0].status == history.STATUS_WARNINGS and entries[0].errors == 1
    assert entries[0].size == 192 and entries[0].files == 5
    assert entries[0].counts == (
        (".BIMProject", 1),
        (".pln", 1),
        (".BIMLibrary", 1),
        ("arquivos", 2),
    )
    assert entries[1].status == history.STATUS_OK


def test_missing_or_damaged_manifest_means_no_details(tmp_path):
    make_backup(tmp_path, WHEN, manifest=False)
    broken = make_backup(tmp_path, WHEN.replace(day=27), files=FILES)
    (broken / MANIFEST_NAME).write_text("{não é json", encoding="utf-8")

    entries = history.read_history(tmp_path)

    assert [e.status for e in entries] == [history.STATUS_NO_DETAILS] * 2
    assert all(e.size is None for e in entries)


def test_interrupted_runs_are_listed_as_incomplete(tmp_path):
    (tmp_path / f".incompleto-{backup_folder_name(WHEN)}").mkdir()
    (tmp_path / ".incompleto-sem-data").mkdir()
    (tmp_path / "pasta do usuário").mkdir()

    entries = history.read_history(tmp_path)

    assert [(e.status, e.when) for e in entries] == [(history.STATUS_INCOMPLETE, WHEN)]


def test_missing_destination_is_an_empty_history(tmp_path):
    assert history.read_history(tmp_path / "nao-existe") == []


def test_manifests_are_read_again_only_when_they_change(tmp_path, monkeypatch):
    make_backup(tmp_path, WHEN, files=FILES)
    cache = {}
    history.read_history(tmp_path, cache)
    reads = []
    monkeypatch.setattr(history, "load_manifest", lambda folder: reads.append(folder))

    entries = history.read_history(tmp_path, cache)

    assert reads == [] and entries[0].files == 5
