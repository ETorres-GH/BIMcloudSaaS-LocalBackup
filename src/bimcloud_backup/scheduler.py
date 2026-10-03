"""Registration of the backup in the Windows Task Scheduler (schtasks.exe).

The task runs as the current user, who owns the refresh token. With `run_logged_off` the Windows
password goes to the Task Scheduler (a logon without password cannot read the Credential Manager).
"""

from __future__ import annotations

import base64
import ctypes
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from bimcloud_backup.config import UNIT_DAYS, UNIT_HOURS, Config
from bimcloud_backup.i18n import t

TASK_NAME = "BIMcloudSaaS-LocalBackup"
# Keeps schtasks from flashing a console window when called from the GUI.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# How the task logs on (the <LogonType> of the task XML).
MODE_LOGGED_ON = "logged_on"  # InteractiveToken: only while the user is logged in
MODE_ALWAYS = "always"  # Password: logged in or not

# Task Scheduler 2.0 (COM) constants.
TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_PASSWORD = 1
# The password arrives on stdin (base64 of UTF-8, so any character survives the console code
# page), never on a command line another process could read. The script prints only "OK" or the
# HRESULT of the failure, never a message that could quote what it got.
PASSWORD_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "try {",
        "  $bytes = [Convert]::FromBase64String([Console]::In.ReadLine())",
        "  $password = [Text.Encoding]::UTF8.GetString($bytes)",
        "  $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "  $service = New-Object -ComObject Schedule.Service",
        "  $service.Connect()",
        "  $folder = $service.GetFolder('\\')",
        f"  $task = $folder.GetTask('{TASK_NAME}')",
        f"  $null = $folder.RegisterTaskDefinition('{TASK_NAME}', $task.Definition, "
        f"{TASK_CREATE_OR_UPDATE}, $user, $password, {TASK_LOGON_PASSWORD})",
        "  [Console]::Out.WriteLine('OK')",
        "} catch {",
        "  [Console]::Out.WriteLine('ERR ' + $_.Exception.HResult)",
        "  exit 1",
        "}",
    ]
)
# The text of each, by its key in the locales.
PASSWORD_ERRORS = {
    0x8007052E: "scheduler.wrong_password",
    0x80070569: "scheduler.no_batch_logon",
    0x80070005: "scheduler.access_denied",
}
TIMEOUT_SECONDS = 60
# GetDriveTypeW: a drive letter mapped to a network share.
DRIVE_REMOTE = 4


class SchedulerError(RuntimeError):
    pass


def backup_command(config_path: Path) -> list[str]:
    """Command line the scheduled task runs."""
    # --scheduled: the run was started by the task, so failures show a Windows notification.
    args = ["--config", str(config_path), "run", "--scheduled"]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    # pythonw.exe runs without opening a console window.
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else python), "-m", "bimcloud_backup", *args]


def create_command(config: Config, command: list[str], now: datetime | None = None) -> list[str]:
    cmd = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/TR", subprocess.list2cmdline(command)]
    every = str(config.schedule_every)
    if config.schedule_unit == UNIT_DAYS:
        cmd += ["/SC", "DAILY", "/MO", every]
    elif config.schedule_unit == UNIT_HOURS:
        cmd += ["/SC", "HOURLY", "/MO", every]
    else:
        cmd += ["/SC", "MINUTE", "/MO", every]
    return [*cmd, "/ST", start_time(config, now or datetime.now())]


def start_time(config: Config, now: datetime) -> str:
    """HH:MM of the first run. Without /ST, schtasks would use the time the task was saved.

    Days start at `run_at`. Minutes and hours start at the next multiple of the interval
    counted from midnight (every 6 hours: 00:00, 06:00, 12:00, 18:00), past midnight at 00:00.
    """
    if config.schedule_unit == UNIT_DAYS:
        return config.run_at.strftime("%H:%M")
    step = config.schedule_every * (60 if config.schedule_unit == UNIT_HOURS else 1)
    minutes = (now.hour * 60 + now.minute) // step * step + step
    if minutes >= 24 * 60:
        minutes = 0
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def install(config: Config, config_path: Path, password: str | None = None) -> None:
    """Create (or replace) the task. With `password`, it runs even with nobody logged in.

    The password goes straight to the Task Scheduler and is never kept nor logged here.
    """
    if password is not None:
        check_destination(config.backup_dir)
    _run(create_command(config, backup_command(config_path)))
    if password is not None:
        _set_password(password)


def windows_account() -> str:
    """DOMAIN\\user of the current user, as the password prompt shows it."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain and user else user


def check_destination(backup_dir: Path) -> None:
    """With nobody logged in there are no mapped drives: a share needs its \\\\server path."""
    drive = backup_dir.drive
    if len(drive) == 2 and drive[1] == ":" and _drive_type(drive + "\\") == DRIVE_REMOTE:
        raise SchedulerError(t("scheduler.mapped_drive", folder=backup_dir, drive=drive))


def _drive_type(root: str) -> int:
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(root)
    except (AttributeError, OSError):
        return 0


def password_command() -> list[str]:
    encoded = base64.b64encode(PASSWORD_SCRIPT.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def _set_password(password: str) -> None:
    secret = base64.b64encode(password.encode("utf-8")).decode("ascii")
    fallback = t("scheduler.fallback")
    try:
        result = subprocess.run(
            password_command(),
            input=secret + "\n",
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SchedulerError(
            f"{t('scheduler.password_not_sent', error=type(e).__name__)} {fallback}"
        ) from None
    if result.returncode == 0 and result.stdout.strip() == "OK":
        return
    match = re.search(r"ERR (-?\d+)", result.stdout)
    code = int(match.group(1)) & 0xFFFFFFFF if match else None
    if code in PASSWORD_ERRORS:
        message = t(PASSWORD_ERRORS[code])
    elif code is not None:
        message = t("scheduler.password_refused", code=f"0x{code:08X}")
    else:
        message = t("scheduler.unexpected_answer", code=result.returncode)
    raise SchedulerError(f"{message} {fallback}")


def remove() -> None:
    _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])


def status() -> str | None:
    """Return the next run time of the task, or None if it is not registered."""
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        # Field name depends on the Windows language ("Next Run Time", "Próxima Execução"...).
        if key.strip().lower().startswith(("next run", "próxima", "proxima")):
            return value.strip()
    return t("scheduler.scheduled")


def run_mode() -> str | None:
    """MODE_ALWAYS or MODE_LOGGED_ON, read from the task itself; None if it is not registered."""
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        return None
    match = re.search(r"<LogonType>\s*(\w+)\s*</LogonType>", result.stdout)
    # Without <LogonType> the Task Scheduler uses the interactive token.
    logon = match.group(1) if match else "InteractiveToken"
    return MODE_ALWAYS if logon in ("Password", "InteractiveTokenOrPassword") else MODE_LOGGED_ON


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise SchedulerError(message or t("scheduler.schtasks_failed", code=result.returncode))
