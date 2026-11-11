"""Hauptschleife des Hintergrunddienstes."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from pathlib import Path

from .backends import BackendError, FanBackend
from .config import Config, load_config, update_config_values
from .controller import FanController, Reading, ToggleAdapter
from .state import DEFAULT_OVERRIDE, read_override

log = logging.getLogger(__name__)

# Manche HP-BIOS setzen feste Drehzahlen erst um, nachdem ein Leistungsmodus
# gesetzt wurde. Folgen die Lüfter nicht, wird der nächste Modus probiert -
# "default" zuerst, weil er die Leistungsaufnahme nicht verändert.
FAN_MODE_CHAIN = ("default", "cool", "performance")


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


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
                                    float(config.toggle["release_percent"]),
                                    float(config.toggle["min_on_time"]))
        self.stop_event = threading.Event()
        self._last_sent: tuple | None = None
        self._last_sent_at = 0.0
        self._errors = 0
        self._paused = False
        # Zuletzt an das Backend gegebener BIOS-Leistungsmodus
        self._applied_fan_mode: str | None = config.windows.get("fan_mode") or ""
        self._config_mtime = self._mtime()
        self._last_change_at = 0.0
        self._mismatch_since: float | None = None

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
            self._apply_power_mode(self._override(), force=True)
            self._last_sent = None  # neu senden
            self._mismatch_since = None
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
                self._last_change_at = now
            self._last_sent = command
            self._last_sent_at = now
        return f"{command[1]} %" if command[0] == "level" else ("MAX" if command[1] else "Auto")

    def step(self) -> dict:
        self.reload_config_if_changed()
        now = time.monotonic()
        b = self.backend
        on_battery = b.on_battery()
        reading = Reading(_round(b.read_cpu_temp()), _round(b.read_gpu_temp()), on_battery)
        override = self._override()
        mode = override["mode"]
        if mode != "paused" and not self._paused:
            self._apply_power_mode(override)
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
                self._apply_power_mode(override, force=True)
            output = self._apply(decision.percent, decision.critical, now, force_max=(mode == "max"))

        rpm = b.read_fan_rpm()
        warning = self._check_effect(rpm, now, mode)

        status = {
            "warning": warning,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time(),
            "mode": mode,
            "supports_level": b.supports_level,
            "fan_mode": "" if mode == "paused" else (self._applied_fan_mode or ""),
            "power_mode": override["power_mode"],
            "power_modes_available": hasattr(b, "set_fan_mode"),
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
            "dry_run": self.dry_run,
        }
        self._write_status(status)
        return status

    def _check_effect(self, rpm: list[int], now: float, mode: str) -> str | None:
        """Erkennt, ob die Lüfter der Drehzahlvorgabe tatsächlich folgen."""
        sent = self._last_sent
        if mode != "normal" or self.dry_run or not sent or sent[0] != "level":
            self._mismatch_since = None
            return None
        expected = self.backend.expected_rpm(sent[1])
        if not rpm or not expected or now - self._last_change_at < 20:
            # Lüfter brauchen nach einer Änderung Zeit - bisherigen Zustand behalten.
            return self._mismatch_warning(now)
        off = any(abs(r - e) > max(1000, 0.4 * e) for r, e in zip(rpm, expected))
        if not off:
            if self._mismatch_since is not None:
                log.info("Lüfter folgen der Vorgabe wieder")
            self._mismatch_since = None
        elif self._mismatch_since is None:
            self._mismatch_since = now
            log.warning("Drehzahl %s U/min weicht von der Vorgabe %s U/min ab", rpm, expected)
        warning = self._mismatch_warning(now)
        if warning and self._escalate_fan_mode():
            return None
        return warning

    def effective_windows(self, override: dict) -> dict:
        """Windows-Einstellungen mit dem vom Benutzer gewählten Leistungsmodus."""
        settings = dict(self.config.windows)
        if override.get("power_mode", "auto") != "auto":
            settings["fan_mode"] = override["power_mode"]
        return settings

    def _apply_power_mode(self, override: dict, force: bool = False) -> None:
        settings = self.effective_windows(override)
        wanted = settings.get("fan_mode") or ""
        if self.dry_run or (not force and wanted == self._applied_fan_mode):
            return
        if wanted != self._applied_fan_mode:
            log.info("BIOS-Leistungsmodus -> %s", wanted or "(unverändert)")
        self.backend.reconfigure(settings)
        self._applied_fan_mode = wanted
        self._last_sent = None      # Drehzahl im neuen Modus neu senden
        self._mismatch_since = None

    def _escalate_fan_mode(self) -> bool:
        """Nächsten BIOS-Leistungsmodus probieren und merken. False = keiner mehr übrig."""
        b = self.backend
        if not hasattr(b, "set_fan_mode") or self.dry_run:
            return False
        if self._override()["power_mode"] != "auto":
            return False  # vom Benutzer gewählt - nicht eigenmächtig ändern
        current = self.config.windows.get("fan_mode") or ""
        index = FAN_MODE_CHAIN.index(current) + 1 if current in FAN_MODE_CHAIN else 0
        if index >= len(FAN_MODE_CHAIN):
            return False
        new_mode = FAN_MODE_CHAIN[index]
        log.warning("Lüfter folgen nicht - versuche BIOS-Leistungsmodus '%s'", new_mode)
        self.config.raw["windows"]["fan_mode"] = new_mode
        try:
            b.reconfigure(self.config.windows)
            self._applied_fan_mode = new_mode
        except Exception as exc:  # noqa: BLE001
            log.error("Leistungsmodus '%s' fehlgeschlagen: %s", new_mode, exc)
        if self.config.path is not None:
            try:
                update_config_values(self.config.path, "windows", {"fan_mode": new_mode},
                                     note="automatisch gewählt")
                self._config_mtime = self._mtime()
            except Exception as exc:  # noqa: BLE001
                log.warning("Konnte Leistungsmodus nicht in der Konfiguration speichern: %s", exc)
        self._mismatch_since = None
        self._last_sent = None  # Drehzahl neu senden
        return True

    def _mismatch_warning(self, now: float) -> str | None:
        if self._mismatch_since is None or now - self._mismatch_since < 30:
            return None
        return "Die Lüfter folgen der Drehzahlvorgabe nicht. Bitte den Hardware-Test ausführen."

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
                self.backend.shutdown()
