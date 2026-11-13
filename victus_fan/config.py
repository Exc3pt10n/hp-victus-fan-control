"""Konfiguration: Standardwerte + Laden aus einer TOML-Datei."""

from __future__ import annotations

import copy
import tomllib
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
        # Backend: "windows" oder "simulate" (Test ohne Hardware).
        "backend": "windows",
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
# Prüfung: Die config.toml dürfen normale Benutzer bearbeiten (Einstellungs-
# fenster), der Dienst läuft aber als SYSTEM und steuert die Hardware. Deshalb
# wird jeder Wert auf Typ und Wertebereich geprüft; Unsinn wie interval = 0 oder
# critical_temp = 200 wird abgelehnt statt übernommen.
# --------------------------------------------------------------------------
# Höchste erlaubte Notfall-Temperatur - fest, nicht konfigurierbar höher.
MAX_CRITICAL_TEMP = 95.0
# Aufbau der config.toml. Bei jeder Änderung erhöhen und migrate.py ergänzen.
CONFIG_VERSION = 3
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
    version = raw.get("config_version", 0)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("config_version: ganze Zahl erwartet")
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


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


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


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    path = Path(path)
    if not path.exists():
        return Config(path=path)
    with path.open("rb") as fh:
        user = tomllib.load(fh)
    return config_from_dict(user, path)


# --------------------------------------------------------------------------
# Schreiben: Die Datei wird immer komplett aus der Vorlage erzeugt - gleicher
# Aufbau, gleiche Reihenfolge, ausgerichtete Kommentare. Egal ob das
# Einstellungsfenster, der Hardware-/Sensor-Test oder ein Update schreibt.
# --------------------------------------------------------------------------
TEMPLATE = Path(__file__).with_name("config.example.toml")
COMMENT_COLUMN = 30
SECTIONS = ("general", "control", "windows", "update", "toggle")


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _comment_start(line: str) -> int | None:
    """Spalte des Kommentars (#) einer TOML-Zeile - ein # in einem String zählt nicht."""
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1                      # Escape überspringen
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            return i
        i += 1
    return None


def _line(key: str, value, comment: str = "") -> str:
    text = f"{key} = {_toml_value(value)}"
    return f"{text.ljust(COMMENT_COLUMN - 1)} {comment}" if comment else text


def render_config(user: dict, template: str | None = None) -> str:
    """Formatierte config.toml: Vorlage mit den Werten aus ``user`` (geladenes TOML).

    Jeder Eintrag der Vorlage bekommt den Wert des Benutzers bzw. den Standard,
    weitere bekannte Einträge mit eigenem Wert werden in ihrer Sektion ergänzt,
    eigene bzw. geänderte Profile folgen am Ende. Unbekannte Einträge entfallen.
    """
    if template is None:
        template = TEMPLATE.read_text(encoding="utf-8-sig")

    def value(section, key):
        given = user.get(section) if isinstance(user.get(section), dict) else {}
        return given.get(key, DEFAULTS[section][key])

    out: list[str] = []
    section = None
    written: set[str] = set()

    def finish_section():
        # Bekannte Einträge, die in der Vorlage fehlen, aber vom Standard abweichen
        if section in SECTIONS:
            extra = [_line(k, value(section, k)) for k in DEFAULTS[section]
                     if k not in written and value(section, k) != DEFAULTS[section][k]]
            while out and not out[-1].strip():
                out.pop()
            out.extend(extra)
            out.append("")

    for raw in template.splitlines():
        stripped = raw.strip()
        if stripped.startswith("["):
            finish_section()
            section = stripped.split("]")[0].lstrip("[").strip()
            written = set()
            out.append(raw)
            continue
        col = _comment_start(raw)
        code = (raw if col is None else raw[:col]).strip()
        if "=" in code and not stripped.startswith("#"):
            key = code.split("=")[0].strip()
            comment = raw[col:].strip() if col is not None else ""
            if section is None and key == "config_version":
                out.append(_line(key, CONFIG_VERSION, comment))
                continue
            if section in SECTIONS and key in DEFAULTS[section]:
                out.append(_line(key, value(section, key), comment))
                written.add(key)
                continue
        out.append(raw.rstrip())
    finish_section()

    profiles = user.get("profiles") if isinstance(user.get("profiles"), dict) else {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        curves = {k: profile[k] for k in ("cpu_curve", "gpu_curve") if k in profile}
        default = DEFAULT_PROFILES.get(name)
        if default and all(curves.get(k) == default[k] for k in ("cpu_curve", "gpu_curve")):
            continue                        # unverändertes Standardprofil: steht im Programm
        out.append(f"[profiles.{name}]")
        out.extend(_line(k, v) for k, v in curves.items())
        out.append("")
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def config_from_dict(user: dict, path: Path | None = None) -> Config:
    """Konfiguration aus geladenem TOML bilden und vollständig prüfen."""
    cfg = Config(raw=_merge(DEFAULTS, user), path=path)
    validate(cfg.raw)
    for name in cfg.raw["profiles"]:
        cfg.profile(name)
    for key in ("profile", "battery_profile"):
        cfg.profile(cfg.general[key])
    return cfg


def write_config(path: str | Path, user: dict) -> Config:
    """``user`` prüfen, formatiert schreiben und die Konfiguration zurückgeben.
    Wirft ValueError, ohne die Datei anzufassen, wenn etwas ungültig ist."""
    path = Path(path)
    text = render_config(user)
    try:
        cfg = config_from_dict(tomllib.loads(text), path)
    except (ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Ungültige Einstellung: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    # Direkt schreiben (nicht ersetzen), damit die Dateirechte für Benutzer erhalten bleiben.
    path.write_text(text, encoding="utf-8")
    return cfg


def read_user_values(path: str | Path) -> dict:
    """Die in der Datei stehenden Werte (ohne Standardwerte); {} wenn sie fehlt."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Die Konfiguration ist nicht lesbar ({exc}) - bitte als Textdatei korrigieren") from exc


def save_settings(path: str | Path, changes: dict[str, dict]) -> Config:
    """Einzelne Werte ändern (Einstellungsfenster, Hardware- und Sensor-Test).

    ``changes`` = {"general": {...}, "profiles.quiet": {"cpu_curve": ...}, ...}.
    Geschrieben wird die ganze Datei formatiert neu; ist das Ergebnis ungültig,
    bleibt sie unverändert und es gibt einen ValueError.
    """
    user = read_user_values(path)
    version = user.get("config_version", 0)
    if user and (not isinstance(version, int) or isinstance(version, bool) or version < CONFIG_VERSION):
        # Datei im alten Format (Migration beim Update fehlgeschlagen o. Ä.): erst
        # umstellen, sonst gingen z. B. die Vermerke des Hardware-Tests verloren.
        from .migrate import migrate_config
        migrate_config(path)
        user = read_user_values(path)
    user = copy.deepcopy(user)
    for section, values in changes.items():
        if not values:
            continue
        if section.startswith("profiles."):
            target = user.setdefault("profiles", {}).setdefault(section.split(".", 1)[1], {})
        else:
            target = user.setdefault(section, {})
            if not isinstance(target, dict):
                target = user[section] = {}
        target.update(values)
    return write_config(path, user)
