import sys
import threading

import pytest

from bimcloud_backup import tray


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRegistry:
    """The bits of winreg the Run entry uses, over a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}
        self.opened: list[str] = []

    def OpenKey(self, root, path):  # noqa: N802 - winreg's name
        self.opened.append(path)
        return FakeKey()

    def CreateKeyEx(self, root, path, reserved, access):  # noqa: N802
        assert (root, access) == ("HKCU", self.KEY_SET_VALUE)
        self.opened.append(path)
        return FakeKey()

    def QueryValueEx(self, key, name):  # noqa: N802
        try:
            return self.values[(tray.RUN_KEY, name)], self.REG_SZ
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        assert kind == self.REG_SZ
        self.values[(tray.RUN_KEY, name)] = value

    def DeleteValue(self, key, name):  # noqa: N802
        try:
            del self.values[(tray.RUN_KEY, name)]
        except KeyError:
            raise FileNotFoundError(name) from None


def test_start_with_windows_is_off_until_turned_on():
    reg = FakeRegistry()
    assert not tray.starts_with_windows(reg)
    tray.set_start_with_windows(True, reg)
    assert tray.starts_with_windows(reg)
    # Only the user's own Run key: no administrator needed.
    assert set(reg.opened) == {r"Software\Microsoft\Windows\CurrentVersion\Run"}
    assert reg.values[(tray.RUN_KEY, "BIMcloudSaaS-LocalBackup")] == tray.startup_command()


def test_turning_it_off_removes_the_entry_and_tolerates_none():
    reg = FakeRegistry()
    tray.set_start_with_windows(True, reg)
    tray.set_start_with_windows(False, reg)
    assert not tray.starts_with_windows(reg)
    tray.set_start_with_windows(False, reg)  # already off
    assert reg.values == {}


def test_startup_command_opens_only_the_icon(monkeypatch, tmp_path):
    exe = tmp_path / "Programa" / "BIMcloudBackup.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert tray.startup_command() == f'"{exe}" gui --bandeja'


def test_startup_command_from_the_sources_uses_pythonw(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert tray.startup_command() == (
        f'"{tmp_path / "pythonw.exe"}" -m bimcloud_backup gui --bandeja'
    )


def test_menu_numbers_map_back_to_actions():
    assert [text for _action, text in tray.MENU if text] == [
        "Abrir",
        "Fazer backup agora",
        "Sair",
    ]
    for action in (tray.OPEN, tray.BACKUP, tray.EXIT):
        assert tray.menu_action(tray.MENU_IDS[action]) == action
    assert tray.menu_action(0) is None  # menu closed without a choice


def test_double_click_opens_and_other_clicks_do_nothing():
    assert tray.click_action(tray.WM_LBUTTONDBLCLK) == tray.OPEN
    assert tray.click_action(0x0202) is None  # single left click


def test_tooltip_is_clipped_to_what_windows_shows():
    assert tray.clip_tip("curto") == "curto"
    clipped = tray.clip_tip("x" * 300)
    assert len(clipped) == tray.TIP_LIMIT and clipped.endswith("…")


def test_installer_uses_the_same_names():
    from tests.test_installer import ISS, define, setting

    assert "Local\\" + define("TaskName") == tray.MUTEX_NAME
    assert setting("AppMutex") == "Local\\{#TaskName}"
    assert define("TaskName") == tray.RUN_VALUE
    assert 'ValueName: "{#TaskName}"; Flags: uninsdeletevalue dontcreatekey' in ISS


@pytest.mark.skipif(sys.platform != "win32", reason="só no Windows")
def test_real_icon_starts_answers_a_second_copy_and_stops():
    """The icon itself, when this Windows has a notification area (not always in CI)."""
    actions: list[str] = []
    got = threading.Event()

    def post(action: str) -> None:
        actions.append(action)
        got.set()

    icon = tray.TrayIcon("BIMcloud Backup Local (teste)")
    if not icon.start(post):
        pytest.skip("sem área de notificação nesta sessão")
    try:
        icon.set_tip("BIMcloud Backup Local\nNenhum backup ainda")
        icon.backup_enabled = False
        # What a second copy of the program does: find the icon's window and call it.
        assert tray.show_existing(wait_seconds=2)
        assert got.wait(5)
        assert actions == [tray.OPEN]
    finally:
        icon.stop()
    assert not icon.running
    # Started again in the same process (the window class is registered only once).
    again = tray.TrayIcon("BIMcloud Backup Local (teste)")
    assert again.start(post)
    again.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="só no Windows")
def test_without_a_notification_area_the_window_still_answers(monkeypatch):
    """Explorer not started yet: no icon, but a second copy still reaches this one."""
    monkeypatch.setattr(tray.TrayIcon, "_notify", lambda self, *a, **k: False)
    actions: list[str] = []
    got = threading.Event()
    icon = tray.TrayIcon("BIMcloud Backup Local (teste)")
    assert icon.start(lambda action: actions.append(action) or got.set())
    try:
        assert icon.listening and not icon.running
        assert tray.show_existing(wait_seconds=2)
        assert got.wait(5) and actions == [tray.OPEN]
    finally:
        icon.stop()
    assert not icon.listening


class FakeUser32:
    def __init__(self, loaded):
        self.loaded = loaded
        self.destroyed: list[int] = []

    def GetSystemMetrics(self, index):  # noqa: N802 - Win32 names
        return 16

    def LoadImageW(self, *args):  # noqa: N802
        return self.loaded

    def LoadIconW(self, *args):  # noqa: N802
        return 999

    def DestroyIcon(self, icon):  # noqa: N802
        self.destroyed.append(icon)


@pytest.mark.parametrize(("loaded", "icon", "owned"), [(123, 123, True), (0, 999, False)])
def test_only_our_own_icon_is_freed(loaded, icon, owned):
    from types import SimpleNamespace

    user32 = FakeUser32(loaded)
    api = SimpleNamespace(user32=user32, MAKEINTRESOURCE=lambda number: number)
    assert tray._load_icon(api) == (icon, owned)
    tray_icon = tray.TrayIcon("x")
    tray_icon.icon, tray_icon.icon_owned = tray._load_icon(api)
    tray_icon._free_icon(api)
    # Windows' default icon is shared by every program: it is never destroyed.
    assert user32.destroyed == ([123] if owned else [])
    assert tray_icon.icon is None and not tray_icon.icon_owned
