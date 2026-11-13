"""Einmalige Übernahme der alten Textkonfiguration (config.toml) in settings.json.

Bis Version 2.8 stand die Konfiguration in einer von Hand bearbeitbaren
config.toml. Seit 2.9 gibt es nur noch das Einstellungsfenster und die beiden
Tests; gespeichert wird in settings.json. Der Installer ruft beim Update
``victus-fan migrate-config`` auf:

* Gibt es schon eine settings.json, passiert nichts.
* Sonst werden aus der config.toml die eigenen, gültigen Werte übernommen:
  Profilwahl, Notfall-Temperatur, Regelung, Updates, Protokoll und angepasste
  Kurven der drei Profile.
* Messwerte des Hardware-Tests (mode, fan_mode, min_level, max_level_*) nur,
  wenn der Hardware-Test sie geschrieben hat - frühe Vorlagen enthielten andere
  Standardwerte (z. B. max_level 55/57), die sonst als vermeintliche Messung
  erhalten blieben. Die vom Sensor-Test gewählte Temperaturquelle bleibt.
* Feste Werte (keepalive, toggle ...), veraltete Einträge (backend = "auto",
  repo, branch), eigene Profile und ungültige Werte entfallen.
* Die config.toml wird in config.toml.bak umbenannt.
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import (DEFAULTS, FIXED, HARDWARE_TEST_VERSION, MEASURED, PROFILE_NAMES, STORABLE, Config,
                     _validate_curve, config_from_values, validate, write_config)

TEST_NOTE = "vom Hardware-Test gesetzt"


@dataclass
class Migration:
    changed: bool = False
    created: bool = False                                  # neue, leere Einstellungen
    kept: list[str] = field(default_factory=list)          # übernommene eigene Werte
    dropped: list[str] = field(default_factory=list)       # entfallen
    backup: Path | None = None
    hardware_tested: bool = False

    def summary(self) -> str:
        if not self.changed:
            return "Einstellungen sind aktuell."
        if self.created:
            return "Einstellungen angelegt (Standardwerte)."
        lines = ["Alte config.toml übernommen"
                 + (f" (gesichert als {self.backup.name})" if self.backup else "") + "."]
        if self.kept:
            lines.append("Übernommen: " + ", ".join(self.kept))
        if self.dropped:
            lines.append("Entfallen: " + ", ".join(self.dropped))
        if not self.hardware_tested:
            lines.append("Hardware-Test noch nicht ausgeführt - das Fenster weist darauf hin.")
        return "\n".join(lines)


def _valid(section: str, key: str, value) -> bool:
    raw = copy.deepcopy(DEFAULTS)
    raw[section][key] = value
    try:
        validate(raw)
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _hardware_tested(text: str, user: dict) -> bool:
    windows = user.get("windows") if isinstance(user.get("windows"), dict) else {}
    stamp = windows.get("hardware_test", 0)
    if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 1:
        return True
    return TEST_NOTE in text            # Vermerk, den der Hardware-Test in die Datei schrieb


def _collect(user: dict, tested: bool, result: Migration) -> dict:
    values: dict = {}
    for section, keys in STORABLE.items():
        given = user.get(section)
        if not isinstance(given, dict):
            continue
        for key, value in given.items():
            name = f"{section}.{key}"
            if (section, key) in FIXED:
                continue                                    # fest im Programm
            if key not in keys:
                result.dropped.append(name)                 # veraltet
                continue
            if section == "windows" and key in MEASURED and key != "cpu_sensor" and not tested:
                continue                                    # alter Vorlagenwert, keine Messung
            if not _valid(section, key, value):
                result.dropped.append(f"{name} = {value!r} (ungültig)")
                continue
            if value != DEFAULTS[section][key]:
                values.setdefault(section, {})[key] = value
                result.kept.append(name)
    if tested:
        values.setdefault("windows", {})["hardware_test"] = HARDWARE_TEST_VERSION
    for section in user:
        if section not in STORABLE and section not in ("profiles", "config_version"):
            result.dropped.append(f"[{section}]")

    profiles = user.get("profiles")
    for name, profile in (profiles.items() if isinstance(profiles, dict) else []):
        if name not in PROFILE_NAMES:
            result.dropped.append(f"eigenes Profil {name}")
            continue
        if not isinstance(profile, dict) or "cpu_curve" not in profile:
            result.dropped.append(f"Profil {name} (unvollständig)")
            continue
        curves = {k: profile[k] for k in ("cpu_curve", "gpu_curve") if k in profile}
        try:
            for key, points in curves.items():
                _validate_curve(points, f"{name}.{key}")
        except (ValueError, TypeError):
            result.dropped.append(f"Profil {name} (ungültige Kurve)")
            continue
        values.setdefault("profiles", {})[name] = curves
        result.kept.append(f"Profil {name}")
    return values


def _build(values: dict, result: Migration) -> Config:
    try:
        return config_from_values(values)
    except (ValueError, TypeError):
        # Einzeln gültig, zusammen nicht (z. B. Profilwahl): nur Messwerte und Kurven behalten.
        keep = {k: v for k, v in values.items() if k in ("windows", "profiles")}
        result.dropped.append("weitere Einstellungen (passten nicht zusammen)")
        result.kept = [k for k in result.kept if k.startswith(("windows.", "Profil "))]
        return config_from_values(keep)


def migrate_config(settings: str | Path, legacy: str | Path) -> Migration:
    """``legacy`` (config.toml) einmalig nach ``settings`` (settings.json) übernehmen."""
    settings, legacy = Path(settings), Path(legacy)
    result = Migration()
    if settings.exists():
        return result
    if not legacy.exists():
        write_config(settings, Config())
        result.changed = result.created = True
        return result

    text = legacy.read_text(encoding="utf-8-sig")
    try:
        user = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        user = {}
        result.dropped.append("gesamte config.toml (nicht lesbar)")
    result.hardware_tested = bool(user) and _hardware_tested(text, user)
    write_config(settings, _build(_collect(user, result.hardware_tested, result), result))

    result.backup = legacy.with_name(legacy.name + ".bak")
    result.backup.unlink(missing_ok=True)
    legacy.rename(result.backup)
    result.changed = True
    return result
