"""Bringt eine vorhandene config.toml beim Update auf den aktuellen Stand.

Die Datei wird aus der aktuellen Vorlage neu erzeugt (``render_config``):
aktuelle Kommentare und Einträge, dazu die eigenen Werte des Benutzers.

* Übernommen werden gültige Werte, die vom Standard abweichen, und eigene bzw.
  angepasste Lüfterkurven.
* Messwerte des Hardware-Tests ([windows] mode, fan_mode, min_level,
  max_level_*) nur, wenn der Hardware-Test sie geschrieben hat - frühe Vorlagen
  enthielten andere Standardwerte (z. B. max_level 55/57), die sonst als
  vermeintliche Messung erhalten blieben.
* Veraltete Einträge (z. B. backend = "auto", [update] repo/branch) und
  ungültige Werte fallen weg; dann gilt der Standard.
* Die alte Datei bleibt als config.toml.bak erhalten. Eine Datei mit aktuellem
  ``config_version`` wird nicht angefasst - die Migration läuft nur einmal.

Der Installer ruft das als Administrator auf (``victus-fan migrate-config``).
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import (CONFIG_VERSION, DEFAULT_PROFILES, DEFAULTS, HARDWARE_TEST_VERSION, TEMPLATE,
                     _validate_curve, config_from_dict, render_config, validate)

TEST_NOTE = "vom Hardware-Test gesetzt"
# Vom Hardware-Test gemessen - nur mit Nachweis übernehmen (siehe oben).
HARDWARE_KEYS = ("mode", "fan_mode", "min_level", "max_level_cpu", "max_level_gpu")
SECTIONS = ("general", "control", "windows", "update", "toggle")


@dataclass
class Migration:
    changed: bool = False
    kept: list[str] = field(default_factory=list)        # übernommene eigene Werte
    dropped: list[str] = field(default_factory=list)     # entfallen (veraltet/ungültig)
    backup: Path | None = None
    hardware_tested: bool = False
    created: bool = False

    def summary(self) -> str:
        if not self.changed:
            return "Konfiguration ist aktuell."
        if self.created:
            return "config.toml angelegt."
        lines = ["Konfiguration auf den aktuellen Stand gebracht"
                 + (f" (Sicherung: {self.backup.name})" if self.backup else "") + "."]
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
    return TEST_NOTE in text           # Dateien vor Version 2.9: Vermerk des Hardware-Tests


def _collect(user: dict, tested: bool, result: Migration) -> dict[str, dict]:
    changes: dict[str, dict] = {}
    for section in SECTIONS:
        values = user.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            name = f"{section}.{key}"
            if key not in DEFAULTS[section] or (section, key, value) == ("general", "backend", "auto"):
                result.dropped.append(name)                       # veraltet
                continue
            if section == "windows" and key == "hardware_test":
                continue                                          # wird unten gesetzt
            if section == "windows" and key in HARDWARE_KEYS and not tested:
                continue                                          # alter Vorlagenwert, keine Messung
            if not _valid(section, key, value):
                result.dropped.append(f"{name} = {value!r} (ungültig)")
                continue
            if value != DEFAULTS[section][key]:
                changes.setdefault(section, {})[key] = value
                result.kept.append(name)
    if tested:
        changes.setdefault("windows", {})["hardware_test"] = HARDWARE_TEST_VERSION

    profiles = user.get("profiles")
    for name, profile in (profiles.items() if isinstance(profiles, dict) else []):
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
        default = DEFAULT_PROFILES.get(name)
        if default and all(curves.get(k) == default[k] for k in ("cpu_curve", "gpu_curve")):
            continue                                              # unverändertes Standardprofil
        changes[f"profiles.{name}"] = curves
        result.kept.append(f"Profil {name}")
    return changes


def _render(template: str, changes: dict[str, dict]) -> str:
    """Formatierte Datei aus den gesammelten Werten; wirft ValueError, wenn ungültig."""
    user: dict = {}
    for section, values in changes.items():
        if section.startswith("profiles."):
            user.setdefault("profiles", {})[section.split(".", 1)[1]] = values
        else:
            user[section] = values
    text = render_config(user, template)
    try:
        config_from_dict(tomllib.loads(text))
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    return text


def migrate_config(path: str | Path, template: str | Path | None = None) -> Migration:
    """``path`` auf den Aufbau der Vorlage bringen (siehe Moduldoku); fehlt die
    Datei, wird sie aus der Vorlage angelegt."""
    path = Path(path)
    template_text = Path(template or TEMPLATE).read_text(encoding="utf-8-sig")
    result = Migration()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(template_text, {}), encoding="utf-8")
        result.changed = True
        result.created = True
        return result

    text = path.read_text(encoding="utf-8-sig")
    try:
        user = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        user = None
        result.dropped.append("gesamte Datei (nicht lesbar)")
    if user is not None:
        version = user.get("config_version", 0)
        if isinstance(version, int) and not isinstance(version, bool) and version >= CONFIG_VERSION:
            return result                                         # schon aktuell
    result.hardware_tested = user is not None and _hardware_tested(text, user)
    changes = _collect(user, result.hardware_tested, result) if user is not None else {}

    try:
        new_text = _render(template_text, changes)
    except ValueError:
        # Einzeln gültig, zusammen nicht (z. B. Profilverweise, toggle-Grenzen):
        # dann nur die Hardware-Werte und eigenen Kurven behalten.
        keep = {k: v for k, v in changes.items() if k == "windows" or k.startswith("profiles.")}
        dropped = [f"[{k}]" for k in changes if k not in keep]
        new_text = _render(template_text, keep)
        result.dropped += [f"Einstellungen {', '.join(dropped)} (passten nicht zusammen)"]
        result.kept = [k for k in result.kept if k.startswith(("windows.", "Profil "))]

    result.backup = path.with_name(path.name + ".bak")
    try:
        result.backup.write_text(text, encoding="utf-8")
    except OSError:
        # Als normaler Benutzer (Speichern im Einstellungsfenster) darf im
        # Datenordner keine neue Datei angelegt werden - dann ohne Sicherung.
        result.backup = None
    # Direkt schreiben (nicht ersetzen), damit die Dateirechte für Benutzer bleiben.
    path.write_text(new_text, encoding="utf-8")
    result.changed = True
    return result
