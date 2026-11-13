"""Regelkern: aus Temperaturen wird eine Ziel-Lüfterleistung (0-100 %).

Die Logik ist bewusst plattformunabhängig und rein rechnerisch, damit sie
ohne Hardware getestet werden kann.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import MAX_CRITICAL_TEMP, Profile

# Feste Untergrenze, unabhängig von Profil und Konfiguration: Auch eine (versehentlich)
# flach eingestellte Kurve kann die Lüfter bei hohen Temperaturen nicht still halten.
# Die mitgelieferten Profile liegen überall darüber.
SAFETY_FLOOR = [(0.0, 0.0), (80.0, 0.0), (88.0, 60.0), (MAX_CRITICAL_TEMP, 100.0)]


def interpolate(curve: list[tuple[float, float]], temp: float) -> float:
    """Lineare Interpolation auf einer Kurve aus (Temperatur, Prozent)."""
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t1, p1), (t2, p2) in zip(curve, curve[1:]):
        if temp <= t2:
            return p1 + (p2 - p1) * (temp - t1) / (t2 - t1)
    return curve[-1][1]


@dataclass
class Reading:
    cpu_temp: float | None
    gpu_temp: float | None = None
    on_battery: bool = False


@dataclass
class Decision:
    percent: float          # gesetzte Leistung nach Rampen/Hysterese
    demand: float           # roher Bedarf laut Kurve
    cpu_smooth: float | None
    gpu_smooth: float | None
    critical: bool
    profile: str


class FanController:
    def __init__(self, control: dict, critical_temp: float):
        self.configure(control, critical_temp)
        self.percent = 0.0
        self._cpu: float | None = None
        self._gpu: float | None = None
        self._lower_since: float | None = None
        self._last_time: float | None = None

    def configure(self, control: dict, critical_temp: float) -> None:
        """Parameter setzen; der aktuelle Zustand bleibt erhalten."""
        self.smoothing_up = float(control["smoothing_up"])
        self.smoothing_down = float(control["smoothing_down"])
        self.hysteresis = float(control["hysteresis"])
        self.spin_down_delay = float(control["spin_down_delay"])
        self.ramp_up_rate = float(control["ramp_up_rate"])
        self.ramp_down_rate = float(control["ramp_down_rate"])
        self.min_running = float(control["min_running_percent"])
        self.critical_temp = min(float(critical_temp), MAX_CRITICAL_TEMP)

    def _smooth(self, prev: float | None, value: float | None) -> float | None:
        if value is None:
            return prev
        if prev is None:
            return value
        alpha = self.smoothing_up if value > prev else self.smoothing_down
        return prev + alpha * (value - prev)

    def _demand(self, profile: Profile, cpu: float | None, gpu: float | None, offset: float) -> float:
        # CPU und GPU teilen sich beim Victus die Heatpipes, deshalb folgen
        # beide Lüfter dem höheren Bedarf.
        demands = [0.0]
        if cpu is not None:
            demands.append(interpolate(profile.cpu_curve, cpu + offset))
            demands.append(interpolate(SAFETY_FLOOR, cpu + offset))
        if gpu is not None:
            demands.append(interpolate(profile.gpu_curve, gpu + offset))
            demands.append(interpolate(SAFETY_FLOOR, gpu + offset))
        return max(demands)

    def update(self, reading: Reading, profile: Profile, now: float) -> Decision:
        dt = 0.0 if self._last_time is None else max(0.0, now - self._last_time)
        self._last_time = now

        self._cpu = self._smooth(self._cpu, reading.cpu_temp)
        self._gpu = self._smooth(self._gpu, reading.gpu_temp)

        raw_temps = [t for t in (reading.cpu_temp, reading.gpu_temp) if t is not None]
        critical = (not raw_temps and self._cpu is None and self._gpu is None) or any(
            t >= self.critical_temp for t in raw_temps
        )
        if critical:
            # Keine Sensordaten oder kritisch heiß: sofort volle Leistung.
            self.percent = 100.0
            self._lower_since = None
            return Decision(100.0, 100.0, self._cpu, self._gpu, True, profile.name)

        up_demand = self._demand(profile, self._cpu, self._gpu, 0.0)
        # Für das Absenken wird die Temperatur künstlich um die Hysterese
        # erhöht: die Drehzahl sinkt erst, wenn es spürbar kühler ist.
        down_demand = self._demand(profile, self._cpu, self._gpu, self.hysteresis)

        target = self.percent
        if up_demand > self.percent:
            self._lower_since = None
            step = self.ramp_up_rate * dt if dt > 0 else up_demand
            target = min(up_demand, self.percent + step)
        elif down_demand < self.percent:
            if self._lower_since is None:
                self._lower_since = now
            if now - self._lower_since >= self.spin_down_delay:
                step = self.ramp_down_rate * dt
                target = max(down_demand, self.percent - step)
        else:
            self._lower_since = None

        # Sehr niedrige Werte lassen manche Lüfter nicht anlaufen: entweder
        # aus oder mindestens die Anlaufdrehzahl.
        if 0 < target < self.min_running:
            target = self.min_running if up_demand > 0 else 0.0

        self.percent = max(0.0, min(100.0, target))
        return Decision(self.percent, up_demand, self._cpu, self._gpu, False, profile.name)


class ToggleAdapter:
    """Für Geräte, die nur "Max-Lüfter an/aus" unterstützen."""

    def __init__(self, engage: float, release: float, min_on: float = 0.0):
        self.configure(engage, release, min_on)
        self.active = False
        self._since = 0.0

    def configure(self, engage: float, release: float, min_on: float = 0.0) -> None:
        if release >= engage:
            raise ValueError("toggle.release_percent muss kleiner als engage_percent sein")
        self.engage = engage
        self.release = release
        # Max bleibt mindestens so lange an - sonst kühlt Max die Sensoren in
        # Sekunden herunter und die Lüfter "pumpen" im Minutentakt.
        self.min_on = min_on

    def update(self, percent: float, critical: bool, now: float = 0.0) -> bool:
        if critical or percent >= self.engage:
            if not self.active:
                self._since = now
            self.active = True
        elif percent <= self.release and self.active and now - self._since >= self.min_on:
            self.active = False
        return self.active
