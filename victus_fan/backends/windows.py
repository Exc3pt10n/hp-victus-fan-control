"""Windows-Backend über die HP-BIOS-WMI-Schnittstelle (root\\wmi, hpqBIntM).

Das ist dieselbe Schnittstelle, die auch OMEN Gaming Hub nutzt. Die Befehls-
codes stammen aus der öffentlichen Community-Dokumentation (z. B. OmenMon):

=========  ====================================================
Command    0x20008 (Standard), Signatur "SECU"
0x23       GetTemp        -> Byte 0: Temperatur in °C
0x2D       GetFanLevel    -> [CPU, GPU] in 100 U/min
0x2E       SetFanLevel    <- [CPU, GPU] in 100 U/min
0x26/0x27  Get/SetMaxFan  <- [1] an, [0] aus
0x1A       SetFanMode     <- [0xFF, Modus]  (Leistungsmodus/Thermal Policy)
=========  ====================================================

Wichtig: Das BIOS übernimmt nach etwa 120 s ohne neuen Befehl wieder die
Automatik. Der Dienst sendet die Werte deshalb regelmäßig erneut.

Benötigt Administratorrechte (der Installer richtet den Dienst als SYSTEM ein).
"""

from __future__ import annotations

import ctypes
import logging

from .base import BackendError, FanBackend

log = logging.getLogger(__name__)

CMD_DEFAULT = 0x20008
GET_TEMP = 0x23
GET_MAX_FAN = 0x26
SET_MAX_FAN = 0x27
GET_FAN_LEVEL = 0x2D
SET_FAN_LEVEL = 0x2E
SET_FAN_MODE = 0x1A

# Leistungsmodi: neuere Geräte nutzen 0x30/0x31/0x50, ältere die Legacy-Codes.
FAN_MODES = {
    "default": (0x30, 0x00),
    "performance": (0x31, 0x01),
    "cool": (0x50, 0x02),
}

SIGN = [0x53, 0x45, 0x43, 0x55]  # "SECU"


class HpBios:
    """Dünne Hülle um hpqBIntM.hpqBIOSIntXXX."""

    def __init__(self):
        try:
            import win32com.client  # type: ignore
        except ImportError as exc:  # pragma: no cover - nur unter Windows
            raise BackendError("pywin32 fehlt: pip install pywin32") from exc
        locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        self.svc = locator.ConnectServer(".", "root\\wmi")
        objs = list(self.svc.ExecQuery("SELECT * FROM hpqBIntM"))
        if not objs:
            raise BackendError("HP-BIOS-WMI (hpqBIntM) nicht gefunden - kein unterstütztes HP-Gerät?")
        self.bios = objs[0]
        self.in_class = self.svc.Get("hpqBDataIn")

    def call(self, command_type: int, data: list[int] | None = None, out_size: int = 128,
             command: int = CMD_DEFAULT) -> list[int]:
        data = list(data or [])
        data += [0] * (4 - len(data)) if len(data) < 4 else []
        inst = self.in_class.SpawnInstance_()
        inst.Properties_.Item("Sign").Value = SIGN
        inst.Properties_.Item("Command").Value = command
        inst.Properties_.Item("CommandType").Value = command_type
        inst.Properties_.Item("Size").Value = len(data)
        inst.Properties_.Item("hpqBData").Value = data

        method_name = {0: "hpqBIOSInt0", 4: "hpqBIOSInt4", 128: "hpqBIOSInt128"}[out_size]
        method = self.bios.Methods_.Item(method_name)
        params = method.InParameters.SpawnInstance_()
        params.Properties_.Item("InData").Value = inst
        result = self.bios.ExecMethod_(method_name, params)
        out = result.Properties_.Item("OutData").Value
        rc = out.Properties_.Item("rwReturnCode").Value
        if rc != 0:
            raise BackendError(f"BIOS-Befehl 0x{command_type:02X} abgelehnt (Code {rc})")
        if out_size == 0:
            return []
        return list(out.Properties_.Item("Data").Value or [])


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


class WindowsBackend(FanBackend):
    name = "windows-hp-bios"
    supports_max = True

    def __init__(self, settings: dict):
        self.bios = HpBios()
        self._max_active: bool | None = None
        self._probed: bool | None = None
        self._fan_mode: str = ""
        self.reconfigure(settings)

    def reconfigure(self, settings: dict) -> None:
        """Einstellungen übernehmen (auch zur Laufzeit nach Konfig-Änderung)."""
        self.max_cpu = int(settings["max_level_cpu"])
        self.max_gpu = int(settings["max_level_gpu"])
        self.keepalive = float(settings["keepalive"])
        fan_mode = settings.get("fan_mode", "") or ""
        if fan_mode != self._fan_mode:
            if fan_mode:
                try:
                    self.set_fan_mode(fan_mode)
                except Exception as exc:  # noqa: BLE001
                    log.error("Leistungsmodus '%s' konnte nicht gesetzt werden: %s", fan_mode, exc)
            elif self._fan_mode:
                self.set_fan_mode("default")
            self._fan_mode = fan_mode
        mode = settings.get("mode", "auto")
        if mode == "level":
            self.supports_level = True
        elif mode == "toggle":
            self.supports_level = False
        else:
            if self._probed is None:
                self._probed = self._probe_level()
            self.supports_level = self._probed
        log.info("Windows-Backend: %s, max. Stufen CPU %d / GPU %d",
                 "stufenlos" if self.supports_level else "Max an/aus", self.max_cpu, self.max_gpu)

    def _probe_level(self) -> bool:
        try:
            current = self.bios.call(GET_FAN_LEVEL)[:2]
            self.bios.call(SET_FAN_LEVEL, current, out_size=0)
            log.info("Stufenlose Lüftersteuerung verfügbar")
            return True
        except Exception as exc:  # noqa: BLE001 - WMI wirft COM-Fehler
            log.info("Stufenlose Steuerung nicht verfügbar (%s) - nutze Max an/aus", exc)
            return False

    def read_cpu_temp(self) -> float | None:
        try:
            t = self.bios.call(GET_TEMP, [0x01])[0]
            if 0 < t < 120:
                return float(t)
        except Exception as exc:  # noqa: BLE001
            log.debug("BIOS-Temperatur nicht lesbar: %s", exc)
        return self._acpi_temp()

    def _acpi_temp(self) -> float | None:
        try:
            temps = [
                z.CurrentTemperature / 10.0 - 273.15
                for z in self.bios.svc.ExecQuery("SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature")
            ]
            temps = [t for t in temps if 0 < t < 120]
            return max(temps) if temps else None
        except Exception:  # noqa: BLE001
            return None

    def on_battery(self) -> bool:
        status = _SystemPowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):  # type: ignore[attr-defined]
            return status.ACLineStatus == 0
        return False

    def read_fan_rpm(self) -> list[int]:
        try:
            return [lvl * 100 for lvl in self.bios.call(GET_FAN_LEVEL)[:2]]
        except Exception:  # noqa: BLE001
            return []

    def levels_for(self, percent: float) -> list[int]:
        p = max(0.0, min(100.0, percent)) / 100
        return [round(self.max_cpu * p), round(self.max_gpu * p)]

    def expected_rpm(self, percent: float) -> list[int] | None:
        return [lvl * 100 for lvl in self.levels_for(percent)]

    def set_levels(self, levels: list[int]) -> None:
        """Lüfterstufen direkt setzen (auch für den Hardware-Test)."""
        if self._max_active:
            self.set_max(False)
        self.bios.call(SET_FAN_LEVEL, levels, out_size=0)

    def set_percent(self, percent: float) -> None:
        if not self.supports_level:
            return super().set_percent(percent)
        self.set_levels(self.levels_for(percent))

    def set_max(self, enabled: bool) -> None:
        self.bios.call(SET_MAX_FAN, [1 if enabled else 0], out_size=0)
        self._max_active = enabled

    def set_fan_mode(self, name: str) -> None:
        """BIOS-Leistungsmodus setzen; probiert neue und Legacy-Codes."""
        if name not in FAN_MODES:
            raise BackendError(f"Unbekannter Leistungsmodus '{name}'")
        errors = []
        for code in FAN_MODES[name]:
            try:
                self.bios.call(SET_FAN_MODE, [0xFF, code], out_size=0)
                log.info("Leistungsmodus -> %s (0x%02X)", name, code)
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"0x{code:02X}: {exc}")
        raise BackendError(f"Leistungsmodus '{name}' abgelehnt ({'; '.join(errors)})")

    def shutdown(self) -> None:
        """Beim Beenden: auch einen selbst gesetzten Leistungsmodus zurücksetzen."""
        self.restore_auto()
        if self._fan_mode and self._fan_mode != "default":
            try:
                self.set_fan_mode("default")
            except Exception as exc:  # noqa: BLE001
                log.error("Leistungsmodus konnte nicht zurückgesetzt werden: %s", exc)

    def restore_auto(self) -> None:
        # Max abschalten; die stufenlose Vorgabe verfällt nach ~120 s von selbst.
        try:
            self.set_max(False)
        except Exception as exc:  # noqa: BLE001
            log.error("Automatikmodus konnte nicht wiederhergestellt werden: %s", exc)

    def describe(self) -> dict:
        info = super().describe()
        info.update(fan_mode=self._fan_mode or "(unverändert)", max_level_cpu=self.max_cpu, max_level_gpu=self.max_gpu, keepalive=self.keepalive)
        return info
