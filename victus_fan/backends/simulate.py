"""Simulations-Backend zum Ausprobieren ohne Hardwarezugriff."""

from __future__ import annotations

import math
import time

from .base import FanBackend


class SimulatedBackend(FanBackend):
    name = "simulate"
    supports_level = True
    supports_max = True

    def __init__(self):
        self.start = time.monotonic()
        self.percent = 0.0
        self.max = False

    def _load(self) -> float:
        # Last wechselt periodisch zwischen Leerlauf und Volllast.
        t = time.monotonic() - self.start
        return 0.5 + 0.5 * math.sin(t / 30)

    def read_cpu_temp(self) -> float | None:
        cooling = (100 if self.max else self.percent) * 0.2
        return round(40 + 50 * self._load() - cooling, 1)

    def read_gpu_temp(self) -> float | None:
        return round(38 + 40 * self._load(), 1)

    def on_battery(self) -> bool:
        return False

    def read_fan_rpm(self) -> list[int]:
        p = 100 if self.max else self.percent
        return [int(55 * p), int(57 * p)]

    def set_percent(self, percent: float) -> None:
        self.percent = percent

    def set_max(self, enabled: bool) -> None:
        self.max = enabled

    def restore_auto(self) -> None:
        self.max = False
