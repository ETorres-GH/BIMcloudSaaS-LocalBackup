"""Items that failed in a backup, and what each failure means in plain Portuguese."""

from __future__ import annotations

import re
from dataclasses import dataclass

KIND_PROJECT = "project"
KIND_LIBRARY = "library"
KIND_FILE = "file"
KIND_FOLDER = "folder"
# Errors saved by versions that did not record what the item was.
KIND_ITEM = "item"

KIND_LABELS = {
    KIND_PROJECT: "Projeto",
    KIND_LIBRARY: "Biblioteca",
    KIND_FILE: "Arquivo",
    KIND_FOLDER: "Pasta",
    KIND_ITEM: "Item",
}


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
            raise TypeError("falha inválida no manifest")
        path, kind, message = data.get("path"), data.get("kind"), data.get("message")
        if not all(isinstance(value, str) for value in (path, kind, message)):
            raise TypeError("falha inválida no manifest")
        return cls(path, kind if kind in KIND_LABELS else KIND_ITEM, message)  # type: ignore[arg-type]

    @classmethod
    def from_text(cls, text: str) -> Failure:
        """A failure saved only as "path: message" (older .backup.json files)."""
        path, separator, message = text.partition(": ")
        if not separator:
            return cls("", KIND_ITEM, text)
        return cls(path, KIND_ITEM, message)


SERVER_JOB_CODE = re.compile(r"job failed with code (\d+)", re.IGNORECASE)
JOB_TIMEOUT = re.compile(r"não terminou em (\d+) minutos")

# (pattern, plain explanation), checked in order: the first match wins.
REASONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"terminou com status '(failed|aborted|abort failed)'", re.IGNORECASE),
        "o BIMcloud não conseguiu gerar o arquivo. Tente exportar pelo BIMcloud Manager; se "
        "falhar lá também, é um problema no servidor.",
    ),
    (
        re.compile(r"no space left|errno 28|winerror 112|espaço livre abaixo", re.IGNORECASE),
        "faltou espaço no disco de destino. Libere espaço ou escolha outra pasta de destino.",
    ),
    (
        re.compile(r"invalid_grant|invalid_token|unauthorized|\b401\b|acesso expirado", re.I),
        "o acesso ao BIMcloud expirou. Clique em Entrar novamente e repita o backup.",
    ),
    (
        re.compile(r"timed? ?out|timeout|tempo esgotado|tempo máximo", re.IGNORECASE),
        "o BIMcloud demorou demais para responder. Tente de novo mais tarde.",
    ),
    (
        re.compile(
            r"não encontrad[ao] no BIMcloud|\b404\b|not found|does not exist|não existe",
            re.IGNORECASE,
        ),
        "o item não existe mais no BIMcloud (foi apagado, movido ou renomeado). Confira as "
        "pastas escolhidas.",
    ),
    (
        re.compile(
            r"falha de conexão|connection ?(error|aborted|reset|refused)|max retries|"
            r"name ?resolution|getaddrinfo|remote ?disconnected|sem conexão|download incompleto",
            re.IGNORECASE,
        ),
        "a conexão com o BIMcloud caiu durante a cópia. Confira a internet e tente de novo.",
    ),
)


def reason(message: str) -> str | None:
    """The failure in plain words, or None when it is not one of the common cases."""
    code = SERVER_JOB_CODE.search(message)
    if code:
        return (
            f"o BIMcloud não conseguiu gerar o arquivo (erro {code.group(1)} do servidor). "
            "Tente exportar pelo BIMcloud Manager; se falhar lá também, é um problema no servidor."
        )
    minutes = JOB_TIMEOUT.search(message)
    if minutes:
        return (
            f"o BIMcloud levou mais de {minutes.group(1)} minutos para gerar o arquivo. Tente de "
            "novo mais tarde, num horário de menos uso."
        )
    return next((text for pattern, text in REASONS if pattern.search(message)), None)


def first_line(message: str) -> str:
    """Servers sometimes append a stack trace: the first line says what happened."""
    return message.strip().splitlines()[0] if message.strip() else ""


def explain(failure: Failure) -> str:
    """One line for the window and the log: what the item is, where, and why it failed."""
    because = reason(failure.message) or first_line(failure.message)
    return f"{where(failure)}: {because}"


def count_failed(n: int) -> str:
    return "1 item falhou" if n == 1 else f"{n} itens falharam"


def where(failure: Failure) -> str:
    """What the item is and its BIMcloud path ("Biblioteca Project Root/...")."""
    label = KIND_LABELS.get(failure.kind, KIND_LABELS[KIND_ITEM])
    return f"{label} {failure.path}" if failure.path else label


def why(failure: Failure) -> str:
    """The reason as a sentence of its own."""
    text = reason(failure.message) or first_line(failure.message)
    return text[:1].upper() + text[1:]
