"""Graphical interface: one window, status on the left and settings on the right."""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import shutil
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

import requests
import sv_ttk

from bimcloud_backup import __version__, history, i18n, scheduler, service, tray
from bimcloud_backup.auth import TokenStore
from bimcloud_backup.backup import BackupCancelled, Progress
from bimcloud_backup.config import (
    PROJECTS_BIMPROJECT,
    PROJECTS_BOTH,
    PROJECTS_PLN,
    UNIT_DAYS,
    UNIT_MINUTES,
    UNITS,
    VERSIONING_HISTORY,
    VERSIONING_LATEST,
    Config,
    ConfigError,
    Selection,
    check_interval,
    default_raw,
    load_config,
    load_raw,
    normalize_selection,
    parse_config,
    save_config,
    save_language,
    saved_language,
    schedule_from,
    selection_from,
    unit_names,
)
from bimcloud_backup.errors import AuthError, BimcloudError
from bimcloud_backup.failures import Failure, count_failed, first_line, where, why
from bimcloud_backup.folder_picker import FolderPicker, describe_selection
from bimcloud_backup.i18n import LANGUAGES, format_count, format_date, format_decimal, plural, t
from bimcloud_backup.logs import RedactingFormatter, get_logger, setup_logging
from bimcloud_backup.paths import log_dir
from bimcloud_backup.redaction import redact
from bimcloud_backup.state import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    LastRun,
    load_last_run,
    load_manifest,
)

APP_TITLE = "BIMcloud Backup Local"
MAX_USERNAME_CHARS = 32
DESIGN_WIDTH = 1040
# Height added at start-up, on top of the minimum: room for the advanced options in the window
# and for longer lists of backups and activity.
LIST_EXTRA_HEIGHT = 200
# Space between the advanced options shown in the window and the row with "Save changes".
ADVANCED_GAP = 8
POLL_MS = 100
# Windows picks the best size for the title bar and taskbar.
WINDOW_ICON_SIZES = (256, 64, 48, 32, 16)
REFRESH_MS = 30_000
# Choices for the automatic backup. A saved value outside them still shows up and is kept.
HOURS = tuple(f"{h:02d}" for h in range(24))
MINUTES = tuple(f"{m:02d}" for m in range(0, 60, 5))

# Segoe Fluent Icons / Segoe MDL2 Assets code points.
ICON = {
    "cloud": "\ue753",
    "check": "\ue73e",
    "warning": "\ue7ba",
    "error": "\ue783",
    "empty": "\ue9ce",
    "calendar": "\ue787",
    "drive": "\ueda2",
    "download": "\ue896",
    "project": "\ue80f",
    "library": "\ue8f1",
    "file": "\ue7c3",
    "folder": "\ue8b7",
    "history": "\ue81c",
    "person": "\ue77b",
    "settings": "\ue713",
    "help": "\ue9ce",
    "globe": "\ue774",
}

# Options with a "?" next to them, because their name does not say everything. The hint of
# each is the text "help.<key>" of the locales.
HELP = (
    "username",
    "projects_format",
    "snapshots",
    "destination",
    "history",
    "latest",
    "every",
    "logged_off",
    "max_duration",
    "min_free_space",
    "client_id",
    "verbose",
    "notify_failures",
    "startup",
)

ASSETS = Path(__file__).resolve().parent / "assets"

# Sun Valley sizes its fonts in pixels (sv_ttk/sv.tcl), which ignore Windows' 125%/150% scale,
# while this program's own fonts are in points and grow. These are the theme's sizes at 100%.
THEME_FONT_PIXELS = {
    "SunValleyCaptionFont": 12,
    "SunValleyBodyFont": 14,
    "SunValleyBodyStrongFont": 14,
    "SunValleyBodyLargeFont": 18,
    "SunValleySubtitleFont": 20,
    "SunValleyTitleFont": 28,
    "SunValleyTitleLargeFont": 40,
    "SunValleyDisplayFont": 68,
}

COLORS = {
    "brand": "#7ebec5",  # BIMcloud teal
    "accent": "#317a81",  # teal dark enough for icons and text on white
    "ink": "#150d12",
    "on_brand": "#1f4448",
    "bg": "#ffffff",
    "muted": "#5f6368",
    "ok": "#2e7d32",
    "warn": "#b26a00",
    "warn_bg": "#fff1d6",
    "bad": "#c62828",
    "text_bg": "#ffffff",
    "text_fg": "#1b1b1b",
}


class Tooltip:
    """A short hint shown while the mouse rests on a widget, even a disabled one.

    `text` is asked each time, so the hint can depend on the moment (or be None: no hint).
    """

    DELAY_MS = 400

    def __init__(self, widget: tk.Widget, text: Callable[[], str | None]):
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        self._pending: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self.hide()
        self._pending = self.widget.after(self.DELAY_MS, self.show)

    def show(self) -> None:
        self._pending = None
        text = self.text()
        if not text:
            return
        self.window = window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        label = tk.Label(
            window,
            text=text,
            background="#ffffe1",
            foreground="#1b1b1b",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=3,
            wraplength=320,
            justify="left",
        )
        label.pack()
        # Near the right edge of the screen the hint would be cut off.
        right = self.widget.winfo_screenwidth() - label.winfo_reqwidth() - 8
        window.wm_geometry(f"+{max(0, min(x, right))}+{y}")

    def hide(self, _event: Any = None) -> None:
        if self._pending is not None:
            self.widget.after_cancel(self._pending)
            self._pending = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class HelpMark:
    """The "?" next to an option. Its hint shows with the mouse over it or, with the keyboard,
    when Tab reaches it; Esc hides it."""

    def __init__(self, parent: tk.Misc, text: str, colors: dict[str, str]):
        self.text = text
        self.colors = colors
        self.label = label = ttk.Label(
            parent,
            text=ICON["help"],
            style="Help.TLabel",
            foreground=colors["accent"],
            cursor="question_arrow",
            takefocus=True,
        )
        self.tooltip = Tooltip(label, lambda: text)
        label.bind("<FocusIn>", self._focus_in, add="+")
        label.bind("<FocusOut>", self._focus_out, add="+")
        label.bind("<Escape>", self.tooltip.hide, add="+")
        # Shown by the keyboard, the hint would stay open after a click on something that takes
        # no focus, when the window moves, or when the "?" leaves the window (advanced options).
        label.bind("<Unmap>", self.tooltip.hide, add="+")
        self.top = top = label.winfo_toplevel()
        top.bind("<ButtonPress>", self._click, add="+")
        top.bind("<Configure>", self._window_changed, add="+")
        top.bind("<Unmap>", self._window_changed, add="+")

    def _click(self, event: Any) -> None:
        if event.widget is not self.label:
            self.tooltip.hide()

    def _window_changed(self, event: Any) -> None:
        # The bindings of the window run for every widget inside it too.
        if event.widget is self.top:
            self.tooltip.hide()

    def _focus_in(self, _event: Any = None) -> None:
        self.label.configure(foreground=self.colors["ink"])
        self.tooltip.hide()
        self.tooltip.show()

    def _focus_out(self, _event: Any = None) -> None:
        self.label.configure(foreground=self.colors["accent"])
        self.tooltip.hide()


class QueueLogHandler(logging.Handler):
    def __init__(self, events: queue.Queue):
        super().__init__()
        self._events = events
        self.setFormatter(RedactingFormatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        # Technical details stay in the log file; the window shows the plain line before them.
        if getattr(record, "technical", False):
            return
        self._events.put(("log", self.format(record)))


HISTORY_COLUMNS = ("when", "size", "status")


def history_longest(column: str) -> tuple[str, ...]:
    """The widest texts a column of the backup list may show, in the chosen language."""
    if column == "when":
        return (format_date(datetime(2000, 12, 28, 23, 59), "format.short_datetime"),)
    if column == "size":
        return (f"{format_decimal(999.9)} GB",)
    return tuple(t(f"history.status.{status}", count=99) for status in HISTORY_STATUSES.values())


class FailuresWindow:
    """What failed in one backup: the item, the reason in plain words and the technical text."""

    def __init__(
        self,
        parent: tk.Misc,
        failures: list[Failure],
        backup_name: str,
        colors: dict[str, str],
        fonts: tuple[tuple[str, int], tuple[str, int], tuple[str, int]],
    ):
        heading_font, text_font, small_font = fonts
        self.failures = failures
        self.window = window = tk.Toplevel(parent)
        window.title(t("failures_window.title", name=backup_name))
        window.configure(background=colors["bg"])
        window.transient(parent)
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(
            body,
            text=t("failures_window.summary", failed=count_failed(len(failures))),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.text = text = tk.Text(
            body,
            width=80,
            height=14,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#d0d0d0",
            background=colors["text_bg"],
            foreground=colors["text_fg"],
            font=text_font,
            padx=10,
            pady=8,
        )
        text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, command=text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.tag_configure("where", font=heading_font, spacing1=6)
        text.tag_configure("why", spacing1=2)
        text.tag_configure(
            "technical", font=small_font, foreground=colors["muted"], spacing1=2, spacing3=6
        )
        for failure in failures:
            text.insert("end", f"{where(failure)}\n", "where")
            text.insert("end", f"{why(failure)}\n", "why")
            technical = t("failures_window.technical", text=first_line(failure.message))
            text.insert("end", f"{technical}\n", "technical")
        text.configure(state="disabled")
        buttons = ttk.Frame(body)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.copy_button = ttk.Button(
            buttons, text=t("failures_window.copy"), command=self.copy_technical
        )
        self.copy_button.pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text=t("button.close"), command=window.destroy).pack(side="left")

    def technical_text(self) -> str:
        """Every failure as recorded, for support: path and the full original message."""
        return "\n\n".join(failure.text for failure in self.failures)

    def copy_technical(self) -> None:
        self.window.clipboard_clear()
        self.window.clipboard_append(self.technical_text())
        self.copy_button.configure(text=t("failures_window.copied"))


# Options with no control in the window: saving keeps the value read from config.toml.
FILE_ONLY_OPTIONS = (("backup", "parallel_downloads"), ("backup", "export_stall_minutes"))


class App:
    def __init__(
        self,
        root: tk.Tk,
        config_path: Path,
        store: TokenStore,
        tray_icon: tray.TrayIcon | None = None,
    ):
        self.root = root
        self.config_path = config_path
        self.store = store
        self.events: queue.Queue = queue.Queue()
        # The icon in the notification area; None when there is none (or it did not start).
        self.tray_icon = _started(tray_icon, lambda action: self.events.put(("tray", action)))
        self.tray_notice_shown = False
        self.last_run: LastRun | None = None
        self.busy = False
        self.dirty = False
        # The settings as last loaded or saved (see `_form_state`).
        self.saved_state: tuple[Any, ...] | None = None
        self.picker_open = False
        self.log_handler = QueueLogHandler(self.events)
        get_logger().addHandler(self.log_handler)

        self.colors = COLORS
        self._apply_theme()
        self._init_fonts()
        self._init_styles()

        root.title(APP_TITLE)
        self._set_window_icon()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._init_vars()
        self._build()
        self._load()
        self._refresh_status()
        self._refresh_auth()
        # Last: the texts filled in above change the natural size of the window.
        self._fit_to_screen()
        root.after(POLL_MS, self._poll)
        root.after(REFRESH_MS, self._periodic_refresh)

    # ------------------------------------------------------------ look & feel

    def _apply_theme(self) -> None:
        """Sun Valley light, with a white base and the accent sprites swapped for teal."""
        sv_ttk.set_theme("light", root=self.root)
        self._scale_theme_fonts()
        self.root.configure(background=self.colors["bg"])
        sheet = (ASSETS / "spritesheet_light.png").as_posix()
        try:
            # The theme re-applies its palette (tk_setPalette) on <<ThemeChanged>>, so
            # the base colors are changed in the theme itself and then re-applied.
            self.root.tk.eval(
                f"""
                set ::ttk::theme::sv_light::colors(-bg) {self.colors["bg"]}
                set ::ttk::theme::sv_light::colors(-selbg) {self.colors["accent"]}
                set ::ttk::theme::sv_light::colors(-accent) {self.colors["accent"]}
                configure_colors
                set sheet [image create photo -file {{{sheet}}} -format png]
                foreach {{name x y width height}} $::spriteinfo {{
                    $::ttk::theme::sv_light::I($name) copy $sheet \\
                        -from $x $y [expr {{$x+$width}}] [expr {{$y+$height}}] \\
                        -compositingrule set
                }}
                image delete $sheet
                """
            )
        except tk.TclError:
            # Cosmetic only: the window still works with the theme's default colors.
            get_logger().warning(t("gui.theme_failed"), exc_info=True)

    def _set_window_icon(self) -> None:
        """Program icon on this window and, as the default, on every window opened later.

        The folder picker is a Toplevel of this window, so it gets the same icon.
        """
        try:
            self.icons = [
                tk.PhotoImage(master=self.root, file=(ASSETS / f"icon-{size}.png").as_posix())
                for size in WINDOW_ICON_SIZES
            ]
            self.root.iconphoto(True, *self.icons)
        except tk.TclError:
            # Cosmetic only: without the PNGs the window keeps the default Tk icon.
            self.icons = []
            get_logger().warning(t("gui.icon_failed"), exc_info=True)

    def _scale_theme_fonts(self) -> None:
        """Make the theme's pixel fonts follow the Windows scale, like the program's own fonts.

        `tk scaling` is 96/72 at 100%; at 150% it is 1.5 times that.
        """
        self.scale = max(1.0, float(self.root.tk.call("tk", "scaling")) * 72 / 96)
        for name, pixels in THEME_FONT_PIXELS.items():
            try:
                tkfont.nametofont(name, root=self.root).configure(size=-round(pixels * self.scale))
            except tk.TclError:
                continue
        try:
            body = tkfont.nametofont("SunValleyBodyFont", root=self.root)
        except tk.TclError:
            return
        # The theme computed the tree row height from the font before it was resized.
        ttk.Style(self.root).configure("Treeview", rowheight=body.metrics("linespace") + 3)

    def px(self, pixels: int) -> int:
        """A size given in pixels at 100%, at the current Windows scale."""
        return round(pixels * self.scale)

    def _init_fonts(self) -> None:
        families = set(tkfont.families(self.root))

        def pick(*names: str) -> str:
            return next((n for n in names if n in families), "Segoe UI")

        text = pick("Segoe UI Variable Text", "Segoe UI")
        display = pick("Segoe UI Variable Display Semib", "Segoe UI Semibold")
        icons = pick("Segoe Fluent Icons", "Segoe MDL2 Assets")
        self.f_title = (display, 17)
        self.f_status = (display, 20)
        self.f_heading = (display, 11)
        self.f_text = (text, 10)
        self.f_small = (text, 9)
        self.f_icon = (icons, 14)
        self.f_icon_help = (icons, 10)
        self.f_icon_big = (icons, 30)
        self.f_icon_header = (icons, 24)

    def _init_styles(self) -> None:
        s = ttk.Style(self.root)
        s.configure("Title.TLabel", font=self.f_title)
        s.configure("Heading.TLabel", font=self.f_heading)
        s.configure("Status.TLabel", font=self.f_status)
        s.configure("Muted.TLabel", font=self.f_small)
        s.configure("Icon.TLabel", font=self.f_icon)
        s.configure("Help.TLabel", font=self.f_icon_help)
        s.configure("Tag.TLabel", font=self.f_small)
        s.configure("Big.Accent.TButton", font=(self.f_heading[0], 12), padding=(16, 10))
        # tk_setPalette (run by the theme) gives every label an explicit foreground,
        # which beats the style, so colored labels get `foreground=` directly.
        s.configure("BigIcon.TLabel", font=self.f_icon_big)
        s.configure("Dot.TLabel", font=self.f_small)

    # ----------------------------------------------------------------- state

    def _init_vars(self) -> None:
        s, b = tk.StringVar, tk.BooleanVar
        self.v: dict[str, tk.Variable] = {
            "server_url": s(),
            "client_id": s(),
            "username": s(),
            "directory": s(),
            "include_projects": b(),
            "include_libraries": b(),
            "include_files": b(),
            "projects_format": s(),
            "include_backups_in_export": b(),
            "include_backups_in_library_export": b(),
            "versioning": s(),
            "retention_days": s(),
            "min_backups_to_keep": s(),
            "max_duration_hours": s(),
            "min_free_space_gb": s(),
            "schedule_every": s(),
            "schedule_unit": s(),
            "run_at": s(),
            "verbose": b(),
            "notify_failures": b(),
            "run_logged_off": b(),
        }
        self.auto = b()
        self.startup = b()
        # Hour and minute of `run_at`, each in its own list (see `_build_schedule`).
        self.run_hour = s()
        self.run_minute = s()
        self.splitting_run_at = False
        # The unit as the list shows it, singular or plural (see `_show_unit`).
        self.unit_label = s()
        self.showing_unit = False
        self.cancel_event = threading.Event()
        self.closing = False
        self.selection = Selection()
        self.selection_text = s()
        self.progress_title = s()
        self.progress_current = s()
        self.progress_files = s()
        self.advanced_window: tk.Toplevel | None = None
        # The "?" of each option in the main window, by its key in HELP.
        self.help_marks: dict[str, HelpMark] = {}
        self.auth_text = s(value=t("auth.checking"))
        self.save_text = s(value="")
        self.last_title = s()
        self.last_detail = s()
        self.next_title = s()
        self.next_detail = s()
        self.logged_off_hint = s()
        self.login_url = ""
        self.disk_title = s()
        self.disk_detail = s()

    def _load(self) -> None:
        self._load_values()
        for var in (*self.v.values(), self.auto, self.startup):
            var.trace_add("write", lambda *_: self._update_dirty())

    def _load_values(self) -> None:
        """Fill the form from config.toml (or the defaults), with nothing left to save."""
        raw = default_raw()
        # The folders are read from the file itself: the defaults always contain an empty
        # `source_folders`, which would hide the old `source_folder` of earlier versions.
        file_bimcloud: dict[str, Any] = {}
        file_schedule: dict[str, Any] = {}
        if self.config_path.exists():
            try:
                for section, values in load_raw(self.config_path).items():
                    if isinstance(values, dict):
                        raw.setdefault(section, {}).update(values)
                        if section == "bimcloud":
                            file_bimcloud = values
                        elif section == "schedule":
                            file_schedule = values
            except ConfigError as e:
                messagebox.showwarning(APP_TITLE, t("gui.config_ignored", error=e))
        self.file_only = {(section, key): raw[section][key] for section, key in FILE_ONLY_OPTIONS}
        bc, bk, sc = raw["bimcloud"], raw["backup"], raw["schedule"]
        every, unit = _file_schedule(file_schedule)
        values = {
            "server_url": bc["server_url"],
            "client_id": bc["client_id"],
            "username": bc["username"],
            "directory": bk["directory"],
            "include_projects": bk["include_projects"],
            "include_libraries": bk["include_libraries"],
            "include_files": bk["include_files"],
            "projects_format": bk["projects_format"],
            "include_backups_in_export": bk["include_backups_in_export"],
            "include_backups_in_library_export": bk["include_backups_in_library_export"],
            "versioning": bk["versioning"],
            "retention_days": bk["retention_days"],
            "min_backups_to_keep": bk["min_backups_to_keep"],
            "max_duration_hours": bk["max_duration_hours"],
            "min_free_space_gb": bk["min_free_space_gb"],
            "schedule_every": every,
            "schedule_unit": unit,
            "run_at": sc["run_at"],
            "verbose": raw["logging"]["verbose"],
            "notify_failures": sc["notify_failures"],
            "run_logged_off": sc["run_logged_off"],
        }
        for key, value in values.items():
            self.v[key].set(value)
        self._show_selection(_file_selection(file_bimcloud))
        self.auto.set(_safe_schedule_status() is not None)
        self.startup.set(_safe_starts_with_windows())
        self._sync_enabled()
        self._remember_saved()

    def _form_raw(self, directory_fallback: str | None = None) -> dict[str, Any]:
        v = {k: var.get() for k, var in self.v.items()}
        every = _number(v["schedule_every"], int, t("schedule.every"))
        if v["schedule_unit"] in UNITS:
            check_interval(every, v["schedule_unit"], t("schedule.every"))
        raw: dict[str, Any] = {
            "bimcloud": {
                "server_url": v["server_url"],
                "client_id": v["client_id"],
                "username": v["username"],
                "source_folders": list(self.selection.folders),
                "source_projects": list(self.selection.projects),
                "source_libraries": list(self.selection.libraries),
            },
            "backup": {
                "directory": v["directory"] or (directory_fallback or ""),
                "include_projects": v["include_projects"],
                "include_libraries": v["include_libraries"],
                "include_files": v["include_files"],
                "projects_format": v["projects_format"],
                "include_backups_in_export": v["include_backups_in_export"],
                "include_backups_in_library_export": v["include_backups_in_library_export"],
                "versioning": v["versioning"],
                "retention_days": _number(v["retention_days"], int, t("field.retention_days")),
                "min_backups_to_keep": _number(
                    v["min_backups_to_keep"], int, t("field.min_backups_to_keep")
                ),
                "max_duration_hours": _number(
                    v["max_duration_hours"], float, t("field.max_duration_hours")
                ),
                "min_free_space_gb": _number(
                    v["min_free_space_gb"], float, t("field.min_free_space_gb")
                ),
            },
            "schedule": {
                "every": every,
                "unit": v["schedule_unit"],
                "run_at": v["run_at"],
                "notify_failures": v["notify_failures"],
                "run_logged_off": v["run_logged_off"],
            },
            "logging": {"verbose": v["verbose"]},
            "interface": {"language": i18n.language()},
        }
        for (section, key), value in self.file_only.items():
            raw[section][key] = value
        return raw

    def _config(self, for_connection_only: bool = False, quiet: bool = False) -> Config | None:
        try:
            raw = self._form_raw(directory_fallback="." if for_connection_only else None)
            return parse_config(raw)
        except ConfigError as e:
            if not quiet:
                messagebox.showerror(APP_TITLE, str(e))
            return None

    # -------------------------------------------------------------- building

    def _build(self) -> None:
        self._build_header(self.root).pack(fill="x")
        # No scrolling: the window's minimum size holds everything, and the lists on the
        # left take whatever height is left over.
        self.body = outer = ttk.Frame(self.root, padding=(20, 12, 20, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        self._build_status_column(outer).grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self._build_settings_column(outer).grid(row=0, column=1, sticky="nsew")

    def _build_header(self, parent: tk.Misc) -> tk.Frame:
        """Teal band across the top of the window."""
        c = self.colors
        bar = tk.Frame(parent, background=c["brand"], padx=20, pady=8)
        bar.columnconfigure(1, weight=1)
        tk.Label(
            bar, text=ICON["cloud"], font=self.f_icon_header, fg="#ffffff", bg=c["brand"]
        ).grid(row=0, column=0, rowspan=2, padx=(0, 14))
        tk.Label(bar, text=APP_TITLE, font=self.f_title, fg=c["ink"], bg=c["brand"]).grid(
            row=0, column=1, sticky="w"
        )
        tk.Label(
            bar,
            text=t("gui.subtitle"),
            font=self.f_small,
            fg=c["on_brand"],
            bg=c["brand"],
        ).grid(row=1, column=1, sticky="w")
        # The version and the language share a corner; the unsaved changes take it over.
        self.version_label = corner = tk.Frame(bar, background=c["brand"])
        corner.grid(row=0, column=2, rowspan=2, sticky="e")
        tk.Label(
            corner,
            text=t("gui.version", version=__version__),
            font=self.f_small,
            fg=c["on_brand"],
            bg=c["brand"],
        ).pack(anchor="e")
        self._build_language_button(corner).pack(anchor="e", pady=(2, 0))
        self._build_unsaved_banner(bar).grid(row=0, column=2, rowspan=2, sticky="e")
        self.unsaved_banner.grid_remove()
        return bar

    def _build_language_button(self, parent: tk.Misc) -> tk.Frame:
        """The current language, which opens a menu to choose another one."""
        c = self.colors
        button = tk.Frame(parent, background=c["brand"], cursor="hand2", takefocus=True)
        tk.Label(
            button, text=ICON["globe"], font=self.f_icon_help, fg=c["ink"], bg=c["brand"]
        ).pack(side="left", padx=(0, 4))
        tk.Label(
            button,
            text=f"{LANGUAGES[i18n.language()]} \u25be",
            font=(*self.f_small, "underline"),
            fg=c["ink"],
            bg=c["brand"],
        ).pack(side="left")
        self.language_menu = menu = tk.Menu(button, tearoff=False)
        self.language_choice = tk.StringVar(value=i18n.language())
        for code, name in LANGUAGES.items():
            menu.add_radiobutton(
                label=name,
                value=code,
                variable=self.language_choice,
                command=lambda: self._change_language(self.language_choice.get()),
            )

        def show(_event: Any = None) -> str:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
            return "break"

        for widget in (button, *button.winfo_children()):
            widget.bind("<Button-1>", show)
        button.bind("<Return>", show)
        button.bind("<space>", show)
        self.language_button = button
        Tooltip(button, lambda: t("gui.language_hint"))
        return button

    def _build_unsaved_banner(self, parent: tk.Misc) -> tk.Frame:
        """Shown in the header while the settings have changes not saved yet."""
        c = self.colors
        banner = tk.Frame(parent, background=c["warn_bg"], padx=12, pady=6)
        tk.Label(
            banner, text=ICON["warning"], font=self.f_icon, fg=c["warn"], bg=c["warn_bg"]
        ).pack(side="left", padx=(0, 10))
        tk.Label(
            banner,
            text=t("gui.unsaved_message"),
            font=self.f_text,
            fg=c["ink"],
            bg=c["warn_bg"],
            justify="left",
            wraplength=self.px(280),
        ).pack(side="left", padx=(0, 12))
        ttk.Button(banner, text=t("gui.discard_changes"), command=self._discard).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            banner, text=t("gui.save_changes"), style="Accent.TButton", command=self._save
        ).pack(side="left")
        self.unsaved_banner = banner
        return banner

    def _card(
        self, parent: tk.Widget, icon: str, title: str, help_key: str | None = None
    ) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 6, 14, 8))
        card.columnconfigure(1, weight=1)
        # The card fill matches the window, so inner frames are plain frames
        # (nesting Card.TFrame would draw extra borders).
        head = ttk.Frame(card)
        head.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        ttk.Label(
            head, text=ICON[icon], style="Icon.TLabel", foreground=self.colors["accent"]
        ).pack(side="left", padx=(0, 8))
        ttk.Label(head, text=title, style="Heading.TLabel").pack(
            side="left", padx=(0, 4 if help_key else 16)
        )
        if help_key:
            self._help(head, help_key).pack(side="left", padx=(0, 16))
        card.head = head  # type: ignore[attr-defined]
        return card

    def _help(self, parent: tk.Misc, key: str, keep: bool = True) -> ttk.Label:
        mark = HelpMark(parent, t(f"help.{key}"), self.colors)
        if keep:
            self.help_marks[key] = mark
        return mark.label

    def _with_help(
        self, parent: tk.Misc, widget: Callable[[tk.Misc], tk.Widget], key: str, keep: bool = True
    ) -> ttk.Frame:
        """`widget` (built inside the returned frame) followed by its "?"."""
        frame = ttk.Frame(parent)
        widget(frame).pack(side="left")
        self._help(frame, key, keep).pack(side="left", padx=(4, 0))
        return frame

    # Left column: what is happening -----------------------------------------

    def _build_status_column(self, parent: ttk.Frame) -> ttk.Frame:
        col = ttk.Frame(parent, width=340)
        self.status_column = col
        col.columnconfigure(0, weight=1)
        # The two lists share the height left over by the window, and shrink first.
        col.rowconfigure(5, weight=1)
        col.rowconfigure(6, weight=1)

        last = ttk.Frame(col, style="Card.TFrame", padding=(16, 10))
        last.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        last.columnconfigure(1, weight=1)
        self.last_icon = ttk.Label(
            last, text=ICON["empty"], style="BigIcon.TLabel", foreground=self.colors["muted"]
        )
        self.last_icon.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 14))
        ttk.Label(
            last,
            text=t("status.last_backup"),
            style="Muted.TLabel",
            foreground=self.colors["muted"],
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(last, textvariable=self.last_title, style="Status.TLabel").grid(
            row=1, column=1, sticky="w"
        )
        ttk.Label(
            last,
            textvariable=self.last_detail,
            style="Muted.TLabel",
            foreground=self.colors["muted"],
            wraplength=self.px(260),
        ).grid(row=2, column=1, sticky="w")
        # Shown only when the last backup had items that failed.
        self.last_failures_link = ttk.Label(
            last,
            text=t("status.see_failed_items"),
            style="Muted.TLabel",
            foreground=self.colors["accent"],
            cursor="hand2",
            font=(*self.f_small, "underline"),
        )
        self.last_failures_link.grid(row=3, column=1, sticky="w")
        self.last_failures_link.bind("<Button-1>", lambda _event: self._show_last_failures())
        self.last_failures_link.grid_remove()
        self.last_run_folder = ""

        info = ttk.Frame(col, style="Card.TFrame", padding=(16, 10))
        info.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        info.columnconfigure(1, weight=1)
        for row, (icon, title_var, detail_var) in enumerate(
            (
                ("calendar", self.next_title, self.next_detail),
                ("drive", self.disk_title, self.disk_detail),
            )
        ):
            ttk.Label(
                info, text=ICON[icon], style="Icon.TLabel", foreground=self.colors["accent"]
            ).grid(row=row * 2, column=0, rowspan=2, sticky="n", padx=(0, 12), pady=(2, 0))
            ttk.Label(info, textvariable=title_var, style="Heading.TLabel").grid(
                row=row * 2, column=1, sticky="w", pady=(6 if row else 0, 0)
            )
            ttk.Label(
                info, textvariable=detail_var, style="Muted.TLabel", foreground=self.colors["muted"]
            ).grid(row=row * 2 + 1, column=1, sticky="w")
        self.disk_bar = ttk.Progressbar(info, maximum=100)
        self.disk_bar.grid(row=4, column=1, sticky="ew", pady=(6, 0))

        self.run_button = ttk.Button(
            col,
            text=f"{ICON['download']}   {t('status.back_up_now')}",
            style="Big.Accent.TButton",
            command=self._run_backup,
        )
        self.run_button.grid(row=2, column=0, sticky="ew")
        self.run_tooltip = Tooltip(
            self.run_button, lambda: None if self.busy else self._blocked_hint("backup")
        )
        ttk.Label(
            col,
            text=t("status.browser_warning"),
            style="Muted.TLabel",
            foreground=self.colors["muted"],
            wraplength=self.px(320),
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))
        # Shown only while a backup runs.
        self.running = ttk.Frame(col)
        self.running.grid(row=4, column=0, sticky="ew", pady=(6, 8))
        self.running.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(self.running, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.cancel_button = ttk.Button(
            self.running, text=t("status.cancel_backup"), command=self._cancel_backup
        )
        self.cancel_button.grid(row=0, column=1)
        # How far the backup is. It takes the place of the list of saved backups while the
        # backup runs, so the window never grows.
        self.progress_panel = ttk.Frame(col)
        self.progress_panel.columnconfigure(0, weight=1)
        for row, (var, style) in enumerate(
            (
                (self.progress_title, "Heading.TLabel"),
                (self.progress_current, "Muted.TLabel"),
                (self.progress_files, "Muted.TLabel"),
            )
        ):
            ttk.Label(
                self.progress_panel,
                textvariable=var,
                style=style,
                foreground=self.colors["muted"] if style == "Muted.TLabel" else "",
                wraplength=self.px(260),
            ).grid(row=row, column=0, sticky="w", pady=(0, 2))
        self.running.grid_remove()

        self.history_panel = self._build_history_list(col)
        self.history_panel.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        self.progress_panel.grid(row=5, column=0, sticky="new", pady=(10, 0))
        self.progress_panel.grid_remove()

        activity = ttk.Frame(col)
        activity.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(1, weight=1)
        head = ttk.Frame(activity)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(head, text=t("status.activity"), style="Heading.TLabel").pack(side="left")
        ttk.Button(head, text=t("status.open_logs"), command=self._open_logs).pack(side="right")
        box = ttk.Frame(activity, style="Card.TFrame", padding=2)
        box.grid(row=1, column=0, columnspan=2, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.activity = tk.Text(
            box,
            height=2,
            width=44,
            state="disabled",
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=self.f_small,
            background=self.colors["text_bg"],
            foreground=self.colors["text_fg"],
            padx=8,
            pady=6,
        )
        self.activity.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, command=self.activity.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.activity.configure(yscrollcommand=scroll.set)
        return col

    # Right column: how it is configured --------------------------------------

    def _build_settings_column(self, parent: ttk.Frame) -> ttk.Frame:
        col = ttk.Frame(parent)
        col.columnconfigure(0, weight=1)
        col.columnconfigure(1, weight=1)

        self._build_connection(col).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._build_content(col).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._build_destination(col).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._build_history(col).grid(row=3, column=0, sticky="nsew", padx=(0, 5), pady=(0, 8))
        self._build_schedule(col).grid(row=3, column=1, sticky="nsew", padx=(5, 0), pady=(0, 8))
        # The advanced options go in the height left over when they fit there (see
        # `_place_advanced`). `place` never changes the size the column asks for, so showing
        # them never makes the window's minimum size larger.
        self.advanced_slot = ttk.Frame(col)
        self.advanced_slot.grid(row=4, column=0, columnspan=2, sticky="nsew")
        self.advanced_inline = self._build_advanced(self.advanced_slot)
        self._build_footer(col).grid(row=5, column=0, columnspan=2, sticky="sew")
        self.settings_column = col
        self.advanced_shown_inline = False
        self._show_advanced_inline(False, force=True)
        col.bind("<Configure>", lambda _event: self._place_advanced(), add="+")
        return col

    def _field(self, card: ttk.Frame, row: int, label: str, key: str, hint: str = "") -> ttk.Entry:
        ttk.Label(card, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
        entry = ttk.Entry(card, textvariable=self.v[key])
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        if hint:
            ttk.Label(card, text=hint, style="Muted.TLabel", foreground=self.colors["muted"]).grid(
                row=row + 1, column=1, sticky="w", pady=(0, 2)
            )
        return entry

    def _build_connection(self, parent: ttk.Frame) -> ttk.Frame:
        card = self._card(parent, "person", t("connection.title"))
        self.login_button = ttk.Button(card.head, text=t("connection.sign_in"), command=self._login)
        self.login_button.pack(side="right")
        ttk.Label(card.head, textvariable=self.auth_text).pack(side="right", padx=(0, 12))
        self.auth_dot = ttk.Label(
            card.head, text="●", style="Dot.TLabel", foreground=self.colors["muted"]
        )
        self.auth_dot.pack(side="right", padx=(0, 6))
        card.columnconfigure(1, weight=3)
        card.columnconfigure(3, weight=2)
        self._field(card, 1, t("connection.address"), "server_url").configure(width=34)
        self._with_help(
            card, lambda f: ttk.Label(f, text=t("connection.username")), "username"
        ).grid(row=1, column=2, padx=(16, 8))
        ttk.Entry(card, textvariable=self.v["username"], width=18).grid(
            row=1, column=3, sticky="ew", pady=3
        )
        # Shown while waiting for the login: a server may have no browser that opens BIMcloud.
        self.login_link = ttk.Label(
            card,
            text=t("connection.copy_login_url"),
            style="Muted.TLabel",
            foreground=self.colors["accent"],
            cursor="hand2",
            font=(*self.f_small, "underline"),
        )
        self.login_link.grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.login_link.bind("<Button-1>", lambda _event: self._copy_login_url())
        self.login_link.grid_remove()
        return card

    def _build_content(self, parent: ttk.Frame) -> ttk.Frame:
        card = self._card(parent, "download", t("content.title"))
        row = ttk.Frame(card)
        row.grid(row=1, column=0, columnspan=3, sticky="ew")
        for column, (icon, text, key) in enumerate(
            (
                ("project", t("content.projects"), "include_projects"),
                ("library", t("content.libraries"), "include_libraries"),
                ("file", t("content.files"), "include_files"),
            )
        ):
            row.columnconfigure(column, weight=1)
            item = ttk.Frame(row)
            item.grid(row=0, column=column, sticky="w")
            ttk.Label(
                item, text=ICON[icon], style="Icon.TLabel", foreground=self.colors["accent"]
            ).pack(side="left", padx=(0, 6))
            ttk.Checkbutton(
                item,
                text=text,
                variable=self.v[key],
                style="Switch.TCheckbutton",
                command=self._sync_enabled,
            ).pack(side="left")

        projects = ttk.Frame(card)
        projects.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(projects, text=t("content.projects_as")).pack(side="left", padx=(0, 4))
        self._help(projects, "projects_format").pack(side="left", padx=(0, 8))
        self.project_format_widgets: list[ttk.Widget] = []
        for value, text in (
            (PROJECTS_BIMPROJECT, "BIMProject"),
            (PROJECTS_PLN, "PLN"),
            (PROJECTS_BOTH, t("content.both")),
        ):
            radio = ttk.Radiobutton(
                projects,
                text=text,
                variable=self.v["projects_format"],
                value=value,
                command=self._sync_enabled,
            )
            radio.pack(side="left", padx=(0, 12))
            self.project_format_widgets.append(radio)
        # One row for both: each on its own row made the window too tall for a notebook.
        snapshots = ttk.Frame(card)
        snapshots.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(snapshots, text=t("content.snapshots")).pack(side="left", padx=(0, 4))
        self._help(snapshots, "snapshots").pack(side="left", padx=(0, 8))
        self.include_backups_check = ttk.Checkbutton(
            snapshots, text=".BIMProject", variable=self.v["include_backups_in_export"]
        )
        self.include_backups_check.pack(side="left", padx=(0, 12))
        self.include_library_backups_check = ttk.Checkbutton(
            snapshots, text=".BIMLibrary", variable=self.v["include_backups_in_library_export"]
        )
        self.include_library_backups_check.pack(side="left")
        ttk.Label(
            card,
            text=t("content.pln_hint"),
            style="Muted.TLabel",
            foreground=self.colors["muted"],
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(2, 0))
        # Which BIMcloud folders is part of *what* to copy, not of where to save it.
        origin = ttk.Frame(card)
        origin.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        origin.columnconfigure(1, weight=1)
        ttk.Label(origin, text=t("content.copy_from")).grid(
            row=0, column=0, sticky="nw", padx=(0, 12), pady=(3, 0)
        )
        ttk.Label(
            origin,
            textvariable=self.selection_text,
            style="Muted.TLabel",
            foreground=self.colors["muted"],
            justify="left",
        ).grid(row=0, column=1, sticky="w", pady=(3, 0))
        self.folders_button = ttk.Button(
            origin, text=t("content.choose"), command=self._choose_folders
        )
        self.folders_button.grid(row=0, column=2, sticky="ne", padx=(8, 0))
        self.folders_tooltip = Tooltip(self.folders_button, lambda: self._blocked_hint("choose"))
        return card

    def _build_destination(self, parent: ttk.Frame) -> ttk.Frame:
        card = self._card(parent, "folder", t("destination.title"), help_key="destination")
        # Everything on the title row: a row of its own cost height the window does not have.
        card.head.grid_configure(pady=0)
        ttk.Button(card.head, text=t("destination.browse"), command=self._browse).pack(
            side="right", padx=(8, 0)
        )
        ttk.Entry(card.head, textvariable=self.v["directory"]).pack(
            side="right", fill="x", expand=True
        )
        return card

    def _build_history(self, parent: ttk.Frame) -> ttk.Frame:
        card = self._card(parent, "history", t("history.title"))
        self._with_help(
            card,
            lambda f: ttk.Radiobutton(
                f,
                text=t("history.keep_history"),
                variable=self.v["versioning"],
                value=VERSIONING_HISTORY,
                command=self._sync_enabled,
            ),
            "history",
        ).grid(row=1, column=0, columnspan=3, sticky="w")
        detail = ttk.Frame(card)
        detail.grid(row=2, column=0, columnspan=3, sticky="w", padx=(28, 0), pady=(2, 4))
        ttk.Label(detail, text=t("history.for")).grid(row=0, column=0)
        self.retention_entry = _number_entry(detail, self.v["retention_days"])
        self.retention_entry.grid(row=0, column=1, padx=6)
        ttk.Label(detail, text=t("history.days_at_least")).grid(row=0, column=2)
        self.min_keep_entry = _number_entry(detail, self.v["min_backups_to_keep"])
        self.min_keep_entry.grid(row=0, column=3, padx=6)
        ttk.Label(detail, text=t("history.backups")).grid(row=0, column=4)
        self._with_help(
            card,
            lambda f: ttk.Radiobutton(
                f,
                text=t("history.keep_latest"),
                variable=self.v["versioning"],
                value=VERSIONING_LATEST,
                command=self._sync_enabled,
            ),
            "latest",
        ).grid(row=3, column=0, columnspan=3, sticky="w")
        return card

    def _build_schedule(self, parent: ttk.Frame) -> ttk.Frame:
        card = self._card(parent, "calendar", t("schedule.title"))
        self.schedule_card = card
        ttk.Checkbutton(
            card.head,
            variable=self.auto,
            style="Switch.TCheckbutton",
            command=self._sync_enabled,
        ).pack(side="right")
        # "Every [2] [days]" and, below, "at [23]:[00]": every day is 1 day, with no option of
        # its own. The time on the same row made the window wider.
        self.every_label = ttk.Label(card, text=t("schedule.every"))
        self.every_label.grid(row=2, column=0, sticky="w", pady=2)
        every = ttk.Frame(card)
        every.grid(row=2, column=1, columnspan=2, sticky="w", padx=6, pady=2)
        self.every_entry = _number_entry(every, self.v["schedule_every"])
        self.every_entry.pack(side="left")
        plurals = tuple(unit_names(unit)[1] for unit in UNITS)
        self.unit_box = _choice_box(every, self.unit_label, plurals)
        self.unit_box.pack(side="left", padx=(6, 0))
        self._help(every, "every").pack(side="left", padx=(6, 0))
        self.unit_label.trace_add("write", lambda *_: self._choose_unit())
        for var in (self.v["schedule_every"], self.v["schedule_unit"]):
            var.trace_add("write", lambda *_: self._show_unit())
        self.at_label = ttk.Label(card, text=t("schedule.at"))
        self.at_label.grid(row=3, column=0, sticky="w", pady=2)
        # Lists instead of free text: "8", "08" or "20" left doubts about what to type.
        at = ttk.Frame(card)
        at.grid(row=3, column=1, columnspan=2, sticky="w", padx=6, pady=2)
        self.hour_box = _choice_box(at, self.run_hour, HOURS)
        self.hour_box.pack(side="left")
        self.colon_label = ttk.Label(at, text=":")
        self.colon_label.pack(side="left", padx=3)
        self.minute_box = _choice_box(at, self.run_minute, MINUTES)
        self.minute_box.pack(side="left")
        for var in (self.run_hour, self.run_minute):
            var.trace_add("write", lambda *_: self._join_run_at())
        self.v["run_at"].trace_add("write", lambda *_: self._split_run_at())
        logged_off = ttk.Frame(card)
        logged_off.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.logged_off_check = ttk.Checkbutton(
            logged_off,
            text=t("schedule.logged_off"),
            variable=self.v["run_logged_off"],
            command=self._sync_enabled,
        )
        self.logged_off_check.pack(side="left")
        self._help(logged_off, "logged_off").pack(side="left", padx=(4, 0))
        ttk.Label(
            card,
            textvariable=self.logged_off_hint,
            style="Muted.TLabel",
            foreground=self.colors["muted"],
            wraplength=self.px(250),
        ).grid(row=5, column=0, columnspan=3, sticky="w")
        return card

    def _build_advanced(self, parent: tk.Misc, keep: bool = True) -> ttk.Frame:
        """Rarely changed options, in a window of their own (see `_open_advanced`)."""
        card = self._card(parent, "settings", t("advanced.title"))

        def label(text: str, key: str) -> ttk.Frame:
            return self._with_help(card, lambda f: ttk.Label(f, text=text), key, keep)

        label(t("advanced.stop_after"), "max_duration").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=3
        )
        # One limit per row: side by side they made the card wider than the window.
        hours = ttk.Frame(card)
        hours.grid(row=1, column=1, columnspan=3, sticky="w")
        _number_entry(hours, self.v["max_duration_hours"]).pack(side="left", padx=(0, 6))
        ttk.Label(hours, text=t("advanced.hours")).pack(side="left")
        label(t("advanced.or_disk_below"), "min_free_space").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=3
        )
        disk = ttk.Frame(card)
        disk.grid(row=2, column=1, columnspan=3, sticky="w")
        _number_entry(disk, self.v["min_free_space_gb"]).pack(side="left", padx=(0, 6))
        ttk.Label(disk, text=t("advanced.gb_free")).pack(side="left", padx=(0, 12))
        ttk.Label(
            disk, text=t("advanced.no_limit"), style="Muted.TLabel", foreground=self.colors["muted"]
        ).pack(side="left")
        label(t("advanced.client_id"), "client_id").grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=3
        )
        extra = ttk.Frame(card)
        extra.grid(row=3, column=1, columnspan=3, sticky="w")
        ttk.Entry(extra, textvariable=self.v["client_id"], width=24).pack(side="left")
        ttk.Checkbutton(
            extra,
            text=t("advanced.verbose"),
            variable=self.v["verbose"],
            style="Switch.TCheckbutton",
        ).pack(side="left", padx=(24, 0))
        self._help(extra, "verbose", keep).pack(side="left", padx=(4, 0))
        # A row of its own: next to the identifier it made the card wider than the window.
        notify = ttk.Frame(card)
        notify.grid(row=4, column=1, columnspan=3, sticky="w", pady=(3, 0))
        notify_check = ttk.Checkbutton(
            notify,
            text=t("advanced.notify_failures"),
            variable=self.v["notify_failures"],
            style="Switch.TCheckbutton",
        )
        notify_check.pack(side="left")
        self._help(notify, "notify_failures", keep).pack(side="left", padx=(4, 0))
        self._with_help(
            card,
            lambda f: ttk.Checkbutton(
                f,
                text=t("advanced.startup"),
                variable=self.startup,
                style="Switch.TCheckbutton",
            ),
            "startup",
            keep,
        ).grid(row=5, column=1, columnspan=3, sticky="w", pady=(3, 0))
        if keep:
            # The copy in the main window. The one in the separate window is destroyed with
            # it, so references to it would go stale.
            self.notify_check = notify_check
            self.advanced_card = card
        return card

    def _build_footer(self, parent: ttk.Frame) -> ttk.Frame:
        bar = ttk.Frame(parent)
        self.advanced_button = ttk.Button(
            bar, text=f"{ICON['settings']}  {t('advanced.button')}", command=self._open_advanced
        )
        self.advanced_button.pack(side="left")
        self.save_button = ttk.Button(
            bar, text=t("gui.save_changes"), style="Accent.TButton", command=self._save
        )
        self.save_button.pack(side="right")
        ttk.Label(
            bar, textvariable=self.save_text, style="Muted.TLabel", foreground=self.colors["muted"]
        ).pack(side="right", padx=12)
        return bar

    # ------------------------------------------------------------ behaviour

    def _sync_enabled(self) -> None:
        history = self.v["versioning"].get() == VERSIONING_HISTORY
        for widget in (self.retention_entry, self.min_keep_entry):
            widget.configure(state="normal" if history else "disabled")
        auto = self.auto.get()
        days = self.v["schedule_unit"].get() == UNIT_DAYS
        for widget in (self.every_label, self.every_entry):
            widget.configure(state="normal" if auto else "disabled")
        self.unit_box.configure(state="readonly" if auto else "disabled")
        for widget in (self.at_label, self.colon_label):
            widget.configure(state="normal" if auto and days else "disabled")
        for widget in (self.hour_box, self.minute_box):
            widget.configure(state="readonly" if auto and days else "disabled")
        self.logged_off_check.configure(state="normal" if auto else "disabled")
        self.logged_off_hint.set(
            t("schedule.logged_off_on" if self.v["run_logged_off"].get() else "schedule.logged_on")
        )
        projects = self.v["include_projects"].get()
        for widget in self.project_format_widgets:
            widget.configure(state="normal" if projects else "disabled")
        exports = projects and self.v["projects_format"].get() != PROJECTS_PLN
        self.include_backups_check.configure(state="normal" if exports else "disabled")
        libraries = self.v["include_libraries"].get()
        self.include_library_backups_check.configure(state="normal" if libraries else "disabled")

    def _split_run_at(self) -> None:
        """Show the saved time in the hour and minute lists, without changing it."""
        parts = _split_time(self.v["run_at"].get())
        hour, minute = parts or ("", "")
        _offer(self.minute_box, MINUTES, minute)
        self.splitting_run_at = True
        try:
            self.run_hour.set(hour)
            self.run_minute.set(minute)
        finally:
            self.splitting_run_at = False

    def _show_unit(self) -> None:
        """The unit list in the singular for 1 ("Every 1 day"), in the plural otherwise."""
        unit = self.v["schedule_unit"].get()
        index = 0 if self.v["schedule_every"].get().strip() == "1" else 1
        self.unit_box.configure(values=[unit_names(u)[index] for u in UNITS])
        self.showing_unit = True
        try:
            self.unit_label.set(unit_names(unit)[index] if unit in UNITS else unit)
        finally:
            self.showing_unit = False
        self._sync_enabled()

    def _choose_unit(self) -> None:
        """A choice in the list becomes the unit to save."""
        if self.showing_unit:
            return
        label = self.unit_label.get()
        for unit in UNITS:
            if label in unit_names(unit):
                self.v["schedule_unit"].set(unit)
                return

    def _join_run_at(self) -> None:
        """A choice in either list becomes the time to save, as HH:MM."""
        if self.splitting_run_at:
            return
        hour = self.run_hour.get() or "00"
        minute = self.run_minute.get() or "00"
        self.v["run_at"].set(f"{hour}:{minute}")

    def _place_advanced(self) -> None:
        """Advanced options inside the window when the height left over holds them.

        Otherwise they stay behind the "Advanced options..." button, in a window of their own,
        and the cards of steps 4 and 5 take the height instead.
        """
        col = self.settings_column
        spare = col.winfo_height() - col.winfo_reqheight()
        need = self.advanced_inline.winfo_reqheight() + ADVANCED_GAP
        inline = spare >= need
        self._show_advanced_inline(inline)
        if inline:
            # Grid shares the spare height in proportion to the weights: the advanced options
            # get exactly their height and the cards of steps 4 and 5 the rest, so neither
            # leaves an empty band. Weights do not change the size the column asks for.
            col.rowconfigure(3, weight=spare - need)
            col.rowconfigure(4, weight=need)

    def _show_advanced_inline(self, inline: bool, force: bool = False) -> None:
        if inline == self.advanced_shown_inline and not force:
            return
        self.advanced_shown_inline = inline
        col = self.settings_column
        col.rowconfigure(3, weight=0 if inline else 1)
        col.rowconfigure(4, weight=1 if inline else 0)
        if inline:
            self.advanced_inline.place(x=0, y=0, relwidth=1, relheight=1, height=-ADVANCED_GAP)
            self.advanced_button.pack_forget()
            if self.advanced_window is not None and self.advanced_window.winfo_exists():
                self.advanced_window.destroy()
        else:
            self.advanced_inline.place_forget()
            self.advanced_button.pack(side="left")

    def _open_advanced(self) -> None:
        """Show the advanced options in a small window: inline they did not fit a notebook.

        The fields share the main window's variables, so "Save changes" saves them.
        """
        if self.advanced_window is not None and self.advanced_window.winfo_exists():
            self.advanced_window.lift()
            return
        window = tk.Toplevel(self.root)
        window.title(t("advanced.title"))
        window.configure(background=self.colors["bg"])
        window.transient(self.root)
        window.resizable(False, False)
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        self._build_advanced(body, keep=False).pack(fill="x")
        ttk.Button(body, text=t("button.close"), command=window.destroy).pack(
            anchor="e", pady=(12, 0)
        )
        self.advanced_window = window

    def _usable_screen(self) -> tuple[int, int]:
        """Screen size minus the taskbar and the window's borders and title bar."""
        width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return width - 16, height - self.px(88)

    def _fit_to_screen(self) -> tuple[int, int]:
        """Open the window with everything in view, never beyond the screen's usable area.

        The minimum size is what the content needs, so nothing is ever hidden or scrolled;
        the extra height goes to the lists on the left.
        """
        self.root.update_idletasks()
        screen_w, screen_h = self._usable_screen()
        self._fit_minimum()
        # The layout was designed for 1040 px at 100%; narrower cuts the right-hand fields.
        width = min(max(self.root.winfo_reqwidth(), self.px(DESIGN_WIDTH)), screen_w)
        height = min(self.root.winfo_reqheight() + self.px(LIST_EXTRA_HEIGHT), screen_h)
        self.root.geometry(f"{width}x{height}")
        return width, height

    def _fit_minimum(self) -> None:
        """Keep the minimum size equal to what the content needs (a longer name, say).

        A window smaller than that grows; a maximized one is left alone.
        """
        need = (self.root.winfo_reqwidth(), self.root.winfo_reqheight())
        screen_w, screen_h = self._usable_screen()
        minimum = (min(need[0], screen_w), min(need[1], screen_h))
        if minimum == self.root.minsize():
            return
        self.root.minsize(*minimum)
        if self.root.state() != "normal":
            return
        width = max(self.root.winfo_width(), minimum[0])
        height = max(self.root.winfo_height(), minimum[1])
        if (width, height) != (self.root.winfo_width(), self.root.winfo_height()):
            self.root.geometry(f"{width}x{height}")

    def _form_state(self) -> tuple[Any, ...]:
        """Everything the settings form holds, to tell whether it differs from what is saved."""
        return (
            tuple((key, var.get()) for key, var in self.v.items()),
            self.auto.get(),
            self.startup.get(),
            self.selection,
        )

    def _remember_saved(self) -> None:
        """The form now matches the saved settings (just loaded or just saved)."""
        self.saved_state = self._form_state()
        self._set_dirty(False)

    def _update_dirty(self) -> None:
        """Changed only while the form differs from what is saved: undoing a change counts."""
        if self.saved_state is not None:
            self._set_dirty(self._form_state() != self.saved_state)

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.save_text.set(t("gui.unsaved" if dirty else "gui.all_saved"))
        if dirty:
            self.version_label.grid_remove()
            self.unsaved_banner.grid()
        else:
            self.unsaved_banner.grid_remove()
            self.version_label.grid()
        self._sync_actions()

    def _sync_actions(self) -> None:
        """Actions that use the saved settings wait until the changes are saved or discarded."""
        self.run_button.configure(state="disabled" if self.busy or self.dirty else "normal")
        if self.tray_icon is not None:
            self.tray_icon.backup_enabled = not (self.busy or self.dirty)
        self.folders_button.configure(
            state="disabled" if self.dirty or self.picker_open else "normal"
        )

    def _blocked_hint(self, action: str) -> str | None:
        """`action`: "backup" or "choose", the end of the key of each hint."""
        if not self.dirty:
            return None
        return t(f"gui.blocked.{action}")

    def _discard(self) -> None:
        """Go back to what is saved in config.toml."""
        self._load_values()
        self._refresh_status()

    def _change_language(self, language: str) -> None:
        """Save the language and show the window in it at once.

        The choice is hidden while there are unsaved changes (the banner takes its place), so
        the form always matches config.toml here and nothing is lost when it is rebuilt.
        """
        if language == i18n.language():
            return
        if self.busy or self.picker_open:
            self.language_choice.set(i18n.language())
            messagebox.showinfo(APP_TITLE, t("gui.language_busy"))
            return
        try:
            save_language(language, self.config_path)
        except (ConfigError, OSError) as e:
            self.language_choice.set(i18n.language())
            messagebox.showerror(APP_TITLE, t("gui.save_failed", error=e))
            return
        i18n.set_language(language)
        self._rebuild()

    def _rebuild(self) -> None:
        """Build the whole window again, in the current language, keeping the activity."""
        activity = self.activity.get("1.0", "end-1c")
        for child in self.root.winfo_children():
            child.destroy()
        # The "?" marks bind to the window itself; the old ones would pile up.
        for sequence in ("<ButtonPress>", "<Configure>", "<Unmap>"):
            self.root.unbind(sequence)
        self.advanced_window = None
        self._init_vars()
        self._build()
        self._load()
        self._refresh_status()
        self._refresh_auth()
        if activity:
            self._append(activity)
        self.root.update_idletasks()
        self._fit_minimum()

    def _browse(self) -> None:
        folder = filedialog.askdirectory(
            title=t("destination.dialog"), initialdir=self.v["directory"].get() or None
        )
        if folder:
            self.v["directory"].set(folder)

    def _save(self) -> Config | None:
        config = self._config()
        if config is None:
            return None
        previous = self._saved_config()
        again = not self.dirty
        try:
            save_config(config, self.config_path)
        except OSError as e:
            messagebox.showerror(APP_TITLE, t("gui.save_failed", error=e))
            return None
        try:
            if self.auto.get():
                self._install_task(config, previous, again)
            elif _safe_schedule_status() is not None:
                scheduler.remove()
        except (scheduler.SchedulerError, OSError) as e:
            messagebox.showerror(APP_TITLE, t("gui.schedule_failed", error=e))
        try:
            # Written again when on: the program may have moved since.
            if self.startup.get() or _safe_starts_with_windows():
                tray.set_start_with_windows(self.startup.get())
        except OSError as e:
            messagebox.showerror(APP_TITLE, t("gui.startup_failed", error=e))
        self._remember_saved()
        self._refresh_status()
        return config

    def _saved_config(self) -> Config | None:
        try:
            return load_config(self.config_path)
        except (ConfigError, OSError):
            return None

    def _install_task(self, config: Config, previous: Config | None, again: bool) -> None:
        """`again`: Save clicked with nothing changed, the way to hand over a new password."""
        path = self.config_path.resolve()
        if not config.run_logged_off:
            scheduler.install(config, path)
            return
        # Before anything else: on a mapped drive letter every run would fail.
        scheduler.check_destination(config.backup_dir)
        if (
            not again
            and previous is not None
            and _same_schedule(previous, config)
            and _safe_run_mode() == scheduler.MODE_ALWAYS
        ):
            # The task already runs like this; touching it would ask for the password again.
            return
        password = self._ask_windows_password()
        if password is None:
            messagebox.showinfo(APP_TITLE, t("gui.no_password"))
            return
        scheduler.install(config, path, password)

    def _ask_windows_password(self) -> str | None:
        """The Windows password, only to hand to the Task Scheduler; never stored here."""
        return simpledialog.askstring(
            APP_TITLE,
            t("gui.password_prompt", account=scheduler.windows_account()),
            show="•",
            parent=self.root,
        )

    def _login(self) -> None:
        config = self._config(for_connection_only=True)
        if config is None:
            return
        self._set_auth(t("auth.waiting"), "warn")
        self.login_button.configure(state="disabled")

        def opened(url: object) -> None:
            self.login_url = str(url)
            self.login_link.grid()

        def open_browser(url: str) -> None:
            webbrowser.open(url)
            # From the worker thread: only `_poll` touches the window.
            self.events.put(("done", (opened, url)))

        def done(result: Any) -> None:
            self.login_url = ""
            self.login_link.grid_remove()
            self.login_button.configure(state="normal")
            if isinstance(result, Exception):
                self._set_auth(t("auth.not_signed_in"), "bad")
                messagebox.showerror(APP_TITLE, t("auth.sign_in_failed", error=redact(str(result))))
            else:
                self._set_signed_in(result)

        self._background(lambda: service.sign_in(config, self.store, open_browser), done)

    def _copy_login_url(self) -> None:
        if not self.login_url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.login_url)
        self._set_auth(t("auth.url_copied"), "warn")

    def _refresh_auth(self) -> None:
        config = self._config(for_connection_only=True, quiet=True)
        if config is None or not self.store.load(config.server_url):
            self._set_auth(t("auth.not_signed_in"), "muted")
            return

        def done(result: Any) -> None:
            if isinstance(result, AuthError):
                self._set_auth(t("auth.expired"), "bad")
            elif isinstance(result, Exception):
                self._set_auth(t("auth.no_connection"), "warn")
            else:
                self._set_signed_in(result)

        self._set_auth(t("auth.checking"), "muted")
        self._background(lambda: service.signed_in_user(config, self.store), done)

    def _choose_folders(self) -> None:
        if self.dirty:
            return
        config = self._config(for_connection_only=True)
        if config is None:
            return
        if not self.store.load(config.server_url):
            messagebox.showinfo(APP_TITLE, t("picker.sign_in_first"))
            return
        session = requests.Session()
        lock = threading.Lock()
        tree: dict[str, service.FolderListing] = {}
        stop = threading.Event()

        def check() -> None:
            if stop.is_set():
                raise BimcloudError(t("picker.cancelled"))

        def load(path: str) -> service.FolderListing:
            # The whole tree comes in one listing, the first time; opening a folder after
            # that needs no call to BIMcloud.
            with lock:
                if not tree:
                    browser = service.open_folder_browser(session, config, self.store)
                    tree.update(browser.tree(check))
                if path not in tree:
                    raise BimcloudError(t("picker.folder_not_found", path=path))
                return tree[path]

        def close() -> None:
            stop.set()
            self.picker_open = False
            self._sync_actions()
            session.close()

        self.picker_open = True
        self._sync_actions()
        FolderPicker(
            self.root,
            t("picker.title"),
            self.selection,
            load,
            self._background,
            self._set_selection,
            on_close=close,
            muted=self.colors["muted"],
        )

    def _set_selection(self, selection: Selection) -> None:
        if selection != self.selection:
            self._show_selection(selection)
            self._update_dirty()

    def _show_selection(self, selection: Selection) -> None:
        self.selection = normalize_selection(
            selection.folders, selection.projects, selection.libraries
        )
        self.selection_text.set(describe_selection(self.selection, paths=True))

    def _set_auth(self, text: str, tone: str) -> None:
        self.auth_text.set(text)
        self.auth_dot.configure(foreground=self.colors[tone])

    def _set_signed_in(self, username: str) -> None:
        # Long names (e-mails) would push the status into the card title.
        if len(username) > MAX_USERNAME_CHARS:
            username = username[: MAX_USERNAME_CHARS - 1] + "…"
        self._set_auth(t("auth.signed_in", username=username), "ok")
        self.login_button.configure(text=t("connection.sign_in_again"))

    def _refresh_status(self) -> None:
        self._show_last_run(load_last_run())
        self._show_next_run()
        self._show_disk()
        self._refresh_history()

    # ---------------------------------------------------------------- history

    def _build_history_list(self, parent: ttk.Frame) -> ttk.Frame:
        """The backups kept in the destination folder: date, size and status only."""
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        head = ttk.Frame(frame)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(head, text=t("saved.title"), style="Heading.TLabel").pack(side="left")
        self.open_backup_button = ttk.Button(
            head, text=t("saved.open_folder"), command=self._open_selected_backup, state="disabled"
        )
        self.open_backup_button.pack(side="right")
        # Sized like the activity box below, so the column (and the window) never widens.
        self.history_tree = ttk.Treeview(
            frame,
            columns=("when", "size", "status"),
            show="headings",
            height=1,
            selectmode="browse",
        )
        for column in HISTORY_COLUMNS:
            self.history_tree.heading(column, text=t(f"saved.column.{column}"), anchor="w")
            self.history_tree.column(column, stretch=True, anchor="w")
        self._size_history_columns()
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.history_tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.history_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_history_detail())
        self.history_tree.bind("<Double-1>", lambda _event: self._open_selected_backup())
        self.history_detail = tk.StringVar()
        # With errors, the line becomes a link to the list of items: a button in the title
        # row would widen the column.
        self.history_detail_label = ttk.Label(
            frame,
            textvariable=self.history_detail,
            style="Muted.TLabel",
            foreground=self.colors["muted"],
            wraplength=self.px(270),
        )
        self.history_detail_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.history_detail_label.bind("<Button-1>", lambda _event: self._show_history_failures())
        self.history_entries: dict[str, history.HistoryEntry] = {}
        self.history_cache: dict[str, tuple[float, history.HistoryEntry]] = {}
        self.history_dir = ""
        return frame

    def _size_history_columns(self) -> None:
        """Each column as wide as its longest text, so dates and statuses are never cut."""
        try:
            font = tkfont.nametofont("SunValleyBodyFont", root=self.root)
        except tk.TclError:
            font = tkfont.nametofont("TkDefaultFont", root=self.root)
        for column in HISTORY_COLUMNS:
            title = t(f"saved.column.{column}")
            longest = max(font.measure(text) for text in (title, *history_longest(column)))
            width = longest + self.px(10)
            self.history_tree.column(column, width=width, minwidth=width)

    def _refresh_history(self) -> None:
        directory = self.v["directory"].get().strip()
        if directory != self.history_dir:
            self.history_dir, self.history_cache = directory, {}
        try:
            entries = history.read_history(Path(directory), self.history_cache) if directory else []
        except OSError:
            # An unreachable destination (a disconnected drive) only empties the list.
            entries = []
        selected = self.history_tree.selection()
        self.history_tree.delete(*self.history_tree.get_children())
        self.history_entries = {}
        for entry in entries:
            key = str(entry.folder)
            self.history_entries[key] = entry
            self.history_tree.insert(
                "",
                "end",
                iid=key,
                values=(_history_when(entry), _history_size(entry), _history_status(entry)),
            )
        if not entries:
            self.history_detail.set(t("saved.none" if directory else "saved.no_destination"))
        if selected and self.history_tree.exists(selected[0]):
            self.history_tree.selection_set(selected[0])
        self._show_history_detail()

    def _show_history_detail(self) -> None:
        selected = self.history_tree.selection()
        entry = self.history_entries.get(selected[0]) if selected else None
        self.open_backup_button.configure(state="normal" if entry else "disabled")
        with_errors = entry is not None and entry.errors > 0
        self.history_detail_label.configure(
            foreground=self.colors["accent" if with_errors else "muted"],
            cursor="hand2" if with_errors else "",
            font=(*self.f_small, "underline") if with_errors else self.f_small,
        )
        if entry is not None:
            detail = _history_detail(entry)
            self.history_detail.set(t("saved.see_which", text=detail) if with_errors else detail)
        elif self.history_entries:
            self.history_detail.set(t("saved.select"))

    def _show_history_failures(self) -> None:
        selected = self.history_tree.selection()
        entry = self.history_entries.get(selected[0]) if selected else None
        if entry is not None and entry.errors:
            self._show_failures(entry.folder)

    def _show_last_failures(self) -> None:
        if self.last_run_folder:
            self._show_failures(Path(self.last_run_folder))

    def _show_failures(self, folder: Path) -> None:
        """The items that failed in one backup, in plain words, in a window of their own."""
        manifest = load_manifest(folder)
        if manifest is None:
            messagebox.showinfo(APP_TITLE, t("saved.manifest_unreadable"))
            return
        fonts = (self.f_heading, self.f_text, self.f_small)
        FailuresWindow(self.root, manifest.failures, folder.name, self.colors, fonts)

    def _open_selected_backup(self) -> None:
        selected = self.history_tree.selection()
        entry = self.history_entries.get(selected[0]) if selected else None
        if entry is None:
            return
        try:
            os.startfile(entry.folder)  # type: ignore[attr-defined]  # Windows only
        except OSError:
            messagebox.showinfo(APP_TITLE, t("saved.folder_gone"))
            self._refresh_history()

    def _show_last_run(self, run: LastRun | None) -> None:
        self.last_run = run
        self._update_tray_tip()
        self.last_failures_link.grid_remove()
        self.last_run_folder = ""
        if run is None:
            self.last_icon.configure(
                text=ICON["empty"], style="BigIcon.TLabel", foreground=self.colors["muted"]
            )
            self.last_title.set(t("status.none_yet"))
            self.last_detail.set(t("status.first_backup"))
            return
        when = _friendly_time(run.finished)
        if run.status == STATUS_CANCELLED:
            self.last_icon.configure(
                text=ICON["warning"], style="BigIcon.TLabel", foreground=self.colors["warn"]
            )
            self.last_title.set(t("status.cancelled"))
            self.last_detail.set(t("status.cancelled_detail", when=when))
            return
        if run.status == STATUS_FAILED:
            self.last_icon.configure(
                text=ICON["error"], style="BigIcon.TLabel", foreground=self.colors["bad"]
            )
            self.last_title.set(t("status.failed"))
            self.last_detail.set(f"{when} · {run.message}")
            return
        size = f"{plural('status.files', run.files)} · {_format_bytes(run.bytes)}"
        if run.status == STATUS_OK:
            self.last_icon.configure(
                text=ICON["check"], style="BigIcon.TLabel", foreground=self.colors["ok"]
            )
            self.last_title.set(t("status.finished"))
            self.last_detail.set(f"{when} · {size}")
            return
        # The errors line is a link to the list of items (see `last_failures_link`).
        notes = [t("status.pending", count=run.pending)] if run.pending else []
        self.last_icon.configure(
            text=ICON["warning"], style="BigIcon.TLabel", foreground=self.colors["warn"]
        )
        self.last_title.set(t("status.with_errors" if run.errors else "status.with_warnings"))
        self.last_detail.set("\n".join([f"{when} · {size}", *notes]))
        if run.errors:
            failed = plural("status.items_with_errors", run.errors)
            self.last_run_folder = run.folder
            self.last_failures_link.configure(
                text=t("saved.see_which", text=failed) if run.folder else failed,
                cursor="hand2" if run.folder else "",
            )
            self.last_failures_link.grid()

    def _show_next_run(self) -> None:
        config = self._config(for_connection_only=True, quiet=True)
        if _safe_schedule_status() is None or config is None:
            self.next_title.set(t("status.auto_off"))
            self.next_detail.set(t("status.auto_off_hint"))
            return
        if (config.schedule_every, config.schedule_unit) == (1, UNIT_DAYS):
            now = datetime.now()
            target = datetime.combine(now.date(), config.run_at)
            if target <= now:
                target += timedelta(days=1)
            self.next_title.set(t("status.next_backup", when=_friendly_time(target)))
        else:
            self.next_title.set(t("status.auto_on"))
        when = describe_schedule(config)
        # Read from the task itself, not from the settings: that is what will happen.
        mode_key = RUN_MODE_TEXT.get(_safe_run_mode() or "")
        mode = t(mode_key) if mode_key else ""
        self.next_detail.set(f"{when}\n{mode}" if mode else when)

    def _show_disk(self) -> None:
        directory = self.v["directory"].get().strip()
        if not directory:
            self.disk_title.set(t("status.no_destination"))
            self.disk_detail.set(t("status.no_destination_hint"))
            self.disk_bar.configure(value=0)
            return
        path = Path(directory)
        probe = path if path.exists() else Path(path.anchor or ".")
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            self.disk_title.set(t("status.destination_unavailable"))
            self.disk_detail.set(directory)
            self.disk_bar.configure(value=0)
            return
        self.disk_title.set(t("status.free", size=_format_bytes(usage.free)))
        self.disk_detail.set(
            t("status.disk", total=_format_bytes(usage.total), drive=path.anchor or directory)
        )
        self.disk_bar.configure(value=100 * usage.used / usage.total if usage.total else 0)

    def _run_backup(self) -> None:
        # The backup uses the saved settings: with changes pending the button is disabled.
        if self.dirty or self.busy:
            return
        config = self._config()
        if config is None:
            return
        self.busy = True
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        self.run_button.configure(text=t("status.backing_up"))
        self._sync_actions()
        self.cancel_button.configure(state="normal", text=t("status.cancel_backup"))
        self.running.grid()
        self.history_panel.grid_remove()
        self.progress_panel.grid()
        # The progress lines keep their height: the activity box takes what is left.
        self.status_column.rowconfigure(5, weight=0)
        self._show_progress(Progress())
        self._update_tray_tip()
        self._append(t("status.backup_started", folder=config.backup_dir))
        self._append(t("status.browser_warning"))

        def work() -> Any:
            setup_logging(config.verbose_logging)
            return service.backup_now(
                config,
                self.store,
                cancel=cancel,
                progress=lambda progress: self.events.put(("progress", progress)),
            )

        self._background(work, self._backup_done)

    def _backup_done(self, result: Any) -> None:
        self.busy = False
        self.progress.stop()
        self.running.grid_remove()
        self.progress_panel.grid_remove()
        self.history_panel.grid()
        self.status_column.rowconfigure(5, weight=1)
        self.run_button.configure(text=f"{ICON['download']}   {t('status.back_up_now')}")
        self._sync_actions()
        if self.closing:
            # The window was closed during the backup: it stopped safely, now close.
            self._close_window()
            return
        self._refresh_status()
        if self.tray_icon is not None and self.tray_icon.running and not self.root.winfo_viewable():
            # The window is in the notification area: a notice there instead of a dialog.
            if isinstance(result, BackupCancelled):
                self._append(t("cli.run.cancelled"))
            self.tray_icon.balloon(APP_TITLE, _tray_result(result))
            return
        if isinstance(result, BackupCancelled):
            self._append(t("cli.run.cancelled"))
            messagebox.showinfo(APP_TITLE, t("status.cancelled_dialog"))
        elif isinstance(result, AuthError):
            self._set_auth(t("auth.expired"), "bad")
            messagebox.showerror(APP_TITLE, t("status.sign_in_again"))
        elif isinstance(result, Exception):
            messagebox.showerror(APP_TITLE, t("status.backup_failed", error=redact(str(result))))

    def _show_progress(self, progress: Progress) -> None:
        bar = self.progress
        if progress.listing:
            if str(bar.cget("mode")) != "indeterminate":
                bar.configure(mode="indeterminate")
            bar.start(12)
            self.progress_title.set(t("progress.listing"))
            self.progress_current.set("")
            self.progress_files.set("")
            return
        bar.stop()
        bar.configure(mode="determinate", maximum=100)
        title, current, files = _progress_texts(progress)
        self.progress_title.set(title)
        self.progress_current.set(current)
        self.progress_files.set(files)
        if progress.exports_total:
            bar.configure(value=100 * progress.exports_done / progress.exports_total)
        elif progress.files_total:
            bar.configure(value=100 * progress.files_done / progress.files_total)
        else:
            bar.configure(value=100)

    def _cancel_backup(self) -> None:
        if not self.busy or self.cancel_event.is_set():
            return
        if not messagebox.askyesno(APP_TITLE, t("status.cancel_question")):
            return
        self.request_cancel()

    def request_cancel(self) -> None:
        """Ask the running backup to stop at its next check."""
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled", text=t("status.cancelling"))
        self._append(t("status.cancelling_detail"))

    def _open_logs(self) -> None:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(directory)  # type: ignore[attr-defined]  # Windows only

    def _periodic_refresh(self) -> None:
        if not self.busy:
            self._refresh_status()
        self.root.after(REFRESH_MS, self._periodic_refresh)

    def _on_close(self) -> None:
        """The window's X: with the icon in the notification area, the program stays there."""
        if self.tray_icon is not None and self.tray_icon.running:
            self._hide_to_tray()
            return
        self._quit()

    def _hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self.tray_notice_shown and self.tray_icon is not None:
            self.tray_notice_shown = True
            self.tray_icon.balloon(APP_TITLE, t("tray.still_open"))

    def _show_window(self) -> None:
        root = self.root
        root.deiconify()
        if root.state() == "iconic":
            root.state("normal")
        root.lift()
        # Briefly on top: brought from the notification area, it must not open behind others.
        root.attributes("-topmost", True)
        root.after_idle(root.attributes, "-topmost", False)
        root.focus_force()

    def _tray_action(self, action: str) -> None:
        if action == tray.OPEN:
            self._show_window()
        elif action == tray.BACKUP:
            self._show_window()
            self._run_backup()
        elif action == tray.EXIT:
            if self.dirty or self.busy:
                # The questions about the changes and the running backup need the window.
                self._show_window()
            self._quit()

    def _update_tray_tip(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.set_tip(_tray_tip(self.busy, self.last_run))

    def _quit(self) -> None:
        """Leave the program, with the same questions as closing the window always had."""
        if self.closing:
            return
        # The pending changes first: once a running backup starts to stop, the window closes
        # by itself and there would be no moment left to ask.
        if self.dirty and not self._settle_changes():
            return
        if self.busy:
            if not messagebox.askyesno(APP_TITLE, t("status.close_question")):
                return
            # Same safe stop as the Cancel button; the window closes when the backup ends.
            self.closing = True
            if not self.cancel_event.is_set():
                self.request_cancel()
            self._append(t("status.closing"))
            return
        self._close_window()

    def _settle_changes(self) -> bool:
        """Save or discard the pending changes as the user chooses; False keeps the window."""
        choice = self._ask_unsaved()
        if choice == "save":
            return self._save() is not None
        if choice == "discard":
            self._discard()
            return True
        return False

    def _ask_unsaved(self) -> str | None:
        """Ask what to do with the pending changes: "save", "discard" or None (keep open)."""
        answer: list[str | None] = [None]
        dialog = tk.Toplevel(self.root)
        dialog.title(APP_TITLE)
        dialog.configure(background=self.colors["bg"])
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=t("gui.unsaved_question"), justify="left").pack(
            anchor="w", pady=(0, 16)
        )
        buttons = ttk.Frame(body)
        buttons.pack(anchor="e")

        def choose(value: str | None) -> None:
            answer[0] = value
            dialog.destroy()

        ttk.Button(
            buttons, text=t("button.save"), style="Accent.TButton", command=lambda: choose("save")
        ).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text=t("button.discard"), command=lambda: choose("discard")).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text=t("button.cancel"), command=lambda: choose(None)).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        dialog.bind("<Escape>", lambda _event: choose(None))
        if self.root.winfo_viewable():
            # Modal: the main window waits for the answer.
            dialog.wait_visibility()
            dialog.grab_set()
        self.root.wait_window(dialog)
        return answer[0]

    def _close_window(self) -> None:
        get_logger().removeHandler(self.log_handler)
        if self.tray_icon is not None:
            self.tray_icon.stop()
        self.root.destroy()

    # --------------------------------------------------------------- plumbing

    def _background(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        def target() -> None:
            try:
                result = work()
            except (BimcloudError, OSError, ConfigError) as e:
                result = e
            except Exception as e:  # noqa: BLE001 - shown to the user instead of lost
                get_logger().exception(t("gui.unexpected_error"))
                result = e
            self.events.put(("done", (done, result)))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "progress":
                    if self.busy:
                        self._show_progress(payload)
                elif kind == "tray":
                    self._tray_action(payload)
                else:
                    callback, result = payload
                    callback(result)
        except queue.Empty:
            pass
        # Texts that changed since (a user name, the folder list) may need a larger minimum.
        self._fit_minimum()
        self.root.after(POLL_MS, self._poll)

    def _append(self, line: str) -> None:
        self.activity.configure(state="normal")
        self.activity.insert("end", line + "\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")


# ------------------------------------------------------------------- helpers


# Long project names would wrap the progress over more lines than the panel has.
CURRENT_NAME_LIMIT = 33
CURRENT_LINE_LIMIT = 40


def _progress_texts(progress: Progress) -> tuple[str, str, str]:
    """The three lines of the progress panel: what is counted, what is exported now, files."""
    files = ""
    if progress.files_total:
        files = t(
            "backup.files_progress",
            done=format_count(progress.files_done),
            total=format_count(progress.files_total),
            bytes_done=_format_bytes(progress.bytes_done),
            bytes_total=_format_bytes(progress.bytes_total),
        )
    if progress.exports_total:
        title = t("progress.exports", done=progress.exports_done, total=progress.exports_total)
    elif progress.files_total:
        title, files = files, ""
    else:
        title = t("progress.nothing")
    current = ""
    if progress.current:
        name = progress.current.rsplit("/", 1)[-1]
        if len(name) > CURRENT_NAME_LIMIT:
            name = name[: CURRENT_NAME_LIMIT - 1] + "…"
        current = t("progress.now", name=name)
        if progress.detail:
            # Name and job status on one line: the panel has no room for another.
            room = CURRENT_LINE_LIMIT - len(progress.detail) - 3
            name = progress.current.rsplit("/", 1)[-1]
            if len(name) > room:
                name = name[: max(room - 1, 1)] + "…"
            current = f"{name} · {progress.detail}"
    return title, current, files


def _number(value: Any, kind: type, label: str) -> Any:
    text = str(value).strip().replace(",", ".")
    try:
        return kind(float(text)) if kind is int and float(text).is_integer() else kind(text)
    except ValueError as e:
        raise ConfigError(t("gui.invalid_value", label=label, value=value)) from e


def _number_entry(parent: tk.Widget, variable: tk.Variable) -> ttk.Entry:
    return ttk.Entry(parent, textvariable=variable, width=5, justify="center")


def _choice_box(parent: tk.Widget, variable: tk.Variable, values: tuple[str, ...]) -> ttk.Combobox:
    box = ttk.Combobox(
        parent,
        textvariable=variable,
        values=values,
        width=max(len(v) for v in values) + 1,
        justify="center",
        state="readonly",
    )
    # The chosen text stays selected (highlighted) otherwise.
    box.bind("<<ComboboxSelected>>", lambda _e: box.selection_clear())
    # Tk changes the value with the mouse wheel: scrolling over it would change the time.
    box.bind("<MouseWheel>", lambda _e: "break")
    return box


def _offer(box: ttk.Combobox, choices: tuple[str, ...], value: str) -> None:
    """The list holds the choices plus the current value, in order, when it is not one of them."""
    values = list(choices)
    if value.isdigit() and value not in values:
        values = sorted([*values, value], key=int)
    box.configure(values=values)


def _split_time(text: str) -> tuple[str, str] | None:
    try:
        at = datetime.strptime(text.strip(), "%H:%M")
    except ValueError:
        return None
    return f"{at.hour:02d}", f"{at.minute:02d}"


def _history_when(entry: history.HistoryEntry) -> str:
    # No year: it did not fit the column. The detail line below the list shows the full date.
    return format_date(entry.when, "format.short_datetime")


def _history_size(entry: history.HistoryEntry) -> str:
    return "—" if entry.size is None else _format_bytes(entry.size)


# The status of a backup in the list, by the end of its key ("history.status.<status>").
HISTORY_STATUSES = {
    history.STATUS_OK: "ok",
    history.STATUS_WARNINGS: "warnings",
    history.STATUS_INCOMPLETE: "incomplete",
    history.STATUS_NO_DETAILS: "no_details",
}


def _history_status(entry: history.HistoryEntry) -> str:
    status = HISTORY_STATUSES.get(entry.status, "no_details")
    return t(f"history.status.{status}", count=entry.errors)


def _history_detail(entry: history.HistoryEntry) -> str:
    """The full date, then counts only: names of projects or files are never shown here."""
    when = format_date(entry.when, "format.datetime")
    if entry.status == history.STATUS_INCOMPLETE:
        return f"{when} · {t('saved.incomplete')}"
    if entry.status == history.STATUS_NO_DETAILS:
        return f"{when} · {t('saved.no_details')}"
    parts = [
        plural("saved.files", count)
        if label == history.FILES
        else f"{format_count(count)}\u00a0{label}"
        for label, count in entry.counts
    ]
    text = " · ".join(parts) if parts else t("saved.no_files")
    if entry.errors:
        text += f" · {plural('saved.items_with_errors', entry.errors)}"
    return f"{when} · {text}"


def _format_bytes(size: float) -> str:
    """Size in the chosen language ("18.6 GB" or "18,6 GB")."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            number = f"{size:.0f}" if unit == "B" else format_decimal(size)
            return f"{number} {unit}"
        size /= 1024
    return f"{format_decimal(size)} TB"


def _friendly_time(moment: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    days = (moment.date() - now.date()).days
    names = {0: "time.today", -1: "time.yesterday", 1: "time.tomorrow"}
    day = t(names[days]) if days in names else format_date(moment, "format.day")
    return t("time.at", day=day, time=f"{moment:%H:%M}")


def _started(icon: tray.TrayIcon | None, post: Callable[[str], None]) -> tray.TrayIcon | None:
    """The icon once its window is up (shown or not, see `running`); None if it never was."""
    return icon if icon is not None and icon.start(post) else None


def _tray_tip(busy: bool, run: LastRun | None) -> str:
    """The icon's tooltip: how the last backup went, or that one is running now."""
    if busy:
        state = t("tray.tip.running")
    elif run is None:
        state = t("tray.tip.none")
    else:
        if run.status == STATUS_OK:
            state = t("tray.tip.ok")
        elif run.status == STATUS_FAILED:
            state = t("tray.tip.failed")
        elif run.status == STATUS_CANCELLED:
            state = t("tray.tip.cancelled")
        else:
            state = t("tray.tip.errors" if run.errors else "tray.tip.warnings")
        state += f", {_friendly_time(run.finished)}"
    return f"{APP_TITLE}\n{state}"


def _tray_result(result: Any) -> str:
    """The notice next to the icon when a backup started in the window ends with it hidden."""
    if isinstance(result, BackupCancelled):
        return t("cli.run.cancelled")
    if isinstance(result, AuthError):
        return t("status.sign_in_again")
    if isinstance(result, Exception):
        return t("tray.result.failed")
    return t("tray.result.ok")


def _safe_starts_with_windows() -> bool:
    try:
        return tray.starts_with_windows()
    except (OSError, ImportError):
        return False


def _safe_schedule_status() -> str | None:
    try:
        return scheduler.status()
    except OSError:
        return None


def _safe_run_mode() -> str | None:
    try:
        return scheduler.run_mode()
    except OSError:
        return None


# Keys of the texts, by how the task logs on.
RUN_MODE_TEXT = {
    scheduler.MODE_ALWAYS: "status.mode_always",
    scheduler.MODE_LOGGED_ON: "status.mode_logged_on",
}


def _same_schedule(old: Config, new: Config) -> bool:
    """Whether the task would start at the same times, which is what the task stores."""
    return _task_times(old) == _task_times(new)


def _task_times(config: Config) -> tuple[Any, ...]:
    # The time only counts with days: minutes and hours count from when the task is saved.
    at = config.run_at if config.schedule_unit == UNIT_DAYS else None
    return config.schedule_every, config.schedule_unit, at


def describe_schedule(config: Config) -> str:
    """For example "Every day at 23:00", "Every 2 days at 23:00" or "Every 30 minutes"."""
    every, unit = config.schedule_every, config.schedule_unit
    if unit == UNIT_DAYS:
        start = t("schedule.every_day") if every == 1 else t("schedule.every_n_days", count=every)
        return t("schedule.days_at", start=start, time=f"{config.run_at:%H:%M}")
    singular, plural_name = unit_names(unit)
    if every == 1:
        return t("schedule.every_one", unit=singular)
    return t("schedule.every_n", count=every, unit=plural_name)


def _file_schedule(values: dict[str, Any]) -> tuple[str, str]:
    """Number and unit of the [schedule] of config.toml, old format included, for the form.

    Invalid values still show up, as in the other fields, and saving points them out.
    """
    try:
        every, unit = schedule_from(values)
    except ConfigError:
        unit = values.get("unit")
        if unit not in UNITS:
            unit = UNIT_MINUTES if "interval_minutes" in values else UNIT_DAYS
        return str(values.get("every", values.get("interval_minutes", ""))), unit
    return str(every), unit


class LanguageDialog:
    """The first window of a new installation: English or Brazilian Portuguese.

    Its texts are in both languages, since none was chosen yet. English starts selected.
    """

    def __init__(self, root: tk.Misc):
        self.choice: str | None = None
        self.window = window = tk.Toplevel(root)
        window.title(APP_TITLE)
        window.resizable(False, False)
        # Wide enough for the whole title.
        window.minsize(round(340 * _scale(window)), 0)
        with contextlib.suppress(tk.TclError):
            self.icons = [
                tk.PhotoImage(master=window, file=(ASSETS / f"icon-{size}.png").as_posix())
                for size in WINDOW_ICON_SIZES
            ]
            window.iconphoto(False, *self.icons)
        body = ttk.Frame(window, padding=24)
        body.pack(fill="both", expand=True)
        heading = tkfont.nametofont("TkDefaultFont").copy()
        heading.configure(size=12, weight="bold")
        self.heading_font = heading
        ttk.Label(body, text="Choose your language", font=heading).pack(anchor="w")
        ttk.Label(body, text="Escolha o idioma").pack(anchor="w", pady=(2, 14))
        self.language = tk.StringVar(master=window, value=i18n.DEFAULT_LANGUAGE)
        for code, name in LANGUAGES.items():
            ttk.Radiobutton(
                body,
                text=name,
                value=code,
                variable=self.language,
                command=self._show_choice,
            ).pack(anchor="w", pady=2)
        self.button = ttk.Button(body, command=self.confirm, default="active")
        self.button.pack(anchor="e", pady=(18, 0))
        self._show_choice()
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Return>", lambda _event: self.confirm())
        window.bind("<Escape>", lambda _event: self.close())
        _center(window)
        window.lift()
        window.focus_force()
        self.button.focus_set()

    def _show_choice(self) -> None:
        """The button speaks the language that is selected."""
        self.button.configure(text=DIALOG_CONTINUE[self.language.get()])

    def confirm(self) -> None:
        self.choice = self.language.get()
        self.window.destroy()

    def close(self) -> None:
        self.window.destroy()


DIALOG_CONTINUE = {i18n.ENGLISH: "Continue", i18n.PORTUGUESE: "Continuar"}


def ask_language(root: tk.Misc) -> str | None:
    """The language chosen in the first window; None when it was closed without a choice."""
    dialog = LanguageDialog(root)
    root.wait_window(dialog.window)
    return dialog.choice


def _scale(widget: tk.Misc) -> float:
    """The Windows scale (1.5 at 150%), as the main window computes it."""
    return max(1.0, float(widget.tk.call("tk", "scaling")) * 72 / 96)


def _center(window: tk.Toplevel) -> None:
    window.update_idletasks()
    x = (window.winfo_screenwidth() - window.winfo_reqwidth()) // 2
    y = (window.winfo_screenheight() - window.winfo_reqheight()) // 3
    window.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def _enable_dpi_awareness() -> None:
    """Render crisp text on scaled displays instead of letting Windows blur the window."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def run_gui(config_path: Path, store: TokenStore, tray_only: bool = False) -> int:
    """Open the window, or bring back the one already open (one per Windows session)."""
    if not tray.claim_single_instance():
        # Already open: bring that window back (starting with Windows, nothing to do). Never a
        # second copy, even if the first cannot be reached: without its icon, its window is
        # never hidden, so it is already on the screen.
        if not tray_only:
            tray.show_existing()
        return 0
    _enable_dpi_awareness()
    root = tk.Tk()
    # Hidden while it is built; then shown, unless it starts with only the icon.
    root.withdraw()
    language = saved_language(config_path)
    if language is None and not tray_only:
        # The first thing a new user sees: the language, in a window of its own.
        # Closed without a choice: English this time, and the question comes back next time.
        language = ask_language(root)
        if language is not None:
            try:
                save_language(language, config_path)
            except (ConfigError, OSError):
                # Asked again next time; the window still opens in the chosen language.
                get_logger().warning("Could not save the language", exc_info=True)
    i18n.set_language(language)
    app = App(root, config_path, store, tray.TrayIcon(APP_TITLE))
    if not (tray_only and app.tray_icon is not None and app.tray_icon.running):
        root.deiconify()
    root.mainloop()
    return 0


def _file_selection(bimcloud: dict[str, Any]) -> Selection:
    """What the file's [bimcloud] section chooses, with the same rules as the config loader."""
    try:
        return selection_from(bimcloud)
    except ConfigError:
        return Selection()
