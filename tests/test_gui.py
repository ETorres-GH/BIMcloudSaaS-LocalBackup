import logging
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import replace
from datetime import time as clock
from pathlib import Path
from tkinter import ttk

import pytest

from bimcloud_backup import gui
from bimcloud_backup.backup import BackupCancelled, BackupResult, Progress
from bimcloud_backup.config import Config, Selection, load_config, parse_config, save_config, to_raw
from bimcloud_backup.errors import BimcloudError
from bimcloud_backup.folder_picker import EVERYTHING, ITEM, FolderPicker
from bimcloud_backup.gui import (
    BROWSER_WARNING,
    FILE_ONLY_OPTIONS,
    HELP,
    WINDOW_ICON_SIZES,
    App,
    QueueLogHandler,
    _file_selection,
    _format_bytes,
    _number,
    _progress_texts,
    _same_schedule,
    describe_schedule,
)
from bimcloud_backup.service import FolderListing
from bimcloud_backup.state import STATUS_CANCELLED, LastRun
from tests.conftest import MemoryTokenStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    # A single window for the whole module: the real program only ever creates one,
    # and creating a second Tk interpreter in the same process makes the theme's Tcl
    # code fail intermittently.
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"sem ambiente gráfico: {e}")
    root.withdraw()
    yield App(root, ROOT / "config.example.toml", MemoryTokenStore())
    root.destroy()


def test_form_round_trips_the_configuration(app):
    assert app._config() == load_config(ROOT / "config.example.toml")


def test_the_spare_height_always_goes_somewhere(app):
    """Right after start-up too: the cards of steps 4 and 5, or the advanced options."""
    col = app.settings_column
    inline = app.advanced_shown_inline
    assert int(col.rowconfigure(3)["weight"]) == (0 if inline else 1)
    assert int(col.rowconfigure(4)["weight"]) == (1 if inline else 0)


def test_versioning_toggles_retention_fields(app):
    app.v["versioning"].set("latest")
    app._sync_enabled()
    try:
        assert str(app.retention_entry.cget("state")) == "disabled"
    finally:
        app.v["versioning"].set("history")
        app._sync_enabled()


def test_project_options_follow_the_projects_switch(app):
    try:
        app.v["projects_format"].set("pln")
        app._sync_enabled()
        assert str(app.include_backups_check.cget("state")) == "disabled"
        app.v["include_projects"].set(False)
        app._sync_enabled()
        assert all(str(w.cget("state")) == "disabled" for w in app.project_format_widgets)
    finally:
        app.v["projects_format"].set("bimproject")
        app.v["include_projects"].set(True)
        app._sync_enabled()
    assert str(app.include_backups_check.cget("state")) == "normal"


@pytest.mark.parametrize(
    ("value", "kind", "expected"), [("30", int, 30), ("1,5", float, 1.5), (" 4 ", float, 4.0)]
)
def test_number_parsing(value, kind, expected):
    assert _number(value, kind, "x") == expected


def test_activity_panel_never_shows_tokens():
    events = queue.Queue()
    handler = QueueLogHandler(events)
    record = logging.LogRecord(
        "bimcloud_backup", logging.ERROR, __file__, 1, "Falha: %s", ("x?access_token=T0K3N",), None
    )

    handler.emit(record)

    kind, text = events.get_nowait()
    assert kind == "log"
    assert "T0K3N" not in text and "access_token=<REMOVIDO>" in text


LISTINGS = {
    "": FolderListing([("Obras", "Obras"), ("Pastas Exemplo", "Pastas Exemplo")], 0, 0),
    "Pastas Exemplo": FolderListing(
        [("Projeto Teste", "Pastas Exemplo/Projeto Teste")],
        2,
        1,
        [
            ("BibliotecaB", "Pastas Exemplo/BibliotecaB", "library"),
            ("ProjetoA", "Pastas Exemplo/ProjetoA", "project"),
            ("ProjetoC", "Pastas Exemplo/ProjetoC", "project"),
        ],
    ),
    "Pastas Exemplo/Projeto Teste": FolderListing(
        [], 1, 0, [("ProjetoD", "Pastas Exemplo/Projeto Teste/ProjetoD", "project")]
    ),
}


def run_now(work, done):
    try:
        result = work()
    except Exception as e:  # noqa: BLE001 - same contract as App._background
        result = e
    done(result)


def make_picker(app, selected=None, chosen=None, loaded=None):
    def load(path):
        if loaded is not None:
            loaded.append(path)
        return LISTINGS[path]

    return FolderPicker(
        app.root,
        "Pastas",
        selected or Selection(),
        load,
        run_now,
        (chosen if chosen is not None else []).append,
    )


def test_picker_loads_folders_on_demand_and_returns_the_marked_ones(app):
    chosen, loaded = [], []
    picker = make_picker(app, chosen=chosen, loaded=loaded)
    tree = picker.tree

    assert loaded == [""]
    assert tree.get_children("") == ("Obras", "Pastas Exemplo")
    assert tree.item("Obras", "text") == "☐  Obras"
    assert picker.summary.get() == EVERYTHING

    tree.focus("Pastas Exemplo")
    picker._on_open()
    assert loaded == ["", "Pastas Exemplo"]
    assert tree.set("Pastas Exemplo", "content") == "2 projetos · 1 biblioteca"
    # Subfolders first, then the projects and libraries of the same listing.
    assert tree.get_children("Pastas Exemplo") == (
        "Pastas Exemplo/Projeto Teste",
        ITEM + "Pastas Exemplo/BibliotecaB",
        ITEM + "Pastas Exemplo/ProjetoA",
        ITEM + "Pastas Exemplo/ProjetoC",
    )
    assert tree.item(ITEM + "Pastas Exemplo/ProjetoA", "text") == "☐  ProjetoA"
    assert tree.set(ITEM + "Pastas Exemplo/ProjetoA", "content") == "projeto"
    assert tree.set(ITEM + "Pastas Exemplo/BibliotecaB", "content") == "biblioteca"

    picker.toggle("Pastas Exemplo/Projeto Teste")
    picker.toggle("Obras")
    assert tree.item("Obras", "text") == "☑  Obras"
    picker.confirm()

    assert chosen == [Selection(("Obras", "Pastas Exemplo/Projeto Teste"))]
    assert not picker.window.winfo_exists()


def test_single_projects_and_libraries_can_be_marked(app):
    chosen = []
    picker = make_picker(app, chosen=chosen)
    picker.tree.focus("Pastas Exemplo")
    picker._on_open()

    picker.toggle(ITEM + "Pastas Exemplo/ProjetoA")
    picker.toggle(ITEM + "Pastas Exemplo/BibliotecaB")
    assert picker.tree.item(ITEM + "Pastas Exemplo/ProjetoA", "text") == "☑  ProjetoA"
    # The folder itself is not chosen, but something inside it is.
    assert picker.tree.item("Pastas Exemplo", "text") == "☐  Pastas Exemplo"
    assert "partial" in picker.tree.item("Pastas Exemplo", "tags")
    assert picker.summary.get() == "Marcados: 1 projeto, 1 biblioteca"
    picker.toggle(ITEM + "Pastas Exemplo/ProjetoA")
    picker.toggle(ITEM + "Pastas Exemplo/ProjetoC")
    picker.confirm()

    assert chosen == [Selection((), ("Pastas Exemplo/ProjetoC",), ("Pastas Exemplo/BibliotecaB",))]


def test_marking_a_folder_takes_in_the_items_marked_inside_it(app):
    chosen = []
    selected = Selection(projects=("Pastas Exemplo/ProjetoA", "Obras/ProjetoE"))
    picker = make_picker(app, selected=selected, chosen=chosen)
    picker.tree.focus("Pastas Exemplo")
    picker._on_open()
    item = ITEM + "Pastas Exemplo/ProjetoA"

    picker.toggle("Pastas Exemplo")
    assert picker.tree.item(item, "text") == "☑  ProjetoA"
    assert "inherited" in picker.tree.item(item, "tags")
    picker.toggle(item)  # already included by its folder: nothing changes
    picker.confirm()

    assert chosen == [Selection(("Pastas Exemplo",), ("Obras/ProjetoE",))]


FULL_TREE = {
    "": FolderListing(
        [("Obras", "Obras"), ("Pastas Exemplo", "Pastas Exemplo")],
        3,
        1,
        [],
        {"Obras": (0, 0), "Pastas Exemplo": (3, 1)},
    ),
    "Obras": FolderListing([], 0, 0),
    "Pastas Exemplo": FolderListing(
        [("Projeto Teste", "Pastas Exemplo/Projeto Teste")],
        3,
        1,
        LISTINGS["Pastas Exemplo"].items,
        {"Pastas Exemplo/Projeto Teste": (1, 0)},
    ),
    "Pastas Exemplo/Projeto Teste": LISTINGS["Pastas Exemplo/Projeto Teste"],
}


def test_every_folder_shows_its_counts_before_it_is_opened(app):
    picker = FolderPicker(app.root, "Pastas", Selection(), FULL_TREE.__getitem__, run_now, print)
    try:
        assert picker.tree.set("Pastas Exemplo", "content") == "3 projetos · 1 biblioteca"
        assert picker.tree.set("Obras", "content") == "nenhum"
        picker.tree.focus("Pastas Exemplo")
        picker._on_open()
        assert picker.tree.set("Pastas Exemplo/Projeto Teste", "content") == "1 projeto"
    finally:
        picker.cancel()


def test_the_picker_lists_bimcloud_once_and_opens_folders_from_memory(app, monkeypatch):
    listings = []

    class Browser:
        def tree(self, check):
            check()
            listings.append(1)
            return FULL_TREE

    pickers = []
    real_picker = gui.FolderPicker
    monkeypatch.setattr(gui, "FolderPicker", lambda *a, **k: pickers.append(real_picker(*a, **k)))
    monkeypatch.setattr(gui.service, "open_folder_browser", lambda *a: Browser())
    monkeypatch.setattr(app, "_background", run_now)
    monkeypatch.setattr(app.store, "load", lambda url: "token")
    app._choose_folders()
    picker = pickers[0]
    try:
        for folder in ("Pastas Exemplo", "Pastas Exemplo/Projeto Teste", "Obras"):
            picker.tree.focus(folder)
            picker._on_open()
        assert listings == [1]
        assert picker.tree.get_children("Pastas Exemplo/Projeto Teste")
    finally:
        picker.cancel()
    assert not app.picker_open


def test_closing_the_picker_stops_the_listing(app, monkeypatch):
    seen = []

    class Browser:
        def tree(self, check):
            pickers[0].cancel()  # the user closes the window while BIMcloud answers
            try:
                check()
            except BimcloudError as e:
                seen.append(str(e))
                raise
            return FULL_TREE

    pickers = []
    real_picker = gui.FolderPicker
    monkeypatch.setattr(gui, "FolderPicker", lambda *a, **k: pickers.append(real_picker(*a, **k)))
    monkeypatch.setattr(gui.service, "open_folder_browser", lambda *a: Browser())
    monkeypatch.setattr(app.store, "load", lambda url: "token")
    later = []
    monkeypatch.setattr(app, "_background", lambda work, done: later.append((work, done)))
    app._choose_folders()
    work, done = later[0]
    run_now(work, done)

    assert seen == ["Carregamento cancelado"]
    assert not pickers[0].window.winfo_exists()


def test_marking_a_folder_includes_its_subfolders(app):
    chosen = []
    picker = make_picker(app, selected=Selection(("Pastas Exemplo/Projeto Teste",)), chosen=chosen)
    picker.tree.focus("Pastas Exemplo")
    picker._on_open()
    child = "Pastas Exemplo/Projeto Teste"
    assert picker.tree.item(child, "text").startswith("☑")

    picker.toggle("Pastas Exemplo")
    assert "inherited" in picker.tree.item(child, "tags")
    picker.toggle(child)  # already included by its parent: nothing changes
    picker.confirm()

    assert chosen == [Selection(("Pastas Exemplo",))]


def test_cancel_keeps_the_previous_choice(app):
    chosen = []
    picker = make_picker(app, selected=Selection(("Obras",)), chosen=chosen)
    picker.clear()
    picker.cancel()
    assert chosen == []


def test_main_window_shows_a_summary_of_the_selection(app):
    try:
        app._set_selection(
            Selection(
                ("Pastas Exemplo/Projeto Teste", "Obras", "Obras/Sub"),
                ("Outras/ProjetoA", "Outras/ProjetoC", "Obras/ProjetoD"),
                ("Outras/BibliotecaB",),
            )
        )
        assert app.selection == Selection(
            ("Obras", "Pastas Exemplo/Projeto Teste"),
            ("Outras/ProjetoA", "Outras/ProjetoC"),
            ("Outras/BibliotecaB",),
        )
        assert app.selection_text.get() == "2 pastas, 2 projetos, 1 biblioteca"
        config = app._config()
        assert config.source_folders == ("Obras", "Pastas Exemplo/Projeto Teste")
        assert config.source_projects == ("Outras/ProjetoA", "Outras/ProjetoC")
        assert config.source_libraries == ("Outras/BibliotecaB",)
        # A few short paths are shown themselves; long ones would widen the window.
        app._set_selection(Selection(("Obras",), ("Outras/ProjetoA",)))
        assert app.selection_text.get() == "Obras, Outras/ProjetoA"
        app._set_selection(Selection((), (f"Outras/{'ProjetoComNomeLongo' * 3}",)))
        assert app.selection_text.get() == "1 projeto"
    finally:
        app._set_selection(Selection())
    assert app.selection_text.get() == EVERYTHING


@pytest.mark.parametrize(
    ("bimcloud", "expected"),
    [
        ({"source_folder": " Antiga/ "}, ("Antiga",)),
        ({"source_folders": [], "source_folder": "Antiga"}, ()),
        ({"source_folders": ["B", "A"]}, ("A", "B")),
        ({}, ()),
        ({"source_folders": "errado"}, ()),
    ],
)
def test_window_reads_the_folders_like_the_config_loader(bimcloud, expected):
    assert _file_selection(bimcloud).folders == expected
    if bimcloud.get("source_folders") != "errado":
        raw = {
            "bimcloud": {"server_url": "https://x.bimcloud.com", **bimcloud},
            "backup": {"directory": "D:/b"},
        }
        assert parse_config(raw).source_folders == expected


@pytest.mark.parametrize(
    ("bimcloud_toml", "expected"),
    [
        ('source_folder = "Antiga"', ("Antiga",)),
        ('source_folders = []\nsource_folder = "Antiga"', ()),
    ],
)
def test_window_and_loader_agree_on_the_same_file(app, tmp_path, bimcloud_toml, expected):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[bimcloud]\nserver_url = "https://x.bimcloud.com"\n{bimcloud_toml}\n'
        '[backup]\ndirectory = "D:/b"\n',
        encoding="utf-8",
    )
    example = app.config_path
    try:
        app.config_path = path
        app._load()
        assert app.selection.folders == expected == load_config(path).source_folders
        assert app._config().source_folders == expected
    finally:
        app.config_path = example
        app._load()


def test_saving_keeps_options_that_only_exist_in_the_file(app, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[bimcloud]\nserver_url = "https://x.bimcloud.com"\n'
        '[backup]\ndirectory = "D:/b"\nparallel_downloads = 5\n',
        encoding="utf-8",
    )
    example = app.config_path
    try:
        app.config_path = path
        app._load()
        assert app._config().parallel_downloads == 5
        save_config(app._config(), path)  # what "Salvar alterações" writes
        assert load_config(path).parallel_downloads == 5
    finally:
        app.config_path = example
        app._load()


def test_every_option_is_in_the_form_or_kept_from_the_file(app):
    """A new option with no control in the window would otherwise go back to its default."""
    saved = to_raw(app._config())
    form = app._form_raw()
    for section, values in saved.items():
        assert set(values) <= set(form[section]), section
    assert all(key in saved[section] for section, key in FILE_ONLY_OPTIONS)


def test_a_folder_that_failed_to_load_can_be_opened_again(app, monkeypatch):
    monkeypatch.setattr("bimcloud_backup.folder_picker.messagebox.showerror", lambda *a, **k: None)
    attempts = []

    def load(path):
        attempts.append(path)
        if path == "Pastas Exemplo" and attempts.count(path) == 1:
            raise BimcloudError("sem conexão")
        return LISTINGS[path]

    picker = FolderPicker(app.root, "Pastas", Selection(), load, run_now, lambda f: None)
    tree = picker.tree
    tree.item("Pastas Exemplo", open=True)
    tree.focus("Pastas Exemplo")
    picker._on_open()
    assert tree.item("Pastas Exemplo", "open") in (0, False)
    assert tree.get_children("Pastas Exemplo")[0].endswith("carregando")

    tree.focus("Pastas Exemplo")
    picker._on_open()

    assert attempts == ["", "Pastas Exemplo", "Pastas Exemplo"]
    assert tree.get_children("Pastas Exemplo")[0] == "Pastas Exemplo/Projeto Teste"
    picker.cancel()


def test_cancel_button_only_while_a_backup_runs_and_asks_first(app, monkeypatch):
    assert not app.running.winfo_ismapped()
    app.cancel_event = threading.Event()
    app.busy = True
    try:
        monkeypatch.setattr("bimcloud_backup.gui.messagebox.askyesno", lambda *a, **k: False)
        app._cancel_backup()
        assert not app.cancel_event.is_set()

        monkeypatch.setattr("bimcloud_backup.gui.messagebox.askyesno", lambda *a, **k: True)
        app._cancel_backup()
        assert app.cancel_event.is_set()
        assert str(app.cancel_button.cget("state")) == "disabled"
        assert app.cancel_button.cget("text") == "Cancelando..."
    finally:
        app.busy = False


LONG_NAME = "Projeto com um nome muito comprido para caber numa linha só da janela"


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (
            Progress(False, 10, 22, f"Obras/{LONG_NAME}", 120, 1284, 2 * 1024**3, 20 * 1024**3),
            (
                "Projetos e bibliotecas: 10 de 22",
                "Agora: Projeto com um nome muito compri…",
                "Arquivos: 120 de 1.284 (2,0\u00a0GB de 20,0\u00a0GB)",
            ),
        ),
        (
            Progress(False, 0, 0, None, 3, 10, 0, 5 * 1024**2),
            ("Arquivos: 3 de 10 (0\u00a0B de 5,0\u00a0MB)", "", ""),
        ),
        (
            Progress(False, 1, 3, "Bibliotecas/BibliotecaB", detail="na fila · 2 min"),
            (
                "Projetos e bibliotecas: 1 de 3",
                "BibliotecaB · na fila · 2 min",
                "",
            ),
        ),
        (Progress(False), ("Nada para copiar", "", "")),
    ],
)
def test_progress_texts(progress, expected):
    assert _progress_texts(progress) == expected


def drain(app):
    """What _poll does, once, without scheduling itself again."""
    while not app.events.empty():
        kind, payload = app.events.get_nowait()
        if kind == "progress" and app.busy:
            app._show_progress(payload)
        elif kind == "done":
            callback, result = payload
            callback(result)


def test_a_running_backup_shows_its_progress_where_the_saved_backups_are(app, monkeypatch):
    release = threading.Event()
    reported = threading.Event()

    def backup_now(config, store, cancel=None, progress=None):
        progress(Progress())
        progress(Progress(False, 1, 2, "Obras/ProjetoA", 0, 4, 0, 1024**2))
        reported.set()
        assert release.wait(10)
        return BackupResult(folder=None)

    monkeypatch.setattr("bimcloud_backup.gui.service.backup_now", backup_now)
    monkeypatch.setattr(app, "_save", app._config)
    monkeypatch.setattr(app, "_refresh_status", lambda: None)
    try:
        app._run_backup()
        assert reported.wait(10)
        drain(app)
        app.root.update_idletasks()
        assert app.progress_panel.winfo_manager() == "grid"
        assert app.history_panel.winfo_manager() == ""
        assert app.progress_title.get() == "Projetos e bibliotecas: 1 de 2"
        assert app.progress_current.get() == "Agora: ProjetoA"
        assert float(app.progress.cget("value")) == 50
    finally:
        release.set()
        for _ in range(100):
            drain(app)
            if not app.busy:
                break
            time.sleep(0.05)
    app.root.update_idletasks()
    assert app.history_panel.winfo_manager() == "grid"
    assert app.progress_panel.winfo_manager() == ""


@pytest.mark.parametrize("detail", [None, "preparando, etapa 1 de 3 · 12 min"])
@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_the_progress_never_makes_the_window_bigger(app, scale, detail):
    normal = float(app.root.tk.call("tk", "scaling"))
    try:
        app.root.tk.call("tk", "scaling", normal * scale)
        app._scale_theme_fonts()
        app.root.update_idletasks()
        idle = (app.root.winfo_reqwidth(), app.root.winfo_reqheight())

        app.busy = True
        app.running.grid()
        app.history_panel.grid_remove()
        app.progress_panel.grid()
        app._show_progress(
            Progress(
                False,
                10,
                22,
                f"Obras/{LONG_NAME}",
                120,
                1284,
                2 * 1024**3,
                20 * 1024**3,
                detail,
            )
        )
        app.root.update_idletasks()
        running = (app.root.winfo_reqwidth(), app.root.winfo_reqheight())

        assert running[0] == idle[0]
        assert running[1] <= idle[1]
    finally:
        app.busy = False
        app.progress.stop()
        app.running.grid_remove()
        app.progress_panel.grid_remove()
        app.history_panel.grid()
        app.root.tk.call("tk", "scaling", normal)
        app._scale_theme_fonts()
        app._size_history_columns()


def test_cancelled_backup_is_shown_in_the_status_panel(app):
    app._show_last_run(LastRun("2026-09-28T10:00:00", STATUS_CANCELLED, message="x"))
    assert app.last_title.get() == "Cancelado"
    assert "nenhum backup antigo foi apagado" in app.last_detail.get()


def test_browser_warning_is_on_screen():
    assert BROWSER_WARNING == "Durante o backup, feche o BIMcloud Manager no navegador."


def test_closing_during_a_backup_cancels_it_and_waits(app, monkeypatch):
    closed = []
    monkeypatch.setattr(app, "_close_window", lambda: closed.append(True))
    asked = []
    monkeypatch.setattr(
        "bimcloud_backup.gui.messagebox.askyesno", lambda *a, **k: asked.append(a) or True
    )
    app.cancel_event = threading.Event()
    app.busy = True
    try:
        app._on_close()
        assert app.cancel_event.is_set() and app.closing
        assert closed == []  # the window waits for the backup to stop

        app._on_close()  # a second click does not ask again
        assert len(asked) == 1

        app._backup_done(BackupCancelled("Backup cancelado"))
        assert closed == [True]
    finally:
        app.busy = False
        app.closing = False


def test_failure_notification_switch_is_in_the_form(app):
    assert app.v["notify_failures"].get() is True
    try:
        app.v["notify_failures"].set(False)
        assert app._config().notify_failures is False
    finally:
        app.v["notify_failures"].set(True)


def test_theme_fonts_follow_the_windows_scale(app):
    body = tkfont.nametofont("SunValleyBodyFont", root=app.root)
    normal = float(app.root.tk.call("tk", "scaling"))
    try:
        app.root.tk.call("tk", "scaling", normal * 1.5)  # Windows at 150%
        app._scale_theme_fonts()
        assert body.cget("size") == -21
        assert app.px(100) == 150
        rowheight = ttk.Style(app.root).lookup("Treeview", "rowheight")
        assert int(rowheight) == body.metrics("linespace") + 3
    finally:
        app.root.tk.call("tk", "scaling", normal)
        app._scale_theme_fonts()
    assert body.cget("size") == -14


def test_window_fits_a_small_screen_without_scrolling(app, monkeypatch):
    monkeypatch.setattr(app.root, "winfo_screenwidth", lambda: 1366)
    monkeypatch.setattr(app.root, "winfo_screenheight", lambda: 768)
    width, height = app._fit_to_screen()
    assert height <= 768 - app.px(88)
    assert width <= 1366 - 16
    # The minimum size is everything the content needs, and it fits the notebook's screen.
    assert app.root.minsize() == (app.root.winfo_reqwidth(), app.root.winfo_reqheight())
    assert height >= app.root.winfo_reqheight()
    assert width >= app.root.winfo_reqwidth()


@pytest.mark.parametrize(
    ("scale", "screen"), [(1.0, (1366, 768)), (1.25, (1920, 1080)), (1.5, (1920, 1080))]
)
def test_everything_fits_the_screen_at_each_windows_scale(app, scale, screen):
    normal = float(app.root.tk.call("tk", "scaling"))
    try:
        app.root.tk.call("tk", "scaling", normal * scale)
        app._scale_theme_fonts()
        app.root.update_idletasks()
        assert app.root.winfo_reqwidth() <= screen[0] - 16
        assert app.root.winfo_reqheight() <= screen[1] - app.px(88)
    finally:
        app.root.tk.call("tk", "scaling", normal)
        app._scale_theme_fonts()
        app.root.update_idletasks()


def _assert_in_view(app):
    """Every control is inside the window: nothing cut, nothing to scroll to."""
    root = app.root
    root.update()
    right, bottom = (
        root.winfo_rootx() + root.winfo_width(),
        root.winfo_rooty() + root.winfo_height(),
    )
    # The advanced options are in the window or behind their button, never both or neither.
    advanced = app.advanced_inline if app.advanced_shown_inline else app.advanced_button
    assert app.advanced_inline.winfo_ismapped() is not app.advanced_button.winfo_ismapped()
    for widget in (
        app.run_button,
        app.save_button,
        advanced,
        app.folders_button,
        app.activity,
        app.history_tree,
    ):
        assert widget.winfo_ismapped()
        assert widget.winfo_rootx() + widget.winfo_width() <= right
        assert widget.winfo_rooty() + widget.winfo_height() <= bottom
    # The body follows the window, whatever its size.
    assert app.body.winfo_width() == root.winfo_width()


def test_maximize_and_restore_keep_everything_in_view(app):
    root = app.root
    root.deiconify()
    try:
        width, height = root.minsize()
        root.geometry(f"{width}x{height}")
        _assert_in_view(app)
        root.state("zoomed")
        _assert_in_view(app)
        grown = app.activity.winfo_height()
        root.state("normal")
        root.geometry(f"{width}x{height}")
        _assert_in_view(app)
        # The lists on the left took the extra height and gave it back.
        assert app.activity.winfo_height() < grown
    finally:
        root.state("normal")
        root.withdraw()


def test_long_user_names_are_shortened(app):
    app._set_signed_in("usuario.com.um.nome.muito.longo@escritorio-exemplo.invalid")
    text = app.auth_text.get()
    assert text.endswith("…") and len(text) <= len("Conectado como ") + 32


def test_sizes_never_break_between_number_and_unit():
    assert _format_bytes(18.6 * 1024**3) == "18,6\u00a0GB"
    assert _format_bytes(500) == "500\u00a0B"


def test_folders_not_opened_yet_say_so_and_empty_ones_say_none(app):
    picker = make_picker(app)
    assert picker.tree.set("Obras", "content") == "abra para contar"
    picker.tree.focus("Pastas Exemplo")
    picker._on_open()
    picker.tree.focus("Pastas Exemplo/Projeto Teste")
    picker._on_open()
    assert picker.tree.set("Pastas Exemplo/Projeto Teste", "content") == "1 projeto"
    picker.cancel()


def test_history_lists_backups_without_names_and_opens_the_folder(app, tmp_path, monkeypatch):
    from tests.test_history import FILES, WHEN, make_backup

    folder = make_backup(tmp_path, WHEN, files=FILES, errors=["Obras/x.pdf: falhou"])
    (tmp_path / f".incompleto-{folder.name}").mkdir()
    opened = []
    monkeypatch.setattr("bimcloud_backup.gui.os.startfile", opened.append, raising=False)
    directory = app.v["directory"].get()
    try:
        app.v["directory"].set(str(tmp_path))
        app._refresh_history()
        rows = [app.history_tree.item(i, "values") for i in app.history_tree.get_children()]
        assert [r[2] for r in rows] == ["Incompleto", "Com avisos (1)"]
        assert rows[1][0] == "28/09 13:56"

        app.history_tree.selection_set(str(folder))
        app._show_history_detail()
        detail = app.history_detail.get()
        assert detail.startswith("28/09/2026 13:56 \u00b7 ")
        assert "1\u00a0.BIMProject" in detail and "2\u00a0arquivos" in detail
        assert "1 item com erro" in detail
        assert "Casa" not in detail and "Obras" not in detail  # never names
        assert str(app.open_backup_button.cget("state")) == "normal"

        app._open_selected_backup()
        assert opened == [folder]
    finally:
        app.v["directory"].set(directory)
        app._refresh_history()


def test_an_unreadable_destination_only_empties_the_history(app, monkeypatch):
    def broken(*args, **kwargs):
        raise PermissionError("sem acesso")

    monkeypatch.setattr("bimcloud_backup.gui.history.read_history", broken)
    app._refresh_history()
    assert app.history_tree.get_children() == ()


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_history_list_never_widens_the_window(app, scale, tmp_path):
    from tests.test_history import FILES, WHEN, make_backup

    for day in range(20, 28):
        make_backup(tmp_path, WHEN.replace(day=day), files=FILES)
    normal = float(app.root.tk.call("tk", "scaling"))
    directory = app.v["directory"].get()
    try:
        app.root.tk.call("tk", "scaling", normal * scale)
        app._scale_theme_fonts()
        app.v["directory"].set(str(tmp_path))
        app._refresh_history()
        app.history_tree.selection_set(app.history_tree.get_children()[0])
        app._show_history_detail()
        history_frame = app.history_tree.master
        history_frame.grid_remove()
        app.root.update_idletasks()
        without_list = app.body.winfo_reqwidth()

        history_frame.grid()
        app.root.update_idletasks()

        # The list (with a selected backup and its detail line) adds no width at all.
        assert app.body.winfo_reqwidth() == without_list
    finally:
        app.root.tk.call("tk", "scaling", normal)
        app._scale_theme_fonts()
        app._size_history_columns()
        app.v["directory"].set(directory)
        app._refresh_history()


def test_library_snapshots_switch_follows_the_libraries_switch(app):
    try:
        app.v["include_libraries"].set(False)
        app._sync_enabled()
        assert str(app.include_library_backups_check.cget("state")) == "disabled"
        app.v["include_backups_in_library_export"].set(True)
        assert app._config().include_backups_in_library_export is True
    finally:
        app.v["include_libraries"].set(True)
        app.v["include_backups_in_library_export"].set(False)
        app._sync_enabled()
    assert str(app.include_library_backups_check.cget("state")) == "normal"
    label = app.include_backups_check.master.winfo_children()[0]
    assert "snapshots (backups)" in label.cget("text")


def test_advanced_options_open_in_their_own_window(app):
    app.root.update_idletasks()
    before = (app.root.winfo_reqwidth(), app.root.winfo_reqheight())
    app._open_advanced()
    window = app.advanced_window
    try:
        app._open_advanced()
        assert app.advanced_window is window  # a second click only brings it to the front
        app.root.update_idletasks()
        assert (app.root.winfo_reqwidth(), app.root.winfo_reqheight()) == before
        # The fields are the main window's: "Salvar alterações" saves them.
        assert app.notify_check.cget("variable") == str(app.v["notify_failures"])
        # The notification switch has a row of its own, not the identifier's.
        identifier_row = app.advanced_card.grid_slaves(column=0)
        rows = {int(w.grid_info()["row"]) for w in identifier_row}
        # The switch sits in a frame with its "?".
        assert int(app.notify_check.master.grid_info()["row"]) not in rows
    finally:
        window.destroy()


def test_window_uses_the_program_icon_in_every_size(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.root, "iconphoto", lambda *args: calls.append(args))
    app._set_window_icon()
    assert calls == [(True, *app.icons)]
    assert [image.width() for image in app.icons] == list(WINDOW_ICON_SIZES)
    assert [image.height() for image in app.icons] == list(WINDOW_ICON_SIZES)


def test_window_keeps_the_default_icon_when_the_pngs_fail(app, monkeypatch, caplog):
    def fail(*args):
        raise tk.TclError("sem ícone")

    monkeypatch.setattr(app.root, "iconphoto", fail)
    try:
        with caplog.at_level(logging.WARNING):
            app._set_window_icon()
        assert app.icons == []
        assert "Não foi possível carregar o ícone da janela" in caplog.text
    finally:
        monkeypatch.undo()
        app._set_window_icon()


@pytest.fixture
def saved_config(app, tmp_path, monkeypatch):
    """The window reading a config.toml of its own, with the scheduler left alone."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[bimcloud]\nserver_url = "https://exemplo.bimcloud.com"\n'
        '[backup]\ndirectory = "D:/BackupsExemplo"\n'
        '[schedule]\nrun_at = "23:00"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("bimcloud_backup.gui._safe_schedule_status", lambda: None)
    monkeypatch.setattr("bimcloud_backup.gui.scheduler.install", lambda *a: None)
    monkeypatch.setattr("bimcloud_backup.gui.scheduler.remove", lambda *a: None)
    example = app.config_path
    app.config_path = path
    app._discard()
    yield path
    app.config_path = example
    app._discard()


def test_changes_show_the_warning_and_block_backup_and_folders(app, saved_config, monkeypatch):
    started = []
    monkeypatch.setattr(app, "_background", lambda *a: started.append(a))
    assert not app.dirty and app.run_tooltip.text() is None
    app.root.update_idletasks()
    size = (app.root.winfo_reqwidth(), app.root.winfo_reqheight())

    app.v["run_at"].set("22:00")

    assert app.dirty
    app.root.update_idletasks()
    # The warning takes the place of the version number: the window keeps its size.
    assert (app.root.winfo_reqwidth(), app.root.winfo_reqheight()) == size
    assert app.unsaved_banner.winfo_manager() == "grid"
    assert app.version_label.winfo_manager() == ""
    assert str(app.run_button.cget("state")) == "disabled"
    assert str(app.folders_button.cget("state")) == "disabled"
    assert "Salve ou descarte" in app.run_tooltip.text()
    assert "Salve ou descarte" in app.folders_tooltip.text()
    app._run_backup()
    app._choose_folders()
    assert started == []


def test_discarding_goes_back_to_the_saved_settings(app, saved_config):
    app.v["run_at"].set("22:00")
    app._set_selection(Selection(("Obras",)))
    app._discard()
    assert app.v["run_at"].get() == "23:00"
    assert app.selection == Selection()
    assert not app.dirty
    assert app.unsaved_banner.winfo_manager() == ""
    assert app.version_label.winfo_manager() == "grid"
    assert str(app.run_button.cget("state")) == "normal"
    assert str(app.folders_button.cget("state")) == "normal"


def test_saving_clears_the_warning(app, saved_config):
    app.v["run_at"].set("22:00")
    assert app._save() is not None
    assert 'run_at = "22:00"' in saved_config.read_text(encoding="utf-8")
    assert not app.dirty
    assert str(app.run_button.cget("state")) == "normal"


def test_a_running_backup_keeps_its_button_disabled(app, saved_config):
    app.busy = True
    try:
        app._set_dirty(False)
        assert str(app.run_button.cget("state")) == "disabled"
    finally:
        app.busy = False
        app._set_dirty(False)


@pytest.mark.parametrize(
    ("choice", "saved", "closes"),
    [(None, True, False), ("discard", True, True), ("save", True, True), ("save", False, False)],
)
def test_closing_with_changes_asks_save_discard_or_cancel(
    app, saved_config, monkeypatch, choice, saved, closes
):
    closed, saves = [], []
    monkeypatch.setattr(app, "_close_window", lambda: closed.append(True))
    monkeypatch.setattr(app, "_ask_unsaved", lambda: choice)
    monkeypatch.setattr(app, "_save", lambda: saves.append(True) or (object() if saved else None))
    app.v["run_at"].set("22:00")
    app._on_close()
    assert closed == ([True] if closes else [])
    assert saves == ([True] if choice == "save" else [])


def test_the_close_question_offers_save_discard_and_cancel(app):
    app.root.deiconify()
    seen = []

    def answer():
        dialog = next(w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel))
        buttons = [w for w in dialog.winfo_children()[0].winfo_children()[1].winfo_children()]
        seen.extend(b.cget("text") for b in buttons)
        buttons[1].invoke()

    try:
        app.root.after(200, answer)
        assert app._ask_unsaved() == "discard"
        assert seen == ["Salvar", "Descartar", "Cancelar"]
    finally:
        app.root.withdraw()


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_history_dates_and_statuses_are_never_cut(app, scale, tmp_path):
    from tests.test_history import FILES, WHEN, make_backup

    make_backup(tmp_path, WHEN, files=FILES, errors=[f"Obras/x{i}.pdf: falhou" for i in range(12)])
    make_backup(tmp_path, WHEN.replace(day=1, hour=23), files=FILES)
    normal = float(app.root.tk.call("tk", "scaling"))
    directory = app.v["directory"].get()
    app.root.deiconify()
    try:
        app.root.tk.call("tk", "scaling", normal * scale)
        app._scale_theme_fonts()
        app._size_history_columns()
        app.v["directory"].set(str(tmp_path))
        app._refresh_history()
        app.root.update_idletasks()
        width, height = app.root.winfo_reqwidth(), app.root.winfo_reqheight()
        app.root.geometry(f"{width}x{height}")  # the smallest the window can be
        app.root.update()
        font = tkfont.nametofont("SunValleyBodyFont", root=app.root)
        columns = ("when", "size", "status")
        texts = [app.history_tree.item(i, "values") for i in app.history_tree.get_children()]
        assert "Com avisos (12)" in [row[2] for row in texts]
        for row in texts:
            for column, text in zip(columns, row, strict=True):
                assert font.measure(text) < int(app.history_tree.column(column, "width")), text
        # And the three columns fit the list itself: nothing hidden on the right.
        total = sum(int(app.history_tree.column(c, "width")) for c in columns)
        assert total <= app.history_tree.winfo_width()
    finally:
        app.root.withdraw()
        app.root.tk.call("tk", "scaling", normal)
        app._scale_theme_fonts()
        app._size_history_columns()
        app.v["directory"].set(directory)
        app._refresh_history()


def test_undoing_a_change_clears_the_warning(app, saved_config):
    app.v["run_at"].set("22:00")
    app._set_selection(Selection(("Obras",)))
    assert app.dirty

    app.v["run_at"].set("23:00")
    assert app.dirty  # the folders still differ
    app._set_selection(Selection(()))

    assert not app.dirty
    assert app.unsaved_banner.winfo_manager() == ""
    assert str(app.run_button.cget("state")) == "normal"
    assert str(app.folders_button.cget("state")) == "normal"


def test_after_saving_the_old_value_is_a_change(app, saved_config):
    app.v["run_at"].set("22:00")
    assert app._save() is not None and not app.dirty
    app.v["run_at"].set("23:00")
    assert app.dirty
    app.v["run_at"].set("22:00")
    assert not app.dirty


@pytest.mark.parametrize(
    ("choice", "saved", "stops"),
    [(None, True, False), ("save", False, False), ("save", True, True), ("discard", True, True)],
)
def test_closing_during_a_backup_with_changes_asks_about_them_first(
    app, saved_config, monkeypatch, choice, saved, stops
):
    closed, asked = [], []
    monkeypatch.setattr(app, "_close_window", lambda: closed.append(True))
    monkeypatch.setattr(app, "_ask_unsaved", lambda: asked.append("changes") or choice)
    monkeypatch.setattr(app, "_save", lambda: object() if saved else None)
    monkeypatch.setattr(
        "bimcloud_backup.gui.messagebox.askyesno", lambda *a, **k: asked.append("backup") or True
    )
    monkeypatch.setattr("bimcloud_backup.gui.messagebox.showinfo", lambda *a, **k: None)
    app.v["run_at"].set("22:00")
    app.cancel_event = threading.Event()
    app.busy = True
    try:
        app._on_close()
        # The changes are asked about before the backup starts to stop.
        assert asked == (["changes", "backup"] if stops else ["changes"])
        assert app.cancel_event.is_set() is stops and app.closing is stops
        if choice == "discard":
            assert app.v["run_at"].get() == "23:00" and not app.dirty
        app._backup_done(BackupCancelled("Backup cancelado"))
        assert closed == ([True] if stops else [])
    finally:
        app.busy = False
        app.closing = False


def test_technical_details_stay_out_of_the_activity_box():
    events = queue.Queue()
    handler = QueueLogHandler(events)
    plain = logging.LogRecord("x", logging.ERROR, "", 0, "Falha: Biblioteca X: motivo", (), None)
    technical = logging.LogRecord("x", logging.INFO, "", 0, "Detalhe técnico: X", (), None)
    technical.technical = True
    handler.emit(plain)
    handler.emit(technical)
    assert events.qsize() == 1 and "Falha: Biblioteca X" in events.get()[1]


def test_last_backup_with_errors_links_to_the_list(app, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(app, "_show_failures", shown.append)
    try:
        app._show_last_run(
            LastRun("2026-09-28T10:00:00", "warnings", files=3, errors=1, folder=str(tmp_path))
        )
        assert app.last_failures_link.winfo_manager() == "grid"
        assert app.last_failures_link.cget("text") == "1 item com erro · ver quais"
        assert "com erro" not in app.last_detail.get()
        app._show_last_failures()
        assert shown == [tmp_path]

        app._show_last_run(LastRun("2026-09-28T10:00:00", "ok", files=3))
        assert app.last_failures_link.winfo_manager() == ""
    finally:
        app._refresh_status()


def test_selected_backup_with_errors_links_to_the_list(app, tmp_path, monkeypatch):
    from tests.test_history import FILES, WHEN, make_backup

    folder = make_backup(tmp_path, WHEN, files=FILES, errors=["Project Root/Obras/a.pdf: x"])
    shown = []
    monkeypatch.setattr(app, "_show_failures", shown.append)
    directory = app.v["directory"].get()
    try:
        app.v["directory"].set(str(tmp_path))
        app._refresh_history()
        app.history_tree.selection_set(str(folder))
        app._show_history_detail()
        assert app.history_detail.get().endswith("item com erro · ver quais")
        assert str(app.history_detail_label.cget("cursor")) == "hand2"
        app._show_history_failures()
        assert shown == [folder]
    finally:
        app.v["directory"].set(directory)
        app._refresh_history()


def test_failures_window_explains_each_item_and_copies_the_technical_text(app, tmp_path):
    from bimcloud_backup.failures import KIND_LIBRARY, Failure
    from bimcloud_backup.state import load_manifest, save_manifest
    from tests.test_history import WHEN, make_backup

    folder = make_backup(tmp_path, WHEN)
    failure = Failure(
        "Project Root/Libraries/BibliotecaB.pla",
        KIND_LIBRARY,
        "A exportação terminou com status 'failed': ModelServerSideError: Model server job "
        "failed with code 18, message: Failed to create archive\n    at linha 3",
    )
    manifest = load_manifest(folder)
    manifest.errors, manifest.failures = [failure.text], [failure]
    save_manifest(manifest, folder)

    windows = []
    original = gui.FailuresWindow

    def capture(*args, **kwargs):
        windows.append(original(*args, **kwargs))
        return windows[-1]

    try:
        gui.FailuresWindow = capture
        app._show_failures(folder)
    finally:
        gui.FailuresWindow = original
    window = windows[0]
    try:
        shown = window.text.get("1.0", "end")
        assert "Biblioteca Project Root/Libraries/BibliotecaB.pla" in shown
        assert "O BIMcloud não conseguiu gerar o arquivo (erro 18 do servidor)" in shown
        assert "Mensagem técnica: A exportação terminou com status 'failed'" in shown
        assert "at linha 3" not in shown  # the stack trace only goes to the copy
        window.copy_technical()
        assert window.window.clipboard_get() == failure.text
        assert "1 item falhou" in window.window.winfo_children()[0].winfo_children()[0].cget("text")
    finally:
        window.window.destroy()


def _right_column_gap(app):
    """Empty height between the last card on the right and the save row."""
    footer = app.save_button.master
    above = app.advanced_inline if app.advanced_shown_inline else app.schedule_card
    return footer.winfo_rooty() - (above.winfo_rooty() + above.winfo_height())


def test_advanced_options_move_into_the_window_when_they_fit(app, windows_scale):
    root = app.root
    root.deiconify()
    try:
        root.update_idletasks()
        app._fit_minimum()
        width, height = root.minsize()
        root.geometry(f"{width}x{height}")
        root.update()
        # The smallest window has no room: the button opens them in their own window.
        assert not app.advanced_shown_inline
        assert app.advanced_button.winfo_ismapped()
        assert _right_column_gap(app) <= app.px(16)

        taller = height + app.advanced_inline.winfo_reqheight() + 40
        if taller > root.winfo_screenheight() - app.px(88):
            pytest.skip("tela baixa demais para a janela crescer (CI)")
        root.geometry(f"{width}x{taller}")
        root.update()
        assert app.advanced_shown_inline
        assert not app.advanced_button.winfo_ismapped()
        _assert_in_view(app)
        assert _right_column_gap(app) <= app.px(16)
        # The card is as tall as its content: no empty band inside it either.
        assert abs(app.advanced_inline.winfo_height() - app.advanced_inline.winfo_reqheight()) <= 2
        # Showing them did not raise the minimum size.
        assert root.minsize() == (width, height)

        root.geometry(f"{width}x{height}")
        root.update()
        assert not app.advanced_shown_inline and app.advanced_button.winfo_ismapped()
    finally:
        root.withdraw()


def test_taller_window_grows_the_cards_instead_of_leaving_a_gap(app):
    root = app.root
    root.deiconify()
    try:
        root.update_idletasks()
        app._fit_minimum()
        width, height = root.minsize()
        root.geometry(f"{width}x{height}")
        root.update()
        card = app.schedule_card.winfo_height()
        # Taller, but not enough for the advanced options: steps 4 and 5 take the height.
        root.geometry(f"{width}x{height + app.advanced_inline.winfo_reqheight() // 2}")
        root.update()
        assert not app.advanced_shown_inline
        assert app.schedule_card.winfo_height() > card
        assert _right_column_gap(app) <= app.px(16)
    finally:
        root.geometry(f"{width}x{height}")
        root.withdraw()


def test_steps_are_numbered_in_the_order_to_fill_them(app):
    titles = [
        child.cget("text")
        for card in app.settings_column.winfo_children()
        for head in card.winfo_children()[:1]
        for child in head.winfo_children()
        if str(child.winfo_class()) == "TLabel" and child.cget("text")[:1].isdigit()
    ]
    assert titles == [
        "1. Conexão com o BIMcloud",
        "2. O que copiar",
        "3. Onde salvar",
        "4. Histórico",
        "5. Backup automático",
    ]


def test_the_activity_takes_the_height_while_a_backup_runs(app):
    from bimcloud_backup.backup import Progress

    root = app.root
    root.deiconify()
    try:
        width, height = root.minsize()
        root.geometry(f"{width}x{height + 200}")
        app.history_panel.grid_remove()
        app.progress_panel.grid()
        app.status_column.rowconfigure(5, weight=0)
        app._show_progress(Progress())
        root.update()
        progress = app.progress_panel
        activity = app.activity.master.master
        # No empty band between the progress lines and the activity box.
        gap = activity.winfo_rooty() - (progress.winfo_rooty() + progress.winfo_height())
        assert gap <= app.px(16)
    finally:
        app.progress_panel.grid_remove()
        app.history_panel.grid()
        app.status_column.rowconfigure(5, weight=1)
        root.geometry(f"{width}x{height}")
        root.withdraw()


def test_advanced_options_go_inline_only_when_the_spare_height_holds_them(
    app, monkeypatch, windows_scale
):
    col = app.settings_column
    need = app.advanced_inline.winfo_reqheight() + 8
    try:
        for spare, inline in ((need - 1, False), (need, True), (need - 1, False)):
            height = col.winfo_reqheight() + spare
            monkeypatch.setattr(col, "winfo_height", lambda height=height: height)
            app._place_advanced()
            assert app.advanced_shown_inline is inline
            assert app.advanced_button.winfo_manager() == ("" if inline else "pack")
            # Inline, the advanced row gets exactly its height and steps 4 and 5 the rest.
            weights = (int(col.rowconfigure(3)["weight"]), int(col.rowconfigure(4)["weight"]))
            assert weights == ((spare - need, need) if inline else (1, 0))
    finally:
        monkeypatch.undo()
        app._show_advanced_inline(False)


@pytest.fixture(params=[1.0, 1.25, 1.5], ids=["100%", "125%", "150%"])
def windows_scale(app, request):
    """The window's fonts at a Windows scale, back to normal afterwards."""
    normal = float(app.root.tk.call("tk", "scaling"))
    app.root.tk.call("tk", "scaling", normal * request.param)
    app._scale_theme_fonts()
    app.root.update_idletasks()
    yield request.param
    app.root.tk.call("tk", "scaling", normal)
    app._scale_theme_fonts()
    app.root.update_idletasks()
    app._show_advanced_inline(False)


def test_inline_card_is_as_tall_as_its_content_in_a_tall_window(app, monkeypatch):
    col = app.settings_column
    root = app.root
    root.deiconify()
    try:
        root.update_idletasks()
        app._fit_minimum()
        width, height = root.minsize()
        need = app.advanced_inline.winfo_reqheight() + 8
        root.geometry(f"{width}x{height}")
        root.update()
        # Much taller than needed: the extra must not end up inside the advanced card.
        monkeypatch.setattr(col, "winfo_height", lambda: col.winfo_reqheight() + need + 300)
        app._place_advanced()
        assert int(col.rowconfigure(4)["weight"]) == need
        assert int(col.rowconfigure(3)["weight"]) == 300
    finally:
        monkeypatch.undo()
        app._show_advanced_inline(False)
        root.withdraw()


def test_advanced_window_closed_then_save_still_works(app, saved_config):
    app._open_advanced()
    window = app.advanced_window
    inline_check = app.notify_check
    window.destroy()
    app.root.update()
    # The references point at the copy in the main window, which is still there.
    assert app.notify_check is inline_check and app.notify_check.winfo_exists()
    assert app.advanced_card.winfo_exists()

    app.v["notify_failures"].set(not app.v["notify_failures"].get())
    assert app.dirty
    app._sync_enabled()
    assert app._save() is not None
    assert "notify_failures = false" in saved_config.read_text(encoding="utf-8")
    assert not app.dirty


@pytest.fixture(autouse=True)
def no_real_run_entry(monkeypatch):
    """No test reads or writes the Run entry of whoever runs the tests."""
    entry: dict[str, bool] = {"on": False}
    monkeypatch.setattr("bimcloud_backup.tray.starts_with_windows", lambda reg=None: entry["on"])
    monkeypatch.setattr(
        "bimcloud_backup.tray.set_start_with_windows",
        lambda enabled, reg=None: entry.update(on=enabled),
    )
    return entry


class FakeTray:
    """The notification-area icon without Windows: records what the window asks of it."""

    def __init__(self, starts=True, shown=True):
        self.starts = starts
        self.shown = shown
        self.running = False
        self.backup_enabled = True
        self.tips: list[str] = []
        self.balloons: list[str] = []
        self.post = None
        self.stopped = False

    def start(self, post):
        self.post = post
        self.running = self.starts and self.shown
        return self.starts

    def stop(self):
        self.stopped = True
        self.running = False

    def set_tip(self, text):
        self.tips.append(text)

    def balloon(self, title, text):
        self.balloons.append(text)


@pytest.fixture
def with_tray(app, saved_config, monkeypatch):
    icon = FakeTray()
    icon.start(lambda action: app.events.put(("tray", action)))
    app.tray_icon = icon
    monkeypatch.setattr(app, "_close_window", lambda: icon.stop())
    yield icon
    app.tray_icon = None
    app.tray_notice_shown = False
    app.root.withdraw()


def test_closing_the_window_keeps_the_program_in_the_notification_area(app, with_tray):
    app.root.deiconify()
    app.v["run_at"].set("22:00")  # pending changes stay in the window, nothing is asked
    app._on_close()
    assert not app.root.winfo_viewable() and not with_tray.stopped
    assert app.dirty and not app.closing
    assert len(with_tray.balloons) == 1 and "Sair" in with_tray.balloons[0]
    app._show_window()
    app._on_close()
    assert len(with_tray.balloons) == 1  # the notice only the first time
    app._discard()


def test_without_the_icon_closing_the_window_still_leaves(app, saved_config, monkeypatch):
    closed = []
    monkeypatch.setattr(app, "_close_window", lambda: closed.append(True))
    assert app.tray_icon is None
    app._on_close()
    assert closed == [True]


def test_an_icon_that_does_not_start_is_not_used():
    posted = []
    assert gui._started(FakeTray(starts=False), posted.append) is None
    assert gui._started(None, posted.append) is None
    icon = FakeTray()
    assert gui._started(icon, posted.append) is icon
    icon.post("open")
    assert posted == ["open"]


def test_double_click_or_open_shows_the_window(app, with_tray):
    app.root.withdraw()
    with_tray.post("open")
    app._poll()
    app.root.update()
    assert app.root.winfo_viewable()


def test_backup_from_the_menu_shows_the_window_and_starts(app, with_tray, monkeypatch):
    started = []
    monkeypatch.setattr(app, "_background", lambda *a: started.append(a))
    app.root.withdraw()
    try:
        app._tray_action("backup")
        app.root.update()
        assert app.root.winfo_viewable() and len(started) == 1
        assert app.busy and not with_tray.backup_enabled
        assert with_tray.tips[-1].endswith("Backup em andamento")
    finally:
        app._backup_done(BackupResult())
    assert with_tray.backup_enabled
    assert "Backup em andamento" not in with_tray.tips[-1]


def test_menu_backup_is_grayed_out_with_pending_changes(app, with_tray):
    app.v["run_at"].set("22:00")
    assert not with_tray.backup_enabled
    app._discard()
    assert with_tray.backup_enabled


def test_exit_from_the_menu_asks_about_changes_like_closing_did(app, with_tray, monkeypatch):
    asked = []
    monkeypatch.setattr(app, "_ask_unsaved", lambda: asked.append(True) or None)
    app.v["run_at"].set("22:00")
    app._tray_action("exit")
    # "Cancelar" in the question: the program stays open, the window shown.
    assert asked == [True] and not with_tray.stopped and app.root.winfo_viewable()
    app._discard()
    app._tray_action("exit")
    assert with_tray.stopped


def test_exit_during_a_backup_follows_the_usual_safe_stop(app, with_tray, monkeypatch):
    monkeypatch.setattr("bimcloud_backup.gui.messagebox.askyesno", lambda *a, **k: True)
    app.cancel_event = threading.Event()
    app.busy = True
    try:
        app._tray_action("exit")
        assert app.cancel_event.is_set() and app.closing and not with_tray.stopped
        app._backup_done(BackupCancelled("Backup cancelado"))
        assert with_tray.stopped
    finally:
        app.busy = False
        app.closing = False


def test_a_backup_ending_with_the_window_hidden_shows_a_notice(app, with_tray, monkeypatch):
    shown = []
    monkeypatch.setattr("bimcloud_backup.gui.messagebox.showerror", lambda *a: shown.append(a))
    app.root.withdraw()
    app.busy = True
    app._backup_done(BimcloudError("falhou"))
    assert shown == []
    assert with_tray.balloons == ["O backup falhou. Abra o programa para ver os detalhes."]


@pytest.mark.parametrize(
    ("busy", "run", "expected"),
    [
        (True, None, "Backup em andamento"),
        (False, None, "Nenhum backup ainda"),
        (False, LastRun("2026-09-30T23:18:00", "ok"), "Último backup concluído, 30/09 às 23:18"),
        (False, LastRun("2026-09-30T23:18:00", "failed"), "Último backup falhou, 30/09 às 23:18"),
        (
            False,
            LastRun("2026-09-30T23:18:00", "warnings", errors=2),
            "Último backup com erros, 30/09 às 23:18",
        ),
        (False, LastRun("2026-09-30T23:18:00", STATUS_CANCELLED), "Último backup cancelado"),
    ],
)
def test_icon_tooltip_tells_how_the_last_backup_went(busy, run, expected):
    tip = gui._tray_tip(busy, run)
    assert tip.startswith("BIMcloud Backup Local\n")
    assert expected in tip
    assert len(tip) <= 127


def test_start_with_windows_is_a_setting_saved_with_the_others(
    app, saved_config, no_real_run_entry
):
    assert not app.startup.get()
    app.startup.set(True)
    assert app.dirty
    assert not no_real_run_entry["on"]  # only on saving
    assert app._save() is not None
    assert no_real_run_entry["on"] and not app.dirty
    app.startup.set(False)
    assert app._save() is not None
    assert not no_real_run_entry["on"]


def test_second_copy_brings_back_the_open_window(monkeypatch):
    calls = []
    monkeypatch.setattr("bimcloud_backup.gui.tray.claim_single_instance", lambda: False)
    monkeypatch.setattr(
        "bimcloud_backup.gui.tray.show_existing", lambda: calls.append("show") or True
    )
    monkeypatch.setattr("bimcloud_backup.gui.tk.Tk", lambda: pytest.fail("abriu outra janela"))
    assert gui.run_gui(ROOT / "config.example.toml", MemoryTokenStore()) == 0
    assert calls == ["show"]
    # Starting with Windows while already open: not even the window comes up.
    calls.clear()
    assert gui.run_gui(ROOT / "config.example.toml", MemoryTokenStore(), tray_only=True) == 0
    assert calls == []


def test_second_copy_never_opens_even_if_the_first_cannot_be_reached(monkeypatch):
    calls = []
    monkeypatch.setattr("bimcloud_backup.gui.tray.claim_single_instance", lambda: False)
    monkeypatch.setattr(
        "bimcloud_backup.gui.tray.show_existing", lambda: calls.append("show") or False
    )
    monkeypatch.setattr("bimcloud_backup.gui.tk.Tk", lambda: pytest.fail("abriu outra janela"))
    assert gui.run_gui(ROOT / "config.example.toml", MemoryTokenStore()) == 0
    assert calls == ["show"]


def test_without_the_icon_shown_the_window_still_answers_a_second_copy(
    app, saved_config, monkeypatch
):
    closed = []
    monkeypatch.setattr(app, "_close_window", lambda: closed.append(True))
    icon = FakeTray(shown=False)
    assert gui._started(icon, lambda action: app.events.put(("tray", action))) is icon
    app.tray_icon = icon
    try:
        app.root.withdraw()
        icon.post("open")  # what the hidden window does when a second copy calls it
        app._poll()
        app.root.update()
        assert app.root.winfo_viewable()
        # No icon to go back to: the X leaves, as before.
        app._on_close()
        assert closed == [True]
    finally:
        app.tray_icon = None
        app.root.withdraw()


@pytest.fixture
def server_task(app, saved_config, monkeypatch):
    """The scheduler as the window sees it, recording installs and password questions."""
    record = {"installs": [], "asked": 0, "mode": None, "infos": []}

    def install(config, path, password=None):
        record["installs"].append(password)
        record["mode"] = "always" if password is not None else "logged_on"

    def ask():
        record["asked"] += 1
        return record.get("password", "senha")

    monkeypatch.setattr("bimcloud_backup.gui.scheduler.install", install)
    monkeypatch.setattr("bimcloud_backup.gui.scheduler.check_destination", lambda path: None)
    monkeypatch.setattr("bimcloud_backup.gui._safe_run_mode", lambda: record["mode"])
    monkeypatch.setattr(
        "bimcloud_backup.gui._safe_schedule_status",
        lambda: "agendada" if record["mode"] else None,
    )
    monkeypatch.setattr(app, "_ask_windows_password", ask)
    monkeypatch.setattr(
        "bimcloud_backup.gui.messagebox.showinfo", lambda *a, **k: record["infos"].append(a)
    )
    app.auto.set(True)
    yield record
    app.auto.set(False)
    app.v["run_logged_off"].set(False)


def test_the_logged_off_option_follows_the_automatic_switch(app, saved_config):
    app.auto.set(False)
    app._sync_enabled()
    assert str(app.logged_off_check.cget("state")) == "disabled"
    app.auto.set(True)
    app.v["run_logged_off"].set(True)
    app._sync_enabled()
    try:
        assert str(app.logged_off_check.cget("state")) == "normal"
        assert "senha" in app.logged_off_hint.get()
    finally:
        app.auto.set(False)
        app.v["run_logged_off"].set(False)
        app._sync_enabled()
    assert "Só roda com você conectado" in app.logged_off_hint.get()


def test_running_logged_off_asks_the_password_once(app, server_task):
    app.v["run_logged_off"].set(True)
    assert app._save() is not None
    assert server_task["installs"] == ["senha"]
    assert "run_logged_off = true" in app.config_path.read_text(encoding="utf-8")
    assert "senha" not in app.config_path.read_text(encoding="utf-8")
    assert "Mesmo sem ninguém conectado" in app.next_detail.get()

    # Another change that leaves the task as it is does not ask again.
    app.v["notify_failures"].set(not app.v["notify_failures"].get())
    assert app._save() is not None
    assert server_task["asked"] == 1 and len(server_task["installs"]) == 1

    # A new time does: the task has to be registered again, with the password.
    app.v["run_at"].set("02:00")
    assert app._save() is not None
    assert server_task["asked"] == 2 and server_task["installs"] == ["senha", "senha"]

    # Saving with nothing changed is how a new Windows password reaches the task.
    assert not app.dirty
    assert app._save() is not None
    assert server_task["asked"] == 3 and len(server_task["installs"]) == 3


def test_without_the_password_the_task_is_left_alone(app, server_task):
    server_task["password"] = None
    app.v["run_logged_off"].set(True)
    assert app._save() is not None
    assert server_task["installs"] == []
    assert "não foi alterado" in server_task["infos"][0][1]


def test_turning_it_off_goes_back_to_the_logged_on_task(app, server_task):
    app.v["run_logged_off"].set(True)
    app._save()
    app.v["run_logged_off"].set(False)
    app._save()
    assert server_task["installs"] == ["senha", None]
    assert "Só com você conectado" in app.next_detail.get()


def test_a_mapped_drive_is_refused_before_asking_the_password(app, server_task, monkeypatch):
    errors = []

    def mapped(path):
        raise gui.scheduler.SchedulerError("unidade de rede Z:")

    monkeypatch.setattr("bimcloud_backup.gui.scheduler.check_destination", mapped)
    monkeypatch.setattr("bimcloud_backup.gui.messagebox.showerror", lambda *a: errors.append(a))
    app.v["run_logged_off"].set(True)
    app._save()
    assert server_task["asked"] == 0 and server_task["installs"] == []
    assert "unidade de rede Z:" in errors[0][1]


def test_the_login_address_can_be_copied_while_waiting(app, saved_config, monkeypatch):
    url = "https://exemplo.bimcloud.com/login?state=abc"
    jobs = []
    monkeypatch.setattr("bimcloud_backup.gui.webbrowser.open", lambda address: None)
    monkeypatch.setattr(app, "_background", lambda work, done: jobs.append((work, done)))
    monkeypatch.setattr(
        "bimcloud_backup.gui.service.sign_in",
        lambda config, store, open_browser: open_browser(url) or "usuario",
    )
    app._login()
    work, done = jobs[0]
    result = work()
    kind, (callback, payload) = app.events.get_nowait()
    callback(payload)
    assert app.login_link.winfo_manager() == "grid"
    app._copy_login_url()
    assert app.root.clipboard_get() == url
    done(result)
    assert app.login_link.winfo_manager() == ""
    assert app.login_url == ""


def test_schedule_lists_show_the_saved_time(app, saved_config):
    assert (app.run_hour.get(), app.run_minute.get()) == ("23", "00")
    assert app.hour_box.cget("values")[:3] == ("00", "01", "02")
    assert len(app.hour_box.cget("values")) == 24
    assert "05" in app.minute_box.cget("values")
    assert (app.v["schedule_every"].get(), app.v["schedule_unit"].get()) == ("1", "days")
    assert app.unit_label.get() == "dia"


def test_choosing_hour_and_minute_saves_hh_mm(app, saved_config):
    app.run_hour.set("08")
    app.run_minute.set("30")
    assert app.v["run_at"].get() == "08:30" and app.dirty
    assert app._save() is not None
    assert 'run_at = "08:30"' in saved_config.read_text(encoding="utf-8")


def test_lists_follow_a_change_and_a_discard(app, saved_config):
    app.v["run_at"].set("22:00")
    assert app.run_hour.get() == "22"
    app._discard()
    assert (app.run_hour.get(), app.run_minute.get()) == ("23", "00")
    assert not app.dirty


def _write_schedule(path, schedule):
    path.write_text(
        '[bimcloud]\nserver_url = "https://exemplo.bimcloud.com"\n'
        '[backup]\ndirectory = "D:/BackupsExemplo"\n'
        f"[schedule]\n{schedule}",
        encoding="utf-8",
    )


def test_a_saved_minute_outside_the_list_shows_and_is_kept(app, saved_config):
    _write_schedule(saved_config, 'run_at = "7:07"\n')
    app._discard()
    assert (app.run_hour.get(), app.run_minute.get()) == ("07", "07")
    minutes = app.minute_box.cget("values")
    assert "07" in minutes and list(minutes) == sorted(minutes, key=int)
    # Only showing the time does not count as a change, and saving keeps it.
    assert not app.dirty
    assert app._save() is not None
    text = saved_config.read_text(encoding="utf-8")
    assert 'run_at = "07:07"' in text
    # Choosing a different minute takes the extra one out of the list again.
    app.run_minute.set("15")
    assert "07" not in app.minute_box.cget("values")


@pytest.mark.parametrize(
    ("old", "every", "label"),
    [
        ('mode = "interval"\ninterval_minutes = 720\n', "12", "horas"),
        ('mode = "interval"\ninterval_minutes = 90\n', "90", "minutos"),
        ('mode = "interval"\ninterval_minutes = 60\n', "1", "hora"),
        ('mode = "daily"\nrun_at = "07:30"\ninterval_minutes = 90\n', "1", "dia"),
    ],
)
def test_an_old_schedule_shows_in_the_most_natural_unit(app, saved_config, old, every, label):
    _write_schedule(saved_config, old)
    app._discard()
    assert (app.v["schedule_every"].get(), app.unit_label.get()) == (every, label)
    assert not app.dirty
    assert app._save() is not None
    text = saved_config.read_text(encoding="utf-8")
    assert f"every = {every}" in text
    assert "mode" not in text and "interval_minutes" not in text


def test_choosing_a_unit_saves_every_and_unit(app, saved_config):
    app.v["schedule_every"].set("2")
    assert app.unit_label.get() == "dias"
    assert list(app.unit_box.cget("values")) == ["minutos", "horas", "dias"]
    app.unit_label.set("horas")
    assert app.v["schedule_unit"].get() == "hours" and app.dirty
    app.v["schedule_every"].set("1")
    assert app.unit_label.get() == "hora"
    assert list(app.unit_box.cget("values")) == ["minuto", "hora", "dia"]
    app.v["schedule_every"].set("12")
    assert app._save() is not None
    text = saved_config.read_text(encoding="utf-8")
    assert "every = 12" in text and 'unit = "hours"' in text


@pytest.mark.parametrize(
    ("every", "unit", "message"),
    [
        ("24", "horas", "em horas, deve estar entre 1 e 23"),
        ("1440", "minutos", "em minutos, deve estar entre 1 e 1439"),
        ("366", "dias", "em dias, deve estar entre 1 e 365"),
        ("0", "dias", "em dias, deve estar entre 1 e 365"),
        ("", "dias", "valor inválido"),
    ],
)
def test_the_form_checks_the_task_scheduler_limits(
    app, saved_config, monkeypatch, every, unit, message
):
    errors = []
    monkeypatch.setattr("bimcloud_backup.gui.messagebox.showerror", lambda _t, m: errors.append(m))
    app.v["schedule_every"].set(every)
    app.unit_label.set(unit)
    try:
        assert app._save() is None
        assert errors and errors[0].startswith("A cada") and message in errors[0]
        assert 'run_at = "23:00"' in saved_config.read_text(encoding="utf-8")
        assert "every" not in saved_config.read_text(encoding="utf-8")
    finally:
        app._discard()


def test_schedule_lists_are_read_only_and_follow_the_unit(app, saved_config):
    app.auto.set(True)
    app.unit_label.set("dia")
    assert str(app.every_entry.cget("state")) == "normal"
    assert str(app.unit_box.cget("state")) == "readonly"
    assert str(app.hour_box.cget("state")) == "readonly"
    assert str(app.minute_box.cget("state")) == "readonly"
    # The time only counts with days.
    app.unit_label.set("hora")
    assert str(app.hour_box.cget("state")) == "disabled"
    assert str(app.minute_box.cget("state")) == "disabled"
    assert str(app.unit_box.cget("state")) == "readonly"
    app.auto.set(False)
    app._sync_enabled()
    for widget in (app.every_entry, app.unit_box, app.hour_box, app.minute_box):
        assert str(widget.cget("state")) == "disabled"
    app._discard()


def test_mouse_wheel_does_not_change_the_schedule(app, saved_config):
    app.auto.set(True)
    app._sync_enabled()
    try:
        for box in (app.hour_box, app.minute_box, app.unit_box):
            box.configure(state="readonly")
            box.event_generate("<MouseWheel>", delta=-120)
            box.event_generate("<MouseWheel>", delta=120)
        assert app.v["run_at"].get() == "23:00"
        assert app.v["schedule_unit"].get() == "days"
    finally:
        app._discard()


@pytest.mark.parametrize(
    ("every", "unit", "text"),
    [
        (1, "days", "Todo dia às 23:00"),
        (2, "days", "A cada 2 dias às 23:00"),
        (1, "hours", "A cada hora"),
        (12, "hours", "A cada 12 horas"),
        (30, "minutes", "A cada 30 minutos"),
    ],
)
def test_describe_schedule(every, unit, text):
    config = Config(
        server_url="https://x.bimcloud.com",
        backup_dir=Path("D:/B"),
        schedule_every=every,
        schedule_unit=unit,
    )
    assert describe_schedule(config) == text


def test_same_schedule_ignores_the_time_without_days():
    base = Config(server_url="https://x.bimcloud.com", backup_dir=Path("D:/B"))
    hours = replace(base, schedule_unit="hours", schedule_every=2)
    assert _same_schedule(hours, replace(hours, run_at=clock(1, 0)))
    assert not _same_schedule(base, replace(base, run_at=clock(1, 0)))
    assert not _same_schedule(base, replace(base, schedule_every=2))
    assert not _same_schedule(hours, replace(hours, schedule_unit="minutes"))


REQUIRED_HELP = (
    "username",
    "projects_format",
    "snapshots",
    "history",
    "latest",
    "every",
    "logged_off",
    "destination",
    "max_duration",
    "min_free_space",
    "client_id",
    "verbose",
    "notify_failures",
    "startup",
)


def test_every_option_that_needs_it_has_a_help_mark(app):
    assert set(REQUIRED_HELP) <= set(app.help_marks)
    for key, mark in app.help_marks.items():
        assert mark.text == HELP[key]
        assert str(mark.label.cget("takefocus")) in ("1", "True")


@pytest.mark.parametrize("key", sorted(HELP))
def test_help_texts_are_short(key):
    text = HELP[key]
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", text) if part]
    assert 1 <= len(sentences) <= 3, text
    assert len(text) <= 240 and text == text.strip() and text.endswith(".")


def _tip_text(mark):
    window = mark.tooltip.window
    return None if window is None else window.winfo_children()[0].cget("text")


def test_help_shows_with_the_mouse(app):
    mark = app.help_marks["snapshots"]
    mark.label.event_generate("<Enter>")
    try:
        assert mark.tooltip._pending is not None
        mark.tooltip.show()
        assert _tip_text(mark) == HELP["snapshots"]
        mark.label.event_generate("<Leave>")
        assert mark.tooltip.window is None and mark.tooltip._pending is None
    finally:
        mark.tooltip.hide()


def test_help_shows_with_the_keyboard_and_esc_hides_it(app):
    mark = app.help_marks["every"]
    try:
        mark.label.event_generate("<FocusIn>")
        assert _tip_text(mark) == HELP["every"]
        assert str(mark.label.cget("foreground")) == app.colors["ink"]
        # Keys only reach the widget with the focus, which a hidden test window never gets.
        assert mark.label.bind("<Escape>")
        mark.label.event_generate("<FocusIn>")
        mark.label.event_generate("<FocusOut>")
        assert mark.tooltip.window is None
        assert str(mark.label.cget("foreground")) == app.colors["accent"]
    finally:
        mark.tooltip.hide()


@pytest.mark.parametrize(
    "close",
    [
        # A click on something that takes no focus, while the "?" still has it.
        lambda app, mark: app.body.event_generate("<ButtonPress-1>"),
        lambda app, mark: app.root.event_generate("<Configure>"),
        lambda app, mark: app.root.event_generate("<Unmap>"),
        lambda app, mark: mark.label.event_generate("<Unmap>"),
    ],
    ids=["click-outside", "window-moved", "window-hidden", "mark-hidden"],
)
def test_a_hint_opened_by_the_keyboard_does_not_stay_open(app, close):
    mark = app.help_marks["every"]
    try:
        mark.label.event_generate("<FocusIn>")
        assert mark.tooltip.window is not None
        close(app, mark)
        assert mark.tooltip.window is None
    finally:
        mark.tooltip.hide()


def test_a_change_inside_the_window_keeps_the_hint(app):
    mark = app.help_marks["every"]
    try:
        mark.label.event_generate("<FocusIn>")
        app.body.event_generate("<Configure>")
        assert mark.tooltip.window is not None
    finally:
        mark.tooltip.hide()


def test_a_hint_does_not_change_the_window_size(app):
    app.root.update_idletasks()
    before = (app.root.winfo_reqwidth(), app.root.winfo_reqheight(), app.root.minsize())
    mark = app.help_marks["logged_off"]
    try:
        mark.tooltip.show()
        app.root.update_idletasks()
        after = (app.root.winfo_reqwidth(), app.root.winfo_reqheight(), app.root.minsize())
        assert after == before
    finally:
        mark.tooltip.hide()


def test_a_hint_near_the_right_edge_stays_on_the_screen(app, monkeypatch):
    mark = app.help_marks["username"]
    screen = mark.label.winfo_screenwidth()
    monkeypatch.setattr(mark.label, "winfo_rootx", lambda: screen - 5)
    try:
        mark.tooltip.show()
        window = mark.tooltip.window
        window.update_idletasks()
        x = int(window.wm_geometry().split("+")[1])
        assert x + window.winfo_reqwidth() <= screen
    finally:
        mark.tooltip.hide()


def test_the_advanced_window_keeps_the_marks_of_the_main_window(app):
    main = app.help_marks["verbose"]
    app._open_advanced()
    try:
        assert app.help_marks["verbose"] is main
        assert main.label.winfo_toplevel() is app.root
    finally:
        app.advanced_window.destroy()
