"""Persistent summaries of backup runs and completed backup contents."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from bimcloud_backup.failures import Failure
from bimcloud_backup.paths import config_dir

STATUS_OK = "ok"
STATUS_WARNINGS = "warnings"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
MANIFEST_NAME = ".backup.json"
MANIFEST_TMP_NAME = ".backup.tmp"
RESERVED_MANIFEST_NAMES = frozenset({MANIFEST_NAME, MANIFEST_TMP_NAME})


@dataclass
class LastRun:
    finished_at: str
    status: str
    files: int = 0
    bytes: int = 0
    errors: int = 0
    pending: int = 0
    folder: str = ""
    message: str = ""

    @property
    def finished(self) -> datetime:
        return datetime.fromisoformat(self.finished_at)


@dataclass(frozen=True)
class ManifestFile:
    path: str
    size: int
    modified_date: int | None
    # What the file is when it is not a plain BIMcloud file: "bimproject", "pln", "bimlibrary".
    kind: str | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        data: dict[str, str | int | None] = {
            "path": self.path,
            "$size": self.size,
            "$modifiedDate": self.modified_date,
        }
        if self.kind:
            data["type"] = self.kind
        return data

    @classmethod
    def from_dict(cls, data: object) -> ManifestFile:
        if not isinstance(data, dict):
            raise TypeError("entrada de arquivo inválida")
        path = data.get("path")
        size = data.get("$size")
        modified_date = data.get("$modifiedDate")
        kind = data.get("type")
        if (
            not isinstance(path, str)
            or (kind is not None and not isinstance(kind, str))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or (
                modified_date is not None
                and (not isinstance(modified_date, int) or isinstance(modified_date, bool))
            )
        ):
            raise TypeError("metadados de arquivo inválidos")
        manifest_path = PurePosixPath(path)
        if (
            not path
            or manifest_path.is_absolute()
            or ".." in manifest_path.parts
            or ":" in path
            or "\\" in path
        ):
            raise ValueError("caminho de arquivo inválido")
        return cls(path, size, modified_date, kind)


@dataclass
class BackupManifest:
    created_at: str
    version: str
    server_url: str
    # Sorted BIMcloud paths of the folders backed up ("Project Root" for everything).
    source_path: list[str]
    by_type: dict[str, dict[str, int]] = field(default_factory=dict)
    files: list[ManifestFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # The same errors with what each item was; `errors` stays for older readers.
    failures: list[Failure] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.errors and not self.failures:
            # Written before the failure details existed: the plain texts are all there is.
            self.failures = [Failure.from_text(error) for error in self.errors]

    @property
    def created(self) -> datetime:
        return datetime.fromisoformat(self.created_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "created_at": self.created_at,
            "version": self.version,
            "server_url": self.server_url,
            "source_path": list(self.source_path),
            "by_type": self.by_type,
            "files": [item.to_dict() for item in self.files],
            "errors": self.errors,
            "failures": [failure.to_dict() for failure in self.failures],
        }

    @classmethod
    def from_dict(cls, data: object) -> BackupManifest:
        if not isinstance(data, dict):
            raise TypeError("manifest inválido")
        created_at = data.get("created_at")
        version = data.get("version")
        server_url = data.get("server_url")
        source_path = data.get("source_path")
        if isinstance(source_path, str):
            # Manifests written before several source folders were supported.
            source_path = [source_path]
        by_type = data.get("by_type")
        files = data.get("files")
        errors = data.get("errors")
        if (
            not isinstance(created_at, str)
            or not isinstance(version, str)
            or not isinstance(server_url, str)
            or not isinstance(source_path, list)
            or not all(isinstance(p, str) for p in source_path)
        ):
            raise TypeError("cabeçalho do manifest inválido")
        if (
            not isinstance(by_type, dict)
            or not isinstance(files, list)
            or not isinstance(errors, list)
        ):
            raise TypeError("conteúdo do manifest inválido")
        normalized_types: dict[str, dict[str, int]] = {}
        for kind, totals in by_type.items():
            if not isinstance(kind, str) or not isinstance(totals, dict):
                raise TypeError("totais do manifest inválidos")
            count, size = totals.get("count"), totals.get("bytes")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or not isinstance(size, int)
                or isinstance(size, bool)
            ):
                raise TypeError("totais do manifest inválidos")
            normalized_types[kind] = {"count": count, "bytes": size}
        if not all(isinstance(error, str) for error in errors):
            raise TypeError("erros do manifest inválidos")
        manifest = cls(
            created_at,
            version,
            server_url,
            source_path,
            normalized_types,
            [ManifestFile.from_dict(item) for item in files],
            errors,
            _failures(data.get("failures")),
        )
        _ = manifest.created
        return manifest


def _failures(data: object) -> list[Failure]:
    """The detailed failures; none (rebuilt from the error texts) when missing or damaged."""
    if not isinstance(data, list):
        return []
    try:
        return [Failure.from_dict(item) for item in data]
    except TypeError:
        return []


def state_path() -> Path:
    return config_dir() / "last_run.json"


def save_last_run(run: LastRun, path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(run), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_last_run(path: Path | None = None) -> LastRun | None:
    path = path or state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        run = LastRun(**data)
        _ = run.finished  # validates the timestamp
        return run
    except (OSError, ValueError, TypeError):
        return None


def save_manifest(manifest: BackupManifest, folder: Path) -> None:
    path = folder / MANIFEST_NAME
    tmp = folder / MANIFEST_TMP_NAME
    tmp.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def load_manifest(folder: Path) -> BackupManifest | None:
    try:
        data = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
        return BackupManifest.from_dict(data)
    except (OSError, ValueError, TypeError):
        return None
