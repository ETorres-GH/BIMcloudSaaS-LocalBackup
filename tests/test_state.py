import json
from datetime import datetime

from bimcloud_backup.failures import KIND_ITEM, KIND_LIBRARY, Failure
from bimcloud_backup.state import (
    BackupManifest,
    LastRun,
    ManifestFile,
    load_last_run,
    load_manifest,
    save_last_run,
    save_manifest,
)


def test_round_trip(tmp_path):
    path = tmp_path / "last_run.json"
    run = LastRun("2026-09-27T23:04:12", "ok", files=3, bytes=10, folder="D:/B/x")
    save_last_run(run, path)
    loaded = load_last_run(path)
    assert loaded == run
    assert loaded.finished == datetime(2026, 9, 27, 23, 4, 12)


def test_file_with_bom_is_read(tmp_path):
    path = tmp_path / "last_run.json"
    path.write_text('{"finished_at": "2026-09-27T23:04:12", "status": "ok"}', encoding="utf-8-sig")
    assert load_last_run(path).status == "ok"


def test_missing_or_corrupt_file_returns_none(tmp_path):
    assert load_last_run(tmp_path / "nada.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text('{"finished_at": "ontem", "status": "ok"}', encoding="utf-8")
    assert load_last_run(bad) is None


def test_manifest_round_trip(tmp_path):
    manifest = BackupManifest(
        "2026-09-27T23:04:12",
        "1.2.3",
        "https://example.bimcloud.com",
        ["Project Root/Obras"],
        {"blob": {"count": 1, "bytes": 10}},
        [ManifestFile("Obras/planta.dwg", 10, 1_727_467_200_000)],
        ["um erro"],
    )
    save_manifest(manifest, tmp_path)
    assert load_manifest(tmp_path) == manifest


def test_invalid_manifest_returns_none(tmp_path):
    (tmp_path / ".backup.json").write_text(
        '{"created_at":"2026-09-27T23:04:12","version":"1",'
        '"server_url":"https://example.bimcloud.com","source_path":"Project Root",'
        '"by_type":{},"files":[{"path":"../../fora","$size":1,'
        '"$modifiedDate":1727467200000}],"errors":[]}',
        encoding="utf-8",
    )
    assert load_manifest(tmp_path) is None


def test_manifest_rejects_boolean_modified_date(tmp_path):
    (tmp_path / ".backup.json").write_text(
        '{"created_at":"2026-09-27T23:04:12","version":"1",'
        '"server_url":"https://example.bimcloud.com","source_path":"Project Root",'
        '"by_type":{},"files":[{"path":"arquivo.bin","$size":1,'
        '"$modifiedDate":true}],"errors":[]}',
        encoding="utf-8",
    )
    assert load_manifest(tmp_path) is None


def test_manifest_from_before_several_source_folders_still_loads(tmp_path):
    (tmp_path / ".backup.json").write_text(
        '{"created_at":"2026-09-27T23:04:12","version":"1",'
        '"server_url":"https://example.bimcloud.com","source_path":"Project Root/Obras",'
        '"by_type":{},"files":[],"errors":[]}',
        encoding="utf-8",
    )
    assert load_manifest(tmp_path).source_path == ["Project Root/Obras"]


def test_manifest_keeps_what_each_failed_item_was(tmp_path):
    failure = Failure("Project Root/Libraries/BibliotecaB.pla", KIND_LIBRARY, "falhou")
    manifest = BackupManifest(
        "2026-09-27T23:04:12",
        "1.2.3",
        "https://example.bimcloud.com",
        ["Project Root"],
        errors=[failure.text],
        failures=[failure],
    )
    save_manifest(manifest, tmp_path)
    assert load_manifest(tmp_path).failures == [failure]


def test_manifest_from_before_the_failure_details_still_lists_them(tmp_path):
    manifest = BackupManifest(
        "2026-09-27T23:04:12",
        "1.2.3",
        "https://example.bimcloud.com",
        ["Project Root"],
        errors=["Project Root/Obras/a.pdf: sem conexão"],
    )
    data = manifest.to_dict()
    del data["failures"]
    (tmp_path / ".backup.json").write_text(json.dumps(data), encoding="utf-8")
    assert load_manifest(tmp_path).failures == [
        Failure("Project Root/Obras/a.pdf", KIND_ITEM, "sem conexão")
    ]
