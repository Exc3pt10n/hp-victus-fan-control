"""Gemeinsame Schnittstelle aller Hardware-Backends."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod


class BackendError(RuntimeError):
    pass


class FanBackend(ABC):
    name = "base"
    #: True, wenn stufenlose Drehzahlen gesetzt werden können.
    supports_level = False
    #: True, wenn "Max-Lüfter" ein-/ausgeschaltet werden kann.
    supports_max = False
    #: Sekunden, nach denen ein unveränderter Wert erneut gesendet werden muss.
    keepalive: float | None = None

    @abstractmethod
    def read_cpu_temp(self) -> float | None: ...

    def read_gpu_temp(self) -> float | None:
        return _gpu.read()

    @abstractmethod
    def on_battery(self) -> bool: ...

    def read_fan_rpm(self) -> list[int]:
        return []

    def set_percent(self, percent: float) -> None:
        raise BackendError(f"{self.name}: Drehzahlsteuerung nicht unterstützt")

    def set_max(self, enabled: bool) -> None:
        raise BackendError(f"{self.name}: Max-Lüfter nicht unterstützt")

    def expected_rpm(self, percent: float) -> list[int] | None:
        """Erwartete Drehzahlen für eine Vorgabe (None = unbekannt)."""
        return None

    def reconfigure(self, settings: dict) -> None:
        """Backend-spezifische Einstellungen zur Laufzeit übernehmen."""

    @abstractmethod
    def restore_auto(self) -> None:
        """Steuerung an das BIOS/die Firmware zurückgeben."""

    def shutdown(self) -> None:
        """Beim Beenden des Dienstes aufgerufen."""
        self.restore_auto()

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "supports_level": self.supports_level,
            "supports_max": self.supports_max,
        }


class _NvidiaSmi:
    """Liest die GPU-Temperatur über nvidia-smi (mit Zwischenspeicher)."""

    def __init__(self, max_age: float = 4.0):
        self.max_age = max_age
        self._cache: tuple[float, float | None] | None = None
        self._exe = shutil.which("nvidia-smi")
        self._disabled = self._exe is None

    def read(self) -> float | None:
        if self._disabled:
            return None
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.max_age:
            return self._cache[1]
        value = None
        try:
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            out = subprocess.run(
                [self._exe, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, creationflags=flags,
            )
            if out.returncode == 0 and out.stdout.strip():
                value = float(out.stdout.strip().splitlines()[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            value = None
        self._cache = (now, value)
        return value


_gpu = _NvidiaSmi()
