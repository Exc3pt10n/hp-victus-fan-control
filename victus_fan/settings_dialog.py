"""Einstellungsfenster: der einzige Weg, die Einstellungen zu ändern.

Übergeben werden nur Werte, die sich wirklich geändert haben; ``save_settings``
prüft sie und speichert in settings.json. Der Dienst übernimmt die neuen
Einstellungen automatisch innerhalb weniger Sekunden. Die Messwerte von
Hardware- und Sensor-Test stellen nur die Tests selbst ein.
"""

from __future__ import annotations

import copy
import tkinter as tk
from tkinter import messagebox

from .ui import texts, theme, widgets
from .config import DEFAULTS, DEFAULT_PROFILES, MAX_CRITICAL_TEMP, Config, load_config, reset_settings, save_settings

T_MIN, T_MAX = 30, 100

# (Schlüssel, Sektion, Bezeichnung, Einheit, min, max, Schritt, Erklärung)
CONTROL_FIELDS = [
    ("hysteresis", "control", "Hysterese", "°C", 1, 10, 0.5,
     "So viel kühler muss es sein, bevor die Lüfter wieder langsamer werden."),
    ("spin_down_delay", "control", "Verzögerung beim Herunterregeln", "s", 0, 120, 5,
     "So lange muss der niedrigere Bedarf anliegen, bevor die Lüfter langsamer werden."),
    ("ramp_up_rate", "control", "Tempo beim Hochregeln", "%/s", 1, 50, 1,
     "Wie schnell die Lüfter bei Wärme schneller werden."),
    ("ramp_down_rate", "control", "Tempo beim Herunterregeln", "%/s", 0.5, 20, 0.5,
     "Wie sanft die Lüfter danach wieder langsamer werden."),
    ("min_running_percent", "control", "Anlaufdrehzahl", "%", 0, 50, 5,
     "Laufen die Lüfter, dann mindestens so schnell (20 % ≈ 1100 U/min)."),
    ("interval", "general", "Messintervall", "s", 1, 10, 1,
     "Wie oft Temperaturen gemessen werden."),
]


def _num(value: float):
    return int(value) if float(value).is_integer() else float(value)


class CurveEditor(tk.Canvas):
    """Lüfterkurven von CPU und GPU; Punkte lassen sich mit der Maus verschieben.

    Der erste Punkt jeder Kurve (0 °C) ist nicht sichtbar: er übernimmt
    automatisch den Wert des ersten sichtbaren Punkts ("darunter konstant").
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 40, 26, 14, 26

    def __init__(self, master, on_change):
        C = theme.C
        super().__init__(master, bg=C.CARD, height=250, width=520, highlightthickness=0)
        self.on_change = on_change
        self.curves: dict[str, list[list[float]]] = {"cpu": [], "gpu": []}
        self.drag: tuple[str, int] | None = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)

    def set_curves(self, cpu: list[list[float]], gpu: list[list[float]]) -> None:
        self.curves = {"cpu": cpu, "gpu": gpu}
        self.redraw()

    # ---- Koordinaten -------------------------------------------------
    def _x(self, t: float) -> float:
        w = self.winfo_width()
        return self.PAD_L + (w - self.PAD_L - self.PAD_R) * (max(T_MIN, min(T_MAX, t)) - T_MIN) / (T_MAX - T_MIN)

    def _y(self, p: float) -> float:
        h = self.winfo_height()
        return self.PAD_T + (h - self.PAD_T - self.PAD_B) * (1 - p / 100)

    def _t(self, x: float) -> float:
        w = self.winfo_width()
        return T_MIN + (x - self.PAD_L) / (w - self.PAD_L - self.PAD_R) * (T_MAX - T_MIN)

    def _p(self, y: float) -> float:
        h = self.winfo_height()
        return (1 - (y - self.PAD_T) / (h - self.PAD_T - self.PAD_B)) * 100

    # ---- Zeichnen ----------------------------------------------------
    def redraw(self) -> None:
        C = theme.C
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 100:
            return
        for p in (0, 25, 50, 75, 100):
            y = self._y(p)
            self.create_line(self.PAD_L, y, w - self.PAD_R, y, fill=C.BORDER)
            self.create_text(self.PAD_L - 6, y, text=f"{p} %", anchor="e", fill=C.MUTED, font=(theme.FONT, 8))
        for t in range(T_MIN, T_MAX + 1, 10):
            x = self._x(t)
            self.create_line(x, self.PAD_T, x, h - self.PAD_B, fill=C.BORDER, dash=(1, 3))
            self.create_text(x, h - self.PAD_B + 6, text=f"{t} °C", anchor="n", fill=C.MUTED, font=(theme.FONT, 8))
        for key, color in (("gpu", theme.GPU_COLOR), ("cpu", theme.CPU_COLOR)):
            curve = self.curves.get(key) or []
            pts = curve[1:] if len(curve) > 1 else curve
            if not pts:
                continue
            line = [self.PAD_L, self._y(pts[0][1])]
            for t, p in pts:
                line += [self._x(t), self._y(p)]
            line += [w - self.PAD_R, self._y(pts[-1][1])]
            self.create_line(*line, fill=color, width=2)
            for idx, (t, p) in enumerate(pts, start=1 if len(curve) > 1 else 0):
                x, y = self._x(t), self._y(p)
                active = self.drag == (key, idx)
                r = 6 if active else 5
                self.create_oval(x - r, y - r, x + r, y + r, fill=color, outline=C.CARD, width=2)
                if active:
                    self.create_text(x, y - 12, text=f"{t:.0f} °C · {p:.0f} %", anchor="s",
                                     fill=C.TEXT, font=(theme.FONT, 8, "bold"))

    # ---- Maus --------------------------------------------------------
    def _nearest(self, x: float, y: float) -> tuple[str, int] | None:
        best, dist = None, 12.0
        for key in ("cpu", "gpu"):
            curve = self.curves.get(key) or []
            for idx in range(1 if len(curve) > 1 else 0, len(curve)):
                t, p = curve[idx]
                d = ((self._x(t) - x) ** 2 + (self._y(p) - y) ** 2) ** 0.5
                if d < dist:
                    best, dist = (key, idx), d
        return best

    def _hover(self, event) -> None:
        if self.drag is None:
            self.configure(cursor="fleur" if self._nearest(event.x, event.y) else "")

    def _press(self, event) -> None:
        self.drag = self._nearest(event.x, event.y)
        self.redraw()

    def _move(self, event) -> None:
        if self.drag is None:
            return
        key, idx = self.drag
        curve = self.curves[key]
        # Reihenfolge erhalten: Temperatur zwischen den Nachbarn, Leistung steigend
        t_lo = curve[idx - 1][0] + 1 if idx > 1 else T_MIN
        t_hi = curve[idx + 1][0] - 1 if idx + 1 < len(curve) else T_MAX
        p_lo = curve[idx - 1][1] if idx > 1 else 0
        p_hi = curve[idx + 1][1] if idx + 1 < len(curve) else 100
        t = round(max(t_lo, min(t_hi, self._t(event.x))))
        p = round(max(p_lo, min(p_hi, self._p(event.y))))
        curve[idx] = [t, p]
        if idx == 1:
            curve[0] = [0, p]          # unterhalb des ersten Punkts konstant
        self.redraw()
        self.on_change()

    def _release(self, _event) -> None:
        self.drag = None
        self.redraw()


class SettingsDialog:
    def __init__(self, app):
        C = theme.C
        self.app = app
        self.path = app.config_path
        try:
            self.cfg = load_config(self.path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Einstellungen", f"Die gespeicherten Einstellungen sind beschädigt:\n{exc}\n\n"
                                                    "Angezeigt werden die Standardwerte – „Speichern“ ersetzt "
                                                    "die beschädigten Einstellungen.")
            self.cfg = Config(path=self.path)
        raw = self.cfg.raw
        self.profiles = [n for n in raw["profiles"]]
        self.curves = {n: {"cpu": [list(p) for p in self.cfg.profile(n).cpu_curve],
                           "gpu": [list(p) for p in self.cfg.profile(n).gpu_curve]} for n in self.profiles}
        self.original_curves = copy.deepcopy(self.curves)

        self.win = tk.Toplevel(app.root)
        theme.hide_until_placed(self.win)
        self.win.title("Einstellungen – Victus Lüftersteuerung")
        self.win.configure(bg=C.BG)
        self.win.transient(app.root)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        if C.dark:
            theme._dark_title_bar(self.win)

        outer = tk.Frame(self.win, bg=C.BG)
        outer.pack(fill="both", expand=True, padx=18, pady=14)
        tk.Label(outer, text="Einstellungen", bg=C.BG, fg=C.TEXT, font=(theme.FONT, 14, "bold")).pack(anchor="w")
        self.tabs = widgets.Segmented(outer, self.show_page)
        self.tabs.pack(anchor="w", pady=(8, 10))
        self.tabs.set_options([("general", "Allgemein"), ("curves", "Lüfterkurven"),
                               ("control", "Regelung"), ("updates", "Updates")])
        self.body = widgets.Card(outer)
        self.body.pack(fill="both", expand=True)

        self.pages = {"general": self._page_general(), "curves": self._page_curves(),
                      "control": self._page_control(), "updates": self._page_updates()}

        foot = tk.Frame(outer, bg=C.BG)
        foot.pack(fill="x", pady=(12, 0))
        self._button(foot, "Alles zurücksetzen", self.reset_all).pack(side="left")
        self._button(foot, "Speichern", self.save, primary=True).pack(side="right")
        self._button(foot, "Abbrechen", self.close).pack(side="right", padx=(0, 8))

        # Feste Größe = größte Seite, damit das Fenster beim Umschalten nicht springt
        width = height = 0
        for name in self.pages:
            self.show_page(name)
            width = max(width, self.win.winfo_reqwidth())
            height = max(height, self.win.winfo_reqheight())
        self.win.geometry(f"{width}x{height}")
        self.show_page("general")
        self._center(width, height)
        theme.reveal(self.win)
        self.win.grab_set()
        self.win.focus_force()

    # ---- Bausteine ---------------------------------------------------
    def _button(self, parent, text, command, primary=False):
        C = theme.C
        bg, fg = (C.SELECTED, C.SELECTED_TEXT) if primary else (C.TRACK, C.TEXT)
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg, activebackground=C.HOVER,
                         activeforeground=C.TEXT, relief="flat", bd=0, padx=16, pady=5,
                         font=(theme.FONT, 9, "bold" if primary else "normal"), cursor="hand2")

    def _section(self, parent, title: str, hint: str = ""):
        C = theme.C
        box = tk.Frame(parent, bg=C.CARD)
        box.pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(box, text=title, bg=C.CARD, fg=C.TEXT, font=(theme.FONT, 9, "bold")).pack(anchor="w")
        if hint:
            tk.Label(box, text=hint, bg=C.CARD, fg=C.MUTED, font=(theme.FONT, 8), justify="left",
                     wraplength=520).pack(anchor="w")
        return box

    def _spin(self, parent, value, lo, hi, step):
        C = theme.C
        var = tk.StringVar(value=str(_num(value)))
        spin = tk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=var, width=6,
                          font=(theme.FONT, 9), bg=C.CARD, fg=C.TEXT, buttonbackground=C.TRACK,
                          insertbackground=C.TEXT, relief="flat", highlightthickness=1,
                          highlightbackground=C.BORDER, highlightcolor=C.SELECTED, justify="right")
        var.set(str(_num(value)))
        return spin, var

    def _check(self, parent, text, value):
        C = theme.C
        var = tk.BooleanVar(value=bool(value))
        box = tk.Checkbutton(parent, text=text, variable=var, bg=C.CARD, fg=C.TEXT, selectcolor=C.CARD,
                             activebackground=C.CARD, activeforeground=C.TEXT, font=(theme.FONT, 9),
                             anchor="w", highlightthickness=0, bd=0)
        return box, var

    # ---- Seiten ------------------------------------------------------
    def _page(self):
        return tk.Frame(self.body, bg=theme.C.CARD)

    def _page_general(self):
        page = self._page()
        g = self.cfg.general
        options = [(n, texts.profile_label(n)) for n in self.profiles]
        box = self._section(page, "Lüfterprofil bei „Automatisch“ am Netzteil")
        self.ac_seg = widgets.Segmented(box, lambda v: self.ac_seg.set(v))
        self.ac_seg.set_options(options)
        self.ac_seg.set(g["profile"])
        self.ac_seg.pack(anchor="w", pady=(5, 0))
        box = self._section(page, "… im Akkubetrieb")
        self.bat_seg = widgets.Segmented(box, lambda v: self.bat_seg.set(v))
        self.bat_seg.set_options(options)
        self.bat_seg.set(g["battery_profile"])
        self.bat_seg.pack(anchor="w", pady=(5, 0))
        box = self._section(page, "Notfall: volle Drehzahl ab",
                            "Erreicht CPU oder GPU diese Temperatur, laufen die Lüfter sofort mit voller "
                            "Drehzahl – unabhängig vom Profil.")
        row = tk.Frame(box, bg=theme.C.CARD)
        row.pack(anchor="w", pady=(5, 14))
        spin, self.critical_var = self._spin(row, g["critical_temp"], 70, MAX_CRITICAL_TEMP, 1)
        spin.pack(side="left")
        tk.Label(row, text="°C", bg=theme.C.CARD, fg=theme.C.MUTED, font=(theme.FONT, 9)).pack(side="left", padx=(6, 0))
        box = self._section(page, "Protokoll")
        self.log_box, self.verbose_log = self._check(
            box, "Ausführliches Protokoll (jede Messung, nur zur Fehlersuche)",
            str(g.get("log_level", "INFO")).upper() == "DEBUG")
        self.log_box.pack(anchor="w", pady=(4, 14))
        return page

    def _page_curves(self):
        C = theme.C
        page = self._page()
        box = self._section(page, "Profil", "Punkte mit der Maus verschieben. Links vom ersten Punkt gilt "
                                            "dessen Wert, rechts vom letzten ebenso.")
        top = tk.Frame(box, bg=C.CARD)
        top.pack(fill="x", pady=(6, 0))
        self.curve_seg = widgets.Segmented(top, self.select_curve)
        self.curve_seg.set_options([(n, texts.profile_label(n)) for n in self.profiles])
        self.curve_seg.pack(side="left")
        self._button(top, "Standard", self.reset_curve).pack(side="right")
        legend = tk.Frame(box, bg=C.CARD)
        legend.pack(fill="x", pady=(8, 0))
        for text, color in (("CPU-Temperatur", theme.CPU_COLOR), ("GPU-Temperatur", theme.GPU_COLOR)):
            tk.Label(legend, text="●", bg=C.CARD, fg=color, font=(theme.FONT, 8)).pack(side="left")
            tk.Label(legend, text=text, bg=C.CARD, fg=C.MUTED, font=(theme.FONT, 8)).pack(side="left", padx=(2, 12))
        tk.Label(legend, text="senkrecht: Lüfterleistung", bg=C.CARD, fg=C.MUTED,
                 font=(theme.FONT, 8)).pack(side="right")
        self.editor = CurveEditor(page, self._curve_changed)
        self.editor.pack(fill="x", padx=8, pady=(4, 10))
        start = "balanced" if "balanced" in self.profiles else self.profiles[0]
        self.select_curve(start)
        return page

    def _page_control(self):
        page = self._page()
        self.control_vars = {}
        grid = tk.Frame(page, bg=theme.C.CARD)
        grid.pack(fill="x", padx=14, pady=(12, 6))
        for row, (key, section, label, unit, lo, hi, step, hint) in enumerate(CONTROL_FIELDS):
            tk.Label(grid, text=label, bg=theme.C.CARD, fg=theme.C.TEXT, font=(theme.FONT, 9, "bold")).grid(
                row=row * 2, column=0, sticky="w", pady=(6, 0))
            cell = tk.Frame(grid, bg=theme.C.CARD)
            cell.grid(row=row * 2, column=1, sticky="e", pady=(6, 0))
            spin, var = self._spin(cell, self.cfg.raw[section][key], lo, hi, step)
            spin.pack(side="left")
            tk.Label(cell, text=unit, width=4, anchor="w", bg=theme.C.CARD, fg=theme.C.MUTED,
                     font=(theme.FONT, 9)).pack(side="left", padx=(6, 0))
            tk.Label(grid, text=hint, bg=theme.C.CARD, fg=theme.C.MUTED, font=(theme.FONT, 8)).grid(
                row=row * 2 + 1, column=0, columnspan=2, sticky="w")
            self.control_vars[key] = (section, var, lo, hi, label)
        grid.columnconfigure(0, weight=1)
        bottom = tk.Frame(page, bg=theme.C.CARD)
        bottom.pack(fill="x", padx=14, pady=(4, 12))
        self._button(bottom, "Standardwerte", self.reset_control).pack(side="right")
        return page

    def _page_updates(self):
        page = self._page()
        u = self.cfg.update
        box = self._section(page, "Updates")
        self.upd_box, self.upd_enabled = self._check(box, "Nach Updates suchen (beim Start und alle 6 Stunden)",
                                                     u.get("enabled", True))
        self.upd_box.pack(anchor="w", pady=(6, 0))
        self.chk_box, self.upd_check_only = self._check(box, "Nur anzeigen, nicht automatisch installieren",
                                                        u.get("check_only", False))
        self.chk_box.pack(anchor="w", padx=(20, 0))
        self.upd_box.configure(command=self._sync_update_boxes)
        self._sync_update_boxes()
        tk.Label(box, text="Installiert werden nur freigegebene Versionen (Tags). Deine Einstellungen "
                           "bleiben bei Updates erhalten.", bg=theme.C.CARD, fg=theme.C.MUTED, font=(theme.FONT, 8),
                 wraplength=520, justify="left").pack(anchor="w", pady=(8, 14))
        return page

    def _sync_update_boxes(self) -> None:
        self.chk_box.configure(state="normal" if self.upd_enabled.get() else "disabled")

    # ---- Aktionen ----------------------------------------------------
    def show_page(self, name: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.tabs.set(name)
        self.pages[name].pack(fill="both", expand=True)
        self.win.update_idletasks()
        if name == "curves":
            self.editor.redraw()

    def select_curve(self, name: str) -> None:
        self.curve_name = name
        self.curve_seg.set(name)
        self.editor.set_curves(self.curves[name]["cpu"], self.curves[name]["gpu"])

    def _curve_changed(self) -> None:
        pass  # Änderungen liegen direkt in self.curves; gespeichert wird mit "Speichern"

    def reset_curve(self) -> None:
        name = self.curve_name
        default = DEFAULT_PROFILES.get(name)
        if default is None:
            messagebox.showinfo("Lüfterkurven", "Für eigene Profile gibt es keine Standardkurve.", parent=self.win)
            return
        self.curves[name] = {"cpu": [list(p) for p in default["cpu_curve"]],
                             "gpu": [list(p) for p in default["gpu_curve"]]}
        self.select_curve(name)

    def reset_control(self) -> None:
        for key, (section, var, *_rest) in self.control_vars.items():
            var.set(str(_num(DEFAULTS[section][key])))

    def _collect(self) -> dict[str, dict]:
        raw = self.cfg.raw
        changes: dict[str, dict] = {"general": {}, "control": {}, "update": {}}

        def put(section, key, value):
            if raw[section].get(key) != value:
                changes.setdefault(section, {})[key] = value

        put("general", "profile", self.ac_seg.value)
        put("general", "battery_profile", self.bat_seg.value)
        put("general", "critical_temp", self._read(self.critical_var, 70, MAX_CRITICAL_TEMP, "Notfall-Temperatur"))
        for key, (section, var, lo, hi, label) in self.control_vars.items():
            put(section, key, self._read(var, lo, hi, label))
        put("general", "log_level", "DEBUG" if self.verbose_log.get() else "INFO")
        put("update", "enabled", bool(self.upd_enabled.get()))
        put("update", "check_only", bool(self.upd_check_only.get()))
        for name in self.profiles:
            if self.curves[name] != self.original_curves[name]:
                changes[f"profiles.{name}"] = {
                    "cpu_curve": [[_num(t), _num(p)] for t, p in self.curves[name]["cpu"]],
                    "gpu_curve": [[_num(t), _num(p)] for t, p in self.curves[name]["gpu"]],
                }
        return {k: v for k, v in changes.items() if v}

    @staticmethod
    def _read(var, lo, hi, label) -> float:
        try:
            value = float(str(var.get()).replace(",", "."))
        except ValueError:
            raise ValueError(f"{label}: bitte eine Zahl eingeben") from None
        if not lo <= value <= hi:
            raise ValueError(f"{label}: erlaubt sind {_num(lo)} bis {_num(hi)}")
        return float(value)

    def reset_all(self) -> None:
        if not messagebox.askokcancel(
                "Alles zurücksetzen",
                "Alle Einstellungen und Lüfterkurven auf die Standardwerte zurücksetzen?\n\n"
                "Die Messwerte von Hardware- und Sensor-Test bleiben erhalten.", parent=self.win):
            return
        try:
            reset_settings(self.path)
        except OSError as exc:
            messagebox.showerror("Einstellungen", f"Konnte die Einstellungen nicht speichern:\n{exc}",
                                 parent=self.win)
            return
        self.app._fill_profiles()
        self.close()

    def save(self) -> None:
        try:
            changes = self._collect()
            if changes:
                save_settings(self.path, changes)
        except ValueError as exc:
            messagebox.showerror("Einstellungen", str(exc), parent=self.win)
            return
        except OSError as exc:
            messagebox.showerror("Einstellungen", f"Konnte die Einstellungen nicht speichern:\n{exc}",
                                 parent=self.win)
            return
        self.app._fill_profiles()
        self.close()

    def close(self) -> None:
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        self.win.destroy()
        self.app.settings = None

    def _center(self, width: int, height: int) -> None:
        root = self.app.root
        x = root.winfo_rootx() + (root.winfo_width() - width) // 2
        y = root.winfo_rooty() + (root.winfo_height() - height) // 2
        self.win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
