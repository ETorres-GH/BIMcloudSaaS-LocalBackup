from datetime import datetime, timedelta

from bimcloud_backup.retention import (
    backup_folder_name,
    delete_backups,
    find_expired_backups,
    latest_backup,
    list_backups,
)

NOW = datetime(2026, 9, 26, 23, 0, 0)


def make_backups(root, days_ago):
    for days in days_ago:
        (root / backup_folder_name(NOW - timedelta(days=days))).mkdir()


def names(paths):
    return [p.name for p in paths]


def test_missing_root_returns_nothing(tmp_path):
    assert list_backups(tmp_path / "nao-existe") == []
    assert latest_backup(tmp_path / "nao-existe") is None


def test_ignores_foreign_folders_and_files(tmp_path):
    make_backups(tmp_path, [100])
    (tmp_path / "Meus arquivos").mkdir()
    (tmp_path / "2020-01-01_000000.txt").write_text("não é pasta")
    expired = find_expired_backups(tmp_path, retention_days=30, min_keep=0, now=NOW)
    assert names(expired) == [backup_folder_name(NOW - timedelta(days=100))]


def test_latest_backup_ignores_incomplete_folder(tmp_path):
    make_backups(tmp_path, [1])
    (tmp_path / ".incompleto-2099-01-01_000000").mkdir()
    assert latest_backup(tmp_path).name == backup_folder_name(NOW - timedelta(days=1))


def test_expires_only_older_than_retention_oldest_first(tmp_path):
    make_backups(tmp_path, [1, 10, 31, 45])
    expired = find_expired_backups(tmp_path, retention_days=30, min_keep=1, now=NOW)
    assert names(expired) == [
        backup_folder_name(NOW - timedelta(days=45)),
        backup_folder_name(NOW - timedelta(days=31)),
    ]


def test_min_keep_protects_newest_even_if_expired(tmp_path):
    # Backups stopped running 60 days ago: the last good copies must survive.
    make_backups(tmp_path, [60, 61, 62])
    expired = find_expired_backups(tmp_path, retention_days=30, min_keep=2, now=NOW)
    assert names(expired) == [backup_folder_name(NOW - timedelta(days=62))]


def test_delete_backups(tmp_path):
    make_backups(tmp_path, [40])
    (tmp_path / backup_folder_name(NOW - timedelta(days=40)) / "projeto.pln").write_text("x")
    delete_backups(find_expired_backups(tmp_path, retention_days=30, min_keep=0, now=NOW))
    assert list(tmp_path.iterdir()) == []
