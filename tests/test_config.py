from datetime import time
from pathlib import Path

import pytest

from bimcloud_backup.config import (
    DEFAULT_CLIENT_ID,
    ConfigError,
    default_raw,
    load_config,
    parse_config,
    save_config,
)

ROOT = Path(__file__).resolve().parent.parent


def valid_raw() -> dict:
    return {
        "bimcloud": {"server_url": "https://example.bimcloud.com/", "username": "user"},
        "backup": {"directory": "D:/Backups", "retention_days": 30},
        "schedule": {"run_at": "23:00"},
    }


def test_example_config_is_valid():
    config = load_config(ROOT / "config.example.toml")
    assert config.retention_days == 30
    assert config.run_at == time(23, 0)
    assert config.include_files is True


def test_defaults_and_normalization():
    raw = valid_raw()
    raw["bimcloud"]["source_folder"] = "/Obras/2026/"
    config = parse_config(raw)
    assert config.server_url == "https://example.bimcloud.com"
    assert config.client_id == DEFAULT_CLIENT_ID
    assert config.source_folders == ("Obras/2026",)
    assert config.min_backups_to_keep == 1
    assert config.versioning == "history"
    assert (config.schedule_every, config.schedule_unit) == (1, "days")


def test_blank_username_becomes_none():
    raw = valid_raw()
    raw["bimcloud"]["username"] = "  "
    assert parse_config(raw).username is None


def test_save_and_load_round_trip(tmp_path):
    raw = valid_raw()
    raw["backup"].update({"versioning": "latest", "max_duration_hours": 1.5})
    raw["schedule"].update({"every": 3, "unit": "hours"})
    config = parse_config(raw)
    path = tmp_path / "sub" / "config.toml"

    save_config(config, path)

    assert load_config(path) == config


def test_defaults_are_complete_except_required_values():
    raw = default_raw()
    raw["bimcloud"]["server_url"] = "https://x.bimcloud.com"
    raw["backup"]["directory"] = "D:/B"
    parse_config(raw)


def test_missing_file():
    with pytest.raises(ConfigError, match="não encontrado"):
        load_config(Path("nao-existe.toml"))


def test_missing_section():
    raw = valid_raw()
    del raw["backup"]
    with pytest.raises(ConfigError, match=r"\[backup\]"):
        parse_config(raw)


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("bimcloud", "server_url", "http://inseguro.com", "https://"),
        ("bimcloud", "server_url", "https://", "https://"),
        ("backup", "directory", "  ", "vazio"),
        ("backup", "retention_days", 0, "pelo menos 1"),
        ("backup", "retention_days", True, "int"),
        ("backup", "retention_days", "30", "int"),
        ("backup", "min_backups_to_keep", 0, "pelo menos 1"),
        ("backup", "versioning", "snapshots", "history"),
        ("backup", "max_duration_hours", -1, "pelo menos 0"),
        ("schedule", "run_at", "25:00", "HH:MM"),
        ("schedule", "unit", "weeks", "minutes"),
        ("schedule", "every", 0, "entre 1 e 365"),
        ("schedule", "every", "2", "int"),
    ],
)
def test_invalid_values(section, key, value, message):
    raw = valid_raw()
    raw[section][key] = value
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


@pytest.mark.parametrize(
    ("every", "unit", "message"),
    [
        (1440, "minutes", "minutos, deve estar entre 1 e 1439"),
        (24, "hours", "horas, deve estar entre 1 e 23"),
        (366, "days", "dias, deve estar entre 1 e 365"),
    ],
)
def test_interval_limits_of_the_task_scheduler(every, unit, message):
    raw = valid_raw()
    raw["schedule"].update({"every": every, "unit": unit})
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)
    raw["schedule"]["every"] = every - 1
    assert parse_config(raw).schedule_every == every - 1


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ({"mode": "daily", "interval_minutes": 90}, (1, "days")),
        ({}, (1, "days")),
        ({"mode": "interval", "interval_minutes": 720}, (12, "hours")),
        ({"mode": "interval", "interval_minutes": 60}, (1, "hours")),
        ({"mode": "interval", "interval_minutes": 90}, (90, "minutes")),
        ({"mode": "interval", "interval_minutes": 1439}, (1439, "minutes")),
        ({"mode": "interval"}, (1, "hours")),
    ],
)
def test_old_schedule_becomes_the_most_natural_unit(old, expected):
    raw = valid_raw()
    raw["schedule"].update(old)
    config = parse_config(raw)
    assert (config.schedule_every, config.schedule_unit) == expected
    assert config.run_at == time(23, 0)


@pytest.mark.parametrize(
    ("old", "message"),
    [
        ({"mode": "weekly"}, "daily"),
        ({"mode": "interval", "interval_minutes": 1440}, "entre 1 e 1439"),
        ({"mode": "interval", "interval_minutes": "60"}, "int"),
    ],
)
def test_invalid_old_schedule(old, message):
    raw = valid_raw()
    raw["schedule"].update(old)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_new_schedule_wins_over_the_old_keys(tmp_path):
    raw = valid_raw()
    raw["schedule"].update({"mode": "interval", "interval_minutes": 90, "every": 2, "unit": "days"})
    config = parse_config(raw)
    assert (config.schedule_every, config.schedule_unit) == (2, "days")
    path = tmp_path / "config.toml"
    save_config(config, path)
    text = path.read_text(encoding="utf-8")
    assert "every = 2" in text and 'unit = "days"' in text
    assert "mode" not in text and "interval_minutes" not in text


def test_source_folders_list():
    raw = valid_raw()
    raw["bimcloud"]["source_folders"] = [
        "Pastas Exemplo/Projeto Teste",
        "\\Obras\\2026\\",
        "Obras",
        "",
        "Obras/2026/Casa",
        "Pastas Exemplo/Projeto Teste",
    ]
    config = parse_config(raw)
    assert config.source_folders == ("Obras", "Pastas Exemplo/Projeto Teste")


def test_no_source_folder_means_everything():
    assert parse_config(valid_raw()).source_folders == ()


def test_source_folders_list_wins_over_the_old_string():
    raw = valid_raw()
    raw["bimcloud"]["source_folder"] = "Antiga"
    raw["bimcloud"]["source_folders"] = ["Nova"]
    assert parse_config(raw).source_folders == ("Nova",)


@pytest.mark.parametrize("value", ["Obras", ["Obras", 3]])
def test_invalid_source_folders(value):
    raw = valid_raw()
    raw["bimcloud"]["source_folders"] = value
    with pytest.raises(ConfigError, match="source_folders"):
        parse_config(raw)


def test_source_folders_round_trip(tmp_path):
    raw = valid_raw()
    raw["bimcloud"]["source_folders"] = ["B", "A/x"]
    config = parse_config(raw)
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path) == config
    assert "source_folder =" not in path.read_text(encoding="utf-8")


def test_single_projects_and_libraries_are_kept_besides_folders(tmp_path):
    raw = valid_raw()
    raw["bimcloud"]["source_folders"] = ["Obras"]
    raw["bimcloud"]["source_projects"] = [
        " Outras / ProjetoA ",
        "Obras/ProjetoC",
        "Outras/ProjetoA",
    ]
    raw["bimcloud"]["source_libraries"] = ["Bibliotecas/BibliotecaB", ""]
    config = parse_config(raw)
    # ProjetoC is inside a chosen folder, so it is already copied with it.
    assert config.source_projects == ("Outras/ProjetoA",)
    assert config.source_libraries == ("Bibliotecas/BibliotecaB",)
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path) == config


def test_old_files_without_projects_or_libraries_still_load():
    config = parse_config(valid_raw())
    assert (config.source_projects, config.source_libraries) == ((), ())
    assert config.selection.everything


@pytest.mark.parametrize("key", ["source_projects", "source_libraries"])
@pytest.mark.parametrize("value", ["Obras/ProjetoA", ["Obras/ProjetoA", 3]])
def test_invalid_project_or_library_lists(key, value):
    raw = valid_raw()
    raw["bimcloud"][key] = value
    with pytest.raises(ConfigError, match=key):
        parse_config(raw)


def test_export_stall_minutes_default_limits_and_round_trip(tmp_path):
    assert parse_config(valid_raw()).export_stall_minutes == 20
    for value in (0, 601):
        raw = valid_raw()
        raw["backup"]["export_stall_minutes"] = value
        with pytest.raises(ConfigError, match="export_stall_minutes"):
            parse_config(raw)
    raw = valid_raw()
    raw["backup"]["export_stall_minutes"] = 45
    config = parse_config(raw)
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path).export_stall_minutes == 45


@pytest.mark.parametrize(
    ("folders", "expected"),
    [
        ({"source_folder": " Obras "}, ("Obras",)),
        ({"source_folder": " / Obras / 2026 / "}, ("Obras/2026",)),
        (
            {"source_folders": [" Pastas Exemplo / Projeto Teste "]},
            ("Pastas Exemplo/Projeto Teste",),
        ),
    ],
)
def test_every_folder_component_is_trimmed(folders, expected):
    raw = valid_raw()
    raw["bimcloud"].pop("source_folder", None)
    raw["bimcloud"].update(folders)
    assert parse_config(raw).source_folders == expected


def test_parallel_downloads_default_and_limits():
    assert parse_config(valid_raw()).parallel_downloads == 3
    raw = valid_raw()
    raw["backup"]["parallel_downloads"] = 8
    assert parse_config(raw).parallel_downloads == 8
    for value in (0, 9):
        raw["backup"]["parallel_downloads"] = value
        with pytest.raises(ConfigError, match="parallel_downloads"):
            parse_config(raw)


def test_library_snapshots_are_off_by_default_and_round_trip(tmp_path):
    assert parse_config(valid_raw()).include_backups_in_library_export is False
    raw = valid_raw()
    raw["backup"]["include_backups_in_library_export"] = True
    config = parse_config(raw)
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path).include_backups_in_library_export is True


def test_failure_notifications_are_on_by_default_and_can_be_turned_off(tmp_path):
    assert parse_config(valid_raw()).notify_failures is True
    raw = valid_raw()
    raw["schedule"]["notify_failures"] = False
    config = parse_config(raw)
    assert config.notify_failures is False
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path).notify_failures is False


def test_running_logged_off_is_off_by_default_and_saved(tmp_path):
    assert parse_config(valid_raw()).run_logged_off is False
    raw = valid_raw()
    raw["schedule"]["run_logged_off"] = True
    path = tmp_path / "config.toml"
    save_config(parse_config(raw), path)
    assert load_config(path).run_logged_off is True
    raw["schedule"]["run_logged_off"] = "sim"
    with pytest.raises(ConfigError, match="run_logged_off"):
        parse_config(raw)
