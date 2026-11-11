"""Hauptschleife des Hintergrunddienstes."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from pathlib import Path

from .backends import BackendError, FanBackend
from .config import Config
from .controller import FanController, Reading, ToggleAdapter

log = logging.getLogger(__name__)


def read_override(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = data.get("profile")
        return profile if profile and profile != "auto" else None
    except (OSError, ValueError, AttributeError):
        return None


class FanService:
    def __init__(self, config: Config, backend: FanBackend, status_file: Path | None = None,
                 override_file: Path | None = None, dry_run: bool = False):
        self.config = config
        self.backend = backend
        self.status_file = status_file
        self.override_file = override_file
        self.dry_run = dry_run
        self.controller = FanController(config.control, config.general["critical_temp"])
        self.toggle = ToggleAdapter(float(config.toggle["engage_percent"]),
                                    float(config.toggle["release_percent"]))
        self.stop_event = threading.Event()
        self._last_sent: tuple | None = None
        self._last_sent_at = 0.0
        self._platform_profile: str | None = None
        self._errors = 0

    # ------------------------------------------------------------------
    def active_profile_name(self, on_battery: bool) -> tuple[str, str]:
        override = read_override(self.override_file) if self.override_file else None
        if override and override in self.config.raw["profiles"]:
            return override, "manuell"
        if on_battery:
            return self.config.general["battery_profile"], "Akku"
        return self.config.general["profile"], "Netz"

    def _apply(self, percent: float, critical: bool, now: float) -> str:
        b = self.backend
        if b.supports_level and not critical:
            command = ("level", round(percent))
        elif b.supports_level and critical:
            command = ("max", True)
        else:
            command = ("max", self.toggle.update(percent, critical))

        keepalive = b.keepalive or 0
        due = keepalive and now - self._last_sent_at >= keepalive
        if command != self._last_sent or due:
            if not self.dry_run:
                if command[0] == "level":
                    b.set_percent(command[1])
                else:
                    # Der Wechsel max -> level wird von set_percent erledigt.
                    b.set_max(command[1])
            if command != self._last_sent:
                log.debug("Setze %s", command)
            self._last_sent = command
            self._last_sent_at = now
        return f"{command[1]} %" if command[0] == "level" else ("MAX" if command[1] else "Auto")

    def step(self) -> dict:
        now = time.monotonic()
        b = self.backend
        on_battery = b.on_battery()
        reading = Reading(b.read_cpu_temp(), b.read_gpu_temp(), on_battery)
        name, reason = self.active_profile_name(on_battery)
        profile = self.config.profile(name)

        if (self.config.general.get("switch_platform_profile") and profile.platform_profile
                and profile.platform_profile != self._platform_profile and not self.dry_run):
            b.set_platform_profile(profile.platform_profile)
            self._platform_profile = profile.platform_profile

        decision = self.controller.update(reading, profile, now)
        output = self._apply(decision.percent, decision.critical, now)

        status = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backend": b.name,
            "profile": name,
            "profile_reason": reason,
            "on_battery": on_battery,
            "cpu_temp": reading.cpu_temp,
            "gpu_temp": reading.gpu_temp,
            "cpu_smoothed": None if decision.cpu_smooth is None else round(decision.cpu_smooth, 1),
            "gpu_smoothed": None if decision.gpu_smooth is None else round(decision.gpu_smooth, 1),
            "demand_percent": round(decision.demand, 1),
            "fan_percent": round(decision.percent, 1),
            "output": output,
            "critical": decision.critical,
            "fan_rpm": b.read_fan_rpm(),
            "dry_run": self.dry_run,
        }
        self._write_status(status)
        return status

    def _write_status(self, status: dict) -> None:
        if not self.status_file:
            return
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.status_file)
        except OSError as exc:
            log.debug("Status nicht schreibbar: %s", exc)

    # ------------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("Signal %s empfangen, beende ...", signum)
            self.stop_event.set()
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass

    def run(self, verbose: bool = False) -> None:
        interval = float(self.config.general["interval"])
        log.info("Lüftersteuerung gestartet: %s", self.backend.describe())
        try:
            while not self.stop_event.is_set():
                try:
                    status = self.step()
                    self._errors = 0
                    msg = ("CPU %s°C GPU %s°C | Profil %s (%s) | Bedarf %.0f%% -> %s | %s"
                           % (status["cpu_temp"], status["gpu_temp"], status["profile"],
                              status["profile_reason"], status["demand_percent"],
                              status["output"], status["fan_rpm"]))
                    (log.info if verbose else log.debug)(msg)
                except BackendError as exc:
                    self._errors += 1
                    log.warning("Hardwarefehler (%d): %s", self._errors, exc)
                    if self._errors >= 5:
                        log.error("Zu viele Fehler - gebe Steuerung an das BIOS zurück und warte")
                        self.backend.restore_auto()
                        self._last_sent = None
                        self.stop_event.wait(30)
                except Exception:  # noqa: BLE001 - Dienst darf nicht abstürzen
                    self._errors += 1
                    log.exception("Unerwarteter Fehler")
                self.stop_event.wait(interval)
        finally:
            if not self.dry_run:
                log.info("Gebe Lüftersteuerung an das BIOS zurück")
                self.backend.restore_auto()
