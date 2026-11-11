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
