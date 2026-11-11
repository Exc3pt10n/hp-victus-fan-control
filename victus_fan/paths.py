"""Standardpfade für Konfiguration, Status und Log."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "VictusFan"
else:
    DATA_DIR = Path("/etc/victus-fan")

CONFIG_FILE = DATA_DIR / "config.toml"
# Vom Dienst geschrieben: aktueller Zustand für "victus-fan status".
STATUS_FILE = (DATA_DIR if sys.platform == "win32" else Path("/run/victus-fan")) / "status.json"
# Vom Benutzer geschrieben: Profil-Override für "victus-fan profile <name>".
OVERRIDE_FILE = (DATA_DIR if sys.platform == "win32" else Path("/run/victus-fan")) / "override.json"
LOG_FILE = DATA_DIR / "victus-fan.log" if sys.platform == "win32" else None

# Installation und Auto-Update (Windows)
INSTALL_DIR = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "VictusFan"
# Vom Installer geschrieben: installierter Stand (Commit) - auch die Oberfläche
# erkennt darüber, dass ein Update installiert wurde.
INSTALL_INFO = INSTALL_DIR / "installed.json"
# Vom Updater geschrieben: Ergebnis der letzten Prüfung
UPDATE_STATUS = DATA_DIR / "update_status.json"
UPDATE_LOG = DATA_DIR / "update.log"
UPDATE_TASK = "VictusFanUpdate"
