"""Windows-Helfer der Oberfläche: Dateien öffnen, Befehle mit Adminrechten,
Dienst neu starten, Update-Suche anstoßen und Einzelinstanz (Mutex)."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

from .. import paths

TASK_NAME = "VictusFanControl"

# --------------------------------------------------------------------------
# Systemaktionen
# --------------------------------------------------------------------------
NOT_WINDOWS = "Nur unter Windows verfügbar."


def open_file(path: Path) -> None:
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
    subprocess.Popen(["notepad.exe", str(path)])


def run_elevated_console(config_path: Path, command: str) -> None:
    """Startet einen victus-fan-Befehl in einem sichtbaren Konsolenfenster als Administrator."""
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
    import ctypes

    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    params = f'-m victus_fan -c "{config_path}" {command}'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), params, None, 1)  # type: ignore[attr-defined]
    if rc <= 32:
        raise OSError("Abgebrochen oder konnte nicht gestartet werden")


def run_selftest(config_path: Path) -> None:
    run_elevated_console(config_path, "selftest --apply --wait")


def start_update_check() -> None:
    """Startet die Update-Aufgabe sofort (fragt nach Administratorrechten)."""
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
    import ctypes

    rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", "schtasks.exe", f'/Run /TN "{paths.UPDATE_TASK}"', None, 0)
    if rc <= 32:
        raise OSError("Update-Suche abgebrochen oder konnte nicht gestartet werden")


def restart_service() -> None:
    """Startet den Hintergrunddienst neu (fragt nach Administratorrechten)."""
    if sys.platform != "win32":
        raise OSError(NOT_WINDOWS)
    import ctypes

    script = (
        f"Stop-ScheduledTask -TaskName {TASK_NAME} -ErrorAction SilentlyContinue; "
        "Get-CimInstance Win32_Process -Filter \"Name = 'pythonw.exe' OR Name = 'python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*victus_fan run*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        f"Start-ScheduledTask -TaskName {TASK_NAME}"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", "powershell.exe",
        f"-NoProfile -WindowStyle Hidden -EncodedCommand {encoded}", None, 0)
    if rc <= 32:
        raise OSError("Neustart abgebrochen oder fehlgeschlagen")


class SingleInstance:
    """Verhindert doppelte Tray-Icons. Ein zweiter Start bittet die laufende
    Instanz stattdessen, ihr Fenster zu zeigen."""

    def __init__(self):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "VictusFan"
        base.mkdir(parents=True, exist_ok=True)
        self.show_flag = base / "show.flag"
        self.primary = True
        if sys.platform == "win32":
            import ctypes

            self._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VictusFanTray")  # type: ignore[attr-defined]
            self.primary = ctypes.windll.kernel32.GetLastError() != 183  # type: ignore[attr-defined]

    def release(self) -> None:
        """Mutex freigeben, damit eine neue Instanz starten kann."""
        if sys.platform == "win32" and getattr(self, "_mutex", None):
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._mutex)  # type: ignore[attr-defined]
            self._mutex = None

    def request_show(self) -> None:
        self.show_flag.write_text("1")

    def show_requested(self) -> bool:
        if self.show_flag.exists():
            try:
                self.show_flag.unlink()
            except OSError:
                pass
            return True
        return False


