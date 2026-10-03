"""Per-user locations for the configuration file and logs."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "BIMcloudSaaS-LocalBackup"


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    return Path(base) / APP_DIR_NAME


def default_config_path() -> Path:
    return config_dir() / "config.toml"


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return Path(base) / APP_DIR_NAME / "logs"
