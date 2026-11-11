"""Austausch zwischen Dienst (SYSTEM) und Oberfläche (Benutzer) über zwei Dateien.

* status.json   - schreibt der Dienst, liest die Oberfläche
* override.json - schreibt die Oberfläche/CLI, liest der Dienst
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import paths

MODES = ("normal", "max", "paused")
# BIOS-Leistungsmodus: "auto" = Wert aus der config.toml ([windows] fan_mode)
POWER_MODES = ("auto", "default", "cool", "performance")
DEFAULT_OVERRIDE = {"profile": "auto", "mode": "normal", "power_mode": "auto"}
# Älter als das gilt der Status als veraltet (Dienst läuft nicht).
STALE_AFTER = 15.0


def read_override(path: Path | None = None) -> dict:
    path = path or paths.OVERRIDE_FILE
    data = dict(DEFAULT_OVERRIDE)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            data.update({k: v for k, v in loaded.items() if k in DEFAULT_OVERRIDE and isinstance(v, str)})
    except (OSError, ValueError):
        pass
    if data["mode"] not in MODES:
        data["mode"] = "normal"
    if data["power_mode"] not in POWER_MODES:
        data["power_mode"] = "auto"
    return data


def write_override(path: Path | None = None, **changes) -> dict:
    path = path or paths.OVERRIDE_FILE
    data = read_override(path)
    data.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Direkt (nicht über eine Temp-Datei) schreiben, damit die vom Installer
    # gesetzten Dateirechte für normale Benutzer erhalten bleiben.
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def read_status(path: Path | None = None) -> dict | None:
    """Status des Dienstes oder None. Setzt ``stale``, wenn er zu alt ist."""
    path = path or paths.STATUS_FILE
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ts = status.get("timestamp")
    status["stale"] = not isinstance(ts, (int, float)) or time.time() - ts > STALE_AFTER
    return status
