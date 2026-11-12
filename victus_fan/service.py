"""Hauptschleife des Hintergrunddienstes."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from pathlib import Path

from .backends import BackendError, FanBackend
from .config import Config, load_config
from .controller import FanController, Reading, ToggleAdapter
from .power_events import DISPLAY_OFF, DISPLAY_ON, RESUME, SUSPEND, PowerEvents
from .state import DEFAULT_OVERRIDE, read_override

log = logging.getLogger(__name__)

# Folgen die Lüfter der Vorgabe nicht (BIOS-Automatik übernimmt, Lüfter steht),
# werden Leistungsmodus und Drehzahl erneut gesetzt - höchstens einmal pro
# REARM_INTERVAL, aber ohne aufzugeben. Nach REARM_WARN erfolglosen Versuchen
# in Folge erscheint zusätzlich eine Warnung.
REARM_INTERVAL = 60.0
REARM_WARN = 3
# Nach einer Änderung der Vorgabe um mindestens so viele Prozentpunkte bekommen
# die Lüfter SETTLE_TIME Sekunden Zeit, bevor verglichen wird.
SIGNIFICANT_CHANGE = 5
SETTLE_TIME = 20.0
# Liegen zwischen zwei Messungen mehr als so viele Sekunden, war das System
# im Standby/Ruhezustand (Ersatz, falls die Windows-Benachrichtigung fehlt).
RESUME_GAP = 30.0


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


class FanService:
    def __init__(self, config: Config, backend: FanBackend, status_file: Path | None = None,
                 override_file: Path | None = None, dry_run: bool = False, power_events=None):
        self.config = config
        self.backend = backend
        # Standby/Bildschirm aus: die Firmware regelt, Abweichungen sind dann normal.
        self.power_events = power_events
        self._low_power = False
        self._last_wall: float | None = None
        self.status_file = status_file
        self.override_file = override_file
        self.dry_run = dry_run
        self.controller = FanController(config.control, config.general["critical_temp"])
        self.toggle = ToggleAdapter(float(config.toggle["engage_percent"]),
                                    float(config.toggle["release_percent"]),
                                    float(config.toggle["min_on_time"]))
        self.stop_event = threading.Event()
        self._last_sent: tuple | None = None
        self._last_sent_at = 0.0
        self._errors = 0
        self._paused = False
        self._config_mtime = self._mtime()
        self._last_change_at = 0.0
        self._mismatch_since: float | None = None
        self._rearm_count = 0
        self._last_rearm_at = float("-inf")

    # ------------------------------------------------------------------
    def _mtime(self) -> float | None:
        try:
            return self.config.path.stat().st_mtime if self.config.path else None
        except OSError:
            return None

    def reload_config_if_changed(self) -> None:
        """Übernimmt Änderungen an der config.toml ohne Neustart."""
        mtime = self._mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            new = load_config(self.config.path)
        except Exception as exc:  # noqa: BLE001
            log.error("Konfiguration fehlerhaft, behalte die alte: %s", exc)
            return
        old_windows = self.config.windows
        self.config = new
        if new.windows != old_windows:
            self._apply_backend_settings()
        self.controller.configure(new.control, new.general["critical_temp"])
        self.toggle.configure(float(new.toggle["engage_percent"]), float(new.toggle["release_percent"]),
                              float(new.toggle["min_on_time"]))
        log.info("Konfiguration neu geladen")

    def _override(self) -> dict:
        return read_override(self.override_file) if self.override_file else dict(DEFAULT_OVERRIDE)

    def active_profile_name(self, on_battery: bool, override: dict | None = None) -> tuple[str, str]:
        wanted = (override or self._override())["profile"]
        if wanted != "auto" and wanted in self.config.raw["profiles"]:
            return wanted, "manuell"
        if on_battery:
            return self.config.general["battery_profile"], "Akku"
        return self.config.general["profile"], "Netz"

    def _apply(self, percent: float, critical: bool, now: float, force_max: bool = False) -> str:
        b = self.backend
        if force_max:
            command = ("max", True)
        elif b.supports_level and not critical:
            command = ("level", round(percent))
        elif b.supports_level and critical:
            command = ("max", True)
        else:
            command = ("max", self.toggle.update(percent, critical, now))

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
                prev = self._last_sent
                if (prev is None or prev[0] != command[0] or command[0] != "level"
                        or abs(command[1] - prev[1]) >= SIGNIFICANT_CHANGE):
                    self._last_change_at = now
            self._last_sent = command
            self._last_sent_at = now
        return f"{command[1]} %" if command[0] == "level" else ("MAX" if command[1] else "Auto")

    def _handle_power_events(self, now: float) -> None:
        events = self.power_events.pop() if self.power_events else []
        wall = time.time()
        gap = RESUME_GAP if self._last_wall is None else wall - self._last_wall
        if self._last_wall is not None and gap > max(RESUME_GAP, 5 * float(self.config.general["interval"])):
            log.info("%.0f s ohne Messung - Rückkehr aus Standby/Ruhezustand angenommen", gap)
            events.append(RESUME)
        self._last_wall = wall
        for event in events:
            if event in (SUSPEND, DISPLAY_OFF):
                if not self._low_power:
                    log.info("%s - die Firmware regelt, Abweichungen werden ignoriert",
                             "Standby" if event == SUSPEND else "Bildschirm aus")
                self._low_power = True
            elif event in (RESUME, DISPLAY_ON):
                if self._low_power or event == RESUME:
                    log.info("%s - setze Leistungsmodus und Drehzahl neu",
                             "Aus dem Standby zurück" if event == RESUME else "Bildschirm an")
                    self._take_back_control(now)
                self._low_power = False

    def _take_back_control(self, now: float) -> None:
        """Nach dem Aufwachen sofort neu setzen, statt auf die Abweichung zu warten."""
        if not self.dry_run and hasattr(self.backend, "rearm"):
            self.backend.rearm()
        self._last_sent = None          # Drehzahl sofort neu senden
        self._mismatch_since = None
        self._rearm_count = 0
        self._last_rearm_at = now

    def step(self) -> dict:
        self.reload_config_if_changed()
        now = time.monotonic()
        self._handle_power_events(now)
        b = self.backend
        on_battery = b.on_battery()
        reading = Reading(_round(b.read_cpu_temp()), _round(b.read_gpu_temp()), on_battery)
        override = self._override()
        mode = override["mode"]
        name, reason = self.active_profile_name(on_battery, override)
        profile = self.config.profile(name)


        # Der Regler läuft immer mit, damit die Glättung beim Fortsetzen stimmt.
        decision = self.controller.update(reading, profile, now)
        if mode == "paused":
            if not self._paused:
                log.info("Pausiert - Lüfter laufen in der BIOS-Automatik")
                if not self.dry_run:
                    b.shutdown()  # inkl. BIOS-Leistungsmodus zurück auf Standard
                self._paused = True
                self._last_sent = None
            output = "BIOS"
        else:
            if self._paused:
                log.info("Steuerung fortgesetzt")
                self._paused = False
                self._apply_backend_settings()   # Leistungsmodus wieder setzen
            output = self._apply(decision.percent, decision.critical, now, force_max=(mode == "max"))

        if mode == "normal" and not self.dry_run and hasattr(b, "maintain"):
            b.maintain()           # z. B. Anlaufhilfe beenden, sobald die Lüfter drehen
        rpm = b.read_fan_rpm()
        warning = self._check_effect(rpm, now, mode)

        status = {
            "warning": warning,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time(),
            "mode": mode,
            "supports_level": b.supports_level,
            "fan_mode": "" if mode == "paused" else (self.config.windows.get("fan_mode") or ""),
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
            "fan_rpm": rpm,
            # Für die Oberfläche: Maximal- und Zieldrehzahl je Lüfter
            "fan_max_rpm": [getattr(b, "max_cpu", 55) * 100, getattr(b, "max_gpu", 57) * 100],
            "fan_target_rpm": (b.expected_rpm(self._last_sent[1])
                               if self._last_sent and self._last_sent[0] == "level" else None),
            "dry_run": self.dry_run,
            "low_power": self._low_power,
        }
        self._write_status(status)
        return status

    def _check_effect(self, rpm: list[int], now: float, mode: str) -> str | None:
        """Erkennt, ob die Lüfter der Drehzahlvorgabe tatsächlich folgen."""
        sent = self._last_sent
        if mode != "normal" or self.dry_run or not sent or sent[0] != "level" or self._low_power:
            self._mismatch_since = None
            return None
        expected = self.backend.expected_rpm(sent[1])
        if (not rpm or not expected or now - self._last_change_at < SETTLE_TIME
                or getattr(self.backend, "kicking", False)):
            # Lüfter brauchen nach einer Änderung bzw. beim Anlaufen Zeit.
            return self._mismatch_warning(now)
        off = any(abs(r - e) > max(1000, 0.4 * e) for r, e in zip(rpm, expected))
        if not off:
            if self._mismatch_since is not None:
                log.info("Lüfter folgen der Vorgabe wieder (%s U/min)", rpm)
            self._mismatch_since = None
            self._rearm_count = 0
            return None
        if self._mismatch_since is None:
            self._mismatch_since = now
            since = getattr(self.backend, "seconds_since_mode", lambda: float("nan"))()
            log.warning("Drehzahl %s U/min weicht von der Vorgabe %s U/min ab "
                        "(%.0f s nach dem letzten Setzen des Leistungsmodus)", rpm, expected, since)
            context = getattr(self.backend, "diagnostic_context", None)
            if context is not None:
                try:
                    log.info("Umgebung: %s", context())
                except Exception as exc:  # noqa: BLE001 - nur Zusatzinfo
                    log.debug("Umgebungsinfo nicht lesbar: %s", exc)
        if now - self._last_rearm_at >= REARM_INTERVAL and hasattr(self.backend, "rearm"):
            self._rearm(now)
        return self._mismatch_warning(now)

    def _rearm(self, now: float) -> None:
        """Leistungsmodus und Drehzahl erneut setzen (inkl. Anlaufhilfe)."""
        self._rearm_count += 1
        self._last_rearm_at = now
        log.info("Setze Leistungsmodus und Drehzahl erneut (Versuch %d)", self._rearm_count)
        self.backend.rearm()
        self._last_sent = None       # Drehzahl sofort neu senden

    def _apply_backend_settings(self) -> None:
        """[windows]-Einstellungen (u. a. BIOS-Leistungsmodus) an das Backend geben
        - nach einer Konfig-Änderung und beim Fortsetzen nach einer Pause."""
        if self.dry_run:
            return
        self.backend.reconfigure(self.config.windows)
        self._last_sent = None      # Drehzahl neu senden
        self._mismatch_since = None

    def _mismatch_warning(self, now: float) -> str | None:
        if self._mismatch_since is None:
            return None
        if hasattr(self.backend, "rearm"):
            if self._rearm_count < REARM_WARN:
                return None
        elif now - self._mismatch_since < 30:
            return None
        return ("Die Lüfter folgen der Drehzahlvorgabe nicht. Die Steuerung versucht es jede "
                "Minute erneut – bleibt das so, bitte den Hardware-Test ausführen.")

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
        if self.power_events is None and not self.dry_run:
            events = PowerEvents()
            if events.start():
                self.power_events = events
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
                self.backend.shutdown()
