"""Autostart des Tray-Icons mit Windows (pro Benutzer, ohne Adminrechte).

Eintrag unter ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``. Windows
zeigt ihn unter Einstellungen > Apps > Autostart und im Task-Manager; wird er
dort deaktiviert, steht das unter ``...\\Explorer\\StartupApproved\\Run``.

Die Wahl des Benutzers wird in ``%LOCALAPPDATA%\\VictusFan\\settings.json``
gemerkt und bei jedem Start der Oberfläche angewendet - so überlebt sie Updates.
Der Hintergrunddienst startet unabhängig davon immer mit Windows (geplante Aufgabe).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Victus Lüftersteuerung"
# StartupApproved: erstes Byte gerade (0x02/0x06) = aktiviert, ungerade (0x03) = deaktiviert
APPROVED_ENABLED = bytes([0x02] + [0x00] * 11)


def settings_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "VictusFan"
    return base / "settings.json"


def _load() -> dict:
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def wanted() -> bool:
    """Gemerkte Wahl des Benutzers (Standard: an)."""
    return bool(_load().get("autostart", True))


def _remember(enabled: bool) -> None:
    data = _load()
    data["autostart"] = enabled
    try:
        settings_file().parent.mkdir(parents=True, exist_ok=True)
        settings_file().write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        log.warning("Autostart-Einstellung nicht speicherbar: %s", exc)


def command() -> str:
    """Startbefehl: pythonw der installierten Umgebung, Tray-Icon ohne Fenster."""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):          # Konsolenfenster vermeiden
        exe = exe[: -len("python.exe")] + "pythonw.exe"
    return f'"{exe}" -m victus_fan gui --hidden'


def _winreg():
    if sys.platform != "win32":
        return None
    import winreg

    return winreg


def is_enabled() -> bool | None:
    """Ist der Autostart in Windows aktiv? None = nicht unter Windows."""
    winreg = _winreg()
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            data, _ = winreg.QueryValueEx(key, VALUE_NAME)
            if data and data[0] % 2 == 1:      # in den Windows-Einstellungen deaktiviert
                return False
    except OSError:
        pass
    return True


def set_enabled(enabled: bool, remember: bool = True) -> None:
    winreg = _winreg()
    if remember:
        _remember(enabled)
    if winreg is None:
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except OSError:
                pass
    if enabled:
        # Eine frühere Deaktivierung in den Windows-Einstellungen aufheben.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_BINARY, APPROVED_ENABLED)
    log.info("Autostart mit Windows %s", "aktiviert" if enabled else "deaktiviert")


def sync() -> None:
    """Beim Start der Oberfläche: Registry an die gemerkte Wahl angleichen.

    Wurde der Autostart in den Windows-Einstellungen abgeschaltet, gilt das als
    Wahl des Benutzers und wird übernommen statt überschrieben.
    """
    if _winreg() is None:
        return
    current = is_enabled()
    want = wanted()
    try:
        if want and current is False and _approved_disabled():
            _remember(False)                    # in Windows abgeschaltet -> respektieren
        elif want and not current:
            set_enabled(True)
        elif want and current:
            set_enabled(True)                   # Befehl aktualisieren (z. B. neuer Pfad)
        elif not want and current:
            set_enabled(False)
    except OSError as exc:
        log.warning("Autostart konnte nicht eingerichtet werden: %s", exc)


def _approved_disabled() -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            data, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(data) and data[0] % 2 == 1
    except OSError:
        return False
