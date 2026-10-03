"""Loading, validation and saving of the TOML configuration file."""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

import tomli_w

DEFAULT_CLIENT_ID = "bimcloud-localbackup"

VERSIONING_HISTORY = "history"
VERSIONING_LATEST = "latest"
VERSIONING_MODES = (VERSIONING_HISTORY, VERSIONING_LATEST)

# How Teamwork projects are saved: exported .BIMProject, the server's latest .pln backup, or both.
PROJECTS_BIMPROJECT = "bimproject"
PROJECTS_PLN = "pln"
PROJECTS_BOTH = "both"
PROJECTS_FORMATS = (PROJECTS_BIMPROJECT, PROJECTS_PLN, PROJECTS_BOTH)

# Automatic backup: every N minutes, hours or days; with days, at `run_at`.
UNIT_MINUTES = "minutes"
UNIT_HOURS = "hours"
UNIT_DAYS = "days"
# Limits of the Windows Task Scheduler: /SC MINUTE, HOURLY and DAILY with /MO.
UNIT_LIMITS = {UNIT_MINUTES: 1439, UNIT_HOURS: 23, UNIT_DAYS: 365}
UNITS = tuple(UNIT_LIMITS)
# Singular and plural, as the window shows them.
UNIT_NAMES = {
    UNIT_MINUTES: ("minuto", "minutos"),
    UNIT_HOURS: ("hora", "horas"),
    UNIT_DAYS: ("dia", "dias"),
}
# How earlier versions stored it (`mode` with `interval_minutes`); still read, never written.
SCHEDULE_DAILY = "daily"
SCHEDULE_INTERVAL = "interval"
SCHEDULE_MODES = (SCHEDULE_DAILY, SCHEDULE_INTERVAL)
# Plain files downloaded at the same time. Exports stay one at a time regardless.
MAX_PARALLEL_DOWNLOADS = 8

MAX_EXPORT_STALL_MINUTES = 600

MAX_INTERVAL_MINUTES = UNIT_LIMITS[UNIT_MINUTES]

# Values used by the GUI when there is no configuration file yet.
DEFAULTS: dict[str, dict[str, Any]] = {
    "bimcloud": {
        "server_url": "",
        "client_id": DEFAULT_CLIENT_ID,
        "username": "",
        "source_folders": [],
        "source_projects": [],
        "source_libraries": [],
    },
    "backup": {
        "directory": "",
        "include_projects": True,
        "include_libraries": True,
        "include_files": True,
        "projects_format": PROJECTS_BIMPROJECT,
        "include_backups_in_export": False,
        "include_backups_in_library_export": False,
        "versioning": VERSIONING_HISTORY,
        "retention_days": 30,
        "min_backups_to_keep": 1,
        "max_duration_hours": 4,
        "min_free_space_gb": 10,
        "parallel_downloads": 3,
        "export_stall_minutes": 20,
    },
    "schedule": {
        "every": 1,
        "unit": UNIT_DAYS,
        "run_at": "23:00",
        "notify_failures": True,
        "run_logged_off": False,
    },
    "logging": {
        "verbose": False,
    },
}


class ConfigError(ValueError):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class Config:
    server_url: str
    backup_dir: Path
    client_id: str = DEFAULT_CLIENT_ID
    username: str | None = None
    # BIMcloud folders to back up, relative to the root; empty = the whole BIMcloud.
    source_folders: tuple[str, ...] = ()
    # Single projects and libraries chosen besides whole folders, as paths from the root.
    source_projects: tuple[str, ...] = ()
    source_libraries: tuple[str, ...] = ()
    include_projects: bool = True
    include_libraries: bool = True
    include_files: bool = True
    projects_format: str = PROJECTS_BIMPROJECT
    include_backups_in_export: bool = False
    # The same for libraries: the snapshots (backups) BIMcloud keeps go inside the .BIMLibrary.
    include_backups_in_library_export: bool = False
    versioning: str = VERSIONING_HISTORY
    retention_days: int = 30
    min_backups_to_keep: int = 1
    max_duration_hours: float = 4
    min_free_space_gb: float = 10
    parallel_downloads: int = 3
    # An export with no progress on BIMcloud for this long is aborted and becomes an error.
    export_stall_minutes: int = 20
    schedule_every: int = 1
    schedule_unit: str = UNIT_DAYS
    run_at: time = time(23, 0)
    # Windows notification when a scheduled backup fails, is cancelled or needs a new login.
    notify_failures: bool = True
    # Task runs even with nobody logged in (servers); needs the Windows password once.
    run_logged_off: bool = False
    verbose_logging: bool = False

    @property
    def selection(self) -> Selection:
        return Selection(self.source_folders, self.source_projects, self.source_libraries)


def default_raw() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(DEFAULTS)


def load_raw(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"Arquivo de configuração não encontrado: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML inválido em {path}: {e}") from e


def load_config(path: Path) -> Config:
    return parse_config(load_raw(path))


def save_config(config: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(to_raw(config)), encoding="utf-8")
    tmp.replace(path)


def to_raw(config: Config) -> dict[str, dict[str, Any]]:
    return {
        "bimcloud": {
            "server_url": config.server_url,
            "client_id": config.client_id,
            "username": config.username or "",
            "source_folders": list(config.source_folders),
            "source_projects": list(config.source_projects),
            "source_libraries": list(config.source_libraries),
        },
        "backup": {
            "directory": config.backup_dir.as_posix(),
            "include_projects": config.include_projects,
            "include_libraries": config.include_libraries,
            "include_files": config.include_files,
            "projects_format": config.projects_format,
            "include_backups_in_export": config.include_backups_in_export,
            "include_backups_in_library_export": config.include_backups_in_library_export,
            "versioning": config.versioning,
            "retention_days": config.retention_days,
            "min_backups_to_keep": config.min_backups_to_keep,
            "max_duration_hours": config.max_duration_hours,
            "min_free_space_gb": config.min_free_space_gb,
            "parallel_downloads": config.parallel_downloads,
            "export_stall_minutes": config.export_stall_minutes,
        },
        "schedule": {
            "every": config.schedule_every,
            "unit": config.schedule_unit,
            "run_at": config.run_at.strftime("%H:%M"),
            "notify_failures": config.notify_failures,
            "run_logged_off": config.run_logged_off,
        },
        "logging": {"verbose": config.verbose_logging},
    }


def parse_config(raw: dict[str, Any]) -> Config:
    bimcloud = _section(raw, "bimcloud", required=True)
    backup = _section(raw, "backup", required=True)
    schedule = _section(raw, "schedule")
    logging_ = _section(raw, "logging")
    d = DEFAULTS

    server_url = _require(bimcloud, "bimcloud", "server_url", str).strip()
    if not server_url.startswith("https://") or len(server_url) <= len("https://"):
        raise ConfigError("bimcloud.server_url deve começar com https://")

    directory = _require(backup, "backup", "directory", str).strip()
    if not directory:
        raise ConfigError("backup.directory não pode ficar vazio")

    versioning = _choice(backup, "backup", "versioning", VERSIONING_MODES)
    retention_days = _optional(backup, "backup", "retention_days", int, d)
    _at_least(retention_days, 1, "backup.retention_days")
    min_keep = _optional(backup, "backup", "min_backups_to_keep", int, d)
    _at_least(min_keep, 1, "backup.min_backups_to_keep")
    max_hours = _optional(backup, "backup", "max_duration_hours", (int, float), d)
    _at_least(max_hours, 0, "backup.max_duration_hours")
    min_free = _optional(backup, "backup", "min_free_space_gb", (int, float), d)
    _at_least(min_free, 0, "backup.min_free_space_gb")
    parallel = _optional(backup, "backup", "parallel_downloads", int, d)
    stall = _optional(backup, "backup", "export_stall_minutes", int, d)
    if not 1 <= stall <= MAX_EXPORT_STALL_MINUTES:
        raise ConfigError(
            f"backup.export_stall_minutes deve estar entre 1 e {MAX_EXPORT_STALL_MINUTES}"
        )
    if not 1 <= parallel <= MAX_PARALLEL_DOWNLOADS:
        raise ConfigError(
            f"backup.parallel_downloads deve estar entre 1 e {MAX_PARALLEL_DOWNLOADS}"
        )

    every, unit = schedule_from(schedule)

    username = _optional(bimcloud, "bimcloud", "username", str, d).strip()
    selection = selection_from(bimcloud)

    return Config(
        server_url=server_url.rstrip("/"),
        client_id=_optional(bimcloud, "bimcloud", "client_id", str, d).strip() or DEFAULT_CLIENT_ID,
        username=username or None,
        source_folders=selection.folders,
        source_projects=selection.projects,
        source_libraries=selection.libraries,
        backup_dir=Path(directory),
        include_projects=_optional(backup, "backup", "include_projects", bool, d),
        include_libraries=_optional(backup, "backup", "include_libraries", bool, d),
        include_files=_optional(backup, "backup", "include_files", bool, d),
        projects_format=_choice(backup, "backup", "projects_format", PROJECTS_FORMATS),
        include_backups_in_export=_optional(backup, "backup", "include_backups_in_export", bool, d),
        include_backups_in_library_export=_optional(
            backup, "backup", "include_backups_in_library_export", bool, d
        ),
        versioning=versioning,
        retention_days=retention_days,
        min_backups_to_keep=min_keep,
        max_duration_hours=max_hours,
        min_free_space_gb=min_free,
        parallel_downloads=parallel,
        export_stall_minutes=stall,
        schedule_every=every,
        schedule_unit=unit,
        run_at=_parse_time(_optional(schedule, "schedule", "run_at", str, d)),
        notify_failures=_optional(schedule, "schedule", "notify_failures", bool, d),
        run_logged_off=_optional(schedule, "schedule", "run_logged_off", bool, d),
        verbose_logging=_optional(logging_, "logging", "verbose", bool, d),
    )


def schedule_from(schedule: dict[str, Any]) -> tuple[int, str]:
    """`every` and `unit` of a [schedule] section, converting the format of earlier versions.

    Old files have `mode` ("daily" or "interval") and `interval_minutes`: "daily" is every
    1 day, and an interval becomes the most natural unit (720 minutes = 12 hours). Used by the
    loader and the GUI, so both read the same schedule from the same file.
    """
    if "every" not in schedule and "unit" not in schedule:
        return _old_schedule(schedule)
    every = _optional(schedule, "schedule", "every", int, DEFAULTS)
    unit = _choice(schedule, "schedule", "unit", UNITS)
    check_interval(every, unit, "schedule.every")
    return every, unit


def _old_schedule(schedule: dict[str, Any]) -> tuple[int, str]:
    mode = _check_type(schedule.get("mode", SCHEDULE_DAILY), "schedule", "mode", str)
    if mode not in SCHEDULE_MODES:
        raise ConfigError(f"schedule.mode deve ser um de: {', '.join(SCHEDULE_MODES)}")
    if mode == SCHEDULE_DAILY:
        return 1, UNIT_DAYS
    minutes = _check_type(schedule.get("interval_minutes", 60), "schedule", "interval_minutes", int)
    if not 1 <= minutes <= MAX_INTERVAL_MINUTES:
        raise ConfigError(f"schedule.interval_minutes deve estar entre 1 e {MAX_INTERVAL_MINUTES}")
    return natural_interval(minutes)


def natural_interval(minutes: int) -> tuple[int, str]:
    """Minutes as whole hours when they are, otherwise as minutes."""
    if minutes % 60 == 0 and minutes // 60 <= UNIT_LIMITS[UNIT_HOURS]:
        return minutes // 60, UNIT_HOURS
    return minutes, UNIT_MINUTES


def check_interval(every: int, unit: str, label: str) -> None:
    limit = UNIT_LIMITS[unit]
    if not 1 <= every <= limit:
        raise ConfigError(f"{label}: em {UNIT_NAMES[unit][1]}, deve estar entre 1 e {limit}")


def source_folders_from(bimcloud: dict[str, Any]) -> tuple[str, ...]:
    """`source_folders` (list), or the old `source_folder` (string) of earlier versions.

    The list wins whenever the key exists, even empty. Used by the loader and the GUI, so both
    read the same folders from the same file.
    """
    if "source_folders" in bimcloud:
        folders = _check_type(bimcloud["source_folders"], "bimcloud", "source_folders", list)
        if not all(isinstance(f, str) for f in folders):
            raise ConfigError("bimcloud.source_folders deve ser uma lista de textos")
    else:
        old = _check_type(bimcloud.get("source_folder", ""), "bimcloud", "source_folder", str)
        folders = [old]
    return normalize_folders(folders)


@dataclass(frozen=True)
class Selection:
    """What to copy: whole folders plus single projects and libraries. Empty = everything."""

    folders: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    libraries: tuple[str, ...] = ()

    @property
    def everything(self) -> bool:
        return not (self.folders or self.projects or self.libraries)


def selection_from(bimcloud: dict[str, Any]) -> Selection:
    """The folders, projects and libraries of a [bimcloud] section. Old files only have folders."""
    items = []
    for key in ("source_projects", "source_libraries"):
        paths = _check_type(bimcloud.get(key, []), "bimcloud", key, list)
        if not all(isinstance(path, str) for path in paths):
            raise ConfigError(f"bimcloud.{key} deve ser uma lista de textos")
        items.append(paths)
    return normalize_selection(source_folders_from(bimcloud), *items)


def normalize_selection(
    folders: list[str] | tuple[str, ...],
    projects: list[str] | tuple[str, ...] = (),
    libraries: list[str] | tuple[str, ...] = (),
) -> Selection:
    """Clean, sorted paths; projects and libraries inside a chosen folder are already in it."""
    kept_folders = normalize_folders(folders)

    def items(paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        cleaned = sorted({_clean_folder(p) for p in paths} - {""})
        return tuple(p for p in cleaned if not any(p.startswith(f + "/") for f in kept_folders))

    return Selection(kept_folders, items(projects), items(libraries))


def normalize_folders(folders: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Clean, sorted folder paths, dropping empty ones, repeats and subfolders of chosen ones."""
    cleaned = sorted({_clean_folder(f) for f in folders} - {""})
    kept: list[str] = []
    for folder in cleaned:
        if not any(folder.startswith(parent + "/") for parent in kept):
            kept.append(folder)
    return tuple(kept)


def _clean_folder(folder: str) -> str:
    # Every component is trimmed, as the old `source_folder` was: " Obras " means "Obras".
    parts = (part.strip() for part in folder.replace("\\", "/").split("/"))
    return "/".join(part for part in parts if part)


def _section(raw: dict[str, Any], name: str, required: bool = False) -> dict[str, Any]:
    section = raw.get(name)
    if section is None and not required:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"Seção [{name}] ausente na configuração")
    return section


def _require(section: dict[str, Any], prefix: str, key: str, kind: type | tuple) -> Any:
    if key not in section:
        raise ConfigError(f"{prefix}.{key} é obrigatório")
    return _check_type(section[key], prefix, key, kind)


def _optional(
    section: dict[str, Any], prefix: str, key: str, kind: type | tuple, defaults: dict
) -> Any:
    if key not in section:
        return defaults[prefix][key]
    return _check_type(section[key], prefix, key, kind)


def _choice(section: dict[str, Any], prefix: str, key: str, options: tuple[str, ...]) -> str:
    value = _optional(section, prefix, key, str, DEFAULTS)
    if value not in options:
        raise ConfigError(f"{prefix}.{key} deve ser um de: {', '.join(options)}")
    return value


def _check_type(value: Any, prefix: str, key: str, kind: type | tuple) -> Any:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    # bool is a subclass of int, so reject it explicitly for numeric fields.
    if not isinstance(value, kinds) or (bool not in kinds and isinstance(value, bool)):
        names = " ou ".join(k.__name__ for k in kinds)
        raise ConfigError(f"{prefix}.{key} deve ser do tipo {names}")
    return value


def _at_least(value: float, minimum: float, name: str) -> None:
    if value < minimum:
        raise ConfigError(f"{name} deve ser pelo menos {minimum}")


def _parse_time(value: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as e:
        raise ConfigError(
            f'schedule.run_at deve estar no formato "HH:MM", recebido "{value}"'
        ) from e
