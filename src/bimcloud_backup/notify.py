"""Windows notifications (toasts) for scheduled backups that need the user's attention.

The texts are fixed and never carry tokens, paths or error details.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from collections.abc import Callable

from bimcloud_backup.i18n import plural, t

APP_NAME = "BIMcloud Backup Local"


def failed() -> str:
    return t("notify.failed")


def cancelled() -> str:
    return t("notify.cancelled")


def auth_expired() -> str:
    return t("notify.auth_expired")


def finished_with_errors(errors: int) -> str:
    return plural("notify.finished_with_errors", errors)


# Windows only shows toasts from registered apps; PowerShell's ID is always registered.
POWERSHELL_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
)
# The texts arrive through environment variables, never inside the script.
MANAGER = "[Windows.UI.Notifications.ToastNotificationManager]"
TOAST_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "$null = [Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime]",
        "$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02",
        f"$xml = {MANAGER}::GetTemplateContent($template)",
        "$texts = $xml.GetElementsByTagName('text')",
        "$null = $texts.Item(0).AppendChild($xml.CreateTextNode($env:BCB_TOAST_TITLE))",
        "$null = $texts.Item(1).AppendChild($xml.CreateTextNode($env:BCB_TOAST_TEXT))",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
        f"{MANAGER}::CreateToastNotifier('{POWERSHELL_APP_ID}').Show($toast)",
    ]
)
TIMEOUT_SECONDS = 20
# No console window flashes when called from the windowed executable.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Notifier = Callable[[str], None]

log = logging.getLogger("bimcloud_backup")


def toast_command() -> list[str]:
    encoded = base64.b64encode(TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-EncodedCommand",
        encoded,
    ]


def show(message: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    """Show `message` as a Windows toast. Never raises: a missing toast must not fail a backup."""
    env = {**os.environ, "BCB_TOAST_TITLE": APP_NAME, "BCB_TOAST_TEXT": message}
    try:
        result = run(
            toast_command(),
            env=env,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(t("notify.show_failed", error=type(e).__name__))
        return
    if result.returncode != 0:
        log.warning(t("notify.show_failed_code", code=result.returncode))
