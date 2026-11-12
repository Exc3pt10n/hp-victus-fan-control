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
Automatik. Beim Victus BV8D4EA setzt es feste Drehzahlen außerdem nur um,
solange ein kürzlich gesetzter Leistungsmodus sie "freigibt" - nach einer
Weile fällt es in seine eigene Automatik zurück. Der Dienst sendet deshalb
Drehzahl und Leistungsmodus regelmäßig erneut.

Benötigt Administratorrechte (der Installer richtet den Dienst als SYSTEM ein).
"""

from __future__ import annotations

import csv
import ctypes
import io
import logging
import subprocess
import time

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

# Anlaufhilfe: Ein stehender Lüfter läuft bei niedriger Vorgabe (≈ 1100 U/min)
# nicht von selbst an. Er bekommt dann kurz diese Stufe, bis er dreht.
KICK_LEVEL = 30          # ≈ 3000 U/min
SPINNING_RPM = 500       # ab hier gilt ein Lüfter als laufend
KICK_TIMEOUT = 15.0      # danach Anlaufhilfe abbrechen (Lüfter defekt/blockiert?)


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


def hp_processes() -> list[str]:
    """Namen laufender Prozesse von HP bzw. OMEN (über tasklist)."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=10,
                             creationflags=0x08000000)  # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError):
        return []
    names = set()
    for row in csv.reader(io.StringIO(out.stdout.decode("mbcs" if hasattr(ctypes, "windll") else "utf-8",
                                                       errors="replace"))):
        if row:
            name = row[0]
            low = name.lower()
            if "omen" in low or low.startswith("hp"):
                names.add(name)
    return sorted(names)[:12]


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
        self._bios_temp_ok: bool | None = None   # wird beim ersten Lesen geprüft
        self._fan_mode: str = ""
        self._mode_set_at: float | None = None
        self._levels: list[int] | None = None     # zuletzt vorgegebene Stufen
        self._kicking: set[int] = set()            # Lüfter mit aktiver Anlaufhilfe
        self._kick_started = 0.0
        self._cpu_sensor_id = "auto"
        self._cpu_sensor = None
        self._sensors: dict | None = None
        self.reconfigure(settings)

    def reconfigure(self, settings: dict) -> None:
        """Einstellungen übernehmen (auch zur Laufzeit nach Konfig-Änderung)."""
        self.max_cpu = int(settings["max_level_cpu"])
        self.max_gpu = int(settings["max_level_gpu"])
        self.min_level = int(settings.get("min_level", 0))
        self.keepalive = float(settings["keepalive"])
        # Leistungsmodus spätestens nach dieser Zeit erneut setzen (Freigabe auffrischen).
        self.mode_keepalive = float(settings.get("mode_keepalive", 50.0))
        sensor_id = settings.get("cpu_sensor", "auto") or "auto"
        if sensor_id != self._cpu_sensor_id:
            self._cpu_sensor_id = sensor_id
            self._cpu_sensor = None
            if sensor_id != "auto":
                from ..sensors import discover

                if self._sensors is None:
                    self._sensors = discover(self.bios)
                self._cpu_sensor = self._sensors.get(sensor_id)
                if self._cpu_sensor is None:
                    log.warning("CPU-Sensor '%s' nicht gefunden - nutze automatische Auswahl", sensor_id)
                else:
                    log.info("CPU-Temperatur von: %s", self._cpu_sensor.label)
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
        # "level" = stufenlos, "toggle" = nur Max an/aus (Sicherheitsnetz für BIOS
        # ohne Drehzahlvorgabe; der Hardware-Test stellt das bei Bedarf ein).
        self.supports_level = settings.get("mode", "level") != "toggle"
        log.info("Windows-Backend: %s, max. Stufen CPU %d / GPU %d",
                 "stufenlos" if self.supports_level else "Max an/aus", self.max_cpu, self.max_gpu)

    def read_cpu_temp(self) -> float | None:
        if self._cpu_sensor is not None:
            value = self._cpu_sensor.read()
            if value is not None:
                return value
        # Automatik: HP-BIOS-Sensor, sonst die wärmste ACPI-Thermalzone. Liefert
        # das BIOS beim ersten Versuch nichts (wie beim Victus BV8D4EA), wird es
        # danach nicht mehr gefragt.
        if self._bios_temp_ok is not False:
            try:
                t = self.bios.call(GET_TEMP, [0x01])[0]
            except Exception:  # noqa: BLE001
                t = 0
            if 0 < t < 120:
                self._bios_temp_ok = True
                return float(t)
            if self._bios_temp_ok is None:
                log.info("HP-BIOS liefert keine Temperatur - nutze ACPI-Thermalzone")
                self._bios_temp_ok = False
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
        if p == 0:
            return [0, 0]
        return [max(self.min_level, round(self.max_cpu * p)), max(self.min_level, round(self.max_gpu * p))]

    def expected_rpm(self, percent: float) -> list[int] | None:
        levels = self.levels_for(percent)
        if 0 in levels:
            return None  # bei Stufe 0 entscheidet das BIOS (Stillstand oder Minimum)
        return [lvl * 100 for lvl in levels]

    def set_levels(self, levels: list[int], kick: bool = True) -> None:
        """Lüfterstufen setzen. Mit ``kick`` bekommen stehende Lüfter, die laufen
        sollen, kurz eine höhere Stufe (Anlaufhilfe, siehe ``maintain``)."""
        if self._max_active:
            self.set_max(False)
        if self._fan_mode and self.seconds_since_mode() >= self.mode_keepalive:
            self.rearm(quiet=True)
        self._levels = list(levels)
        actual = list(levels)
        if kick:
            rpm = self.read_fan_rpm()
            stalled = {i for i, lvl in enumerate(levels)
                       if 0 < lvl < KICK_LEVEL and i < len(rpm) and rpm[i] < SPINNING_RPM}
            if stalled:
                if not self._kicking:
                    self._kick_started = time.monotonic()
                    log.info("Anlaufhilfe für Lüfter %s (stehen, Vorgabe %s)",
                             ", ".join(("CPU", "GPU")[i] for i in sorted(stalled)), levels)
                self._kicking = stalled
                for i in stalled:
                    actual[i] = KICK_LEVEL
            else:
                self._kicking = set()
        self.bios.call(SET_FAN_LEVEL, actual, out_size=0)

    @property
    def kicking(self) -> bool:
        return bool(self._kicking)

    def maintain(self) -> None:
        """Bei jedem Messzyklus: Anlaufhilfe beenden, sobald die Lüfter drehen."""
        if not self._kicking or self._levels is None:
            return
        rpm = self.read_fan_rpm()
        started = all(i < len(rpm) and rpm[i] >= SPINNING_RPM for i in self._kicking)
        if started or time.monotonic() - self._kick_started > KICK_TIMEOUT:
            if not started:
                sent = [KICK_LEVEL if i in self._kicking else lvl for i, lvl in enumerate(self._levels)]
                log.warning("Lüfter reagieren nicht auf die Vorgabe (Stufen %s, gemessen %s U/min) - "
                            "vermutlich regelt gerade das BIOS selbst", sent, rpm)
            self._kicking = set()
            self.bios.call(SET_FAN_LEVEL, self._levels, out_size=0)

    def seconds_since_mode(self) -> float:
        """Sekunden seit dem letzten Setzen des Leistungsmodus (inf = nie)."""
        return float("inf") if self._mode_set_at is None else time.monotonic() - self._mode_set_at

    def rearm(self, quiet: bool = False) -> None:
        """Aktuellen Leistungsmodus erneut setzen - gibt die Drehzahlvorgabe wieder frei.

        Ohne ``quiet`` (Reaktion auf eine Abweichung) zusätzlich "Max-Lüfter aus"
        senden: ein Versuch, das BIOS zuverlässiger aus seiner Automatik zu holen.
        """
        if not quiet:
            try:
                self.set_max(False)
            except Exception as exc:  # noqa: BLE001
                log.debug("Max-Lüfter aus fehlgeschlagen: %s", exc)
        if not self._fan_mode:
            return
        try:
            self.set_fan_mode(self._fan_mode, quiet=quiet)
        except Exception as exc:  # noqa: BLE001
            log.error("Leistungsmodus '%s' konnte nicht erneut gesetzt werden: %s", self._fan_mode, exc)

    def diagnostic_context(self) -> str:
        """Kurzinfo fürs Log, wenn die Lüfter abweichen: Stromversorgung und
        laufende HP-/OMEN-Software (könnte die Lüftersteuerung zurücksetzen)."""
        parts = ["Akku" if self.on_battery() else "Netzteil"]
        procs = hp_processes()
        parts.append("HP/OMEN-Prozesse: " + (", ".join(procs) if procs else "keine"))
        return "; ".join(parts)

    def set_percent(self, percent: float) -> None:
        if not self.supports_level:
            return super().set_percent(percent)
        self.set_levels(self.levels_for(percent))

    def set_max(self, enabled: bool) -> None:
        self.bios.call(SET_MAX_FAN, [1 if enabled else 0], out_size=0)
        self._max_active = enabled

    def set_fan_mode(self, name: str, quiet: bool = False) -> None:
        """BIOS-Leistungsmodus setzen; probiert neue und Legacy-Codes."""
        if name not in FAN_MODES:
            raise BackendError(f"Unbekannter Leistungsmodus '{name}'")
        errors = []
        for code in FAN_MODES[name]:
            try:
                self.bios.call(SET_FAN_MODE, [0xFF, code], out_size=0)
                self._mode_set_at = time.monotonic()
                (log.debug if quiet else log.info)("Leistungsmodus -> %s (0x%02X)", name, code)
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"0x{code:02X}: {exc}")
        raise BackendError(f"Leistungsmodus '{name}' abgelehnt ({'; '.join(errors)})")

    def shutdown(self) -> None:
        """Beim Beenden/Pausieren: Max aus und einen selbst gesetzten
        Leistungsmodus zurücksetzen. ``reconfigure`` setzt ihn wieder."""
        self.restore_auto()
        if self._fan_mode and self._fan_mode != "default":
            try:
                self.set_fan_mode("default")
            except Exception as exc:  # noqa: BLE001
                log.error("Leistungsmodus konnte nicht zurückgesetzt werden: %s", exc)
        self._fan_mode = ""

    def restore_auto(self) -> None:
        # Max abschalten; die stufenlose Vorgabe verfällt nach ~120 s von selbst.
        try:
            self.set_max(False)
        except Exception as exc:  # noqa: BLE001
            log.error("Automatikmodus konnte nicht wiederhergestellt werden: %s", exc)

    def describe(self) -> dict:
        info = super().describe()
        info.update(fan_mode=self._fan_mode or "(unverändert)", min_level=self.min_level,
                    cpu_sensor=self._cpu_sensor.label if self._cpu_sensor else "auto (BIOS, sonst ACPI)",
                    max_level_cpu=self.max_cpu, max_level_gpu=self.max_gpu, keepalive=self.keepalive)
        return info
