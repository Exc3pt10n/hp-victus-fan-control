"""Linux-Backend über den Kernel-Treiber ``hp-wmi``.

Der Treiber stellt unter /sys/class/hwmon/hwmonX (name = "hp") bereit:

* ``pwm1_enable``: 0 = volle Drehzahl, 2 = Automatik, 1 = manuell
  (manuell nur bei neueren Kerneln/Modellen, dann gibt es auch ``pwm1``)
* ``fan1_input`` / ``fan2_input``: Drehzahl in U/min
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import BackendError, FanBackend

log = logging.getLogger(__name__)

HWMON = Path("/sys/class/hwmon")
POWER = Path("/sys/class/power_supply")
PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")

PWM_FULL, PWM_MANUAL, PWM_AUTO = 0, 1, 2


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _find_hwmon(name: str) -> Path | None:
    if not HWMON.exists():
        return None
    for d in sorted(HWMON.iterdir()):
        if _read(d / "name") == name:
            return d
    return None


class LinuxBackend(FanBackend):
    name = "linux-hp-wmi"

    def __init__(self):
        self.hp = _find_hwmon("hp")
        if self.hp is None:
            raise BackendError(
                "Kein hp-wmi hwmon-Gerät gefunden. Ist das Modul geladen? (sudo modprobe hp-wmi)"
            )
        self.pwm_enable = self.hp / "pwm1_enable"
        self.pwm = self.hp / "pwm1"
        if not self.pwm_enable.exists():
            raise BackendError(f"{self.pwm_enable} fehlt - dieser Kernel kann die Lüfter nicht steuern")
        self.supports_max = True
        self.supports_level = self.pwm.exists() and self._probe_manual()
        self._cpu_sensor = self._find_cpu_sensor()
        self._gpu_sensor = self._find_gpu_sensor()

    def _probe_manual(self) -> bool:
        before = _read(self.pwm_enable)
        try:
            self.pwm_enable.write_text(str(PWM_MANUAL))
            ok = _read(self.pwm_enable) == str(PWM_MANUAL)
        except OSError:
            ok = False
        finally:
            if before is not None:
                try:
                    self.pwm_enable.write_text(before)
                except OSError:
                    pass
        log.info("Manuelle PWM-Steuerung %s", "verfügbar" if ok else "nicht verfügbar")
        return ok

    # --- Sensoren -------------------------------------------------------
    def _find_cpu_sensor(self) -> Path | None:
        for chip, labels in (("coretemp", ("Package id 0",)), ("k10temp", ("Tctl", "Tdie"))):
            d = _find_hwmon(chip)
            if d is None:
                continue
            for label_file in sorted(d.glob("temp*_label")):
                if _read(label_file) in labels:
                    return label_file.with_name(label_file.name.replace("_label", "_input"))
            first = d / "temp1_input"
            if first.exists():
                return first
        # Fallback: ACPI-Thermalzone
        for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            if _read(z / "type") in ("x86_pkg_temp", "acpitz"):
                return z / "temp"
        return None

    def _find_gpu_sensor(self) -> Path | None:
        d = _find_hwmon("amdgpu")
        if d is not None and (d / "temp1_input").exists():
            # Bei AMD-iGPU + NVIDIA-dGPU liefert nvidia-smi die relevantere Zahl.
            return d / "temp1_input"
        return None

    def read_cpu_temp(self) -> float | None:
        if self._cpu_sensor is None:
            return None
        raw = _read(self._cpu_sensor)
        return int(raw) / 1000.0 if raw and raw.lstrip("-").isdigit() else None

    def read_gpu_temp(self) -> float | None:
        temps = [super().read_gpu_temp()]
        if self._gpu_sensor is not None:
            raw = _read(self._gpu_sensor)
            if raw and raw.isdigit():
                temps.append(int(raw) / 1000.0)
        temps = [t for t in temps if t is not None]
        return max(temps) if temps else None

    def on_battery(self) -> bool:
        if not POWER.exists():
            return False
        for supply in POWER.iterdir():
            if _read(supply / "type") == "Mains":
                return _read(supply / "online") == "0"
        return False

    def read_fan_rpm(self) -> list[int]:
        rpms = []
        for f in sorted(self.hp.glob("fan*_input")):
            raw = _read(f)
            if raw and raw.isdigit():
                rpms.append(int(raw))
        return rpms

    # --- Aktoren --------------------------------------------------------
    def _write(self, path: Path, value: int) -> None:
        try:
            path.write_text(str(value))
        except OSError as exc:
            raise BackendError(f"Schreiben nach {path} fehlgeschlagen: {exc}") from exc

    def set_percent(self, percent: float) -> None:
        if not self.supports_level:
            return super().set_percent(percent)
        if _read(self.pwm_enable) != str(PWM_MANUAL):
            self._write(self.pwm_enable, PWM_MANUAL)
        self._write(self.pwm, round(max(0.0, min(100.0, percent)) * 255 / 100))

    def set_max(self, enabled: bool) -> None:
        self._write(self.pwm_enable, PWM_FULL if enabled else PWM_AUTO)

    def set_platform_profile(self, profile: str) -> None:
        choices = _read(PLATFORM_PROFILE.with_name("platform_profile_choices")) or ""
        if profile not in choices.split():
            log.debug("Plattformprofil '%s' nicht verfügbar (%s)", profile, choices)
            return
        if _read(PLATFORM_PROFILE) != profile:
            try:
                PLATFORM_PROFILE.write_text(profile)
                log.info("Plattformprofil -> %s", profile)
            except OSError as exc:
                log.warning("Plattformprofil setzen fehlgeschlagen: %s", exc)

    def restore_auto(self) -> None:
        try:
            self.pwm_enable.write_text(str(PWM_AUTO))
        except OSError as exc:
            log.error("Automatikmodus konnte nicht wiederhergestellt werden: %s", exc)

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            hwmon=str(self.hp),
            cpu_sensor=str(self._cpu_sensor),
            gpu_sensor=str(self._gpu_sensor),
            pwm1_enable=_read(self.pwm_enable),
            platform_profile=_read(PLATFORM_PROFILE),
            platform_profile_choices=_read(PLATFORM_PROFILE.with_name("platform_profile_choices")),
        )
        return info
