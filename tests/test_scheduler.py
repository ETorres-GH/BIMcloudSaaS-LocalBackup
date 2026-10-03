import base64
import subprocess
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path

import pytest

from bimcloud_backup import scheduler
from bimcloud_backup.config import Config

CONFIG = Config(server_url="https://example.bimcloud.com", backup_dir=Path("D:/B"))
COMMAND = [r"C:\Program Files\App\BIMcloudBackup.exe", "--config", r"C:\cfg\config.toml", "run"]


def test_daily_task():
    cmd = scheduler.create_command(replace(CONFIG, run_at=time(2, 5)), COMMAND)
    assert cmd[:5] == ["schtasks", "/Create", "/F", "/TN", scheduler.TASK_NAME]
    assert cmd[cmd.index("/TR") + 1] == (
        r'"C:\Program Files\App\BIMcloudBackup.exe" --config C:\cfg\config.toml run'
    )
    assert cmd[-6:] == ["/SC", "DAILY", "/MO", "1", "/ST", "02:05"]


def test_every_few_days_task():
    config = replace(CONFIG, schedule_every=2, run_at=time(23, 0))
    assert scheduler.create_command(config, COMMAND)[-6:] == [
        "/SC",
        "DAILY",
        "/MO",
        "2",
        "/ST",
        "23:00",
    ]


@pytest.mark.parametrize(
    ("unit", "every", "expected"),
    [
        ("minutes", 90, ["/SC", "MINUTE", "/MO", "90", "/ST", "15:00"]),
        ("hours", 12, ["/SC", "HOURLY", "/MO", "12", "/ST", "00:00"]),
    ],
)
def test_interval_task_always_has_a_start_time(unit, every, expected):
    config = replace(CONFIG, schedule_unit=unit, schedule_every=every)
    cmd = scheduler.create_command(config, COMMAND, now=datetime(2026, 10, 3, 14, 20))
    assert cmd[-6:] == expected


@pytest.mark.parametrize(
    ("unit", "every", "now", "expected"),
    [
        ("minutes", 15, (14, 20), "14:30"),
        ("minutes", 15, (14, 30), "14:45"),
        ("minutes", 1, (9, 59), "10:00"),
        ("minutes", 45, (23, 50), "00:00"),
        ("hours", 1, (14, 20), "15:00"),
        ("hours", 6, (14, 20), "18:00"),
        ("hours", 6, (18, 0), "00:00"),
        ("hours", 5, (21, 0), "00:00"),
        ("days", 2, (14, 20), "02:05"),
    ],
)
def test_start_time_is_the_next_multiple_of_the_interval(unit, every, now, expected):
    config = replace(CONFIG, schedule_unit=unit, schedule_every=every, run_at=time(2, 5))
    assert scheduler.start_time(config, datetime(2026, 10, 3, *now)) == expected


def test_backup_command_from_source_uses_module(monkeypatch):
    monkeypatch.delattr("sys.frozen", raising=False)
    cmd = scheduler.backup_command(Path("cfg.toml"))
    assert cmd[1:] == ["-m", "bimcloud_backup", "--config", "cfg.toml", "run", "--agendado"]


def test_backup_command_from_executable(monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", r"C:\App\BIMcloudBackup.exe")
    assert scheduler.backup_command(Path("cfg.toml")) == [
        r"C:\App\BIMcloudBackup.exe",
        "--config",
        "cfg.toml",
        "run",
        "--agendado",
    ]


def test_the_scheduled_task_marks_its_runs():
    cmd = scheduler.create_command(CONFIG, scheduler.backup_command(Path("cfg.toml")))
    assert cmd[cmd.index("/TR") + 1].endswith("run --agendado")


SECRET = "s3nh@ çã"


class FakeRun:
    """Stands in for subprocess.run: records each call and answers like Windows would."""

    def __init__(self, password_reply="OK\n", password_code=0, xml=""):
        self.calls = []
        self.password_reply = password_reply
        self.password_code = password_code
        self.xml = xml

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if cmd[0] == "powershell.exe":
            return subprocess.CompletedProcess(cmd, self.password_code, self.password_reply, "")
        if "/XML" in cmd:
            code = 0 if self.xml else 1
            return subprocess.CompletedProcess(cmd, code, self.xml, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(scheduler.subprocess, "run", fake)
    monkeypatch.setattr(scheduler, "_drive_type", lambda root: 3)
    return fake


def test_install_without_password_keeps_the_logged_on_task(fake_run):
    scheduler.install(CONFIG, Path("cfg.toml"))
    assert [cmd[0] for cmd, _ in fake_run.calls] == ["schtasks"]
    assert "/RU" not in fake_run.calls[0][0] and "/RP" not in fake_run.calls[0][0]


def test_the_password_goes_through_stdin_never_the_command_line(fake_run):
    scheduler.install(CONFIG, Path("cfg.toml"), SECRET)
    (create, _), (powershell, kwargs) = fake_run.calls
    assert create[:2] == ["schtasks", "/Create"]
    assert powershell == scheduler.password_command()
    encoded = kwargs["input"].strip()
    # Neither the password nor its base64 (what stdin carries) is on a command line.
    assert all(SECRET not in part and encoded not in part for part in create + powershell)
    assert base64.b64decode(encoded).decode("utf-8") == SECRET
    assert kwargs["timeout"] == scheduler.TIMEOUT_SECONDS


def test_a_failed_schtasks_never_reaches_the_password_step(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "ERRO: acesso negado.")

    monkeypatch.setattr(scheduler.subprocess, "run", run)
    monkeypatch.setattr(scheduler, "_drive_type", lambda root: 3)
    with pytest.raises(scheduler.SchedulerError, match="acesso negado"):
        scheduler.install(CONFIG, Path("cfg.toml"), SECRET)
    assert [cmd[0] for cmd in calls] == ["schtasks"]


def test_the_script_registers_the_task_with_the_password_logon():
    script = scheduler.PASSWORD_SCRIPT
    assert f"GetTask('{scheduler.TASK_NAME}')" in script
    assert f"$user, $password, {scheduler.TASK_LOGON_PASSWORD})" in script
    assert "[Console]::In.ReadLine()" in script
    decoded = base64.b64decode(scheduler.password_command()[-1]).decode("utf-16-le")
    assert decoded == script


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("ERR -2147023570\n", "não aceitou a senha"),
        ("ERR -2147023511\n", "trabalho em lotes"),
        ("ERR -2147024891\n", "negou acesso"),
        ("ERR -2147024894\n", "0x80070002"),
        ("", "não respondeu como esperado"),
    ],
)
def test_a_refused_password_says_why_and_never_quotes_it(fake_run, reply, expected):
    fake_run.password_reply, fake_run.password_code = reply, 1
    with pytest.raises(scheduler.SchedulerError) as error:
        scheduler.install(CONFIG, Path("cfg.toml"), SECRET)
    assert expected in str(error.value)
    assert "só com você conectado" in str(error.value)
    assert SECRET not in str(error.value)


def test_powershell_missing_is_a_scheduler_error(monkeypatch):
    def run(cmd, **kwargs):
        if cmd[0] == "powershell.exe":
            raise FileNotFoundError(SECRET)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(scheduler.subprocess, "run", run)
    monkeypatch.setattr(scheduler, "_drive_type", lambda root: 3)
    with pytest.raises(scheduler.SchedulerError) as error:
        scheduler.install(CONFIG, Path("cfg.toml"), SECRET)
    assert "FileNotFoundError" in str(error.value)
    assert SECRET not in str(error.value)


def test_a_mapped_drive_is_refused_before_anything_runs(fake_run, monkeypatch):
    monkeypatch.setattr(scheduler, "_drive_type", lambda root: scheduler.DRIVE_REMOTE)
    config = replace(CONFIG, backup_dir=Path("Z:/Backups"))
    with pytest.raises(scheduler.SchedulerError, match=r"\\\\servidor\\pasta"):
        scheduler.install(config, Path("cfg.toml"), SECRET)
    assert fake_run.calls == []


def test_unc_and_local_destinations_are_fine(monkeypatch):
    seen = []
    monkeypatch.setattr(scheduler, "_drive_type", lambda root: seen.append(root) or 3)
    scheduler.check_destination(Path(r"\\servidor\backups\bimcloud"))
    scheduler.check_destination(Path("D:/Backups"))
    assert seen == ["D:\\"]


def test_a_logged_on_task_with_a_mapped_drive_is_left_as_before(fake_run, monkeypatch):
    monkeypatch.setattr(scheduler, "_drive_type", lambda root: scheduler.DRIVE_REMOTE)
    scheduler.install(replace(CONFIG, backup_dir=Path("Z:/B")), Path("cfg.toml"))
    assert len(fake_run.calls) == 1


@pytest.mark.parametrize(
    ("xml", "mode"),
    [
        ("<Principal><LogonType>Password</LogonType></Principal>", scheduler.MODE_ALWAYS),
        ("<LogonType>InteractiveToken</LogonType>", scheduler.MODE_LOGGED_ON),
        ("<Principal><UserId>S-1-5-21-1</UserId></Principal>", scheduler.MODE_LOGGED_ON),
        ("", None),
    ],
)
def test_run_mode_is_read_from_the_task(fake_run, xml, mode):
    fake_run.xml = xml
    assert scheduler.run_mode() == mode


def test_windows_account(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "ESCRITORIO")
    monkeypatch.setenv("USERNAME", "backup")
    assert scheduler.windows_account() == "ESCRITORIO\\backup"
