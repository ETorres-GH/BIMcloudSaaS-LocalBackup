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

from bimcloud_backup import __version__, scheduler, service
from bimcloud_backup import notify as notifications
from bimcloud_backup.auth import KeyringTokenStore, TokenStore
from bimcloud_backup.backup import BackupCancelled, backup_folder_lock
from bimcloud_backup.config import Config, ConfigError, load_config
from bimcloud_backup.errors import AuthError, BimcloudError
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
        description="Backup local dos projetos, bibliotecas e arquivos do BIMcloud SaaS.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help=f"arquivo de configuração (padrão: {default_config_path()})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gui = sub.add_parser("gui", help="abre a interface gráfica")
    gui.add_argument(
        "--bandeja",
        action="store_true",
        help="abre só o ícone na área de notificação, com a janela escondida "
        "(usado ao iniciar com o Windows)",
    )
    sub.add_parser("check-config", help="valida o arquivo de configuração")
    sub.add_parser("login", help="entra no BIMcloud pelo navegador e guarda o acesso")
    sub.add_parser("logout", help="apaga o acesso guardado neste computador")
    sub.add_parser("list", help="lista tudo o que o seu usuário vê no BIMcloud")
    run = sub.add_parser("run", help="faz o backup agora")
    run.add_argument(
        "--agendado",
        action="store_true",
        help="execução do Agendador de Tarefas: avisa no Windows se falhar (notify_failures)",
    )

    prune = sub.add_parser("prune", help="remove backups mais antigos que retention_days")
    prune.add_argument(
        "--apply",
        action="store_true",
        help="apaga de fato; sem esta opção, apenas lista o que seria apagado",
    )

    schedule = sub.add_parser("schedule", help="gerencia a tarefa no Agendador do Windows")
    schedule.add_argument("action", choices=["install", "remove", "status"])
    return parser


def main(argv: list[str] | None = None, token_store: TokenStore | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["gui"]
    args = build_parser().parse_args(argv)
    store = token_store or KeyringTokenStore()

    if args.command == "gui":
        from bimcloud_backup.gui import run_gui

        return run_gui(args.config, store, tray_only=args.bandeja)
    if not getattr(args, "agendado", False):
        attach_parent_console()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        if args.command == "check-config":
            return _check_config(config, args.config)
        if args.command == "login":
            print("Abrindo o navegador para você entrar no BIMcloud (aguardando até 5 minutos)...")
            username = service.sign_in(config, store, _open_login_page)
            print(f"Login realizado como {username}.")
            print("O acesso ficou guardado no Gerenciador de Credenciais do Windows.")
            return 0
        if args.command == "logout":
            store.delete(config.server_url)
            print("Acesso removido deste computador.")
            return 0
        if args.command == "list":
            return _list(config, store)
        if args.command == "run":
            return _run(config, store, scheduled=args.agendado)
        if args.command == "prune":
            return _prune(config, apply=args.apply)
        if args.command == "schedule":
            return _schedule(config, args.config, args.action)
    except AuthError as e:
        _fail(f"Erro de autenticação: {e}")
        print("Rode `bimcloud-backup login` para entrar novamente.", file=sys.stderr)
        return EXIT_AUTH
    except BimcloudError as e:
        _fail(f"Erro do BIMcloud: {e}")
        return EXIT_ERROR
    except requests.RequestException as e:
        _fail(f"Não foi possível conectar ao BIMcloud: {e}")
        return EXIT_ERROR
    except scheduler.SchedulerError as e:
        _fail(f"Erro no Agendador de Tarefas: {e}")
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
    print("Se o navegador não abrir, abra este endereço em qualquer navegador, mesmo em outro")
    print("computador, e entre por lá:")
    print(url)


def _fail(message: str) -> None:
    message = redact(message)
    # `run` logs to a file (and stderr); the other commands just print.
    if get_logger().handlers:
        get_logger().error(message)
    else:
        print(message, file=sys.stderr)


def _check_config(config: Config, path: Path) -> int:
    print(f"Configuração válida: {path}")
    print(f"{'  Servidor:':<16}{config.server_url}")
    print(f"{'  Destino:':<16}{config.backup_dir}")
    selection = config.selection
    groups = [
        ("  Pastas:", selection.folders),
        ("  Projetos:", selection.projects),
        ("  Bibliotecas:", selection.libraries),
    ]
    if selection.everything:
        groups = [("  Pastas:", ("todo o BIMcloud",))]
    for label, paths in groups:
        for index, path in enumerate(paths):
            print(f"{label if index == 0 else '':<16}{path}")
    retention = f"{config.retention_days} dias (mínimo {config.min_backups_to_keep})"
    print(f"{'  Retenção:':<16}{retention}")
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
    print("Total por tipo: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "0"))
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
        notify(notifications.CANCELLED)
        print("Backup cancelado. Nenhum backup antigo foi apagado.", file=sys.stderr)
        return EXIT_CANCELLED
    except BackupCancelled:
        notify(notifications.CANCELLED)
        raise
    except AuthError:
        notify(notifications.AUTH_EXPIRED)
        raise
    except (BimcloudError, requests.RequestException, OSError):
        notify(notifications.FAILED)
        raise
    if result.errors:
        notify(notifications.finished_with_errors(len(result.errors)))
    elif not result.ok:
        notify(notifications.FAILED)
    if result.pending:
        get_logger().warning(
            "%d projetos/bibliotecas ainda não são exportados por esta versão", len(result.pending)
        )
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
        print("Nenhum backup expirado.")
        return 0
    verb = "Apagando" if apply else "Seriam apagados (use --apply para apagar)"
    print(f"{verb}:")
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
            password = getpass.getpass(f"Senha do Windows de {account} (não aparece na tela): ")
        scheduler.install(config, config_path.resolve(), password)
        print(f"Tarefa agendada. Próxima execução: {scheduler.status()}")
        print(MODE_TEXT[scheduler.run_mode() or scheduler.MODE_LOGGED_ON])
    elif action == "remove":
        scheduler.remove()
        print("Tarefa agendada removida.")
    else:
        next_run = scheduler.status()
        if not next_run:
            print("Nenhuma tarefa agendada.")
            return 0
        print(f"Próxima execução: {next_run}")
        print(MODE_TEXT[scheduler.run_mode() or scheduler.MODE_LOGGED_ON])
    return 0


MODE_TEXT = {
    scheduler.MODE_ALWAYS: "Roda mesmo sem ninguém conectado no Windows.",
    scheduler.MODE_LOGGED_ON: "Roda só com você conectado no Windows.",
}
