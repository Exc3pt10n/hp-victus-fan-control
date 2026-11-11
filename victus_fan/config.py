"""Konfiguration: Standardwerte + Laden aus einer TOML-Datei."""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Lüfterkurven: (Temperatur in °C, Lüfterleistung in %). Zwischen den Punkten
# wird linear interpoliert.
DEFAULT_PROFILES: dict[str, dict] = {
    "quiet": {
        "cpu_curve": [[0, 0], [55, 0], [62, 25], [72, 45], [82, 70], [90, 100]],
        "gpu_curve": [[0, 0], [52, 0], [60, 25], [70, 45], [78, 70], [85, 100]],
        "platform_profile": "quiet",
    },
    "balanced": {
        "cpu_curve": [[0, 0], [48, 0], [55, 25], [65, 45], [75, 70], [85, 100]],
        "gpu_curve": [[0, 0], [45, 0], [52, 25], [62, 45], [72, 70], [82, 100]],
        "platform_profile": "balanced",
    },
    "performance": {
        "cpu_curve": [[0, 25], [45, 30], [55, 45], [65, 65], [75, 85], [80, 100]],
        "gpu_curve": [[0, 25], [42, 30], [52, 45], [62, 65], [70, 85], [76, 100]],
        "platform_profile": "performance",
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
        # Backend: "auto", "windows", "linux" oder "simulate".
        "backend": "auto",
        # Plattform-/Leistungsprofil des Notebooks mit umschalten (Linux).
        "switch_platform_profile": False,
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
        # "auto": Drehzahlsteuerung testen, sonst Max-Umschaltung.
        # "level": nur Drehzahlstufen, "toggle": nur Max an/aus.
        "mode": "auto",
        # Leistungsmodus des BIOS, der beim Start gesetzt wird:
        # "" (nicht ändern), "default", "performance" oder "cool".
        # Manche BIOS setzen feste Drehzahlen nur im Modus "performance" um.
        "fan_mode": "",
        # Kleinste Stufe > 0, die das BIOS wirklich umsetzt (0 = keine Grenze).
        "min_level": 0,
        # Maximale Lüfterstufe in 100 U/min (55 = 5500 U/min).
        "max_level_cpu": 55,
        "max_level_gpu": 57,
        # Das BIOS fällt nach ca. 120 s ohne Befehl in den Automatikmodus
        # zurück - deshalb regelmäßig erneut senden.
        "keepalive": 60.0,
    },
    "toggle": {
        # Für Geräte, die nur "Max an/aus" können.
        "engage_percent": 80.0,
        "release_percent": 55.0,
        # Max bleibt mindestens so viele Sekunden an.
        "min_on_time": 60.0,
    },
    "profiles": DEFAULT_PROFILES,
}


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
    platform_profile: str | None = None


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
            platform_profile=p.get("platform_profile"),
        )


def _validate_curve(points, label: str) -> list[tuple[float, float]]:
    if not points:
        raise ValueError(f"{label}: Kurve ist leer")
    curve = [(float(t), float(p)) for t, p in points]
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
    cfg = Config(raw=_merge(DEFAULTS, user), path=path)
    # Profile früh prüfen, damit Fehler beim Start auffallen.
    for name in cfg.raw["profiles"]:
        cfg.profile(name)
    for key in ("profile", "battery_profile"):
        cfg.profile(cfg.general[key])
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_config_values(path: str | Path, section: str, values: dict, note: str = "") -> None:
    """Setzt einzelne Werte in einer Sektion der config.toml und lässt den Rest
    (inkl. Kommentaren) unverändert."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    comment = f"  # {note}" if note else ""
    header = f"[{section}]"
    start = next((i for i, l in enumerate(lines) if l.strip().split("#")[0].strip() == header), None)
    if start is None:
        lines += ["", header] + [f"{k} = {_toml_value(v)}{comment}" for k, v in values.items()]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        pending = dict(values)
        for i in range(start + 1, end):
            key = lines[i].split("=")[0].strip()
            if "=" in lines[i] and not lines[i].lstrip().startswith("#") and key in pending:
                lines[i] = f"{key} = {_toml_value(pending.pop(key))}{comment}"
        for offset, (k, v) in enumerate(pending.items()):
            lines.insert(start + 1 + offset, f"{k} = {_toml_value(v)}{comment}")
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # nur gültiges TOML schreiben
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
