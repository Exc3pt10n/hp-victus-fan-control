from __future__ import annotations

import sys

from .base import BackendError, FanBackend


def create_backend(kind: str, config) -> FanBackend:
    if kind == "auto":
        kind = "windows" if sys.platform == "win32" else "linux"
    if kind == "windows":
        from .windows import WindowsBackend
        return WindowsBackend(config.windows)
    if kind == "linux":
        from .linux import LinuxBackend
        return LinuxBackend()
    if kind == "simulate":
        from .simulate import SimulatedBackend
        return SimulatedBackend()
    raise BackendError(f"Unbekanntes Backend '{kind}'")


__all__ = ["BackendError", "FanBackend", "create_backend"]
