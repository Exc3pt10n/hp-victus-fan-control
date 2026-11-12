"""Standardpfade für Konfiguration, Status, Logs und Installation (Windows)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "VictusFan"
    INSTALL_DIR = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "VictusFan"
else:  # nur für Entwicklung/Tests (z. B. --backend simulate)
    DATA_DIR = Path.home() / ".victus-fan"
    INSTALL_DIR = DATA_DIR / "install"

CONFIG_FILE = DATA_DIR / "config.toml"
# Vom Dienst geschrieben: aktueller Zustand für Oberfläche und "victus-fan status".
STATUS_FILE = DATA_DIR / "status.json"
# Von Oberfläche/CLI geschrieben: Lüfterprofil und Steuerung.
OVERRIDE_FILE = DATA_DIR / "override.json"
LOG_FILE = DATA_DIR / "victus-fan.log"

# Vom Installer geschrieben: installierter Stand (Commit). Die Oberfläche
# erkennt daran auch, dass ein Update installiert wurde.
INSTALL_INFO = INSTALL_DIR / "installed.json"
# Vom Updater geschrieben
UPDATE_STATUS = DATA_DIR / "update_status.json"
UPDATE_LOG = DATA_DIR / "update.log"
UPDATE_INSTALL_LOG = DATA_DIR / "update-install.log"
UPDATE_TASK = "VictusFanUpdate"
