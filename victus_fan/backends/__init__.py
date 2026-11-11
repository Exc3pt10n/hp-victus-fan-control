from __future__ import annotations

from .base import BackendError, FanBackend

BACKENDS = ("windows", "simulate")


def create_backend(kind: str, config) -> FanBackend:
    if kind in ("auto", "windows"):
        from .windows import WindowsBackend
        return WindowsBackend(config.windows)
    if kind == "simulate":
        from .simulate import SimulatedBackend
        return SimulatedBackend()
    raise BackendError(f"Unbekanntes Backend '{kind}'")


__all__ = ["BACKENDS", "BackendError", "FanBackend", "create_backend"]
