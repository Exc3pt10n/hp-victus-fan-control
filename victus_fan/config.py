"""Einstellungen: Standardwerte, feste Werte und die gespeicherten Einstellungen.

Gespeichert wird in ``settings.json`` (Datenordner). Die Datei ist kein
Bearbeitungsformat: geändert wird nur über das Einstellungsfenster bzw. vom
Hardware- und Sensor-Test. Sie enthält nur Werte, die vom Standard abweichen;
jeder Wert wird vor dem Speichern und beim Laden geprüft.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

# Lüfterkurven: (Temperatur in °C, Lüfterleistung in %). Zwischen den Punkten
# wird linear interpoliert.
# Ausgelegt auf das Victus BV8D4EA: 20 % ~ 1100 U/min (leiser als die
# BIOS-Automatik im Leerlauf), 100 % ~ 5400 U/min. 0 % = Lüfter stehen still -
# das erlaubt nur "quiet" und nur unter 50 °C, weil Spannungswandler, SSD und
# Akku keine eigenen Sensoren haben und sonst gar nicht mehr gekühlt würden.
DEFAULT_PROFILES: dict[str, dict] = {
    "quiet": {
        "cpu_curve": [[0, 0], [49, 0], [55, 20], [65, 30], [75, 50], [85, 75], [92, 100]],
        "gpu_curve": [[0, 0], [46, 0], [52, 20], [62, 30], [72, 50], [80, 75], [87, 100]],
    },
    "balanced": {
        "cpu_curve": [[0, 20], [50, 20], [60, 30], [70, 50], [80, 75], [88, 100]],
        "gpu_curve": [[0, 20], [47, 20], [57, 30], [67, 50], [76, 75], [84, 100]],
    },
    "performance": {
        "cpu_curve": [[0, 30], [45, 30], [55, 45], [65, 65], [75, 85], [82, 100]],
        "gpu_curve": [[0, 30], [42, 30], [52, 45], [62, 65], [70, 85], [77, 100]],
    },
}


DEFAULTS: dict = {
    "general": {
        # Aktives Profil im Netzbetrieb und im Akkubetrieb.
        "profile": "balanced",
        "battery_profile": "quiet",
        # Abfrageintervall in Sekunden.
        "interval": 2.0,
        # Ab dieser (ungeglätteten) Temperatur sofort Maximaldrehzahl.
        "critical_temp": 92.0,
        # Backend: "windows" oder "simulate" (nur per Befehlszeile, siehe FIXED).
        "backend": "windows",
        # "DEBUG" = ausführliches Protokoll (Schalter im Einstellungsfenster).
        "log_level": "INFO",
    },
    "control": {
        # Exponentielle Glättung: schnell nach oben, langsam nach unten.
        "smoothing_up": 0.6,
        "smoothing_down": 0.15,
        # Erst runterregeln, wenn die Temperatur so viele °C unter der
        # Schwelle liegt, die die aktuelle Drehzahl ausgelöst hat.
        "hysteresis": 4.0,
        # So lange (s) muss der niedrigere Bedarf anliegen, bevor gesenkt wird.
        "spin_down_delay": 20.0,
        # Maximale Änderung in Prozentpunkten pro Sekunde.
        "ramp_up_rate": 15.0,
        "ramp_down_rate": 3.0,
        # Untergrenze, sobald die Lüfter überhaupt laufen (Anlaufdrehzahl).
        "min_running_percent": 20.0,
    },
    "windows": {
        # Die Standardwerte sind die Messwerte des Victus BV8D4EA (Hardware-Test).
        # "level" = stufenlose Drehzahl, "toggle" = nur Max an/aus (Notlösung,
        # falls ein BIOS feste Drehzahlen ignoriert).
        "mode": "level",
        # BIOS-Modus, der beim Start gesetzt wird: "default", "cool" oder
        # "performance". Das BIOS setzt feste Drehzahlen erst um, nachdem ein
        # Modus gesetzt wurde. "default" genügt; die anderen bringen auf dem
        # Victus keine messbare Mehrleistung.
        "fan_mode": "default",
        # Quelle der CPU-Temperatur: "auto" (HP-BIOS, sonst wärmste ACPI-Zone)
        # oder eine id aus "victus-fan sensors", z. B. "perf:\\_TZ.THRM".
        "cpu_sensor": "auto",
        # Kleinste Stufe > 0, die das BIOS wirklich umsetzt (0 = keine Grenze).
        "min_level": 0,
        # Maximale Lüfterstufe in 100 U/min (54 = 5400 U/min).
        "max_level_cpu": 54,
        "max_level_gpu": 52,
        # Stand des Hardware-Tests, der die Werte oben gemessen hat (0 = noch nie).
        # Ist er kleiner als HARDWARE_TEST_VERSION, bittet das Fenster um einen Test.
        "hardware_test": 0,
        # Das BIOS fällt nach ca. 120 s ohne Befehl in seine Automatik zurück -
        # deshalb regelmäßig erneut senden.
        "keepalive": 60.0,
        # BIOS-Modus so oft neu setzen: Das BIOS des Victus BV8D4EA setzt
        # feste Drehzahlen nur um, solange ein Modus kürzlich gesetzt wurde.
        "mode_keepalive": 50.0,
    },
    "update": {
        # Automatisch nach Updates suchen und sie installieren.
        "enabled": True,
        # Nur prüfen und im Fenster anzeigen, nicht selbst installieren.
        "check_only": False,
        # Quelle und Test-Branch stehen bewusst nicht hier (siehe updater.py).
    },
    "toggle": {
        # Nur für mode = "toggle": Max an ab/aus unter diesem Bedarf.
        "engage_percent": 80.0,
        "release_percent": 55.0,
        # Max bleibt mindestens so viele Sekunden an.
        "min_on_time": 60.0,
    },
    "profiles": DEFAULT_PROFILES,
}


# --------------------------------------------------------------------------
# Prüfung: Die settings.json dürfen normale Benutzer schreiben (Einstellungs-
# fenster), der Dienst läuft aber als SYSTEM und steuert die Hardware. Deshalb
# wird jeder Wert auf Typ und Wertebereich geprüft; Unsinn wie interval = 0 oder
# critical_temp = 200 wird abgelehnt statt übernommen.
# --------------------------------------------------------------------------
# Höchste erlaubte Notfall-Temperatur - fest, nicht konfigurierbar höher.
MAX_CRITICAL_TEMP = 95.0
# Aufbau der settings.json. Bei jeder Änderung erhöhen und die Übernahme ergänzen.
SETTINGS_VERSION = 1
# Messverfahren des Hardware-Tests. Erhöhen, wenn alte Messwerte nicht mehr
# passen - dann bittet das Fenster einmal um einen neuen Test.
HARDWARE_TEST_VERSION = 1

NUMBER_LIMITS: dict[str, dict[str, tuple[float, float]]] = {
    "general": {"interval": (0.5, 30), "critical_temp": (60, MAX_CRITICAL_TEMP)},
    "control": {"smoothing_up": (0.01, 1), "smoothing_down": (0.01, 1), "hysteresis": (0, 20),
                "spin_down_delay": (0, 600), "ramp_up_rate": (0.1, 100), "ramp_down_rate": (0.1, 100),
                "min_running_percent": (0, 60)},
    "windows": {"min_level": (0, 40), "max_level_cpu": (20, 80), "max_level_gpu": (20, 80),
                "hardware_test": (0, 1000), "keepalive": (5, 110), "mode_keepalive": (5, 110)},
    "toggle": {"engage_percent": (1, 100), "release_percent": (0, 99), "min_on_time": (0, 600)},
}
INTEGERS = {("windows", "min_level"), ("windows", "max_level_cpu"), ("windows", "max_level_gpu"),
            ("windows", "hardware_test")}
CHOICES: dict[tuple[str, str], tuple[str, ...]] = {
    # "auto" stand in Konfigurationen früherer Versionen und gilt weiter (= windows bzw. level).
    ("general", "backend"): ("windows", "simulate", "auto"),
    ("general", "log_level"): ("DEBUG", "INFO", "WARNING", "ERROR"),
    ("windows", "mode"): ("level", "toggle", "auto"),
    ("windows", "fan_mode"): ("default", "cool", "performance", ""),
}
BOOLEANS = {("update", "enabled"), ("update", "check_only")}
STRINGS = {("general", "profile"), ("general", "battery_profile"), ("windows", "cpu_sensor")}


def validate(raw: dict) -> None:
    """Prüft alle bekannten Werte; wirft ValueError mit verständlicher Meldung."""
    for section in ("general", "control", "windows", "update", "toggle", "profiles"):
        if not isinstance(raw.get(section), dict):
            raise ValueError(f"[{section}] muss eine Sektion sein")
    for section, limits in NUMBER_LIMITS.items():
        for key, (lo, hi) in limits.items():
            value = raw[section].get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{section}.{key}: Zahl erwartet, gefunden {value!r}")
            if (section, key) in INTEGERS and not float(value).is_integer():
                raise ValueError(f"{section}.{key}: ganze Zahl erwartet")
            if not lo <= value <= hi:
                raise ValueError(f"{section}.{key} = {value}: erlaubt sind {lo:g} bis {hi:g}")
    for (section, key), allowed in CHOICES.items():
        value = raw[section].get(key)
        check = value.upper() if key == "log_level" and isinstance(value, str) else value
        if check not in allowed:
            raise ValueError(f"{section}.{key} = {value!r}: erlaubt sind {', '.join(repr(a) for a in allowed)}")
    for section, key in BOOLEANS:
        if not isinstance(raw[section].get(key), bool):
            raise ValueError(f"{section}.{key}: true oder false erwartet")
    for section, key in STRINGS:
        if not isinstance(raw[section].get(key), str):
            raise ValueError(f"{section}.{key}: Text erwartet")
    t = raw["toggle"]
    if t["release_percent"] >= t["engage_percent"]:
        raise ValueError("toggle.release_percent muss kleiner als engage_percent sein")
    w = raw["windows"]
    if w["min_level"] > min(w["max_level_cpu"], w["max_level_gpu"]):
        raise ValueError("windows.min_level darf nicht größer als die Maximalstufen sein")
    for name, p in raw["profiles"].items():
        if not isinstance(p, dict) or "cpu_curve" not in p:
            raise ValueError(f"Profil '{name}': cpu_curve fehlt")


# --------------------------------------------------------------------------
# Feste Werte: am Gerät gemessen bzw. technisch bedingt - nicht einstellbar.
# --------------------------------------------------------------------------
FIXED = {("general", "backend"), ("windows", "keepalive"), ("windows", "mode_keepalive"),
         ("toggle", "engage_percent"), ("toggle", "release_percent"), ("toggle", "min_on_time")}
SECTIONS = ("general", "control", "windows", "update", "toggle")
STORABLE = {s: [k for k in DEFAULTS[s] if (s, k) not in FIXED] for s in SECTIONS}
PROFILE_NAMES = tuple(DEFAULT_PROFILES)
# Messwerte von Hardware- und Sensor-Test
MEASURED = ("mode", "fan_mode", "min_level", "max_level_cpu", "max_level_gpu", "hardware_test", "cpu_sensor")


@dataclass
class Profile:
    name: str
    cpu_curve: list[tuple[float, float]]
    gpu_curve: list[tuple[float, float]]


@dataclass
class Config:
    raw: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path | None = None

    @property
    def general(self) -> dict:
        return self.raw["general"]

    @property
    def control(self) -> dict:
        return self.raw["control"]

    @property
    def windows(self) -> dict:
        return self.raw["windows"]

    @property
    def update(self) -> dict:
        return self.raw["update"]

    @property
    def toggle(self) -> dict:
        return self.raw["toggle"]

    def profile(self, name: str) -> Profile:
        profiles = self.raw["profiles"]
        if name not in profiles:
            raise KeyError(f"Unbekanntes Profil '{name}'. Verfügbar: {', '.join(profiles)}")
        p = profiles[name]
        return Profile(
            name=name,
            cpu_curve=_validate_curve(p["cpu_curve"], f"{name}.cpu_curve"),
            gpu_curve=_validate_curve(p.get("gpu_curve", p["cpu_curve"]), f"{name}.gpu_curve"),
        )


def _validate_curve(points, label: str) -> list[tuple[float, float]]:
    if not points:
        raise ValueError(f"{label}: Kurve ist leer")
    try:
        curve = [(float(t), float(p)) for t, p in points]
    except (TypeError, ValueError):
        raise ValueError(f"{label}: Punkte als [Temperatur, Prozent] angeben") from None
    for (t1, _), (t2, _) in zip(curve, curve[1:]):
        if t2 <= t1:
            raise ValueError(f"{label}: Temperaturen müssen aufsteigend sein")
    for _, p in curve:
        if not 0 <= p <= 100:
            raise ValueError(f"{label}: Prozentwerte müssen zwischen 0 und 100 liegen")
    return curve


def config_from_values(values: dict, path: Path | None = None) -> Config:
    """Gespeicherte Werte (nur Abweichungen) mit den Standardwerten zusammenführen
    und vollständig prüfen. Unbekannte und feste Einträge werden ignoriert.
    Wirft ValueError bei ungültigen Werten."""
    if not isinstance(values, dict):
        raise ValueError("Einstellungen: unerwartetes Format")
    raw = copy.deepcopy(DEFAULTS)
    for section, keys in STORABLE.items():
        given = values.get(section)
        if given is None:
            continue
        if not isinstance(given, dict):
            raise ValueError(f"Einstellungen: [{section}] hat ein unerwartetes Format")
        raw[section].update({k: v for k, v in given.items() if k in keys})
    profiles = values.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("Einstellungen: Profile haben ein unerwartetes Format")
    for name in PROFILE_NAMES:
        given = profiles.get(name)
        if isinstance(given, dict) and "cpu_curve" in given:
            raw["profiles"][name] = {"cpu_curve": given["cpu_curve"],
                                     "gpu_curve": given.get("gpu_curve", given["cpu_curve"])}
    validate(raw)
    cfg = Config(raw=raw, path=path)
    try:
        for name in raw["profiles"]:
            cfg.profile(name)
        for key in ("profile", "battery_profile"):
            cfg.profile(cfg.general[key])
    except KeyError as exc:
        raise ValueError(str(exc).strip("'\"")) from None
    return cfg


def stored_values(cfg: Config) -> dict:
    """Was gespeichert wird: Abweichungen vom Standard, Messwerte vollständig."""
    out: dict = {"version": SETTINGS_VERSION}
    for section, keys in STORABLE.items():
        tested = section == "windows" and cfg.windows["hardware_test"] >= 1
        values = {k: cfg.raw[section][k] for k in keys
                  if cfg.raw[section][k] != DEFAULTS[section][k] or (tested and k in MEASURED)}
        if values:
            out[section] = values
    profiles = {name: {"cpu_curve": [list(p) for p in cfg.raw["profiles"][name]["cpu_curve"]],
                       "gpu_curve": [list(p) for p in cfg.raw["profiles"][name]["gpu_curve"]]}
                for name in PROFILE_NAMES
                if cfg.raw["profiles"][name] != DEFAULT_PROFILES[name]}
    if profiles:
        out["profiles"] = profiles
    return out


def read_values(path: str | Path) -> dict:
    """Gespeicherte Werte lesen ({} wenn die Datei fehlt); ValueError wenn unlesbar."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError) as exc:
        raise ValueError(f"Einstellungen nicht lesbar ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError("Einstellungen: unerwartetes Format")
    return data


def load_config(path: str | Path | None) -> Config:
    """Einstellungen laden; fehlt die Datei, gelten die Standardwerte.
    Wirft ValueError, wenn sie unlesbar oder ungültig ist."""
    if path is None:
        return Config()
    path = Path(path)
    return config_from_values(read_values(path), path)


def write_config(path: str | Path, cfg: Config) -> Config:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(stored_values(cfg), indent=2, ensure_ascii=False) + "\n"
    # Direkt schreiben (nicht ersetzen), damit die Dateirechte für Benutzer erhalten bleiben.
    path.write_text(text, encoding="utf-8")
    cfg.path = path
    return cfg


def save_settings(path: str | Path, changes: dict[str, dict]) -> Config:
    """Werte ändern (Einstellungsfenster, Hardware- und Sensor-Test).

    ``changes`` = {"general": {...}, "profiles.quiet": {"cpu_curve": ...}, ...}.
    Ist das Ergebnis ungültig, bleibt die Datei unverändert (ValueError). Sind
    die gespeicherten Einstellungen beschädigt, wird von den Standardwerten
    ausgegangen - Speichern repariert sie so.
    """
    try:
        values = copy.deepcopy(read_values(path))
        config_from_values(values)
    except ValueError:
        values = {}
    for section, changed in changes.items():
        if not changed:
            continue
        if section.startswith("profiles."):
            values.setdefault("profiles", {}).setdefault(section.split(".", 1)[1], {}).update(changed)
        else:
            values.setdefault(section, {}).update(changed)
    try:
        cfg = config_from_values(values, Path(path))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Ungültige Einstellung: {exc}") from exc
    return write_config(path, cfg)


def reset_settings(path: str | Path) -> Config:
    """Alles auf Standard - nur die Messwerte von Hardware- und Sensor-Test bleiben."""
    try:
        measured = {k: v for k, v in load_config(path).windows.items() if k in MEASURED}
    except ValueError:
        measured = {}
    cfg = config_from_values({"windows": measured} if measured else {})
    return write_config(path, cfg)
