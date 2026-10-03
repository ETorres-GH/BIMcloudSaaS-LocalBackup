"""Window to choose what to copy from BIMcloud: folders, projects and libraries, in a tree.

The whole tree is loaded once, in the background; only opened folders go into the widget.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import Any

from bimcloud_backup.config import Selection, normalize_selection
from bimcloud_backup.redaction import redact
from bimcloud_backup.service import KIND_LIBRARY, KIND_PROJECT, FolderListing

CHECKED = "☑"
UNCHECKED = "☐"
LOADING = "\0carregando"
# Tree ids of projects and libraries; folders use their path, which never starts with this.
ITEM = "\0item:"
EVERYTHING = "Nada marcado: o BIMcloud inteiro será copiado."
NOT_COUNTED = "abra para contar"
KIND_LABELS = {KIND_PROJECT: "projeto", KIND_LIBRARY: "biblioteca"}

Loader = Callable[[str], FolderListing]
Runner = Callable[[Callable[[], Any], Callable[[Any], None]], None]


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


# With a few short paths chosen, the paths themselves say more than "1 pasta, 1 projeto".
# Past these limits the text would widen the main window, so the counts are shown instead.
PATHS_SHOWN = 3
PATHS_TEXT_LIMIT = 48


def describe_selection(selection: Selection, paths: bool = False) -> str:
    """Short text for what is chosen: the counts, or (`paths`) the paths when they are few."""
    if selection.everything:
        return EVERYTHING
    chosen = [*selection.folders, *selection.projects, *selection.libraries]
    if paths and len(chosen) <= PATHS_SHOWN:
        text = ", ".join(chosen)
        if len(text) <= PATHS_TEXT_LIMIT:
            return text
    parts = []
    if selection.folders:
        parts.append(_plural(len(selection.folders), "pasta", "pastas"))
    if selection.projects:
        parts.append(_plural(len(selection.projects), "projeto", "projetos"))
    if selection.libraries:
        parts.append(_plural(len(selection.libraries), "biblioteca", "bibliotecas"))
    return ", ".join(parts)


def _counts(listing: FolderListing) -> str:
    if not listing.projects and not listing.libraries:
        return "nenhum"
    parts = []
    if listing.projects:
        parts.append(_plural(listing.projects, "projeto", "projetos"))
    if listing.libraries:
        parts.append(_plural(listing.libraries, "biblioteca", "bibliotecas"))
    return " · ".join(parts)


class FolderPicker:
    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        selected: Selection,
        load: Loader,
        run: Runner,
        on_done: Callable[[Selection], None],
        on_close: Callable[[], None] = lambda: None,
        muted: str = "#6b6b6b",
    ):
        selected = normalize_selection(selected.folders, selected.projects, selected.libraries)
        self.folders: set[str] = set(selected.folders)
        # Chosen projects and libraries: path -> kind.
        self.items: dict[str, str] = {
            **dict.fromkeys(selected.projects, KIND_PROJECT),
            **dict.fromkeys(selected.libraries, KIND_LIBRARY),
        }
        self._load = load
        self._run = run
        self._on_done = on_done
        self._on_close = on_close
        self._names: dict[str, str] = {}
        # Tree id -> (name, path, kind) of every project and library shown.
        self._items: dict[str, tuple[str, str, str]] = {}
        self._loading: set[str] = set()

        self.window = tk.Toplevel(parent)
        self.window.title(title)
        # Sizes in pixels at 100%, grown with the Windows scale (like the main window).
        scale = max(1.0, float(self.window.tk.call("tk", "scaling")) * 72 / 96)

        def px(pixels: int) -> int:
            return round(pixels * scale)

        self.window.geometry(f"{px(560)}x{px(520)}")
        self.window.minsize(px(420), px(360))
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        body = ttk.Frame(self.window, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(
            body,
            text="Marque pastas inteiras ou só alguns projetos e bibliotecas. Marcar uma pasta "
            "inclui tudo o que está dentro dela.",
            wraplength=px(520),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.tree = ttk.Treeview(body, columns=("content",), selectmode="browse")
        self.tree.heading("#0", text="Pasta, projeto ou biblioteca")
        self.tree.heading("content", text="Projetos e bibliotecas")
        self.tree.column("#0", width=px(330), stretch=True)
        self.tree.column("content", width=px(170), stretch=False)
        self.tree.tag_configure("inherited", foreground=muted)
        self.tree.tag_configure("info", foreground=muted)
        # A folder with something chosen inside it, but not the whole folder, is in bold: the
        # theme has no "half checked" box as wide as the others.
        bold = tkfont.Font(font=ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
        bold.configure(weight="bold")
        self._bold = bold
        self.tree.tag_configure("partial", font=bold)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)

        self.summary = tk.StringVar()
        ttk.Label(body, textvariable=self.summary, foreground=muted, wraplength=px(520)).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Limpar", command=self.clear).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Cancelar", command=self.cancel).pack(side="left", padx=(0, 8))
        ttk.Button(
            buttons, text="Usar esta seleção", style="Accent.TButton", command=self.confirm
        ).pack(side="left")

        self._update_summary()
        self._insert_loading("")
        self._fetch("")

    @property
    def selection(self) -> Selection:
        return normalize_selection(
            tuple(self.folders),
            tuple(p for p, kind in self.items.items() if kind == KIND_PROJECT),
            tuple(p for p, kind in self.items.items() if kind == KIND_LIBRARY),
        )

    # ------------------------------------------------------------- loading

    def _insert_loading(self, parent: str) -> None:
        text = "Carregando pastas, projetos e bibliotecas do BIMcloud..." if not parent else "..."
        self.tree.insert(parent, "end", iid=parent + LOADING, text=text, tags=("info",))

    def _fetch(self, path: str) -> None:
        def done(result: Any) -> None:
            self._loading.discard(path)
            if not self.window.winfo_exists():
                return
            if isinstance(result, Exception):
                self._failed(path, result)
            else:
                self._fill(path, result)

        self._run(lambda: self._load(path), done)

    def _fill(self, path: str, listing: FolderListing) -> None:
        if self.tree.exists(path + LOADING):
            self.tree.delete(path + LOADING)
        if path:
            self.tree.set(path, "content", _counts(listing))
        if not path and not listing.folders and not listing.items:
            self.tree.insert("", "end", text="Nada no BIMcloud", tags=("info",))
        for name, child in listing.folders:
            self._names[child] = name
            counts = listing.totals.get(child)
            content = _counts(FolderListing([], *counts)) if counts else NOT_COUNTED
            self.tree.insert(path, "end", iid=child, text=self._label(child), values=(content,))
            self._insert_loading(child)
        for name, item_path, kind in listing.items:
            iid = ITEM + item_path
            self._items[iid] = (name, item_path, kind)
            self.tree.insert(
                path, "end", iid=iid, text=self._label(iid), values=(KIND_LABELS[kind],)
            )
        self._refresh_labels()

    def _failed(self, path: str, error: Exception) -> None:
        if self.tree.exists(path + LOADING):
            self.tree.item(path + LOADING, text="Não foi possível carregar: feche e abra a pasta")
        if path and self.tree.exists(path):
            # Closed again, so opening it once more retries the load.
            self.tree.item(path, open=False)
        messagebox.showerror(
            self.window.title(),
            f"Não foi possível listar as pastas do BIMcloud:\n{redact(str(error))}",
            parent=self.window,
        )
        if not path:
            self.cancel()

    def _on_open(self, _event: Any = None) -> None:
        path = self.tree.focus()
        if path and self.tree.exists(path + LOADING) and path not in self._loading:
            self._loading.add(path)
            self._fetch(path)

    # ------------------------------------------------------------ checking

    def _on_click(self, event: Any) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "tree":
            return None
        if "indicator" in self.tree.identify_element(event.x, event.y):
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.focus(iid)
            self.tree.selection_set(iid)
            self.toggle(iid)
        return "break"

    def _on_space(self, _event: Any = None) -> str:
        iid = self.tree.focus()
        if iid:
            self.toggle(iid)
        return "break"

    def toggle(self, iid: str) -> None:
        """Mark or unmark a folder (by its path) or a project or library (by its tree id)."""
        if iid in self._items:
            _, path, kind = self._items[iid]
            if self._inherited(path):
                return
            if path in self.items:
                del self.items[path]
            else:
                self.items[path] = kind
        elif iid in self._names and not self._inherited(iid):
            if iid in self.folders:
                self.folders.discard(iid)
            else:
                # A chosen folder already includes everything below it.
                self.folders = {p for p in self.folders if not p.startswith(iid + "/")}
                self.items = {p: k for p, k in self.items.items() if not p.startswith(iid + "/")}
                self.folders.add(iid)
        else:
            return
        self._refresh_labels()

    def clear(self) -> None:
        self.folders.clear()
        self.items.clear()
        self._refresh_labels()

    def _inherited(self, path: str) -> bool:
        return any(path.startswith(p + "/") for p in self.folders)

    def _partial(self, path: str) -> bool:
        prefix = path + "/"
        return any(p.startswith(prefix) for p in (*self.folders, *self.items))

    def _label(self, iid: str) -> str:
        if iid in self._items:
            name, path, _ = self._items[iid]
            chosen = path in self.items or self._inherited(path)
            return f"{CHECKED if chosen else UNCHECKED}  {name}"
        mark = CHECKED if iid in self.folders or self._inherited(iid) else UNCHECKED
        return f"{mark}  {self._names[iid]}"

    def _refresh_labels(self) -> None:
        for iid in (*self._names, *self._items):
            if self.tree.exists(iid):
                path = self._items[iid][1] if iid in self._items else iid
                if self._inherited(path):
                    tags: tuple[str, ...] = ("inherited",)
                elif iid in self._names and iid not in self.folders and self._partial(iid):
                    tags = ("partial",)
                else:
                    tags = ()
                self.tree.item(iid, text=self._label(iid), tags=tags)
        self._update_summary()

    def _update_summary(self) -> None:
        selection = self.selection
        text = describe_selection(selection)
        self.summary.set(text if selection.everything else f"Marcados: {text}")

    # ------------------------------------------------------------- closing

    def confirm(self) -> None:
        self._on_done(self.selection)
        self._close()

    def cancel(self) -> None:
        self._close()

    def _close(self) -> None:
        self._on_close()
        if self.window.winfo_exists():
            self.window.destroy()
