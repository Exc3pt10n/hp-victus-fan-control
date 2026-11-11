"""Temperaturquellen finden und vergleichen.

``discover()`` liefert alle lesbaren Quellen als ``{id: Sensor}``. ``measure()``
erzeugt kurz CPU-Last und zeichnet alle Quellen auf - so lässt sich objektiv
erkennen, welcher Sensor die CPU-Temperatur am schnellsten und deutlichsten
abbildet. Die gewählte Quelle wird als ``[windows] cpu_sensor`` gespeichert.
"""

from __future__ import annotations

import logging
import multiprocessing
import sys
import time
from dataclasses import dataclass, field
from statistics import mean
from typing import Callable

log = logging.getLogger(__name__)

VALID = (1.0, 125.0)


@dataclass
class Sensor:
    id: str
    label: str
    read: Callable[[], float | None]
    gpu: bool = False


def _valid(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if VALID[0] < v < VALID[1] else None


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
def _wmi(namespace: str):
    import win32com.client  # type: ignore

    return win32com.client.Dispatch("WbemScripting.SWbemLocator").ConnectServer(".", namespace)


def _query(svc, wql: str) -> list:
    try:
        return list(svc.ExecQuery(wql))
    except Exception:  # noqa: BLE001 - Klasse/Namespace fehlt
        return []


def _windows_sensors(bios) -> list[Sensor]:
    sensors: list[Sensor] = []

    # 1) HP-BIOS (liefert beim Victus BV8D4EA nichts, bei anderen Modellen schon)
    if bios is not None:
        from .backends.windows import GET_TEMP

        def read_bios():
            try:
                return _valid(bios.call(GET_TEMP, [0x01])[0])
            except Exception:  # noqa: BLE001
                return None
        sensors.append(Sensor("bios", "HP-BIOS", read_bios))

    # 2) ACPI-Thermalzonen über root\wmi (oft nur selten aktualisiert)
    try:
        wmi = _wmi("root\\wmi")
        for zone in _query(wmi, "SELECT InstanceName FROM MSAcpi_ThermalZoneTemperature"):
            name = zone.InstanceName

            def read_acpi(name=name, wmi=wmi):
                for z in _query(wmi, "SELECT InstanceName, CurrentTemperature FROM MSAcpi_ThermalZoneTemperature"):
                    if z.InstanceName == name:
                        return _valid(z.CurrentTemperature / 10.0 - 273.15)
                return None
            sensors.append(Sensor(f"acpi:{name}", f"ACPI {name}", read_acpi))
    except Exception as exc:  # noqa: BLE001
        log.debug("root\\wmi nicht verfügbar: %s", exc)

    # 3) Dieselben Zonen über die Leistungsindikatoren (laufend aktualisiert)
    try:
        cim = _wmi("root\\cimv2")
        cls = "Win32_PerfFormattedData_Counters_ThermalZoneInformation"
        for zone in _query(cim, f"SELECT Name FROM {cls}"):
            name = zone.Name

            def read_perf(name=name, cim=cim, cls=cls):
                for z in _query(cim, f"SELECT Name, HighPrecisionTemperature, Temperature FROM {cls}"):
                    if z.Name == name:
                        hp = getattr(z, "HighPrecisionTemperature", None)
                        kelvin = hp / 10.0 if hp else z.Temperature
                        return _valid(kelvin - 273.15)
                return None
            sensors.append(Sensor(f"perf:{name}", f"Leistungsindikator {name}", read_perf))
    except Exception as exc:  # noqa: BLE001
        log.debug("root\\cimv2 nicht verfügbar: %s", exc)

    # 4) LibreHardwareMonitor / OpenHardwareMonitor (falls installiert und gestartet)
    for prefix, ns in (("lhm", "root\\LibreHardwareMonitor"), ("ohm", "root\\OpenHardwareMonitor")):
        try:
            svc = _wmi(ns)
        except Exception:  # noqa: BLE001
            continue
        for s in _query(svc, "SELECT Identifier, Name FROM Sensor WHERE SensorType='Temperature'"):
            ident, name = s.Identifier, s.Name

            def read_hm(ident=ident, svc=svc):
                for x in _query(svc, f"SELECT Value FROM Sensor WHERE Identifier='{ident}'"):
                    return _valid(x.Value)
                return None
            sensors.append(Sensor(f"{prefix}:{ident}", f"{prefix.upper()} {name} ({ident})", read_hm,
                                  gpu="gpu" in ident.lower()))
    return sensors


def discover(bios=None) -> dict[str, Sensor]:
    from .backends.base import _gpu

    found = _windows_sensors(bios) if sys.platform == "win32" else []
    found.append(Sensor("nvidia-smi", "NVIDIA GPU (nvidia-smi)", _gpu.read, gpu=True))
    # Nur Quellen behalten, die tatsächlich etwas liefern.
    return {s.id: s for s in found if s.read() is not None}


# --------------------------------------------------------------------------
# Messung unter Last
# --------------------------------------------------------------------------
def _burn(until: float) -> None:
    x = 0
    while time.time() < until:
        x = (x * 31 + 7) % 1_000_003


@dataclass
class Trace:
    sensor: Sensor
    idle: list[float] = field(default_factory=list)
    load: list[tuple[float, float]] = field(default_factory=list)   # (Sekunde, Wert)
    cool: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        idle = mean(self.idle) if self.idle else None
        values = [v for _, v in self.load]
        peak = max(values) if values else None
        rise = None if idle is None or peak is None else round(peak - idle, 1)
        react = None
        if idle is not None:
            react = next((t for t, v in self.load if v >= idle + 3), None)
        distinct = len({*self.idle, *values, *self.cool})
        return {
            "id": self.sensor.id, "label": self.sensor.label, "gpu": self.sensor.gpu,
            "idle": None if idle is None else round(idle, 1), "peak": peak, "rise": rise,
            "react_s": react, "static": distinct <= 1,
        }


def measure(sensors: dict[str, Sensor], idle: float = 6, load: float = 20, cool: float = 6,
            interval: float = 1.0, burn: bool = True, say=print) -> list[dict]:
    traces = {sid: Trace(s) for sid, s in sensors.items()}

    def sample(phase: str, start: float, seconds: float, label: str) -> None:
        end = start + seconds
        while time.time() < end:
            t0 = time.time()
            for tr in traces.values():
                v = tr.sensor.read()
                if v is None:
                    continue
                if phase == "load":
                    tr.load.append((round(t0 - start, 1), v))
                else:
                    getattr(tr, phase).append(v)
            say(f"\r  {label}: noch {max(0, int(end - time.time()))} s   ", end="")
            time.sleep(max(0.0, interval - (time.time() - t0)))
        say("")

    sample("idle", time.time(), idle, "Leerlauf messen")
    procs = []
    start = time.time()
    if burn:
        until = start + load
        for _ in range(multiprocessing.cpu_count()):
            p = multiprocessing.Process(target=_burn, args=(until,), daemon=True)
            p.start()
            procs.append(p)
    try:
        sample("load", start, load, "Volle CPU-Last")
    finally:
        for p in procs:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
    sample("cool", time.time(), cool, "Abkühlen")
    return [tr.summary() for tr in traces.values()]


def recommend(results: list[dict]) -> dict | None:
    """Bester CPU-Sensor: reagiert deutlich (>= 5 °C) und am schnellsten."""
    candidates = [r for r in results if not r["gpu"] and not r["static"]
                  and r["rise"] is not None and r["rise"] >= 5 and r["react_s"] is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (r["react_s"], -r["rise"]))


def report(results: list[dict], best: dict | None, say=print) -> None:
    say(f"\n{'Quelle':<52} {'Leerlauf':>8} {'Last max':>8} {'Anstieg':>8} {'Reaktion':>9}")
    for r in sorted(results, key=lambda r: (r["gpu"], -(r["rise"] or -99))):
        def f(v, unit="°C"):
            return "-" if v is None else f"{v:.1f} {unit}"
        note = "  statisch!" if r["static"] else ("  (GPU)" if r["gpu"] else "")
        mark = "→ " if best and r["id"] == best["id"] else "  "
        say(f"{mark}{r['label'][:50]:<50} {f(r['idle']):>8} {f(r['peak']):>8} {f(r['rise']):>8} "
            f"{f(r['react_s'], 's'):>9}{note}")
    if best:
        say(f"\nEmpfohlene CPU-Quelle: {best['label']}  (id: {best['id']})")
    else:
        say("\nKeine Quelle reagiert deutlich auf CPU-Last.")
