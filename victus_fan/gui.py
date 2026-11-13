"""Tray-Icon und Steuerfenster.

Die Oberfläche läuft im Benutzerkontext und spricht nicht direkt mit der
Hardware: Sie liest ``status.json`` des Dienstes und schreibt Wünsche
(Lüfter, Steuerung an/aus, Leistungsmodus) nach ``override.json``.
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
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

from . import __version__, autostart, paths
from .config import load_config
from .state import read_override, read_status, write_override
from .updater import installed as read_installed, read_json

log = logging.getLogger(__name__)

TASK_NAME = "VictusFanControl"
HISTORY_SECONDS = 600
FONT = "Segoe UI"

# --------------------------------------------------------------------------
# Texte
# --------------------------------------------------------------------------
# Eine Skala für die Lüfter: die Profile plus "Maximum" (Steuerung "max").
PROFILE_LABELS = {"auto": "Automatisch", "quiet": "Leise", "balanced": "Ausgewogen",
                  "performance": "Stark"}
MAX_LABEL = "Maximum"
FAN_HINTS = {
    "auto": "Ausgewogen am Netzteil, Leise im Akkubetrieb.",
    "quiet": "Lüfter stehen unter 50 °C still und laufen danach sanft an.",
    "balanced": "Leise im Alltag, kühlt unter Last kräftig.",
    "performance": "Kühlt früh und kräftig – für Spiele und Rendering.",
    "max": "Beide Lüfter laufen dauerhaft mit voller Drehzahl (≈ 5400 U/min).",
}
PAUSED_HINT = "Die Lüftersteuerung ist aus – das BIOS regelt die Lüfter wie ab Werk."

# BIOS-Leistungsmodus (wie im OMEN Gaming Hub).
BIOS_LABELS = {"default": "Standard", "cool": "Kühl", "performance": "Performance"}
BIOS_HINTS = {
    "default": "Normale Leistungsgrenzen des Notebooks.",
    "cool": "Laut HP weniger Leistung und ein kühleres Gehäuse.",
    "performance": "Laut HP mehr Leistung für Spiele.",
}


def profile_label(name: str) -> str:
    return PROFILE_LABELS.get(name, name)


def fan_choice(override: dict) -> str:
    """Gewählte Stufe auf der Lüfter-Skala: Profil oder "max"."""
    return "max" if override.get("mode") == "max" else override.get("profile", "auto")


# --------------------------------------------------------------------------
# Farben: feste Statusfarben + helles/dunkles Schema passend zu Windows
# --------------------------------------------------------------------------
ACCENT = "#2563eb"
OK, WARN, HOT, OFF = "#16a34a", "#d97706", "#dc2626", "#9ca3af"
CPU_COLOR, GPU_COLOR, FAN_COLOR = "#3b82f6", "#a855f7", "#14b8a6"


@dataclass
class Theme:
    BG: str
    CARD: str
    TEXT: str
    MUTED: str
    BORDER: str
    TRACK: str      # Hintergrund der Umschaltfelder/Balken
    HOVER: str
    SELECTED: str   # gewählte Option
    SELECTED_TEXT: str
    BANNER_BG: str
    BANNER_FG: str
    BANNER_BORDER: str
    dark: bool = False


LIGHT = Theme(BG="#f3f4f6", CARD="#ffffff", TEXT="#111827", MUTED="#6b7280", BORDER="#e5e7eb",
              TRACK="#e9ebef", HOVER="#dde1e7", SELECTED=ACCENT, SELECTED_TEXT="#ffffff",
              BANNER_BG="#fff4e5", BANNER_FG="#8a4b00", BANNER_BORDER="#f5c26b")
DARK = Theme(BG="#202020", CARD="#2b2b2b", TEXT="#f3f4f6", MUTED="#a1a1aa", BORDER="#3a3a3a",
             TRACK="#383838", HOVER="#454545", SELECTED="#4c8df6", SELECTED_TEXT="#ffffff",
             BANNER_BG="#3b2a12", BANNER_FG="#fcd9a4", BANNER_BORDER="#7a5520", dark=True)


def windows_uses_dark_mode() -> bool:
    override = os.environ.get("VICTUS_FAN_THEME")
    if override in ("dark", "light"):
        return override == "dark"
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


C: Theme = LIGHT


def temp_color(temp: float | None) -> str:
    if temp is None:
        return OFF
    if temp >= 85:
        return HOT
    if temp >= 70:
        return WARN
    return OK


def temp_word(temp: float | None) -> str:
    if temp is None:
        return "kein Sensor"
    if temp >= 85:
        return "heiß"
    if temp >= 70:
        return "warm"
    return "normal"


def fmt_temp(t: float | None) -> str:
    return "–" if t is None else f"{t:.0f} °C"


def tray_tooltip(s: dict | None, running: bool) -> str:
    """Text beim Überfahren des Tray-Icons (Windows kürzt nach 127 Zeichen)."""
    if not running or not s:
        return "Victus Lüftersteuerung\nDienst läuft nicht"
    lines = [f"CPU {fmt_temp(s.get('cpu_temp'))} · GPU {fmt_temp(s.get('gpu_temp'))}"]
    rpm = s.get("fan_rpm") or []
    if rpm:
        fans = " · ".join(f"{name} {value}" for name, value in zip(("CPU", "GPU"), rpm))
        lines.append(f"Lüfter {fans} U/min")
    output = s.get("output", "")
    if s.get("mode") == "paused":
        lines.append("Lüftersteuerung aus (BIOS regelt)")
    elif output == "MAX":
        lines.append(MAX_LABEL)
    elif s.get("warning"):
        lines.append("⚠ Lüfter folgen nicht")
    else:
        lines.append(f"{profile_label(s.get('profile', ''))} · Vorgabe {s.get('fan_percent', 0):.0f} %")
    return "\n".join(["Victus Lüftersteuerung"] + lines)[:127]


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
NOT_WINDOWS = "Nur unter Windows verfügbar."


def open_file(path: Path) -> None:
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
    subprocess.Popen(["notepad.exe", str(path)])


def run_elevated_console(config_path: Path, command: str) -> None:
    """Startet einen victus-fan-Befehl in einem sichtbaren Konsolenfenster als Administrator."""
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
    import ctypes

    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    params = f'-m victus_fan -c "{config_path}" {command}'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), params, None, 1)  # type: ignore[attr-defined]
    if rc <= 32:
        raise OSError("Abgebrochen oder konnte nicht gestartet werden")


def run_selftest(config_path: Path) -> None:
    run_elevated_console(config_path, "selftest --apply --wait")


def start_update_check() -> None:
    """Startet die Update-Aufgabe sofort (fragt nach Administratorrechten)."""
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
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
        return "Noch nicht nach Updates gesucht", C.MUTED
    text = UPDATE_TEXT.get(state, state)
    if state == "updated" and status.get("updated_at"):
        text += f" am {status['updated_at']}"
    elif status.get("checked_at") and state in ("current", "error", "available"):
        text += f" · geprüft {status['checked_at']}"
    color = {"error": WARN, "available": ACCENT, "installing": ACCENT, "updated": OK}.get(state, C.MUTED)
    return text, color


def restart_service() -> None:
    """Startet den Hintergrunddienst neu (fragt nach Administratorrechten)."""
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
    import ctypes

    script = (
        f"Stop-ScheduledTask -TaskName {TASK_NAME} -ErrorAction SilentlyContinue; "
        "Get-CimInstance Win32_Process -Filter \"Name = 'pythonw.exe' OR Name = 'python.exe'\" | "
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
# Bausteine
# --------------------------------------------------------------------------
class Card(tk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, bg=C.CARD, highlightbackground=C.BORDER, highlightthickness=1, **kw)


class Segmented(tk.Frame):
    """Umschaltfeld im Stil von Windows 11: eine Leiste, die gewählte Option hervorgehoben."""

    def __init__(self, master, command):
        super().__init__(master, bg=C.TRACK, padx=3, pady=3)
        self.command = command
        self.value: str | None = None
        self.enabled = True
        self.items: dict[str, tk.Label] = {}

    def set_options(self, options: list[tuple[str, str]]) -> None:
        if [v for v, _ in options] == list(self.items):
            return
        for w in self.items.values():
            w.destroy()
        self.items = {}
        for value, label in options:
            w = tk.Label(self, text=label, font=(FONT, 9), padx=12, pady=5, cursor="hand2")
            w.pack(side="left", padx=(0, 2))
            w.bind("<Button-1>", lambda _e, v=value: self.enabled and self.command(v))
            w.bind("<Enter>", lambda _e, v=value: self._paint(hover=v))
            w.bind("<Leave>", lambda _e: self._paint())
            self.items[value] = w
        self._paint()

    def set(self, value: str, enabled: bool = True) -> None:
        if value != self.value or enabled != self.enabled:
            self.value, self.enabled = value, enabled
            self._paint()

    def _paint(self, hover: str | None = None) -> None:
        for value, w in self.items.items():
            if value == self.value:
                bg = C.SELECTED if self.enabled else C.HOVER
                fg = C.SELECTED_TEXT if self.enabled else C.MUTED
            else:
                bg = C.HOVER if value == hover and self.enabled else C.TRACK
                fg = C.TEXT if self.enabled else C.MUTED
            w.config(bg=bg, fg=fg, activebackground=bg, cursor="hand2" if self.enabled else "arrow")


class Switch(tk.Canvas):
    """Ein/Aus-Schalter im Stil von Windows 11."""

    W, H = 44, 22

    def __init__(self, master, command, bg: str):
        super().__init__(master, width=self.W, height=self.H, bg=bg, highlightthickness=0, cursor="hand2")
        self.command = command
        self.on = True
        self.bind("<Button-1>", lambda _e: self.command(not self.on))
        self._draw()

    def set(self, on: bool) -> None:
        if on != self.on:
            self.on = on
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h, r = self.W, self.H, self.H / 2
        fill = C.SELECTED if self.on else C.TRACK
        outline = C.SELECTED if self.on else C.MUTED
        self.create_oval(1, 1, h - 1, h - 1, fill=fill, outline=outline)
        self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=fill, outline=outline)
        self.create_rectangle(r, 1, w - r, h - 1, fill=fill, outline="")
        self.create_line(r, 1, w - r, 1, fill=outline)
        self.create_line(r, h - 1, w - r, h - 1, fill=outline)
        knob = 5
        x = w - r if self.on else r
        self.create_oval(x - r + knob, knob, x + r - knob, h - knob,
                         fill=C.SELECTED_TEXT if self.on else C.MUTED, outline="")


class Metric(Card):
    """Kachel: Titel, großer Wert, optional Balken, kleine Zeile."""

    def __init__(self, master, title: str, accent: str, bar: bool = False):
        super().__init__(master)
        head = tk.Frame(self, bg=C.CARD)
        head.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(head, text="●", bg=C.CARD, fg=accent, font=(FONT, 8)).pack(side="left")
        tk.Label(head, text=title, bg=C.CARD, fg=C.MUTED, font=(FONT, 9)).pack(side="left", padx=(4, 0))
        self.value = tk.Label(self, text="–", bg=C.CARD, fg=C.TEXT, font=(FONT, 20, "bold"), anchor="w")
        self.value.pack(fill="x", padx=12)
        self.bar = None
        if bar:
            self.bar = tk.Canvas(self, height=6, width=40, bg=C.CARD, highlightthickness=0)
            self.bar.pack(fill="x", padx=12, pady=(2, 0))
            self.bar.bind("<Configure>", lambda _e: self._draw_bar())
        self.sub = tk.Label(self, text=" ", bg=C.CARD, fg=C.MUTED, font=(FONT, 8), anchor="w")
        self.sub.pack(fill="x", padx=12, pady=(2, 10))
        self._fraction = 0.0
        self._target: float | None = None
        self._bar_color = accent

    def set(self, value: str, sub: str = " ", color: str | None = None,
            fraction: float | None = None, target: float | None = None) -> None:
        self.value.config(text=value, fg=color or C.TEXT)
        self.sub.config(text=sub)
        if self.bar is not None:
            self._fraction = max(0.0, min(1.0, fraction or 0.0))
            self._target = None if target is None else max(0.0, min(1.0, target))
            self._draw_bar()

    def _draw_bar(self) -> None:
        if self.bar is None:
            return
        c, w = self.bar, self.bar.winfo_width()
        c.delete("all")
        if w < 10:
            return
        c.create_rectangle(0, 0, w, 6, fill=C.TRACK, width=0)
        if self._fraction > 0:
            c.create_rectangle(0, 0, max(3, w * self._fraction), 6, fill=self._bar_color, width=0)
        if self._target is not None:      # Markierung: Zielwert der Steuerung
            x = min(w - 1, max(1, w * self._target))
            c.create_line(x, 0, x, 6, fill=C.TEXT, width=2)


class Chart(tk.Canvas):
    """Verlauf der letzten 10 Minuten: CPU, GPU (°C) und Lüfter (% der Maximaldrehzahl).

    Mit der Maus darüber: Messpunkt mit Uhrzeit, Werten, Lüfterprofil und
    Leistungsmodus. Gestrichelte Linien markieren, wann Profil oder Leistung
    gewechselt haben.
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 30, 8, 8, 18
    SERIES = (("fan", FAN_COLOR), ("gpu", GPU_COLOR), ("cpu", CPU_COLOR))

    def __init__(self, master, history: deque):
        super().__init__(master, bg=C.CARD, height=150, width=300, highlightthickness=0)
        self.history = history
        self.hover_x: int | None = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)

    def _on_motion(self, event) -> None:
        self.hover_x = event.x
        self.redraw()

    def _on_leave(self, _event) -> None:
        self.hover_x = None
        self.redraw()

    def _xy(self, t, v, now, w, h):
        pw, ph = w - self.PAD_L - self.PAD_R, h - self.PAD_T - self.PAD_B
        return (self.PAD_L + pw * (1 - (now - t) / HISTORY_SECONDS),
                self.PAD_T + ph * (1 - max(0, min(100, v)) / 100))

    def _visible(self, now: float) -> list[dict]:
        return [e for e in self.history if now - e["t"] <= HISTORY_SECONDS]

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50:
            return
        now = time.time()
        entries = self._visible(now)
        for v in (0, 50, 100):
            _, y = self._xy(now, v, now, w, h)
            self.create_line(self.PAD_L, y, w - self.PAD_R, y, fill=C.BORDER)
            self.create_text(self.PAD_L - 6, y, text=str(v), anchor="e", fill=C.MUTED, font=(FONT, 8))
        for minutes, anchor in ((10, "sw"), (5, "s"), (0, "se")):
            x, _ = self._xy(now - minutes * 60, 0, now, w, h)
            self.create_text(x, h - 2, text="jetzt" if minutes == 0 else f"−{minutes} min",
                             anchor=anchor, fill=C.MUTED, font=(FONT, 8))

        # Wechsel von Lüfterprofil oder Leistung: gestrichelte senkrechte Linie
        for prev, cur in zip(entries, entries[1:]):
            if (prev["fan_label"], prev["bios"]) != (cur["fan_label"], cur["bios"]):
                x, _ = self._xy(cur["t"], 0, now, w, h)
                self.create_line(x, self.PAD_T, x, h - self.PAD_B, fill=C.MUTED, dash=(2, 3))

        drawn = False
        for key, color in self.SERIES:
            points = []
            for e in entries:
                if e[key] is not None:
                    points.extend(self._xy(e["t"], e[key], now, w, h))
            if len(points) >= 4:
                self.create_line(*points, fill=color, width=2, smooth=True)
                drawn = True
        if not drawn:
            self.create_text(w / 2, h / 2 - 6, text="Verlauf erscheint nach einigen Sekunden",
                             fill=C.MUTED, font=(FONT, 9))
            return
        if self.hover_x is not None and self.PAD_L <= self.hover_x <= w - self.PAD_R:
            self._draw_hover(entries, now, w, h)

    def _draw_hover(self, entries: list[dict], now: float, w: int, h: int) -> None:
        nearest = min(entries, key=lambda e: abs(self._xy(e["t"], 0, now, w, h)[0] - self.hover_x))
        x, _ = self._xy(nearest["t"], 0, now, w, h)
        self.create_line(x, self.PAD_T, x, h - self.PAD_B, fill=C.TEXT, width=1)
        for key, color in self.SERIES:
            if nearest[key] is not None:
                _, y = self._xy(nearest["t"], nearest[key], now, w, h)
                self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=C.CARD, width=2)

        lines = [time.strftime("%H:%M:%S", time.localtime(nearest["t"])),
                 f"CPU {fmt_temp(nearest['cpu'])} · GPU {fmt_temp(nearest['gpu'])}"]
        rpm = nearest.get("rpm") or []
        fan = f"{nearest['fan']:.0f} %" if nearest["fan"] is not None else "–"
        lines.append(f"Lüfter {fan}" + (f" · {' / '.join(f'{r:,}'.replace(',', '.') for r in rpm)} U/min"
                                        if rpm else ""))
        lines.append(f"Profil: {nearest['fan_label']}")
        lines.append(f"Leistung: {nearest['bios'] or '–'}")

        # Infofeld neben der Linie, auf der Seite mit mehr Platz: Uhrzeit fett, darunter die Werte
        head = self.create_text(0, 0, text=lines[0], anchor="nw", fill=C.TEXT, font=(FONT, 8, "bold"))
        body = self.create_text(0, 0, text="\n".join(lines[1:]), anchor="nw", fill=C.TEXT,
                                font=(FONT, 8), justify="left")
        hx1, hy1, hx2, hy2 = self.bbox(head)
        bx1, by1, bx2, by2 = self.bbox(body)
        bw = max(hx2 - hx1, bx2 - bx1) + 16
        bh = (hy2 - hy1) + 2 + (by2 - by1) + 12
        left = x + 10 if x + 10 + bw <= w - self.PAD_R else x - 10 - bw
        top = self.PAD_T
        self.create_rectangle(left, top, left + bw, top + bh, fill=C.TRACK, outline=C.BORDER)
        self.coords(head, left + 8, top + 6)
        self.coords(body, left + 8, top + 6 + (hy2 - hy1) + 2)
        self.tag_raise(head)
        self.tag_raise(body)


def _dark_title_bar(root: tk.Tk) -> None:
    """Dunkle Titelleiste unter Windows 11 (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())  # type: ignore[attr-defined]
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Fenster
# --------------------------------------------------------------------------
class App:
    POLL_MS = 1000

    def __init__(self, show: bool, config_path: str | None, instance: SingleInstance):
        global C
        C = DARK if windows_uses_dark_mode() else LIGHT

        self.config_path = Path(config_path) if config_path else paths.CONFIG_FILE
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
        self.root.minsize(680, 610)   # feste Breite: das Fenster springt nicht mit der Textlänge
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
        b = tk.Label(self.banner, text="Hardware-Test", bg=C.BANNER_BG, fg=C.BANNER_FG, cursor="hand2",
                     font=(FONT, 9, "underline"))
        b.pack(side="right", padx=10)
        b.bind("<Button-1>", lambda _e: self.selftest())
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

        # Einstellungen: zwei getrennte Fragen - wie arbeiten die Lüfter, und wie
        # viel Leistung darf das Notebook aufnehmen. Jeweils mit Erklärung.
        settings = Card(outer)
        settings.pack(fill="x", pady=(10, 0))
        self.fan_seg, self.fan_hint = self._setting(settings, "Lüfter", self.set_fan)
        self.bios_seg, self.bios_hint = self._setting(settings, "Leistung (BIOS)", self.set_bios, last=True)
        self.bios_seg.set_options(list(BIOS_LABELS.items()))
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

        def bios_items():
            return [pystray.MenuItem(label, call(self.set_bios, value), radio=True,
                                     checked=lambda _i, v=value: self._bios_choice(read_override()) == v,
                                     enabled=lambda _i: read_override()["mode"] != "paused")
                    for value, label in BIOS_LABELS.items()]

        menu = pystray.Menu(
            pystray.MenuItem("Fenster öffnen", call(self.show), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Lüfter", pystray.Menu(fan_items)),
            pystray.MenuItem("Leistung (BIOS)", pystray.Menu(bios_items)),
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

    def set_bios(self, value: str) -> None:
        self._write(bios_mode=value)

    def set_control(self, on: bool) -> None:
        """Hauptschalter: aus = Steuerung pausiert, das BIOS regelt."""
        self._write(mode=self._resume_mode if on else "paused")

    def toggle_control(self) -> None:
        self.set_control(read_override()["mode"] == "paused")

    def _bios_choice(self, override: dict) -> str:
        s = self.status if self.status and not self.status.get("stale") else {}
        return override.get("bios_mode") or s.get("fan_mode") or "default"

    def edit_config(self) -> None:
        self._open(self.config_path)

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
                "bios": BIOS_LABELS.get(status.get("fan_mode", ""), ""),
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

        bios = self._bios_choice(override)
        self.bios_seg.set(bios, enabled=active)
        if not active:
            self.bios_hint.config(text="Bei ausgeschalteter Lüftersteuerung gilt der BIOS-Standard.")
        else:
            self.bios_hint.config(text=BIOS_HINTS.get(bios, ""))

    def _set_state(self, text: str, color: str) -> None:
        self.state_dot.config(fg=color)
        self.state_text.config(text=text, fg=C.TEXT if color != OFF else C.MUTED)

    def _show_banner(self, text: str | None) -> None:
        if text:
            self.banner_text.config(text=text)
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
        self._show_banner(s.get("warning"))

        for card, key in ((self.cpu_card, "cpu_temp"), (self.gpu_card, "gpu_temp")):
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
