import sys
from datetime import datetime, timedelta

import pytest
import requests
import responses

from bimcloud_backup import notify
from bimcloud_backup.backup import BackupCancelled, BackupResult, backup_folder_lock
from bimcloud_backup.cli import attach_parent_console, main
from bimcloud_backup.errors import AuthError, BimcloudError
from bimcloud_backup.retention import backup_folder_name
from tests.conftest import API, OAUTH, SERVER, MemoryTokenStore, token_response

CONFIG = """
[bimcloud]
server_url = "https://example.bimcloud.com"
username = "user"

[backup]
directory = "{directory}"
retention_days = 30
min_free_space_gb = 0
"""


def write_config(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(directory=backup_dir.as_posix()), encoding="utf-8")
    return config, backup_dir


def test_check_config_ok(tmp_path, capsys):
    config, _ = write_config(tmp_path)
    assert main(["--config", str(config), "check-config"]) == 0
    assert "Valid configuration" in capsys.readouterr().out


def test_check_config_lists_the_source_folders(tmp_path, capsys):
    config, _ = write_config(tmp_path)
    assert main(["--config", str(config), "check-config"]) == 0
    assert "Folders:      the whole BIMcloud" in capsys.readouterr().out

    text = config.read_text(encoding="utf-8").replace(
        "[bimcloud]",
        '[bimcloud]\nsource_folders = ["Obras", "Pastas Exemplo/Projeto Teste"]\n'
        'source_projects = ["Outras/ProjetoA"]\nsource_libraries = ["Outras/BibliotecaB"]',
    )
    config.write_text(text, encoding="utf-8")
    assert main(["--config", str(config), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "Folders:      Obras" in out and "Pastas Exemplo/Projeto Teste" in out
    assert "Projects:     Outras/ProjetoA" in out
    assert "Libraries:    Outras/BibliotecaB" in out
    # Every value starts in the same column.
    values = [line[16:] for line in out.splitlines()[1:]]
    assert all(value and not value[0].isspace() for value in values), out
    assert any(line.startswith("  Server:       ") for line in out.splitlines())


def test_invalid_config_returns_2(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "x.toml"), "check-config"]) == 2
    assert "Erro" in capsys.readouterr().err


def test_prune_is_dry_run_by_default(tmp_path):
    config, backup_dir = write_config(tmp_path)
    old = backup_dir / backup_folder_name(datetime.now() - timedelta(days=90))
    new = backup_dir / backup_folder_name(datetime.now())
    old.mkdir()
    new.mkdir()

    assert main(["--config", str(config), "prune"]) == 0
    assert old.exists()

    assert main(["--config", str(config), "prune", "--apply"]) == 0
    assert not old.exists()
    assert new.exists()


def test_list_without_login_asks_to_login(tmp_path, capsys):
    config, _ = write_config(tmp_path)
    assert main(["--config", str(config), "list"], token_store=MemoryTokenStore()) == 3
    assert "bimcloud-backup login" in capsys.readouterr().err


@responses.activate
def test_list_refreshes_token_and_prints_resources(tmp_path, capsys):
    config, _ = write_config(tmp_path)
    store = MemoryTokenStore("refresh-old")
    responses.post(f"{OAUTH}/token", json=token_response("access-2", "refresh-new"))
    responses.post(
        f"{API}/get-resources-by-criterion",
        json=[{"id": "p1", "type": "project", "$path": "Project Root/Casa"}],
    )

    assert main(["--config", str(config), "list"], token_store=store) == 0

    out = capsys.readouterr().out
    assert "Project Root/Casa" in out
    assert "project=1" in out
    assert store.load(SERVER) == "refresh-new"


def test_logout_removes_token(tmp_path):
    config, _ = write_config(tmp_path)
    store = MemoryTokenStore("refresh")
    assert main(["--config", str(config), "logout"], token_store=store) == 0
    assert store.load(SERVER) is None


def test_run_writes_log_and_reports_failure(tmp_path, monkeypatch):
    from bimcloud_backup import service
    from bimcloud_backup.backup import BackupResult

    config, backup_dir = write_config(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(
        service,
        "backup_now",
        lambda cfg, store: BackupResult(folder=backup_dir / "x", errors=["a.pdf: falhou"]),
    )

    assert main(["--config", str(config), "run"], token_store=MemoryTokenStore("t")) == 1
    logs = list((tmp_path / "local" / "BIMcloudSaaS-LocalBackup" / "logs").glob("*.log"))
    assert len(logs) == 1


def test_prune_apply_waits_for_no_running_backup(tmp_path, capsys, caplog):
    config, backup_dir = write_config(tmp_path)
    old = backup_dir / backup_folder_name(datetime.now() - timedelta(days=90))
    old.mkdir()
    (backup_dir / backup_folder_name(datetime.now())).mkdir()

    with backup_folder_lock(backup_dir):
        assert main(["--config", str(config), "prune", "--apply"]) == 1

    assert old.exists()
    # `_fail` prints, or logs when another test left the file logging on.
    assert "already running" in capsys.readouterr().err + caplog.text
    assert main(["--config", str(config), "prune", "--apply"]) == 0
    assert not old.exists()


def test_ctrl_c_during_run_exits_130(tmp_path, monkeypatch, capsys, caplog):
    config, _ = write_config(tmp_path)

    def interrupted(config, store):
        raise KeyboardInterrupt

    monkeypatch.setattr("bimcloud_backup.cli.service.backup_now", interrupted)

    assert main(["--config", str(config), "run"], token_store=MemoryTokenStore()) == 130
    assert "Backup cancelled" in capsys.readouterr().err


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    """A config file and a fake notifier for runs started by the Task Scheduler."""
    config, backup_dir = write_config(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    shown = []
    monkeypatch.setattr("bimcloud_backup.cli.notifications.show", shown.append)
    return config, backup_dir, shown


def outcome(monkeypatch, value):
    def backup_now(cfg, store):
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("bimcloud_backup.cli.service.backup_now", backup_now)


@pytest.mark.parametrize(
    ("value", "code", "message"),
    [
        (AuthError("invalid_grant"), 3, notify.auth_expired),
        (BimcloudError("Espaço livre abaixo de 10 GB"), 1, notify.failed),
        (requests.ConnectionError("sem rede"), 1, notify.failed),
        (KeyboardInterrupt(), 130, notify.cancelled),
        (BackupCancelled("Backup cancelado."), 1, notify.cancelled),
    ],
)
def test_scheduled_run_notifies_what_went_wrong(scheduled, monkeypatch, value, code, message):
    config, _, shown = scheduled
    outcome(monkeypatch, value)

    assert main(["--config", str(config), "run", "--scheduled"]) == code
    assert shown == [message()]


def test_scheduled_run_notifies_a_disk_error_before_raising_it(scheduled, monkeypatch):
    config, _, shown = scheduled
    outcome(monkeypatch, OSError("disco cheio"))

    with pytest.raises(OSError):
        main(["--config", str(config), "run", "--scheduled"])
    assert shown == [notify.failed()]


def test_scheduled_run_with_failed_items_notifies_the_count(scheduled, monkeypatch):
    config, backup_dir, shown = scheduled
    outcome(monkeypatch, BackupResult(folder=backup_dir / "x", errors=["a", "b"]))

    assert main(["--config", str(config), "run", "--scheduled"]) == 1
    assert shown == [notify.finished_with_errors(2)]


def test_successful_or_manual_runs_do_not_notify(scheduled, monkeypatch):
    config, backup_dir, shown = scheduled
    outcome(monkeypatch, BackupResult(folder=backup_dir / "x"))
    assert main(["--config", str(config), "run", "--scheduled"]) == 0

    outcome(monkeypatch, BimcloudError("falhou"))
    assert main(["--config", str(config), "run"]) == 1  # started by hand: the user is watching

    assert shown == []


def test_notifications_can_be_turned_off(scheduled, monkeypatch):
    config, _, shown = scheduled
    text = config.read_text(encoding="utf-8") + "\n[schedule]\nnotify_failures = false\n"
    config.write_text(text, encoding="utf-8")
    outcome(monkeypatch, BimcloudError("falhou"))

    assert main(["--config", str(config), "run", "--scheduled"]) == 1
    assert shown == []


def test_login_prints_the_address_for_another_browser(tmp_path, monkeypatch, capsys):
    config, _ = write_config(tmp_path)
    opened = []
    monkeypatch.setattr("bimcloud_backup.cli.webbrowser.open", opened.append)

    def sign_in(config, store, open_browser):
        open_browser("https://example.bimcloud.com/login?state=abc")
        return "user"

    monkeypatch.setattr("bimcloud_backup.cli.service.sign_in", sign_in)
    assert main(["--config", str(config), "login"], token_store=MemoryTokenStore()) == 0
    out = capsys.readouterr().out
    assert opened == ["https://example.bimcloud.com/login?state=abc"]
    assert "another" in out and "https://example.bimcloud.com/login?state=abc" in out


@pytest.fixture
def task(monkeypatch):
    """The Task Scheduler as the CLI sees it, with no real task."""
    installed = []
    monkeypatch.setattr(
        "bimcloud_backup.cli.scheduler.install",
        lambda config, path, password=None: installed.append(password),
    )
    monkeypatch.setattr("bimcloud_backup.cli.scheduler.status", lambda: "03/10/2026 23:00:00")
    monkeypatch.setattr("bimcloud_backup.cli.scheduler.run_mode", lambda: "always")
    monkeypatch.setattr("bimcloud_backup.cli.scheduler.windows_account", lambda: "SRV\\backup")
    return installed


def test_schedule_install_asks_the_password_only_to_run_logged_off(
    tmp_path, monkeypatch, capsys, task
):
    config, _ = write_config(tmp_path)
    asked = []
    monkeypatch.setattr(
        "bimcloud_backup.cli.getpass.getpass", lambda prompt: asked.append(prompt) or "senha"
    )
    assert main(["--config", str(config), "schedule", "install"]) == 0
    assert asked == [] and task == [None]

    config.write_text(
        config.read_text(encoding="utf-8") + "\n[schedule]\nrun_logged_off = true\n",
        encoding="utf-8",
    )
    assert main(["--config", str(config), "schedule", "install"]) == 0
    assert task == [None, "senha"]
    assert "SRV\\backup" in asked[0]
    out = capsys.readouterr().out
    assert "senha" not in out
    assert "even with nobody signed in" in out


class FakeKernel32:
    def __init__(self, with_console):
        self.with_console = with_console
        self.tried = []

    def AttachConsole(self, pid):  # noqa: N802 - the Windows name
        self.tried.append(pid)
        return pid in self.with_console


def test_with_a_console_nothing_is_attached(monkeypatch):
    kernel32 = FakeKernel32(with_console={1})
    assert attach_parent_console(kernel32, [1]) is False
    assert kernel32.tried == []


def test_the_windowed_executable_borrows_the_console_it_was_started_from(monkeypatch):
    opened = []
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)
    monkeypatch.setattr("sys.stdin", None)
    for name in ("__stdin__", "__stdout__", "__stderr__"):
        monkeypatch.setattr(f"sys.{name}", None)
    monkeypatch.setattr(
        "bimcloud_backup.cli.open", lambda name, *a, **k: opened.append(name) or name, raising=False
    )
    # The loader of the one-file executable (the parent) has no console; the prompt does.
    kernel32 = FakeKernel32(with_console={20})
    assert attach_parent_console(kernel32, [10, 20]) is True
    assert kernel32.tried == [10, 20]
    assert opened == ["CONOUT$", "CONOUT$", "CONIN$"]
    assert sys.__stdin__ == "CONIN$"


def test_without_any_console_the_output_stays_off(monkeypatch):
    monkeypatch.setattr("sys.stdout", None)
    kernel32 = FakeKernel32(with_console=set())
    assert attach_parent_console(kernel32, [10, 20]) is False
    assert sys.stdout is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_the_ancestors_start_with_the_parent():
    import ctypes
    import os

    from bimcloud_backup.cli import _ancestors

    ancestors = _ancestors(ctypes.windll.kernel32)
    assert ancestors[0] == os.getppid()
    assert len(ancestors) in (1, 2)


def test_scheduled_runs_never_look_for_a_console(tmp_path, monkeypatch):
    config, _ = write_config(tmp_path)
    attached = []
    monkeypatch.setattr("bimcloud_backup.cli.attach_parent_console", lambda: attached.append(1))
    monkeypatch.setattr("bimcloud_backup.cli._run", lambda *a, **k: 0)
    main(["--config", str(config), "run", "--scheduled"])
    assert attached == []
    main(["--config", str(config), "check-config"])
    assert attached == [1]


def test_schedule_status_shows_the_mode(tmp_path, capsys, task):
    config, _ = write_config(tmp_path)
    assert main(["--config", str(config), "schedule", "status"]) == 0
    out = capsys.readouterr().out
    assert "03/10/2026 23:00:00" in out
    assert "even with nobody signed in" in out


def test_the_command_line_speaks_the_saved_language(tmp_path, capsys):
    config, _ = write_config(tmp_path)
    assert main(["--config", str(config), "check-config"]) == 0
    assert "Valid configuration" in capsys.readouterr().out

    config.write_text(
        config.read_text(encoding="utf-8") + '\n[interface]\nlanguage = "pt-BR"\n',
        encoding="utf-8",
    )
    assert main(["--config", str(config), "check-config"]) == 0
    assert "Configuração válida" in capsys.readouterr().out


def test_the_options_of_earlier_versions_still_work(tmp_path, monkeypatch):
    # Scheduled tasks and the Run key created by earlier versions use the old names.
    config, _ = write_config(tmp_path)
    runs = []
    monkeypatch.setattr("bimcloud_backup.cli._run", lambda *a, **k: runs.append(k) or 0)
    assert main(["--config", str(config), "run", "--agendado"]) == 0
    assert main(["--config", str(config), "run", "--scheduled"]) == 0
    assert runs == [{"scheduled": True}, {"scheduled": True}]

    opened = []
    monkeypatch.setattr(
        "bimcloud_backup.gui.run_gui", lambda path, store, tray_only: opened.append(tray_only) or 0
    )
    assert main(["gui", "--bandeja"]) == 0
    assert main(["gui", "--tray"]) == 0
    assert opened == [True, True]
