"""Tray-Icon und Steuerfenster.

Die Oberfläche läuft im Benutzerkontext und spricht nicht direkt mit der
Hardware: Sie liest ``status.json`` des Dienstes und schreibt Wünsche
(Profil, Modus) nach ``override.json``.
"""

from __future__ import annotations

import base64
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__, paths
from .config import load_config
from .state import read_override, read_status, write_override
from .updater import installed as read_installed, read_json

log = logging.getLogger(__name__)

TASK_NAME = "VictusFanControl"
PROFILE_LABELS = {"auto": "Automatisch", "quiet": "Leise", "balanced": "Ausgewogen",
                  "performance": "Leistung"}
MODE_LABELS = {"normal": "Intelligent", "max": "Volle Drehzahl", "paused": "Pause (BIOS)"}
POWER_LABELS = {"auto": "Automatisch", "default": "Standard", "cool": "Kühl", "performance": "Performance"}
POWER_HINTS = {
    "auto": "Wert aus der Konfiguration (vom Hardware-Test gewählt)",
    "default": "normale Leistungsgrenzen",
    "cool": "weniger Leistung, kühleres Gehäuse, leiser unter Last",
    "performance": "mehr Leistung für Spiele, dafür mehr Wärme und Lüfter",
}
HISTORY_SECONDS = 600

# Farben
BG = "#f4f5f7"
CARD = "#ffffff"
TEXT = "#1d2129"
MUTED = "#6b7280"
BORDER = "#e3e5e8"
ACCENT = "#2563eb"
CPU_COLOR = "#2563eb"
GPU_COLOR = "#9333ea"
FAN_COLOR = "#0d9488"
OK, WARN, HOT, OFF = "#16a34a", "#d97706", "#dc2626", "#9ca3af"


def temp_color(temp: float | None) -> str:
    if temp is None:
        return OFF
    if temp >= 85:
        return HOT
    if temp >= 70:
        return WARN
    return OK


def profile_label(name: str) -> str:
    return PROFILE_LABELS.get(name, name)


# --------------------------------------------------------------------------
# Tray-Icon zeichnen
# --------------------------------------------------------------------------
def _font(size: int):
    from PIL import ImageFont

    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_fan_image(size: int = 64, color: str = ACCENT):
    """Lüftersymbol (für Fenster und Tray ohne Messwerte)."""
    from PIL import Image, ImageDraw

    s = size * 4  # überabtasten für glatte Kanten
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=color)
    c, r = s / 2, s * 0.36
    for angle in (0, 90, 180, 270):
        d.pieslice((c - r, c - r, c + r, c + r), angle + 10, angle + 70, fill="white")
    hub = s * 0.11
    d.ellipse((c - hub, c - hub, c + hub, c + hub), fill=color, outline="white", width=s // 32)
    return img.resize((size, size), Image.LANCZOS)


def make_tray_image(temp: float | None, mode: str, running: bool):
    """Tray-Icon: höchste Temperatur (CPU/GPU) als Zahl auf farbigem Grund.

    Grün < 70 °C, Orange < 85 °C, Rot darüber; grau = pausiert/kein Dienst.
    """
    from PIL import Image, ImageDraw

    if not running or temp is None:
        return make_fan_image(64, OFF)
    color = OFF if mode == "paused" else temp_color(temp)
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=48, fill=color)
    text = str(min(99, round(temp)))
    font = _font(190)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((s - w) / 2 - box[0], (s - h) / 2 - box[1]), text, font=font, fill="white")
    return img.resize((64, 64), Image.LANCZOS)


# --------------------------------------------------------------------------
# Systemaktionen
# --------------------------------------------------------------------------
def open_file(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def run_selftest(config_path: Path) -> None:
    """Startet den Hardware-Test in einem sichtbaren Konsolenfenster als Administrator."""
    if sys.platform != "win32":
        raise OSError("Bitte im Terminal ausführen: sudo victus-fan selftest --apply")
    import ctypes

    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    params = f'-m victus_fan -c "{config_path}" selftest --apply --wait'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), params, None, 1)  # type: ignore[attr-defined]
    if rc <= 32:
        raise OSError("Hardware-Test abgebrochen oder konnte nicht gestartet werden")


def start_update_check() -> None:
    """Startet die Update-Aufgabe sofort (fragt nach Administratorrechten)."""
    if sys.platform != "win32":
        raise OSError("Bitte im Terminal ausführen: sudo victus-fan update")
    import ctypes

    rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", "schtasks.exe", f'/Run /TN "{paths.UPDATE_TASK}"', None, 0)
    if rc <= 32:
        raise OSError("Update-Suche abgebrochen oder konnte nicht gestartet werden")


UPDATE_TEXT = {
    "current": "Aktuell",
    "available": "Update verfügbar",
    "installing": "Update wird installiert …",
    "updated": "Aktualisiert",
    "error": "Update-Prüfung fehlgeschlagen",
}


def update_summary(status: dict, installed: dict | None = None) -> tuple[str, str]:
    """Kurztext + Farbe für den Update-Status."""
    installed = installed or {}
    state = status.get("state")
    if state == "installing":
        if status.get("latest_sha") and status.get("latest_sha") == installed.get("sha"):
            # Installation fertig, der Updater konnte es nur nicht mehr eintragen.
            state = "updated"
            status = dict(status, updated_at=installed.get("installed_at", ""))
        else:
            try:
                started = time.mktime(time.strptime(status.get("checked_at", ""), "%Y-%m-%d %H:%M"))
            except ValueError:
                started = None
            if started is not None and time.time() - started > 45 * 60:
                return f"Update hängt seit {status['checked_at']} – siehe update.log", WARN
    if not state:
        return "Noch nicht nach Updates gesucht", MUTED
    text = UPDATE_TEXT.get(state, state)
    if state == "updated" and status.get("updated_at"):
        text += f" am {status['updated_at']}"
    elif status.get("checked_at") and state in ("current", "error", "available"):
        text += f" (geprüft {status['checked_at']})"
    color = {"error": WARN, "available": ACCENT, "installing": ACCENT, "updated": OK}.get(state, MUTED)
    return text, color


def restart_service() -> None:
    """Startet den Hintergrunddienst neu (fragt nach Administratorrechten)."""
    if sys.platform == "win32":
        import ctypes

        script = (
            f"Stop-ScheduledTask -TaskName {TASK_NAME} -ErrorAction SilentlyContinue; "
            "Get-CimInstance Win32_Process -Filter \"Name = 'pythonw.exe'\" | "
            "Where-Object { $_.CommandLine -like '*victus_fan run*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
            f"Start-ScheduledTask -TaskName {TASK_NAME}"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", "powershell.exe",
            f"-NoProfile -WindowStyle Hidden -EncodedCommand {encoded}", None, 0)
        if rc <= 32:
            raise OSError("Neustart abgebrochen oder fehlgeschlagen")
    else:
        subprocess.Popen(["pkexec", "systemctl", "restart", "victus-fan.service"])


class SingleInstance:
    """Verhindert doppelte Tray-Icons. Ein zweiter Start bittet die laufende
    Instanz stattdessen, ihr Fenster zu zeigen."""

    def __init__(self):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "VictusFan"
        base.mkdir(parents=True, exist_ok=True)
        self.show_flag = base / "show.flag"
        self.primary = True
        if sys.platform == "win32":
            import ctypes

            self._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VictusFanTray")  # type: ignore[attr-defined]
            self.primary = ctypes.windll.kernel32.GetLastError() != 183  # type: ignore[attr-defined]

    def release(self) -> None:
        """Mutex freigeben, damit eine neue Instanz starten kann."""
        if sys.platform == "win32" and getattr(self, "_mutex", None):
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._mutex)  # type: ignore[attr-defined]
            self._mutex = None

    def request_show(self) -> None:
        self.show_flag.write_text("1")

    def show_requested(self) -> bool:
        if self.show_flag.exists():
            try:
                self.show_flag.unlink()
            except OSError:
                pass
            return True
        return False


# --------------------------------------------------------------------------
# Fenster
# --------------------------------------------------------------------------
class ValueCard(tk.Frame):
    def __init__(self, master, title: str, accent: str):
        super().__init__(master, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        bar = tk.Frame(self, bg=accent, height=3)
        bar.pack(fill="x")
        tk.Label(self, text=title, bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(8, 0))
        self.value = tk.Label(self, text="–", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 22))
        self.value.pack(anchor="w", padx=12)
        self.sub = tk.Label(self, text=" ", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.sub.pack(anchor="w", padx=12, pady=(0, 10))

    def set(self, value: str, sub: str = " ", color: str = TEXT) -> None:
        self.value.config(text=value, fg=color)
        self.sub.config(text=sub)


class Chart(tk.Canvas):
    """Verlauf der letzten 10 Minuten: CPU, GPU (°C) und Lüfter (%)."""

    PAD_L, PAD_R, PAD_T, PAD_B = 34, 10, 10, 20

    def __init__(self, master, history: deque):
        super().__init__(master, bg=CARD, height=170, highlightthickness=1, highlightbackground=BORDER)
        self.history = history
        self.bind("<Configure>", lambda _e: self.redraw())

    def _xy(self, t: float, v: float, now: float, w: int, h: int) -> tuple[float, float]:
        pw = w - self.PAD_L - self.PAD_R
        ph = h - self.PAD_T - self.PAD_B
        x = self.PAD_L + pw * (1 - (now - t) / HISTORY_SECONDS)
        y = self.PAD_T + ph * (1 - max(0, min(100, v)) / 100)
        return x, y

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50:
            return
        now = time.time()
        for v in (0, 25, 50, 75, 100):
            _, y = self._xy(now, v, now, w, h)
            self.create_line(self.PAD_L, y, w - self.PAD_R, y, fill=BORDER)
            self.create_text(self.PAD_L - 6, y, text=str(v), anchor="e", fill=MUTED, font=("Segoe UI", 8))
        for minutes in (10, 5, 0):
            x, _ = self._xy(now - minutes * 60, 0, now, w, h)
            label = "jetzt" if minutes == 0 else f"-{minutes} min"
            anchor = {10: "sw", 5: "s", 0: "se"}[minutes]
            self.create_text(x, h - 4, text=label, anchor=anchor, fill=MUTED, font=("Segoe UI", 8))
        for index, color in ((3, FAN_COLOR), (2, GPU_COLOR), (1, CPU_COLOR)):
            points = []
            for entry in self.history:
                if entry[index] is not None and now - entry[0] <= HISTORY_SECONDS:
                    points.extend(self._xy(entry[0], entry[index], now, w, h))
            if len(points) >= 4:
                self.create_line(*points, fill=color, width=2, smooth=True)


class App:
    POLL_MS = 1000

    def __init__(self, show: bool, config_path: str | None, instance: SingleInstance):
        self.config_path = Path(config_path) if config_path else paths.CONFIG_FILE
        self.instance = instance
        self.history: deque = deque(maxlen=HISTORY_SECONDS // 2 + 10)
        self.last_ts = None
        self.status: dict | None = None
        self.calls: queue.Queue = queue.Queue()
        self.tray = None
        self._install_mtime = self._installed_mtime()
        self._relaunch_pending = False

        self.root = tk.Tk()
        self.root.title("Victus Lüftersteuerung")
        self.root.configure(bg=BG)
        self.root.minsize(520, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            from PIL import ImageTk

            self._icon = ImageTk.PhotoImage(make_fan_image(64))
            self.root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001
            pass

        self.profile_var = tk.StringVar(value="auto")
        self.mode_var = tk.StringVar(value="normal")
        self.power_var = tk.StringVar(value="auto")
        self._build()
        self._start_tray()
        if not show and self.tray is not None:
            self.root.withdraw()
        self.root.after(0, self.poll)

    # ---- Aufbau -----------------------------------------------------------
    def _profiles(self) -> list[str]:
        try:
            names = list(load_config(self.config_path).raw["profiles"])
        except Exception:  # noqa: BLE001 - kaputte Konfiguration: Standardprofile zeigen
            names = ["quiet", "balanced", "performance"]
        return ["auto"] + names

    def _build(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Seg.Toolbutton", padding=(10, 6), font=("Segoe UI", 9))

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=16, pady=14)

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Victus Lüftersteuerung", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(side="left")
        self.state_label = tk.Label(head, text="● Verbinde …", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.state_label.pack(side="right")

        cards = tk.Frame(outer, bg=BG)
        cards.pack(fill="x", pady=(12, 10))
        self.cpu_card = ValueCard(cards, "CPU", CPU_COLOR)
        self.gpu_card = ValueCard(cards, "GPU", GPU_COLOR)
        self.fan_card = ValueCard(cards, "Lüfter (Vorgabe)", FAN_COLOR)
        for i, card in enumerate((self.cpu_card, self.gpu_card, self.fan_card)):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            cards.columnconfigure(i, weight=1, uniform="cards")

        legend = tk.Frame(outer, bg=BG)
        legend.pack(fill="x")
        tk.Label(legend, text="Verlauf", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 10)).pack(side="left")
        for text, color in (("Lüfter %", FAN_COLOR), ("GPU °C", GPU_COLOR), ("CPU °C", CPU_COLOR)):
            tk.Label(legend, text=text, bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="right", padx=(2, 8))
            tk.Label(legend, text="━", bg=BG, fg=color, font=("Segoe UI", 9)).pack(side="right")
        self.chart = Chart(outer, self.history)
        self.chart.pack(fill="x", pady=(4, 12))

        self._section(outer, "Profil")
        self.profile_bar = tk.Frame(outer, bg=BG)
        self.profile_bar.pack(fill="x", pady=(4, 10))
        self._fill_profiles()

        self._section(outer, "Modus")
        bar = tk.Frame(outer, bg=BG)
        bar.pack(fill="x", pady=(4, 10))
        for mode, label in MODE_LABELS.items():
            ttk.Radiobutton(bar, text=label, value=mode, variable=self.mode_var, style="Seg.Toolbutton",
                            command=lambda m=mode: self.set_mode(m)).pack(side="left", padx=(0, 4))

        self._section(outer, "Leistungsmodus (BIOS)")
        bar = tk.Frame(outer, bg=BG)
        bar.pack(fill="x", pady=(4, 2))
        for power, label in POWER_LABELS.items():
            ttk.Radiobutton(bar, text=label, value=power, variable=self.power_var, style="Seg.Toolbutton",
                            command=lambda p=power: self.set_power(p)).pack(side="left", padx=(0, 4))
        self.power_hint = tk.Label(outer, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w")
        self.power_hint.pack(fill="x", pady=(0, 10))

        self.info = tk.Label(outer, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), justify="left", anchor="w")
        self.info.pack(fill="x", pady=(2, 6))

        self.warning = tk.Frame(outer, bg="#fff4e5", highlightbackground="#f5c26b", highlightthickness=1)
        self.warning_text = tk.Label(self.warning, text="", bg="#fff4e5", fg="#8a4b00", font=("Segoe UI", 9),
                                     justify="left", anchor="w", wraplength=300)
        self.warning_text.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        ttk.Button(self.warning, text="Hardware-Test", command=self.selftest).pack(side="right", padx=8, pady=6)
        self._warning_shown = False

        buttons = tk.Frame(outer, bg=BG)
        buttons.pack(fill="x", side="bottom")
        self._buttons = buttons
        ttk.Button(buttons, text="Konfiguration …", command=self.edit_config).pack(side="left")
        ttk.Button(buttons, text="Log", command=self.open_log).pack(side="left", padx=6)
        ttk.Button(buttons, text="Dienst neu starten", command=self.restart).pack(side="left")
        ttk.Button(buttons, text="Hardware-Test", command=self.selftest).pack(side="left", padx=6)

        updates = tk.Frame(outer, bg=BG)
        updates.pack(fill="x", side="bottom", before=buttons, pady=(8, 0))
        self.version_label = tk.Label(updates, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.version_label.pack(side="left")
        self.update_label = tk.Label(updates, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.update_label.pack(side="left", padx=(6, 0))
        link = tk.Label(updates, text="Nach Updates suchen", bg=BG, fg=ACCENT, cursor="hand2",
                        font=("Segoe UI", 8, "underline"))
        link.pack(side="right")
        link.bind("<Button-1>", lambda _e: self.check_updates())

    def _section(self, parent, text: str) -> None:
        tk.Label(parent, text=text, bg=BG, fg=TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w")

    def _fill_profiles(self) -> None:
        for child in self.profile_bar.winfo_children():
            child.destroy()
        for name in self._profiles():
            ttk.Radiobutton(self.profile_bar, text=profile_label(name), value=name, variable=self.profile_var,
                            style="Seg.Toolbutton",
                            command=lambda n=name: self.set_profile(n)).pack(side="left", padx=(0, 4))

    # ---- Tray -------------------------------------------------------------
    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001
            log.warning("Kein Tray-Icon möglich: %s", exc)
            return

        def call(fn, *args):
            return lambda _icon=None, _item=None: self.calls.put((fn, args))

        def profile_item(name):
            return pystray.MenuItem(profile_label(name), call(self.set_profile, name), radio=True,
                                    checked=lambda _i, n=name: read_override()["profile"] == n)

        def power_item(power):
            return pystray.MenuItem(POWER_LABELS[power], call(self.set_power, power), radio=True,
                                    checked=lambda _i, p=power: read_override()["power_mode"] == p)

        def mode_item(mode):
            return pystray.MenuItem(MODE_LABELS[mode], call(self.set_mode, mode), radio=True,
                                    checked=lambda _i, m=mode: read_override()["mode"] == m)

        menu = pystray.Menu(
            pystray.MenuItem("Fenster öffnen", call(self.show), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Profil", pystray.Menu(lambda: [profile_item(n) for n in self._profiles()])),
            pystray.MenuItem("Modus", pystray.Menu(*[mode_item(m) for m in MODE_LABELS])),
            pystray.MenuItem("Leistungsmodus", pystray.Menu(*[power_item(p) for p in POWER_LABELS])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Konfiguration bearbeiten", call(self.edit_config)),
            pystray.MenuItem("Log öffnen", call(self.open_log)),
            pystray.MenuItem("Dienst neu starten", call(self.restart)),
            pystray.MenuItem("Hardware-Test …", call(self.selftest)),
            pystray.MenuItem("Nach Updates suchen", call(self.check_updates)),
            pystray.Menu.SEPARATOR,
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

    def set_profile(self, name: str) -> None:
        self._write(profile=name)

    def set_mode(self, mode: str) -> None:
        self._write(mode=mode)

    def set_power(self, power: str) -> None:
        self._write(power_mode=power)

    def edit_config(self) -> None:
        open_file(self.config_path)

    def open_log(self) -> None:
        if paths.LOG_FILE and paths.LOG_FILE.exists():
            open_file(paths.LOG_FILE)
        else:
            messagebox.showinfo("Victus Lüftersteuerung", "Noch keine Logdatei vorhanden.")

    def selftest(self) -> None:
        if not messagebox.askokcancel(
                "Hardware-Test",
                "Der Test prüft etwa 2 Minuten lang, welche Lüfterbefehle dein Notebook wirklich "
                "umsetzt. Die Lüfter werden dabei hörbar schneller und langsamer.\n\n"
                "Das Ergebnis wird automatisch in die Konfiguration übernommen.\n"
                "Bitte die Administrator-Abfrage bestätigen."):
            return
        try:
            run_selftest(self.config_path)
        except OSError as exc:
            messagebox.showwarning("Hardware-Test", str(exc))

    def _show_warning(self, text: str | None) -> None:
        if text:
            self.warning_text.config(text=text)
            if not self._warning_shown:
                self.warning.pack(fill="x", pady=(0, 10), before=self._buttons)
                self._warning_shown = True
        elif self._warning_shown:
            self.warning.pack_forget()
            self._warning_shown = False

    def restart(self) -> None:
        try:
            restart_service()
        except OSError as exc:
            messagebox.showwarning("Victus Lüftersteuerung", str(exc))

    def show(self) -> None:
        self._fill_profiles()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        if self.tray is None:
            self.quit()
        else:
            self.root.withdraw()

    def check_updates(self) -> None:
        try:
            start_update_check()
        except OSError as exc:
            messagebox.showwarning("Victus Lüftersteuerung", str(exc))

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
        self.profile_var.set(override["profile"])
        self.mode_var.set(override["mode"])
        self.power_var.set(override["power_mode"])
        self.power_hint.config(text=POWER_HINTS.get(override["power_mode"], ""))

        status = read_status()
        self.status = status
        running = status is not None and not status["stale"]
        if running and status["timestamp"] != self.last_ts:
            self.last_ts = status["timestamp"]
            fan = 100.0 if status["output"] == "MAX" else (None if status["output"] == "BIOS"
                                                             else status["fan_percent"])
            self.history.append((status["timestamp"], status["cpu_temp"], status["gpu_temp"], fan))
        self._render(status, running)
        if reschedule:
            self.root.after(self.POLL_MS, self.poll)

    def _render(self, s: dict | None, running: bool) -> None:
        if not running:
            self._show_warning(None)
            self.state_label.config(text="● Dienst läuft nicht", fg=HOT)
            for card in (self.cpu_card, self.gpu_card, self.fan_card):
                card.set("–", " ", MUTED)
            self.info.config(text="Der Hintergrunddienst sendet keine Daten. "
                                  "„Dienst neu starten“ versuchen oder Log prüfen.")
            self._update_tray(None, "normal", False, "Victus: Dienst läuft nicht")
            self.chart.redraw()
            return

        mode = s.get("mode", "normal")
        if s.get("critical"):
            self.state_label.config(text="● Kritisch heiß – volle Drehzahl", fg=HOT)
        elif s.get("warning"):
            self.state_label.config(text="● Lüfter folgen nicht", fg=WARN)
        elif mode == "paused":
            self.state_label.config(text="● Pausiert – BIOS regelt", fg=WARN)
        elif mode == "max":
            self.state_label.config(text="● Volle Drehzahl", fg=WARN)
        else:
            self.state_label.config(text="● Aktiv", fg=OK)

        def fmt(t):
            return "–" if t is None else f"{t:.0f} °C"

        rpm = s.get("fan_rpm") or []
        self.cpu_card.set(fmt(s["cpu_temp"]), f"geglättet {fmt(s['cpu_smoothed'])}", temp_color(s["cpu_temp"]))
        self.gpu_card.set(fmt(s["gpu_temp"]),
                          "kein Sensor" if s["gpu_temp"] is None else f"geglättet {fmt(s['gpu_smoothed'])}",
                          temp_color(s["gpu_temp"]))
        output = s["output"]
        fan_text = {"MAX": "MAX", "BIOS": "BIOS", "Auto": "Auto"}.get(output, f"{s['fan_percent']:.0f} %")
        # Wert = Vorgabe der Steuerung, Unterzeile = tatsächlich gemessene Drehzahl
        self.fan_card.set(fan_text, ("gemessen " + " / ".join(map(str, rpm)) + " U/min") if rpm else " ",
                          WARN if s.get("warning") else TEXT)
        self._show_warning(s.get("warning"))

        if self.power_var.get() == "auto" and s.get("fan_mode"):
            self.power_hint.config(text=f"aus der Konfiguration: {POWER_LABELS.get(s['fan_mode'], s['fan_mode'])} "
                                        f"– {POWER_HINTS.get(s['fan_mode'], '')}")
        method = "stufenlos" if s.get("supports_level", True) else "Max an/aus (BIOS-Automatik dazwischen)"
        if s.get("fan_mode"):
            method += f" · BIOS-Modus {POWER_LABELS.get(s['fan_mode'], s['fan_mode'])}"
        self.info.config(text=f"Profil {profile_label(s['profile'])} ({s['profile_reason']})  ·  "
                              f"Bedarf {s['demand_percent']:.0f} %  ·  Steuerung: {method}"
                              + ("  ·  Akkubetrieb" if s.get("on_battery") else ""))
        temps = [t for t in (s["cpu_temp"], s["gpu_temp"]) if t is not None]
        hottest = max(temps) if temps else None
        # Das Tray-Icon zeigt immer die höchste Temperatur (CPU oder GPU).
        tip = (f"Victus: max. {fmt(hottest)}\nCPU {fmt(s['cpu_temp'])} · GPU {fmt(s['gpu_temp'])}"
               f" · Lüfter {fan_text}")
        self._update_tray(hottest, mode, True, tip)
        if self.root.state() != "withdrawn":
            self.chart.redraw()

    def _render_update(self) -> None:
        info = read_installed()
        sha = (info.get("sha") or "")[:7]
        self.version_label.config(text=f"v{__version__}" + (f" ({sha})" if sha else ""))
        text, color = update_summary(read_json(paths.UPDATE_STATUS), info)
        self.update_label.config(text="· " + text, fg=color)

    def _update_tray(self, temp, mode, running, tip) -> None:
        if self.tray is None:
            return
        key = (None if temp is None else round(temp), mode, running)
        if getattr(self, "_tray_key", None) != key:
            self._tray_key = key
            self.tray.icon = make_tray_image(temp, mode, running)
        if self.tray.title != tip:
            self.tray.title = tip[:120]

    def run(self) -> None:
        self.root.mainloop()


def main(show: bool = True, config_path: str | None = None) -> int:
    instance = SingleInstance()
    if not instance.primary:
        instance.request_show()
        return 0
    App(show=show, config_path=config_path, instance=instance).run()
    # Hintergrund-Threads des Tray-Icons dürfen das Beenden nicht aufhalten.
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
