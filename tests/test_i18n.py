import re
from datetime import datetime
from pathlib import Path

import pytest

from bimcloud_backup import i18n
from bimcloud_backup.config import (
    ConfigError,
    load_config,
    parse_config,
    save_language,
    saved_language,
)
from bimcloud_backup.failures import Failure, reason, where
from bimcloud_backup.i18n import format_count, format_date, format_decimal, plural, t

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "src" / "bimcloud_backup"
FIELD = re.compile(r"{(\w*)}")
# t("key") and plural("key", ...) with a literal key.
LITERAL_KEY = re.compile(r"""\b(t|plural)\(\s*["']([\w.]+)["']""")


@pytest.fixture
def english():
    i18n.set_language(i18n.ENGLISH)


def test_english_is_the_default():
    assert i18n.DEFAULT_LANGUAGE == "en"
    assert i18n.set_language(None) == "en"
    assert i18n.set_language("fr") == "en"
    assert i18n.set_language("pt-BR") == "pt-BR"


def test_both_languages_have_the_same_texts_and_fields():
    english, portuguese = i18n.texts("en"), i18n.texts("pt-BR")
    assert set(english) == set(portuguese)
    for key, text in english.items():
        assert set(FIELD.findall(text)) == set(FIELD.findall(portuguese[key])), key
        assert text.strip() and portuguese[key].strip(), key


def test_every_text_the_code_asks_for_exists():
    english = i18n.texts("en")
    missing = []
    for path in SOURCES.glob("*.py"):
        for kind, key in LITERAL_KEY.findall(path.read_text(encoding="utf-8")):
            found = key in english
            if kind == "plural":
                found = f"{key}.one" in english and f"{key}.other" in english
            if not found and key != "key":
                missing.append(f"{path.name}: {key}")
    assert missing == []


def test_the_locales_are_inside_the_package():
    # Packaged with the program (PyInstaller --collect-data bimcloud_backup and the wheel).
    assert {p.name for p in (SOURCES / "locales").glob("*.json")} == {"en.json", "pt-BR.json"}


def test_a_missing_text_falls_back_to_english_then_to_the_key(english, monkeypatch):
    i18n.set_language("pt-BR")
    monkeypatch.setitem(i18n.texts("en"), "only.english", "Only {name}")
    assert t("only.english", name="here") == "Only here"
    assert t("no.such.key") == "no.such.key"


def test_numbers_and_dates_follow_the_language(english):
    moment = datetime(2026, 10, 3, 23, 5)
    assert format_count(1284) == "1,284"
    assert format_decimal(18.64) == "18.6"
    assert format_date(moment) == "10/03/2026 23:05"
    assert plural("picker.projects", 1) == "1 project"
    assert plural("picker.projects", 2) == "2 projects"
    i18n.set_language("pt-BR")
    assert format_count(1284) == "1.284"
    assert format_decimal(18.64) == "18,6"
    assert format_date(moment) == "03/10/2026 23:05"
    assert plural("picker.projects", 1284) == "1.284 projetos"


def test_failures_are_explained_in_the_chosen_language(english):
    failure = Failure("Obras/A", "project", "A exportação não terminou em 120 minutos")
    # Messages saved in Portuguese by an earlier backup are still recognized.
    assert reason(failure.message).startswith("BIMcloud took more than 120 minutes")
    assert reason("The export did not finish in 30 minutes").startswith("BIMcloud took more")
    assert reason("The export finished with status 'failed'").startswith("BIMcloud could not")
    assert reason("Free space below 10 GB on D:/").startswith("the destination disk")
    assert where(failure) == "Project Obras/A"
    i18n.set_language("pt-BR")
    assert where(failure) == "Projeto Obras/A"
    assert reason("The export did not finish in 30 minutes").startswith("o BIMcloud levou")


# ------------------------------------------------------------------ config

CONFIG = """
[bimcloud]
server_url = "https://example.bimcloud.com"

[backup]
directory = "D:/Backups"
"""


def test_the_language_is_saved_in_the_configuration(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    assert saved_language(path) is None
    assert load_config(path).language == "en"

    save_language("pt-BR", path)
    assert saved_language(path) == "pt-BR"
    config = load_config(path)
    # Everything else in the file is kept.
    assert config.language == "pt-BR" and config.server_url == "https://example.bimcloud.com"


def test_the_language_can_be_saved_before_anything_else(tmp_path):
    path = tmp_path / "new" / "config.toml"
    save_language("en", path)
    assert saved_language(path) == "en"


def test_an_unreadable_file_is_never_overwritten_by_the_language(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("not [valid", encoding="utf-8")
    assert saved_language(path) is None
    with pytest.raises(ConfigError):
        save_language("en", path)
    assert path.read_text(encoding="utf-8") == "not [valid"


def test_an_unknown_language_is_refused():
    raw = {
        "bimcloud": {"server_url": "https://example.bimcloud.com"},
        "backup": {"directory": "D:/Backups"},
        "interface": {"language": "fr"},
    }
    with pytest.raises(ConfigError, match="interface.language"):
        parse_config(raw)
