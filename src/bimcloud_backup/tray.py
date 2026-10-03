"""Icon in the Windows notification area, a single window per user, and start with Windows.

Nothing here starts backups by itself; the automatic backup stays with the Task Scheduler.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("bimcloud_backup")

# Hidden window of the icon; a second copy of the program finds the first one by this name.
WINDOW_CLASS = "BIMcloudSaaS-LocalBackup.Bandeja"
# Named after the window, per Windows session. The installer checks it too (AppMutex).
MUTEX_NAME = "Local\\BIMcloudSaaS-LocalBackup"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "BIMcloudSaaS-LocalBackup"
# Command-line option of the entry in Run: start with only the icon, the window hidden.
TRAY_ONLY_OPTION = "--bandeja"
TIP_LIMIT = 127

# What the icon asks the window to do.
OPEN = "open"
BACKUP = "backup"
EXIT = "exit"

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
# Posted by a second copy of the program: show the window.
WM_SHOW_WINDOW = WM_APP + 2
WM_STOP = WM_APP + 3

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON, SM_CYSMICON = 49, 50
IDI_APPLICATION = 32512
ERROR_ALREADY_EXISTS = 183

MENU = ((OPEN, "Abrir"), (BACKUP, "Fazer backup agora"), (None, None), (EXIT, "Sair"))
MENU_IDS = {action: number for number, (action, _text) in enumerate(MENU, start=1) if action}

ICON_FILE = Path(__file__).resolve().parent / "assets" / "icon.ico"

_mutex: Any = None
# Icons by their hidden window: the window class, and its callback, are registered only once.
_icons: dict[int, TrayIcon] = {}


def menu_action(command: int) -> str | None:
    """The action of a menu item, by the number TrackPopupMenu returns (0 = none chosen)."""
    return next((action for action, number in MENU_IDS.items() if number == command), None)


def click_action(message: int) -> str | None:
    """Double click opens the window; the right button shows the menu (handled by the icon)."""
    return OPEN if message == WM_LBUTTONDBLCLK else None


def clip_tip(text: str) -> str:
    """Windows shows at most 127 characters in the icon's tooltip."""
    return text if len(text) <= TIP_LIMIT else text[: TIP_LIMIT - 1] + "…"


class TrayIcon:
    """The icon and its menu. `post` receives OPEN, BACKUP or EXIT, from the icon's thread."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.tip = title
        self.backup_enabled = True
        # Icon shown in the notification area.
        self.running = False
        # Hidden window up: a second copy of the program can call this one, icon or not.
        self.listening = False
        self.hwnd: int | None = None
        self.icon: int | None = None
        # Loaded from icon.ico (ours to free), not Windows' shared default icon.
        self.icon_owned = False
        self.post: Callable[[str], None] = lambda _action: None
        self.thread: threading.Thread | None = None

    def start(self, post: Callable[[str], None]) -> bool:
        """Open the hidden window and show the icon.

        False only when not even the window could be created. Without a notification area
        (Explorer not started yet) the window still answers a second copy, `running` stays
        False, and the icon appears when Explorer announces itself.
        """
        if sys.platform != "win32":
            return False
        self.post = post
        ready = threading.Event()
        self.thread = threading.Thread(target=self._loop, args=(ready,), daemon=True)
        self.thread.start()
        ready.wait(5)
        return self.listening

    def stop(self) -> None:
        if self.hwnd and self.listening:
            _api().user32.PostMessageW(self.hwnd, WM_STOP, 0, 0)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(2)

    def set_tip(self, text: str) -> None:
        self.tip = clip_tip(text)
        if self.running:
            self._notify(NIM_MODIFY, NIF_TIP)

    def balloon(self, title: str, text: str) -> None:
        """A short notice next to the icon (the toast area on Windows 10 and 11)."""
        if self.running:
            self._notify(NIM_MODIFY, NIF_INFO, info=(title, text))

    # ----------------------------------------------------------- icon thread

    def _loop(self, ready: threading.Event) -> None:
        try:
            api = _api()
            self._create_window(api)
            self.listening = True
            self.running = self._notify(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)
            if not self.running:
                log.warning("Não foi possível pôr o ícone na área de notificação")
        except OSError:
            log.warning("Não foi possível pôr o ícone na área de notificação", exc_info=True)
        finally:
            ready.set()
        if not self.listening:
            return
        msg = api.MSG()
        while api.user32.GetMessageW(api.byref(msg), None, 0, 0) > 0:
            api.user32.TranslateMessage(api.byref(msg))
            api.user32.DispatchMessageW(api.byref(msg))
        self.running = False
        self.listening = False

    def _create_window(self, api: Any) -> None:
        hinstance = api.kernel32.GetModuleHandleW(None)
        self.hwnd = api.user32.CreateWindowExW(
            0, WINDOW_CLASS, self.title, 0, 0, 0, 0, 0, None, None, hinstance, None
        )
        if not self.hwnd:
            raise OSError("CreateWindowExW falhou")
        _icons[self.hwnd] = self
        # Explorer announces this when it restarts: the icon has to be added again.
        self.taskbar_created = api.user32.RegisterWindowMessageW("TaskbarCreated")
        self.icon, self.icon_owned = _load_icon(api)

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        api = _api()
        try:
            if message == WM_TRAY:
                event = lparam & 0xFFFF
                if event == WM_RBUTTONUP:
                    self._show_menu(api)
                elif action := click_action(event):
                    self.post(action)
                return 0
            if message == WM_SHOW_WINDOW:
                self.post(OPEN)
                return 0
            if message in (WM_STOP, WM_CLOSE):
                api.user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                if self.running:
                    self._notify(NIM_DELETE, 0)
                    self.running = False
                self._free_icon(api)
                _icons.pop(hwnd, None)
                api.user32.PostQuitMessage(0)
                return 0
            if message == getattr(self, "taskbar_created", None):
                self.running = self._notify(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)
                return 0
        except Exception:  # noqa: BLE001 - an error must not escape into Windows' callback
            log.warning("Erro no ícone da área de notificação", exc_info=True)
            return 0
        return api.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self, api: Any) -> None:
        menu = api.user32.CreatePopupMenu()
        try:
            for action, text in MENU:
                if action is None:
                    api.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                grayed = action == BACKUP and not self.backup_enabled
                flags = MF_STRING | (MF_GRAYED if grayed else 0)
                api.user32.AppendMenuW(menu, flags, MENU_IDS[action], text)
            api.user32.SetMenuDefaultItem(menu, MENU_IDS[OPEN], False)
            point = api.POINT()
            api.user32.GetCursorPos(api.byref(point))
            # Without this the menu does not close when clicking elsewhere.
            api.user32.SetForegroundWindow(self.hwnd)
            command = api.user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                self.hwnd,
                None,
            )
            api.user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            api.user32.DestroyMenu(menu)
        if action := menu_action(command):
            self.post(action)

    def _free_icon(self, api: Any) -> None:
        """NIM_DELETE only takes the icon off the notification area; its handle is freed here."""
        if self.icon and self.icon_owned:
            api.user32.DestroyIcon(self.icon)
        self.icon, self.icon_owned = None, False

    def _notify(self, operation: int, flags: int, info: tuple[str, str] | None = None) -> bool:
        api = _api()
        data = api.NOTIFYICONDATAW()
        data.cbSize = api.sizeof(api.NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self.icon
        data.szTip = self.tip
        if info is not None:
            data.szInfoTitle = info[0][:63]
            data.szInfo = info[1][:255]
            data.dwInfoFlags = NIIF_INFO
        return bool(api.shell32.Shell_NotifyIconW(operation, api.byref(data)))


def _window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    icon = _icons.get(hwnd)
    if icon is not None:
        return icon._window_proc(hwnd, message, wparam, lparam)
    return _api().user32.DefWindowProcW(hwnd, message, wparam, lparam)


def _load_icon(api: Any) -> tuple[int, bool]:
    """The program's icon and True (to be freed with DestroyIcon), or Windows' shared one."""
    width = api.user32.GetSystemMetrics(SM_CXSMICON)
    height = api.user32.GetSystemMetrics(SM_CYSMICON)
    icon = api.user32.LoadImageW(None, str(ICON_FILE), IMAGE_ICON, width, height, LR_LOADFROMFILE)
    if icon:
        return icon, True
    # Shared by the whole system: never destroyed.
    return api.user32.LoadIconW(None, api.MAKEINTRESOURCE(IDI_APPLICATION)), False


# ------------------------------------------------------------ single window


def claim_single_instance() -> bool:
    """True for the first copy of the program in this Windows session."""
    global _mutex
    if sys.platform != "win32":
        return True
    api = _api()
    _mutex = api.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return api.get_last_error() != ERROR_ALREADY_EXISTS


def show_existing(wait_seconds: float = 5.0) -> bool:
    """Ask the copy already open to show its window. False if it could not be found."""
    if sys.platform != "win32":
        return False
    api = _api()
    deadline = time.monotonic() + wait_seconds
    while True:
        # The other copy may still be starting: its icon window comes a moment later.
        hwnd = api.user32.FindWindowW(WINDOW_CLASS, None)
        if hwnd:
            pid = api.wintypes.DWORD()
            api.user32.GetWindowThreadProcessId(hwnd, api.byref(pid))
            # Windows only lets the program the user just opened bring a window to the front.
            api.user32.AllowSetForegroundWindow(pid.value)
            return bool(api.user32.PostMessageW(hwnd, WM_SHOW_WINDOW, 0, 0))
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


# ---------------------------------------------------------- start with Windows


def startup_command() -> str:
    """What the Run entry starts: this executable, with only the icon at first."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" gui {TRAY_ONLY_OPTION}'
    # From the sources: pythonw.exe, so no console window opens at login.
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    if windowless.exists():
        python = windowless
    return f'"{python}" -m bimcloud_backup gui {TRAY_ONLY_OPTION}'


def _winreg() -> Any:
    import winreg

    return winreg


def starts_with_windows(reg: Any = None) -> bool:
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            reg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return False
    return True


def set_start_with_windows(enabled: bool, reg: Any = None) -> None:
    """Add or remove the entry in the user's Run key (no administrator needed)."""
    reg = reg or _winreg()
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
        if enabled:
            reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, startup_command())
            return
        with contextlib.suppress(FileNotFoundError):
            reg.DeleteValue(key, RUN_VALUE)


# --------------------------------------------------------------- Win32 API

_API: Any = None


def _api() -> Any:
    """The Win32 functions and structures, declared once with their exact signatures."""
    global _API
    if _API is None:
        _API = _declare()
    return _API


def _declare() -> Any:
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    lresult = ctypes.c_ssize_t
    wndproc = ctypes.WINFUNCTYPE(
        lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    # Private copies of the DLLs: the signatures below do not leak into ctypes.windll.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    def sig(function: Any, restype: Any, *argtypes: Any) -> None:
        function.restype = restype
        function.argtypes = argtypes

    w = wintypes
    sig(kernel32.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
    sig(kernel32.CreateMutexW, w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)
    sig(user32.RegisterClassExW, w.ATOM, ctypes.POINTER(WNDCLASSEXW))
    sig(
        user32.CreateWindowExW,
        w.HWND,
        w.DWORD,
        w.LPCWSTR,
        w.LPCWSTR,
        w.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.HMENU,
        w.HINSTANCE,
        w.LPVOID,
    )
    sig(user32.DefWindowProcW, lresult, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    sig(user32.DestroyWindow, w.BOOL, w.HWND)
    sig(user32.PostMessageW, w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    sig(user32.PostQuitMessage, None, ctypes.c_int)
    sig(user32.GetMessageW, w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
    sig(user32.TranslateMessage, w.BOOL, ctypes.POINTER(w.MSG))
    sig(user32.DispatchMessageW, lresult, ctypes.POINTER(w.MSG))
    sig(user32.RegisterWindowMessageW, w.UINT, w.LPCWSTR)
    sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    sig(
        user32.LoadImageW,
        w.HANDLE,
        w.HINSTANCE,
        w.LPCWSTR,
        w.UINT,
        ctypes.c_int,
        ctypes.c_int,
        w.UINT,
    )
    sig(user32.LoadIconW, w.HICON, w.HINSTANCE, w.LPCWSTR)
    sig(user32.DestroyIcon, w.BOOL, w.HICON)
    sig(user32.CreatePopupMenu, w.HMENU)
    sig(user32.AppendMenuW, w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
    sig(user32.SetMenuDefaultItem, w.BOOL, w.HMENU, w.UINT, w.UINT)
    sig(user32.DestroyMenu, w.BOOL, w.HMENU)
    sig(user32.GetCursorPos, w.BOOL, ctypes.POINTER(w.POINT))
    sig(user32.SetForegroundWindow, w.BOOL, w.HWND)
    sig(
        user32.TrackPopupMenu,
        w.BOOL,
        w.HMENU,
        w.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.LPVOID,
    )
    sig(user32.FindWindowW, w.HWND, w.LPCWSTR, w.LPCWSTR)
    sig(user32.GetWindowThreadProcessId, w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
    sig(user32.AllowSetForegroundWindow, w.BOOL, w.DWORD)
    sig(shell32.Shell_NotifyIconW, w.BOOL, w.DWORD, ctypes.POINTER(NOTIFYICONDATAW))

    # Registered once, for the whole process; the callback lives as long as the module.
    callback = wndproc(_window_proc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = callback
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = WINDOW_CLASS
    user32.RegisterClassExW(ctypes.byref(wc))

    return SimpleNamespace(
        window_proc=callback,
        user32=user32,
        kernel32=kernel32,
        shell32=shell32,
        wintypes=wintypes,
        byref=ctypes.byref,
        sizeof=ctypes.sizeof,
        get_last_error=ctypes.get_last_error,
        MAKEINTRESOURCE=lambda number: ctypes.cast(ctypes.c_void_p(number), w.LPCWSTR),
        WNDPROC=wndproc,
        WNDCLASSEXW=WNDCLASSEXW,
        NOTIFYICONDATAW=NOTIFYICONDATAW,
        MSG=w.MSG,
        POINT=w.POINT,
    )
