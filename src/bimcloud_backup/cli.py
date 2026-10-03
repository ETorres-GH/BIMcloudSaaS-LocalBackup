"""Command-line interface. Without arguments it opens the graphical interface."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import os
import sys
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from bimcloud_backup import __version__, i18n, scheduler, service
from bimcloud_backup import notify as notifications
from bimcloud_backup.auth import KeyringTokenStore, TokenStore
from bimcloud_backup.backup import BackupCancelled, backup_folder_lock
from bimcloud_backup.config import Config, ConfigError, load_config, saved_language
from bimcloud_backup.errors import AuthError, BimcloudError
from bimcloud_backup.i18n import t
from bimcloud_backup.logs import get_logger, setup_logging
from bimcloud_backup.notify import Notifier
from bimcloud_backup.paths import default_config_path
from bimcloud_backup.redaction import redact
from bimcloud_backup.retention import delete_backups, find_expired_backups

EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
# Conventional exit code of a program stopped with Ctrl+C.
EXIT_CANCELLED = 130
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bimcloud-backup",
        description=t("cli.description"),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help=t("cli.help.config", path=default_config_path()),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gui = sub.add_parser("gui", help=t("cli.help.gui"))
    # "--bandeja": the name earlier versions wrote in the Run key of Windows.
    gui.add_argument("--tray", "--bandeja", action="store_true", help=t("cli.help.tray"))
    sub.add_parser("check-config", help=t("cli.help.check_config"))
    sub.add_parser("login", help=t("cli.help.login"))
    sub.add_parser("logout", help=t("cli.help.logout"))
    sub.add_parser("list", help=t("cli.help.list"))
    run = sub.add_parser("run", help=t("cli.help.run"))
    # "--agendado": the name in the scheduled tasks created by earlier versions.
    run.add_argument("--scheduled", "--agendado", action="store_true", help=t("cli.help.scheduled"))

    prune = sub.add_parser("prune", help=t("cli.help.prune"))
    prune.add_argument("--apply", action="store_true", help=t("cli.help.apply"))

    schedule = sub.add_parser("schedule", help=t("cli.help.schedule"))
    schedule.add_argument("action", choices=["install", "remove", "status"])
    return parser


def config_path_of(argv: list[str]) -> Path:
    """The --config of `argv`, read before the full parser: its help is in the saved language."""
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--config", type=Path, default=default_config_path())
    known, _ = early.parse_known_args(argv)
    return known.config


def main(argv: list[str] | None = None, token_store: TokenStore | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["gui"]
    i18n.set_language(saved_language(config_path_of(argv)))
    args = build_parser().parse_args(argv)
    store = token_store or KeyringTokenStore()

    if args.command == "gui":
        from bimcloud_backup.gui import run_gui

        return run_gui(args.config, store, tray_only=args.tray)
    if not getattr(args, "scheduled", False):
        attach_parent_console()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(t("cli.error", error=e), file=sys.stderr)
        return EXIT_CONFIG

    try:
        if args.command == "check-config":
            return _check_config(config, args.config)
        if args.command == "login":
            print(t("cli.login.opening"))
            username = service.sign_in(config, store, _open_login_page)
            print(t("cli.login.done", username=username))
            print(t("cli.login.stored"))
            return 0
        if args.command == "logout":
            store.delete(config.server_url)
            print(t("cli.logout.done"))
            return 0
        if args.command == "list":
            return _list(config, store)
        if args.command == "run":
            return _run(config, store, scheduled=args.scheduled)
        if args.command == "prune":
            return _prune(config, apply=args.apply)
        if args.command == "schedule":
            return _schedule(config, args.config, args.action)
    except AuthError as e:
        _fail(t("cli.auth_error", error=e))
        print(t("cli.auth_hint"), file=sys.stderr)
        return EXIT_AUTH
    except BimcloudError as e:
        _fail(t("cli.bimcloud_error", error=e))
        return EXIT_ERROR
    except requests.RequestException as e:
        _fail(t("cli.connection_error", error=e))
        return EXIT_ERROR
    except scheduler.SchedulerError as e:
        _fail(t("cli.scheduler_error", error=e))
        return EXIT_ERROR
    return EXIT_ERROR


def attach_parent_console(kernel32: Any = None, ancestors: list[int] | None = None) -> bool:
    """Give the windowed executable the console it was started from, if there is one.

    Needed for `start /wait` (cmd) and `Start-Process -Wait -NoNewWindow` (PowerShell). The
    one-file executable runs Python in a child of its loader, hence the grandparent too.
    """
    if sys.stdout is not None or sys.platform != "win32":
        return False
    try:
        kernel32 = kernel32 or ctypes.windll.kernel32
        ancestors = _ancestors(kernel32) if ancestors is None else ancestors
        if not any(kernel32.AttachConsole(pid) for pid in ancestors):
            return False
        # Console devices, not files: CONOUT$/CONIN$ take Unicode whatever the code page.
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115, PTH123
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115, PTH123
        if sys.stdin is None:
            sys.stdin = open("CONIN$", encoding="utf-8")  # noqa: SIM115, PTH123
        # These become the process's own streams: getpass only hides what is typed (reading
        # the console directly) when sys.stdin is sys.__stdin__.
        sys.__stdin__, sys.__stdout__, sys.__stderr__ = sys.stdin, sys.stdout, sys.stderr
    except (AttributeError, OSError):
        return False
    return True


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _ancestors(kernel32: Any) -> list[int]:
    """Parent and grandparent process ids, from a snapshot of the running processes."""
    parent = os.getppid()
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE:
        return [parent]
    try:
        entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
        found = kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == parent:
                return [parent, entry.th32ParentProcessID]
            found = kernel32.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    return [parent]


def _open_login_page(url: str) -> None:
    webbrowser.open(url)
    # Server Core has no browser, and old Internet Explorer may not open the BIMcloud page.
    print(t("cli.login.no_browser"))
    print(url)


def _fail(message: str) -> None:
    message = redact(message)
    # `run` logs to a file (and stderr); the other commands just print.
    if get_logger().handlers:
        get_logger().error(message)
    else:
        print(message, file=sys.stderr)


def _check_config(config: Config, path: Path) -> int:
    print(t("cli.check.valid", path=path))
    print(f"{'  ' + t('cli.check.server'):<16}{config.server_url}")
    print(f"{'  ' + t('cli.check.destination'):<16}{config.backup_dir}")
    selection = config.selection
    groups = [
        (t("cli.check.folders"), selection.folders),
        (t("cli.check.projects"), selection.projects),
        (t("cli.check.libraries"), selection.libraries),
    ]
    if selection.everything:
        groups = [(t("cli.check.folders"), (t("cli.check.everything"),))]
    for label, paths in groups:
        for index, path in enumerate(paths):
            print(f"{'  ' + label if index == 0 else '':<16}{path}")
    retention = t(
        "cli.check.retention_value", days=config.retention_days, minimum=config.min_backups_to_keep
    )
    print(f"{'  ' + t('cli.check.retention'):<16}{retention}")
    return 0


def _list(config: Config, store: TokenStore) -> int:
    with requests.Session() as session:
        client = service.connect(session, config, store)
        counts: Counter[str] = Counter()
        for resource in client.walk():
            kind = resource.get("type", "?")
            counts[kind] += 1
            print(f"{kind:<16} {resource.get('$path', resource.get('name', ''))}")
    print()
    totals = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "0"
    print(t("cli.list.totals", totals=totals))
    return 0


def _run(
    config: Config, store: TokenStore, scheduled: bool = False, show: Notifier | None = None
) -> int:
    setup_logging(config.verbose_logging)
    # Only runs nobody is watching need a Windows notification.
    notify = (show or notifications.show) if scheduled and config.notify_failures else _no_notice
    try:
        result = service.backup_now(config, store)
    except KeyboardInterrupt:
        # The run already deleted its incomplete folder and kept every old backup.
        notify(notifications.cancelled())
        print(t("cli.run.cancelled"), file=sys.stderr)
        return EXIT_CANCELLED
    except BackupCancelled:
        notify(notifications.cancelled())
        raise
    except AuthError:
        notify(notifications.auth_expired())
        raise
    except (BimcloudError, requests.RequestException, OSError):
        notify(notifications.failed())
        raise
    if result.errors:
        notify(notifications.finished_with_errors(len(result.errors)))
    elif not result.ok:
        notify(notifications.failed())
    if result.pending:
        get_logger().warning(t("cli.run.pending", count=len(result.pending)))
    return 0 if result.ok else EXIT_ERROR


def _no_notice(message: str) -> None:
    pass


def _prune(config: Config, apply: bool) -> int:
    expired = find_expired_backups(
        config.backup_dir,
        config.retention_days,
        config.min_backups_to_keep,
        now=datetime.now(),
    )
    if not expired:
        print(t("cli.prune.none"))
        return 0
    print(t("cli.prune.deleting" if apply else "cli.prune.would_delete"))
    for path in expired:
        print(f"  {path}")
    if apply:
        # Never delete a folder a running backup may be reusing as its incremental base.
        with backup_folder_lock(config.backup_dir):
            delete_backups(expired)
    return 0


def _schedule(config: Config, config_path: Path, action: str) -> int:
    if action == "install":
        password = None
        if config.run_logged_off:
            # Only for the Task Scheduler: never stored nor logged by this program.
            account = scheduler.windows_account()
            password = getpass.getpass(t("cli.schedule.password", account=account))
        scheduler.install(config, config_path.resolve(), password)
        print(t("cli.schedule.installed", next_run=scheduler.status()))
        print(t(MODE_TEXT[scheduler.run_mode() or scheduler.MODE_LOGGED_ON]))
    elif action == "remove":
        scheduler.remove()
        print(t("cli.schedule.removed"))
    else:
        next_run = scheduler.status()
        if not next_run:
            print(t("cli.schedule.none"))
            return 0
        print(t("cli.schedule.next_run", next_run=next_run))
        print(t(MODE_TEXT[scheduler.run_mode() or scheduler.MODE_LOGGED_ON]))
    return 0


# Keys of the texts, by how the task logs on.
MODE_TEXT = {
    scheduler.MODE_ALWAYS: "cli.schedule.mode_always",
    scheduler.MODE_LOGGED_ON: "cli.schedule.mode_logged_on",
}
