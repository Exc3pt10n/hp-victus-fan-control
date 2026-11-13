"""Farben (helles/dunkles Schema passend zu Windows), Schrift und Fenster-Helfer.

``C`` ist das aktive Farbschema. Es wird beim Start mit ``apply_theme`` an Ort
und Stelle umgestellt, damit ``from .theme import C`` überall dasselbe Objekt
liefert.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tkinter as tk
from dataclasses import dataclass

FONT = "Segoe UI"

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


C: Theme = dataclasses.replace(LIGHT)


def apply_theme(dark: bool) -> Theme:
    """Aktives Schema umstellen (vor dem Anlegen der ersten Widgets)."""
    for f in dataclasses.fields(Theme):
        setattr(C, f.name, getattr(DARK if dark else LIGHT, f.name))
    return C


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


def hide_until_placed(win: tk.Toplevel) -> None:
    """Neues Fenster unsichtbar aufbauen, damit es nicht erst oben links aufblitzt.

    Bewusst über Transparenz statt withdraw(): Das Fenster wird dabei schon
    erzeugt, so dass Windows die dunkle Titelleiste vor dem Anzeigen übernimmt.
    """
    try:
        win.attributes("-alpha", 0.0)
    except tk.TclError:
        pass


def reveal(win: tk.Toplevel) -> None:
    """Fertig platziertes Fenster sichtbar machen (Gegenstück zu hide_until_placed)."""
    win.update_idletasks()
    try:
        win.attributes("-alpha", 1.0)
    except tk.TclError:
        pass
