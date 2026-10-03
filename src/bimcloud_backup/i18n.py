"""The program's texts in English and Brazilian Portuguese.

Every text the user sees lives in `locales/<language>.json` and is read with `t("key")`.
English is the default; the chosen language is saved in config.toml (see `config.py`).
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import cache
from pathlib import Path

ENGLISH = "en"
PORTUGUESE = "pt-BR"
DEFAULT_LANGUAGE = ENGLISH
# Each language by its own name, as the language choice shows it.
LANGUAGES = {ENGLISH: "English", PORTUGUESE: "Português (Brasil)"}

LOCALES = Path(__file__).resolve().parent / "locales"

_language = DEFAULT_LANGUAGE


def set_language(language: str | None) -> str:
    """Use `language` from now on; an unknown or empty one falls back to English."""
    global _language
    _language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    return _language


def language() -> str:
    return _language


@cache
def texts(language: str) -> dict[str, str]:
    with (LOCALES / f"{language}.json").open(encoding="utf-8") as f:
        return json.load(f)


def t(key: str, **values: object) -> str:
    """The text of `key` in the current language, with `{name}` fields filled from `values`.

    A key missing from the current language falls back to English, then to the key itself.
    """
    text = texts(_language).get(key)
    if text is None:
        text = texts(DEFAULT_LANGUAGE).get(key, key)
    return text.format(**values) if values else text


def plural(key: str, count: int, **values: object) -> str:
    """`key.one` for 1 and `key.other` otherwise, with `{count}` already filled in."""
    return t(f"{key}.{'one' if count == 1 else 'other'}", count=format_count(count), **values)


def format_count(count: int) -> str:
    """1284 as "1,284" in English and "1.284" in Portuguese."""
    text = f"{count:,}"
    return text.replace(",", ".") if _language == PORTUGUESE else text


def format_decimal(value: float, digits: int = 1) -> str:
    """18.6 as "18.6" in English and "18,6" in Portuguese."""
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if _language == PORTUGUESE else text


def format_date(moment: datetime, key: str = "format.datetime") -> str:
    """A date in the format the language uses (see the `format.*` keys)."""
    return moment.strftime(t(key))
