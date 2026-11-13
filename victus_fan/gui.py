"""Tray-Icon und Steuerfenster.

Die Oberfläche läuft im Benutzerkontext und spricht nicht direkt mit der
Hardware: Sie liest ``status.json`` des Dienstes und schreibt Wünsche
(Lüfter, Steuerung an/aus) nach ``override.json``.

Bausteine (Farben, Texte, Symbole, Widgets, Windows-Helfer) liegen in
``victus_fan.ui``; hier ist nur das Hauptfenster mit dem Tray-Menü.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

from . import __version__, autostart, paths
from .config import HARDWARE_TEST_VERSION, load_config
from .state import read_override, read_status, write_override
from .ui.icons import make_fan_image, make_tray_image
from .ui.system import (SingleInstance, open_file, restart_service, run_elevated_console,
                        run_selftest, start_update_check)
from .ui.texts import (FAN_HINTS, MAX_LABEL, PAUSED_HINT, fan_choice, profile_label, tray_tooltip,
                       update_summary)
from .ui.theme import (CPU_COLOR, FAN_COLOR, FONT, GPU_COLOR, HOT, OFF, OK, WARN, C, apply_theme,
                       fmt_temp, temp_color, temp_word, windows_uses_dark_mode, _dark_title_bar)
from .ui.widgets import HISTORY_SECONDS, Card, Chart, Metric, Segmented, Switch
from .updater import installed as read_installed, read_json

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Fenster
# --------------------------------------------------------------------------
class App:
    POLL_MS = 1000

    def __init__(self, show: bool, config_path: str | None, instance: SingleInstance):
        apply_theme(windows_uses_dark_mode())

        self.config_path = Path(config_path) if config_path else paths.CONFIG_FILE
        self._hw_check: tuple[float | None, bool] = (None, False)   # (mtime, Hardware-Test nötig?)
        self.instance = instance
        self.history: deque = deque(maxlen=HISTORY_SECONDS // 2 + 10)
        self.last_ts = None
        self.status: dict | None = None
        self.calls: queue.Queue = queue.Queue()
        self.tray = None
        self.settings = None             # offenes Einstellungsfenster
        self.log_window = None           # offenes Protokollfenster
        self._install_mtime = self._installed_mtime()
        self._relaunch_pending = False
        self._warning_shown = False

        self.root = tk.Tk()
        self.root.title("Victus Lüftersteuerung")
        self.root.configure(bg=C.BG)
        self.root.minsize(680, 550)   # feste Breite: das Fenster springt nicht mit der Textlänge
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            from PIL import ImageTk

            self._icon = ImageTk.PhotoImage(make_fan_image(64))
            self.root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001
            pass
        if C.dark:
            _dark_title_bar(self.root)

        autostart.sync()          # Autostart-Eintrag an die gemerkte Wahl angleichen
        self._build()
        self._start_tray()
        if not show and self.tray is not None:
            self.root.withdraw()
        self.poll(reschedule=False)     # Inhalte einmal zeichnen, damit die Größe stimmt
        if self.root.state() != "withdrawn":
            self._center()
        self.root.after(self.POLL_MS, self.poll)

    # ---- Aufbau -----------------------------------------------------------
    def _profiles(self) -> list[str]:
        try:
            names = list(load_config(self.config_path).raw["profiles"])
        except Exception:  # noqa: BLE001 - kaputte Konfiguration: Standardprofile zeigen
            names = ["quiet", "balanced", "performance"]
        return ["auto"] + names

    def _label(self, parent, text, size=9, color=None, bold=False, bg=None, **kw):
        return tk.Label(parent, text=text, bg=bg or C.BG, fg=color or C.TEXT,
                        font=(FONT, size, "bold") if bold else (FONT, size), **kw)

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=C.BG)
        outer.pack(fill="both", expand=True, padx=18, pady=14)

        # Kopf: Titel + verständliche Statuszeile
        head = tk.Frame(outer, bg=C.BG)
        head.pack(fill="x")
        top = tk.Frame(head, bg=C.BG)
        top.pack(fill="x")
        self._label(top, "Victus Lüftersteuerung", 14, bold=True).pack(side="left")
        # Hauptschalter: aus = das BIOS regelt die Lüfter (früher Steuerung "BIOS")
        self.control_switch = Switch(top, self.set_control, C.BG)
        self.control_switch.pack(side="right", pady=(4, 0))
        self.control_label = self._label(top, "Steuerung an", 9, C.MUTED)
        self.control_label.pack(side="right", padx=(0, 8), pady=(4, 0))
        self._resume_mode = "normal"
        line = tk.Frame(head, bg=C.BG)
        line.pack(fill="x", pady=(2, 0))
        self.state_dot = self._label(line, "●", 9, OFF)
        self.state_dot.pack(side="left")
        self.state_text = self._label(line, "Verbinde …", 9, C.MUTED, wraplength=520, justify="left")
        self.state_text.pack(side="left", padx=(4, 0))

        # Warnhinweis (nur bei Bedarf sichtbar)
        self.banner = tk.Frame(outer, bg=C.BANNER_BG, highlightbackground=C.BANNER_BORDER, highlightthickness=1)
        self.banner_text = tk.Label(self.banner, text="", bg=C.BANNER_BG, fg=C.BANNER_FG, font=(FONT, 9),
                                    justify="left", anchor="w", wraplength=420)
        self.banner_text.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        # Aktion passend zum Hinweis (Hardware-Test bzw. Konfiguration öffnen)
        self.banner_action = tk.Label(self.banner, text="", bg=C.BANNER_BG, fg=C.BANNER_FG, cursor="hand2",
                                      font=(FONT, 9, "underline"))
        self.banner_action.pack(side="right", padx=10)
        self._banner_command = None
        self.banner_action.bind("<Button-1>", lambda _e: self._banner_command and self._banner_command())
        self._banner_anchor = tk.Frame(outer, bg=C.BG, height=1)
        self._banner_anchor.pack(fill="x", pady=(10, 0))

        # Kennzahlen: Temperaturen und echte Lüfterdrehzahlen
        grid = tk.Frame(outer, bg=C.BG)
        grid.pack(fill="x")
        self.cpu_card = Metric(grid, "CPU", CPU_COLOR)
        self.gpu_card = Metric(grid, "GPU", GPU_COLOR)
        self.fan1_card = Metric(grid, "CPU-Lüfter", FAN_COLOR, bar=True)
        self.fan2_card = Metric(grid, "GPU-Lüfter", FAN_COLOR, bar=True)
        for i, card in enumerate((self.cpu_card, self.gpu_card, self.fan1_card, self.fan2_card)):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            grid.columnconfigure(i, weight=1, uniform="metrics")

        # Verlauf
        chart_card = Card(outer)
        chart_card.pack(fill="x", pady=(10, 0))
        top = tk.Frame(chart_card, bg=C.CARD)
        top.pack(fill="x", padx=12, pady=(8, 0))
        self._label(top, "Verlauf", 9, bold=True, bg=C.CARD).pack(side="left")
        for text, color in (("Lüfter %", FAN_COLOR), ("GPU °C", GPU_COLOR), ("CPU °C", CPU_COLOR)):
            self._label(top, text, 8, C.MUTED, bg=C.CARD).pack(side="right", padx=(2, 10))
            self._label(top, "●", 8, color, bg=C.CARD).pack(side="right")
        self.chart = Chart(chart_card, self.history)
        self.chart.pack(fill="x", padx=6, pady=(2, 6))

        # Einstellung: wie arbeiten die Lüfter (mit Erklärung)
        settings = Card(outer)
        settings.pack(fill="x", pady=(10, 0))
        self.fan_seg, self.fan_hint = self._setting(settings, "Lüfter", self.set_fan, last=True)
        self._fill_profiles()

        # Fußzeile: Version/Update links, Extras rechts
        foot = tk.Frame(outer, bg=C.BG)
        foot.pack(fill="x", side="bottom", pady=(10, 0))
        self.update_label = self._label(foot, "", 8, C.MUTED)
        self.update_label.pack(side="left")
        extras = tk.Menubutton(foot, text="Extras  ▾", bg=C.TRACK, fg=C.TEXT, activebackground=C.HOVER,
                               activeforeground=C.TEXT, relief="flat", font=(FONT, 9), padx=12, pady=4,
                               cursor="hand2", direction="above")
        menu = tk.Menu(extras, tearoff=False, bg=C.CARD, fg=C.TEXT, activebackground=C.SELECTED,
                       activeforeground=C.SELECTED_TEXT, font=(FONT, 9))
        menu.add_command(label="Einstellungen …", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="Nach Updates suchen", command=self.check_updates)
        menu.add_separator()
        menu.add_command(label="Hardware-Test …", command=self.selftest)
        menu.add_command(label="Temperatursensoren testen …", command=self.sensor_test)
        menu.add_separator()
        menu.add_command(label="Protokoll …", command=self.open_log)
        menu.add_command(label="Dienst neu starten", command=self.restart)
        menu.add_separator()
        self.autostart_var = tk.BooleanVar(value=bool(autostart.is_enabled()))
        menu.add_checkbutton(label="Mit Windows starten", variable=self.autostart_var,
                             command=lambda: self.set_autostart(self.autostart_var.get()),
                             selectcolor=C.TEXT,
                             state="normal" if autostart.is_enabled() is not None else "disabled")
        extras.config(menu=menu)
        extras.pack(side="right")

    def _setting(self, parent, title: str, command, last: bool = False):
        box = tk.Frame(parent, bg=C.CARD)
        box.pack(fill="x", padx=12, pady=(10, 10 if last else 0))
        self._label(box, title, 9, bold=True, bg=C.CARD).pack(anchor="w")
        seg = Segmented(box, command)
        seg.pack(anchor="w", pady=(5, 3))
        hint = self._label(box, "", 8, C.MUTED, bg=C.CARD, anchor="w", justify="left", wraplength=520)
        hint.pack(fill="x")
        return seg, hint

    def _fan_options(self) -> list[tuple[str, str]]:
        return [(n, profile_label(n)) for n in self._profiles()] + [("max", MAX_LABEL)]

    def _fill_profiles(self) -> None:
        self.fan_seg.set_options(self._fan_options())

    # ---- Tray -------------------------------------------------------------
    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001
            log.warning("Kein Tray-Icon möglich: %s", exc)
            return

        def call(fn, *args):
            return lambda _icon=None, _item=None: self.calls.put((fn, args))

        def fan_items():
            return [pystray.MenuItem(label, call(self.set_fan, value), radio=True,
                                     checked=lambda _i, v=value: fan_choice(read_override()) == v,
                                     enabled=lambda _i: read_override()["mode"] != "paused")
                    for value, label in self._fan_options()]

        menu = pystray.Menu(
            pystray.MenuItem("Fenster öffnen", call(self.show), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Lüfter", pystray.Menu(fan_items)),
            pystray.MenuItem("Lüftersteuerung an", call(self.toggle_control),
                             checked=lambda _i: read_override()["mode"] != "paused"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Extras", pystray.Menu(
                pystray.MenuItem("Einstellungen …", call(self.open_settings)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Nach Updates suchen", call(self.check_updates)),
                pystray.MenuItem("Hardware-Test …", call(self.selftest)),
                pystray.MenuItem("Temperatursensoren testen …", call(self.sensor_test)),
                pystray.MenuItem("Protokoll …", call(self.open_log)),
                pystray.MenuItem("Dienst neu starten", call(self.restart)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Mit Windows starten", call(self.toggle_autostart),
                                 checked=lambda _i: bool(autostart.is_enabled())),
            )),
            pystray.MenuItem("Tray-Icon beenden", call(self.quit)),
        )
        try:
            self.tray = pystray.Icon("victus-fan", make_tray_image(None, "normal", False),
                                     "Victus Lüftersteuerung", menu)
            self.tray.run_detached()
        except Exception as exc:  # noqa: BLE001
            log.warning("Tray-Icon konnte nicht gestartet werden: %s", exc)
            self.tray = None

    # ---- Aktionen ---------------------------------------------------------
    def _write(self, **changes) -> None:
        try:
            write_override(**changes)
        except OSError as exc:
            messagebox.showerror("Victus Lüftersteuerung",
                                 f"Einstellung konnte nicht gespeichert werden:\n{exc}\n\n"
                                 "Bitte den Installer erneut ausführen.")
        self.poll(reschedule=False)

    def set_fan(self, value: str) -> None:
        """Eine Skala: Profil wählen oder "max" (volle Drehzahl)."""
        if value == "max":
            self._write(mode="max")
        else:
            self._write(profile=value, mode="normal")

    def set_control(self, on: bool) -> None:
        """Hauptschalter: aus = Steuerung pausiert, das BIOS regelt."""
        self._write(mode=self._resume_mode if on else "paused")

    def toggle_control(self) -> None:
        self.set_control(read_override()["mode"] == "paused")

    def open_settings(self) -> None:
        """Einstellungsfenster öffnen (oder nach vorn holen, falls schon offen)."""
        from .settings_dialog import SettingsDialog

        self.show()
        if self.settings is not None:
            self.settings.win.lift()
            self.settings.win.focus_force()
            return
        try:
            self.settings = SettingsDialog(self)
        except Exception:  # noqa: BLE001 - Fehler wurde im Dialog bereits gemeldet
            self.settings = None

    def open_log(self) -> None:
        """Protokollfenster öffnen (oder nach vorn holen, falls schon offen)."""
        from .log_window import LogWindow

        self.show()
        if self.log_window is not None:
            self.log_window.win.deiconify()
            self.log_window.win.lift()
            self.log_window.win.focus_force()
            return
        self.log_window = LogWindow(self)

    def _open(self, path: Path) -> None:
        try:
            open_file(path)
        except OSError as exc:
            messagebox.showwarning("Victus Lüftersteuerung", str(exc))

    def selftest(self) -> None:
        if not messagebox.askokcancel(
                "Hardware-Test",
                "Der Test prüft etwa 1,5 Minuten lang, welche Lüfterbefehle dein Notebook wirklich "
                "umsetzt. Die Lüfter werden dabei hörbar schneller und langsamer.\n\n"
                "Das Ergebnis wird automatisch übernommen. Bitte die Administrator-Abfrage bestätigen."):
            return
        try:
            run_selftest(self.config_path)
        except OSError as exc:
            messagebox.showwarning("Hardware-Test", str(exc))

    def sensor_test(self) -> None:
        if not messagebox.askokcancel(
                "Temperatursensoren testen",
                "Der Test listet alle Temperaturquellen auf und erzeugt dann etwa 20 Sekunden volle "
                "CPU-Last, um zu sehen, welcher Sensor die CPU am besten abbildet.\n\n"
                "Der beste Sensor wird automatisch übernommen. Bitte die Administrator-Abfrage bestätigen."):
            return
        try:
            run_elevated_console(self.config_path, "sensors --apply --wait")
        except OSError as exc:
            messagebox.showwarning("Temperatursensoren", str(exc))

    def restart(self) -> None:
        try:
            restart_service()
        except OSError as exc:
            messagebox.showwarning("Victus Lüftersteuerung", str(exc))

    def check_updates(self) -> None:
        try:
            start_update_check()
        except OSError as exc:
            messagebox.showwarning("Victus Lüftersteuerung", str(exc))

    def set_autostart(self, enabled: bool) -> None:
        try:
            autostart.set_enabled(enabled)
        except OSError as exc:
            messagebox.showwarning("Victus Lüftersteuerung", f"Autostart konnte nicht geändert werden:\n{exc}")
        self.autostart_var.set(bool(autostart.is_enabled()))

    def toggle_autostart(self) -> None:
        self.set_autostart(not autostart.is_enabled())

    def _center(self) -> None:
        """Fenster mittig auf dem Bildschirm platzieren."""
        r = self.root
        r.update_idletasks()
        min_w, min_h = r.minsize()
        w = max(r.winfo_reqwidth(), min_w)
        h = max(r.winfo_reqheight(), min_h)
        x = max(0, (r.winfo_screenwidth() - w) // 2)
        y = max(0, (r.winfo_screenheight() - h) // 2)
        r.geometry(f"+{x}+{y}")

    def show(self) -> None:
        self._fill_profiles()
        self.autostart_var.set(bool(autostart.is_enabled()))   # evtl. in Windows geändert
        if self.root.state() == "withdrawn":
            self._center()          # aus dem Tray geöffnet: immer mittig
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        if self.tray is None:
            self.quit()
        else:
            self.root.withdraw()

    # ---- Neustart nach Update ---------------------------------------------
    @staticmethod
    def _installed_mtime() -> float | None:
        try:
            return paths.INSTALL_INFO.stat().st_mtime
        except OSError:
            return None

    def _check_relaunch(self) -> None:
        mtime = self._installed_mtime()
        if mtime is None or mtime == self._install_mtime or self._relaunch_pending:
            return
        # Kurz warten, bis der Installer ganz fertig ist.
        self._relaunch_pending = True
        self.root.after(8000, self.relaunch)

    def relaunch(self) -> None:
        """Startet die Oberfläche mit dem aktualisierten Code neu."""
        visible = self.root.state() != "withdrawn"
        log.info("Update installiert - starte Oberfläche neu")
        self.instance.release()
        args = [sys.executable, "-m", "victus_fan", "gui"] + ([] if visible else ["--hidden"])
        flags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
        cwd = str(paths.INSTALL_DIR) if paths.INSTALL_DIR.exists() else None
        subprocess.Popen(args, cwd=cwd, creationflags=flags)
        self.quit()

    def quit(self) -> None:
        if self.tray is not None:
            threading.Thread(target=self.tray.stop, daemon=True).start()
        self.root.destroy()

    # ---- Aktualisierung ---------------------------------------------------
    def poll(self, reschedule: bool = True) -> None:
        while not self.calls.empty():
            fn, args = self.calls.get_nowait()
            fn(*args)
            if fn is self.quit:
                return
        if self.instance.show_requested():
            self.show()
        self._check_relaunch()
        self._render_update()

        override = read_override()
        status = read_status()
        self.status = status
        running = status is not None and not status["stale"]
        if running and status["timestamp"] != self.last_ts:
            self.last_ts = status["timestamp"]
            self.history.append({
                "t": status["timestamp"], "cpu": status["cpu_temp"], "gpu": status["gpu_temp"],
                "fan": self._fan_percent(status), "rpm": status.get("fan_rpm") or [],
                "fan_label": self._fan_label(status),
            })
        self._render_settings(override, status if running else None)
        self._render(status, running)
        if reschedule:
            self.root.after(self.POLL_MS, self.poll)

    @staticmethod
    def _fan_label(s: dict) -> str:
        """Aktive Lüfter-Einstellung zu einem Messpunkt (für den Verlauf)."""
        if s.get("mode") == "paused":
            return "Steuerung aus (BIOS)"
        if s.get("mode") == "max":
            return MAX_LABEL
        label = profile_label(s.get("profile", ""))
        return label + (" (automatisch)" if s.get("profile_reason") in ("Netz", "Akku") else "")

    @staticmethod
    def _fan_percent(s: dict) -> float | None:
        """Tatsächliche Lüfterleistung in % der Maximaldrehzahl (für den Verlauf)."""
        rpm, top = s.get("fan_rpm") or [], s.get("fan_max_rpm") or []
        if rpm and top and len(rpm) == len(top):
            return 100 * max(r / t for r, t in zip(rpm, top) if t)
        return None if s.get("output") == "BIOS" else s.get("fan_percent")

    def _render_settings(self, override: dict, s: dict | None) -> None:
        mode = override["mode"]
        active = mode != "paused"
        if active:
            self._resume_mode = mode            # beim Wiedereinschalten dorthin zurück
        self.control_switch.set(active)
        self.control_label.config(text="Steuerung an" if active else "Steuerung aus",
                                  fg=C.TEXT if active else C.MUTED)

        choice = fan_choice(override)
        self.fan_seg.set(choice, enabled=active)
        if not active:
            hint = PAUSED_HINT
        else:
            hint = FAN_HINTS.get(choice, "Eigenes Profil aus der Konfiguration.")
            if choice == "auto" and s:
                hint = hint.rstrip(".") + f" – gerade {profile_label(s['profile'])}."
        self.fan_hint.config(text=hint)

    def _set_state(self, text: str, color: str) -> None:
        self.state_dot.config(fg=color)
        self.state_text.config(text=text, fg=C.TEXT if color != OFF else C.MUTED)

    def _hardware_test_needed(self) -> bool:
        """Fehlt der Hardware-Test (oder stammt er von einem älteren Messverfahren)?
        Die Konfiguration wird nur neu gelesen, wenn sie sich geändert hat."""
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return False
        if mtime != self._hw_check[0]:
            try:
                needed = load_config(self.config_path).windows["hardware_test"] < HARDWARE_TEST_VERSION
            except Exception:  # noqa: BLE001 - fehlerhafte Datei meldet der Dienst selbst
                needed = False
            self._hw_check = (mtime, needed)
        return self._hw_check[1]

    def _show_banner(self, text: str | None, action: str = "", command=None) -> None:
        if text:
            self.banner_text.config(text=text)
            self.banner_action.config(text=action)
            self._banner_command = command
            if not self._warning_shown:
                self.banner.pack(fill="x", pady=(10, 0), before=self._banner_anchor)
                self._warning_shown = True
        elif self._warning_shown:
            self.banner.pack_forget()
            self._warning_shown = False

    def _render(self, s: dict | None, running: bool) -> None:
        if not running:
            self._show_banner(None)
            self._set_state("Dienst läuft nicht – Extras ▸ „Dienst neu starten“ oder Log prüfen", HOT)
            for card in (self.cpu_card, self.gpu_card, self.fan1_card, self.fan2_card):
                card.set("–", " ", C.MUTED)
            self._update_tray(None, "normal", False, tray_tooltip(None, False))
            self.chart.redraw()
            return

        mode = s.get("mode", "normal")
        battery = " · Akkubetrieb" if s.get("on_battery") else ""
        if s.get("critical"):
            self._set_state("Kritisch heiß – Lüfter auf voller Drehzahl", HOT)
        elif s.get("warning"):
            self._set_state("Lüfter folgen der Vorgabe nicht", WARN)
        elif mode == "paused":
            self._set_state("Lüftersteuerung aus – das BIOS regelt die Lüfter", OFF)
        elif mode == "max":
            self._set_state(f"Läuft · {MAX_LABEL}{battery}", WARN)
        else:
            self._set_state(f"Läuft · {profile_label(s['profile'])}{battery}", OK)
        if s.get("warning"):
            self._show_banner(s["warning"], "Hardware-Test", self.selftest)
        elif s.get("config_error"):
            self._show_banner(f"Konfiguration fehlerhaft: {s['config_error']}", "Konfiguration öffnen",
                              lambda: self._open(self.config_path))
        elif self._hardware_test_needed():
            self._show_banner("Der Hardware-Test wurde noch nicht ausgeführt. Er prüft in etwa 1,5 Minuten, "
                              "welche Lüfterbefehle dein Notebook umsetzt. Optional danach: Extras ▸ "
                              "„Temperatursensoren testen“.", "Hardware-Test", self.selftest)
        else:
            self._show_banner(None)

        for card, key in ((self.cpu_card, "cpu_temp"), (self.gpu_card, "gpu_temp")):
            if key == "gpu_temp" and s.get("gpu_asleep"):
                card.set("–", "Ruhezustand", C.MUTED)
                continue
            t = s.get(key)
            card.set(fmt_temp(t), temp_word(t), temp_color(t) if t is not None else C.MUTED)

        rpm = s.get("fan_rpm") or []
        top = s.get("fan_max_rpm") or [5400, 5200]
        target = s.get("fan_target_rpm")
        output = s.get("output", "")
        for i, card in enumerate((self.fan1_card, self.fan2_card)):
            if i >= len(rpm):
                card.set("–", "keine Daten", C.MUTED)
                continue
            if output == "BIOS":
                sub = "U/min · BIOS regelt"
            elif output == "MAX":
                sub = "U/min · Maximum"
            elif target and i < len(target):
                sub = f"U/min · Ziel {target[i]:,}".replace(",", ".")
            else:
                sub = f"U/min · Vorgabe {s.get('fan_percent', 0):.0f} %"
            mark = None if not target or i >= len(target) or output in ("BIOS", "MAX") else target[i] / top[i]
            card.set(f"{rpm[i]:,}".replace(",", "."), sub, WARN if s.get("warning") else None,
                     fraction=rpm[i] / top[i] if i < len(top) and top[i] else 0, target=mark)

        temps = [t for t in (s.get("cpu_temp"), s.get("gpu_temp")) if t is not None]
        self._update_tray(max(temps) if temps else None, mode, True, tray_tooltip(s, True))
        if self.root.state() != "withdrawn":
            self.chart.redraw()

    def _render_update(self) -> None:
        info = read_installed()
        sha = (info.get("sha") or "")[:7]
        text, color = update_summary(read_json(paths.UPDATE_STATUS), info)
        self.update_label.config(text=f"v{__version__}" + (f" ({sha})" if sha else "") + f"  ·  {text}",
                                 fg=color if color != C.MUTED else C.MUTED)

    def _update_tray(self, temp, mode, running, tip) -> None:
        if self.tray is None:
            return
        key = (None if temp is None else round(temp), mode, running)
        if getattr(self, "_tray_key", None) != key:
            self._tray_key = key
            self.tray.icon = make_tray_image(temp, mode, running)
        if self.tray.title != tip:
            self.tray.title = tip

    def run(self) -> None:
        self.root.mainloop()


def main(show: bool = True, config_path: str | None = None) -> int:
    instance = SingleInstance()
    if not instance.primary:
        if show:                 # z. B. Startmenü: laufendes Fenster nach vorn holen
            instance.request_show()
        return 0                 # Autostart (--hidden) läuft schon - still beenden
    App(show=show, config_path=config_path, instance=instance).run()
    # Hintergrund-Threads des Tray-Icons dürfen das Beenden nicht aufhalten.
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
