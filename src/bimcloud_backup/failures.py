"""Items that failed in a backup, and what each failure means in plain words."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bimcloud_backup.i18n import plural, t

KIND_PROJECT = "project"
KIND_LIBRARY = "library"
KIND_FILE = "file"
KIND_FOLDER = "folder"
# Errors saved by versions that did not record what the item was.
KIND_ITEM = "item"

KINDS = (KIND_PROJECT, KIND_LIBRARY, KIND_FILE, KIND_FOLDER, KIND_ITEM)


@dataclass(frozen=True)
class Failure:
    path: str
    kind: str
    message: str

    @property
    def text(self) -> str:
        """The technical form, as in the `errors` list of .backup.json."""
        return f"{self.path}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "message": self.message}

    @classmethod
    def from_dict(cls, data: object) -> Failure:
        if not isinstance(data, dict):
            raise TypeError("invalid failure in the manifest")
        path, kind, message = data.get("path"), data.get("kind"), data.get("message")
        if not all(isinstance(value, str) for value in (path, kind, message)):
            raise TypeError("invalid failure in the manifest")
        return cls(path, kind if kind in KINDS else KIND_ITEM, message)  # type: ignore[arg-type]

    @classmethod
    def from_text(cls, text: str) -> Failure:
        """A failure saved only as "path: message" (older .backup.json files)."""
        path, separator, message = text.partition(": ")
        if not separator:
            return cls("", KIND_ITEM, text)
        return cls(path, KIND_ITEM, message)


SERVER_JOB_CODE = re.compile(r"job failed with code (\d+)", re.IGNORECASE)
# Messages are saved in the language of the moment, so both languages are recognized.
JOB_TIMEOUT = re.compile(r"(?:não terminou em|did not finish in) (\d+) (?:minutos|minutes)")

# (pattern, key of the plain explanation), checked in order: the first match wins.
REASONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:terminou com|finished with) status '(failed|aborted|abort failed)'",
            re.IGNORECASE,
        ),
        "failures.export_failed",
    ),
    (
        re.compile(
            r"no space left|errno 28|winerror 112|espaço livre abaixo|free space below",
            re.IGNORECASE,
        ),
        "failures.disk_full",
    ),
    (
        re.compile(
            r"invalid_grant|invalid_token|unauthorized|\b401\b|acesso expirado|access expired",
            re.I,
        ),
        "failures.access_expired",
    ),
    (
        re.compile(r"timed? ?out|timeout|tempo esgotado|tempo máximo|time limit", re.IGNORECASE),
        "failures.timeout",
    ),
    (
        re.compile(
            r"não encontrad[ao] no BIMcloud|\b404\b|not found|does not exist|não existe",
            re.IGNORECASE,
        ),
        "failures.not_found",
    ),
    (
        re.compile(
            r"falha de conexão|connection ?(error|aborted|reset|refused|to BIMcloud failed)|"
            r"max retries|name ?resolution|getaddrinfo|remote ?disconnected|sem conexão|"
            r"download incompleto|incomplete download",
            re.IGNORECASE,
        ),
        "failures.connection",
    ),
)


def reason(message: str) -> str | None:
    """The failure in plain words, or None when it is not one of the common cases."""
    code = SERVER_JOB_CODE.search(message)
    if code:
        return t("failures.server_code", code=code.group(1))
    minutes = JOB_TIMEOUT.search(message)
    if minutes:
        return t("failures.job_timeout", minutes=minutes.group(1))
    return next((t(key) for pattern, key in REASONS if pattern.search(message)), None)


def first_line(message: str) -> str:
    """Servers sometimes append a stack trace: the first line says what happened."""
    return message.strip().splitlines()[0] if message.strip() else ""


def explain(failure: Failure) -> str:
    """One line for the window and the log: what the item is, where, and why it failed."""
    because = reason(failure.message) or first_line(failure.message)
    return f"{where(failure)}: {because}"


def count_failed(n: int) -> str:
    return plural("failures.count", n)


def where(failure: Failure) -> str:
    """What the item is and its BIMcloud path ("Library Project Root/...")."""
    label = t(f"failures.kind.{failure.kind if failure.kind in KINDS else KIND_ITEM}")
    return f"{label} {failure.path}" if failure.path else label


def why(failure: Failure) -> str:
    """The reason as a sentence of its own."""
    text = reason(failure.message) or first_line(failure.message)
    return text[:1].upper() + text[1:]
