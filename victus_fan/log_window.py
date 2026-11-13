"""Protokollfenster: zeigt victus-fan.log und update.log lesbar an.

Die Zeilen werden zerlegt (Zeit, Stufe, Meldung), Warnungen und Fehler
farbig markiert. Neue Einträge erscheinen automatisch; steht die Ansicht am
Ende, scrollt sie mit. Mehrzeilige Einträge (z. B. Tracebacks) bleiben zusammen.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

from .ui import theme, widgets
from . import paths
from .logparse import Entry, filter_entries, parse_log, read_tail

REFRESH_MS = 2000
# Standardgröße; auf kleinen Bildschirmen wird sie begrenzt
WIDTH, HEIGHT = 1100, 760


class LogWindow:
    def __init__(self, app):
        C = theme.C
        self.app = app
        self.sources = {"service": ("Lüftersteuerung", paths.LOG_FILE),
                        "update": ("Updates", paths.UPDATE_LOG)}
        self.source = "service"
        self._signature = None
        self.entries: list[Entry] = []

        self.win = tk.Toplevel(app.root)
        theme.hide_until_placed(self.win)
        self.win.title("Protokoll – Victus Lüftersteuerung")
        self.win.configure(bg=C.BG)
        self.win.minsize(640, 420)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        if C.dark:
            theme._dark_title_bar(self.win)

        outer = tk.Frame(self.win, bg=C.BG)
        outer.pack(fill="both", expand=True, padx=18, pady=14)
        tk.Label(outer, text="Protokoll", bg=C.BG, fg=C.TEXT, font=(theme.FONT, 14, "bold")).pack(anchor="w")

        bar = tk.Frame(outer, bg=C.BG)
        bar.pack(fill="x", pady=(8, 8))
        self.source_seg = widgets.Segmented(bar, self.set_source)
        self.source_seg.set_options([(k, v[0]) for k, v in self.sources.items()])
        self.source_seg.set(self.source)
        self.source_seg.pack(side="left")
        self.level_seg = widgets.Segmented(bar, self.set_level)
        self.level_seg.set_options([("all", "Alle"), ("important", "Warnungen & Fehler")])
        self.level_seg.set("all")
        self.level_seg.pack(side="left", padx=(10, 0))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.render())
        search = tk.Entry(bar, textvariable=self.search_var, width=18, font=(theme.FONT, 9), bg=C.CARD, fg=C.TEXT,
                          insertbackground=C.TEXT, relief="flat", highlightthickness=1,
                          highlightbackground=C.BORDER, highlightcolor=C.SELECTED)
        search.pack(side="right", ipady=3)
        tk.Label(bar, text="Suchen", bg=C.BG, fg=C.MUTED, font=(theme.FONT, 9)).pack(side="right", padx=(0, 6))

        card = widgets.Card(outer)
        card.pack(fill="both", expand=True)
        self.text = tk.Text(card, wrap="word", font=("Consolas", 9), bg=C.CARD, fg=C.TEXT, relief="flat",
                            padx=10, pady=8, highlightthickness=0, insertbackground=C.TEXT,
                            selectbackground=C.SELECTED, selectforeground=C.SELECTED_TEXT)
        scroll = tk.Scrollbar(card, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("time", foreground=C.MUTED)
        self.text.tag_configure("Warnung", foreground=theme.WARN)
        self.text.tag_configure("Fehler", foreground=theme.HOT)
        self.text.tag_configure("Debug", foreground=C.MUTED)
        self.text.tag_configure("level", font=("Consolas", 9, "bold"))
        self.text.tag_configure("match", background=theme.WARN, foreground="#ffffff")
        # Hängender Einzug: umbrochene Zeilen beginnen unter der Meldung, nicht unter der Uhrzeit
        indent = tkfont.Font(family="Consolas", size=9).measure("12.11. 00:00:00  Warnung  ")
        self.text.tag_configure("entry", lmargin1=0, lmargin2=indent)
        self.text.configure(state="disabled")

        foot = tk.Frame(outer, bg=C.BG)
        foot.pack(fill="x", pady=(10, 0))
        # Buttons zuerst packen, damit sie immer sichtbar bleiben
        self._button(foot, "Schließen", self.close).pack(side="right")
        self._button(foot, "Als Textdatei öffnen", self.open_file).pack(side="right", padx=(0, 8))
        self._button(foot, "Kopieren", self.copy).pack(side="right", padx=(0, 8))
        self.count_label = tk.Label(foot, text="", bg=C.BG, fg=C.MUTED, font=(theme.FONT, 8), anchor="w")
        self.count_label.pack(side="left", fill="x", expand=True)

        self.refresh(force=True)
        self._center()
        theme.reveal(self.win)
        self.text.see("end")
        self._tick()

    # ---- Bausteine ---------------------------------------------------
    def _button(self, parent, text, command):
        C = theme.C
        return tk.Button(parent, text=text, command=command, bg=C.TRACK, fg=C.TEXT, activebackground=C.HOVER,
                         activeforeground=C.TEXT, relief="flat", bd=0, padx=14, pady=5,
                         font=(theme.FONT, 9), cursor="hand2")

    def _center(self) -> None:
        """Groß und mittig auf dem Bildschirm, aber nie größer als dieser."""
        self.win.update_idletasks()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w = min(WIDTH, sw - 80)
        h = min(HEIGHT, sh - 120)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)   # etwas nach oben, wegen der Taskleiste
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    # ---- Daten -------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.sources[self.source][1]

    def refresh(self, force: bool = False) -> None:
        try:
            st = self.path.stat()
            signature = (st.st_size, st.st_mtime)
        except OSError:
            signature = None
        if not force and signature == self._signature:
            return
        self._signature = signature
        self.entries = parse_log(read_tail(self.path))
        self.render()

    def render(self) -> None:
        C = theme.C
        at_end = self.text.yview()[1] >= 0.999
        shown = filter_entries(self.entries, self.level_seg.value == "important", self.search_var.get())
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if not self.entries:
            self.text.insert("end", "Noch keine Einträge.", "time")
        elif not shown:
            self.text.insert("end", "Keine passenden Einträge.", "time")
        for e in shown:
            self.text.insert("end", f"{e.date} {e.time}  ", ("entry", "time"))
            self.text.insert("end", f"{e.level:<7}  ", ("entry", "level", e.level))
            self.text.insert("end", e.message + "\n", ("entry", e.level) if e.level != "Info" else ("entry",))
        needle = self.search_var.get().strip()
        if needle:
            start = "1.0"
            while True:
                pos = self.text.search(needle, start, stopindex="end", nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(needle)}c"
                self.text.tag_add("match", pos, end)
                start = end
        self.text.configure(state="disabled")
        if at_end or needle:
            self.text.see("end")
        warnings = sum(1 for e in self.entries if e.important)
        self.count_label.configure(
            text=f"{len(shown)} von {len(self.entries)} Einträgen · {warnings} Warnungen/Fehler",
            fg=C.MUTED)

    def _tick(self) -> None:
        if not self.win.winfo_exists():
            return
        self.refresh()
        self._after = self.win.after(REFRESH_MS, self._tick)

    # ---- Aktionen ----------------------------------------------------
    def set_source(self, key: str) -> None:
        self.source = key
        self.source_seg.set(key)
        self.refresh(force=True)
        self.text.see("end")

    def set_level(self, key: str) -> None:
        self.level_seg.set(key)
        self.render()

    def copy(self) -> None:
        shown = filter_entries(self.entries, self.level_seg.value == "important", self.search_var.get())
        self.win.clipboard_clear()
        self.win.clipboard_append("\n".join(e.text() for e in shown))
        self.count_label.configure(text=f"{len(shown)} Einträge in die Zwischenablage kopiert", fg=theme.OK)

    def open_file(self) -> None:
        if self.path.exists():
            self.app._open(self.path)

    def close(self) -> None:
        try:
            self.win.after_cancel(self._after)
        except (AttributeError, tk.TclError):
            pass
        self.win.destroy()
        self.app.log_window = None
