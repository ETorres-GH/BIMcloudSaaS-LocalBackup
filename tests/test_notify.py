import base64
import logging
import subprocess

import pytest

from bimcloud_backup import notify


class FakeRun:
    def __init__(self, returncode=0, error=None):
        self.returncode = returncode
        self.error = error
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, b"", b"")


def test_toast_texts_travel_in_the_environment_not_in_the_command():
    run = FakeRun()

    notify.show(notify.FAILED, run=run)

    command, kwargs = run.calls[0]
    assert command[0] == "powershell.exe" and "-EncodedCommand" in command
    assert kwargs["env"]["BCB_TOAST_TITLE"] == "BIMcloud Backup Local"
    assert kwargs["env"]["BCB_TOAST_TEXT"] == notify.FAILED
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    assert "$env:BCB_TOAST_TEXT" in script
    assert notify.FAILED not in script and notify.FAILED not in " ".join(command)
    assert kwargs["creationflags"] == notify.NO_WINDOW
    assert kwargs["timeout"] == notify.TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(error=FileNotFoundError("powershell.exe")),
        FakeRun(error=subprocess.TimeoutExpired("powershell.exe", 20)),
        FakeRun(returncode=1),
    ],
)
def test_a_toast_that_cannot_be_shown_never_breaks_the_backup(run, caplog):
    caplog.set_level(logging.WARNING, logger="bimcloud_backup")

    notify.show(notify.FAILED, run=run)

    assert "Não foi possível mostrar o aviso do Windows" in caplog.text


@pytest.mark.parametrize(
    "message",
    [
        notify.FAILED,
        notify.CANCELLED,
        notify.AUTH_EXPIRED,
        notify.finished_with_errors(1),
        notify.finished_with_errors(12),
    ],
)
def test_messages_are_short_and_point_to_the_program(message):
    assert "BIMcloud Backup Local" in message
    assert len(message) <= 120
    assert "\\" not in message and "/" not in message and "http" not in message


def test_error_count_reads_well():
    assert "1 item falhou" in notify.finished_with_errors(1)
    assert "3 itens falharam" in notify.finished_with_errors(3)
