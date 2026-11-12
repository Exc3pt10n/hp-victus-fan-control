"""Hardware-Test: prüft, welche Lüfterbefehle tatsächlich wirken.

HP-BIOS bestätigen Befehle auch dann, wenn sie sie ignorieren. Deshalb wird
hier nicht dem Rückgabecode vertraut, sondern die gemessene Drehzahl verglichen.

Ablauf:
1. Drehzahl in der BIOS-Automatik
2. Max-Lüfter an (ergibt auch die echte Maximaldrehzahl)
3. Leistungsmodi der Reihe nach setzen, bis feste Drehzahlen wirken
   (Victus BV8D4EA: das BIOS setzt sie erst nach dem Setzen eines Modus um)
4. Niedrige Stufen im gefundenen Modus (Untergrenze, Stillstand, Anlauf aus
   dem Stillstand - beim Victus BV8D4EA läuft ein stehender Lüfter bei
   niedriger Stufe nicht von selbst an; die Steuerung hilft dann kurz nach)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from statistics import mean

from .state import read_override, read_status, write_override

MIN_DIFF_RPM = 700
# Reihenfolge = Vorzug: "default" und "cool" erhöhen die Leistungsaufnahme
# nicht, "performance" schon.
TEST_FAN_MODES = ("default", "cool", "performance")
LOW_TEST_LEVEL = 11  # ca. 1100 U/min - etwa die Anlaufdrehzahl der Profile


@dataclass
class Result:
    baseline: list[int] = field(default_factory=list)
    max_on: list[int] = field(default_factory=list)
    max_works: bool = False
    level_high_set: list[int] = field(default_factory=list)
    level_low_set: list[int] = field(default_factory=list)
    mode_levels: dict = field(default_factory=dict)    # Modus -> {high, low, works}
    level_works: bool = False
    best_fan_mode: str = ""
    level_min_test: list[int] = field(default_factory=list)
    level_zero: list[int] = field(default_factory=list)
    start_from_stop: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    recommendation: dict = field(default_factory=dict)


def _avg(values: list[int]) -> float:
    return mean(values) if values else 0.0


def _rpm(values: list[int]) -> str:
    return " / ".join(map(str, values)) or "-"


def _wait_and_measure(backend, seconds: float, label: str, say) -> list[int]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        say(f"\r  {label}: {_rpm(backend.read_fan_rpm())} U/min   "
            f"(noch {int(end - time.monotonic())} s)   ", end="")
        time.sleep(1)
    rpm = backend.read_fan_rpm()
    say(f"\r  {label}: {_rpm(rpm)} U/min" + " " * 20)
    return rpm


def pause_service(say, timeout: float = 8.0) -> str:
    """Dienst pausieren, damit er nicht dazwischenfunkt. Gibt den alten Modus zurück."""
    previous = read_override()["mode"]
    write_override(mode="paused")
    status = read_status()
    if status is None or status["stale"]:
        return previous  # Dienst läuft nicht
    say("Pausiere den Hintergrunddienst ...")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = read_status()
        if status and status.get("output") == "BIOS":
            break
        time.sleep(0.5)
    return previous


def _set_levels(backend, levels: list[int]) -> None:
    if hasattr(backend, "set_levels"):
        try:
            backend.set_levels(levels, kick=False)   # ungeschönt messen, ohne Anlaufhilfe
        except TypeError:
            backend.set_levels(levels)
    else:  # z. B. Simulation: arbeitet mit Prozentwerten
        backend.supports_level = True
        backend.set_percent(levels[0])


def run(backend, settle: float = 15.0, say=print) -> Result:
    r = Result()
    has_modes = hasattr(backend, "set_fan_mode")

    say("\n1) Ausgangslage (BIOS-Automatik)")
    try:
        backend.restore_auto()
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"restore_auto: {exc}")
    r.baseline = _wait_and_measure(backend, settle / 2, "BIOS", say)

    say("\n2) Max-Lüfter")
    try:
        backend.set_max(True)
        r.max_on = _wait_and_measure(backend, settle, "Max", say)
        r.max_works = max(r.max_on or [0]) >= max(r.baseline or [0]) + MIN_DIFF_RPM
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"set_max: {exc}")
    finally:
        try:
            backend.set_max(False)
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"set_max(False): {exc}")

    top = [x // 100 for x in r.max_on] if r.max_works and len(r.max_on) >= 2 else \
        [getattr(backend, "max_cpu", 54), getattr(backend, "max_gpu", 52)]
    r.level_high_set = [round(x * 0.85) for x in top]
    r.level_low_set = [round(x * 0.4) for x in top]

    try:
        for mode in (TEST_FAN_MODES if has_modes else ("",)):
            say(f"\n3) Feste Drehzahl{f' im Leistungsmodus {mode}' if mode else ''}")
            if mode:
                try:
                    backend.set_fan_mode(mode)
                except Exception as exc:  # noqa: BLE001
                    r.errors.append(f"set_fan_mode({mode}): {exc}")
                    continue
            _set_levels(backend, r.level_high_set)
            high = _wait_and_measure(backend, settle, f"Stufe {r.level_high_set}", say)
            _set_levels(backend, r.level_low_set)
            low = _wait_and_measure(backend, settle, f"Stufe {r.level_low_set}", say)
            works = _avg(high) - _avg(low) >= MIN_DIFF_RPM
            r.mode_levels[mode or "-"] = {"high": high, "low": low, "works": works}
            if works:
                r.level_works = True
                r.best_fan_mode = mode
                break

        if r.level_works:
            say("\n4) Niedrige Stufen")
            _set_levels(backend, [LOW_TEST_LEVEL, LOW_TEST_LEVEL])
            r.level_min_test = _wait_and_measure(backend, settle, f"Stufe {LOW_TEST_LEVEL}", say)
            _set_levels(backend, [0, 0])
            r.level_zero = _wait_and_measure(backend, settle, "Stufe 0", say)
            if r.level_zero and max(r.level_zero) == 0:
                _set_levels(backend, [LOW_TEST_LEVEL, LOW_TEST_LEVEL])
                r.start_from_stop = _wait_and_measure(
                    backend, settle, f"Stufe {LOW_TEST_LEVEL} aus dem Stillstand", say)
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"Drehzahlvorgabe: {exc}")
    finally:
        if has_modes:
            try:
                backend.set_fan_mode("default")
            except Exception as exc:  # noqa: BLE001
                r.errors.append(f"set_fan_mode(default): {exc}")
        try:
            backend.restore_auto()
        except Exception:  # noqa: BLE001
            pass

    r.recommendation = _recommend(r, has_modes)
    return r


def _recommend(r: Result, has_modes: bool) -> dict:
    rec: dict = {}
    if r.level_works:
        rec["mode"] = "level"
        if has_modes:
            rec["fan_mode"] = r.best_fan_mode
    elif r.max_works:
        rec["mode"] = "toggle"
        if has_modes:
            rec["fan_mode"] = "default"
    if r.max_works and len(r.max_on) >= 2 and has_modes:
        rec["max_level_cpu"] = max(1, r.max_on[0] // 100)
        rec["max_level_gpu"] = max(1, r.max_on[1] // 100)
    if r.level_min_test:
        # Hebt das BIOS kleine Stufen an, ist das die echte Untergrenze.
        floor = max(r.level_min_test) // 100
        rec["min_level"] = floor if floor > LOW_TEST_LEVEL + 3 else 0
    return rec


def report(r: Result, say=print) -> None:
    say("\n=== Ergebnis ===")
    say(f"Max-Lüfter:      {'wirkt' if r.max_works else 'wirkt NICHT'} ({_rpm(r.max_on)} U/min)")
    for mode, lv in r.mode_levels.items():
        label = f" ({mode})" if mode != "-" else ""
        say(f"Feste Drehzahl{label}: {'wirkt' if lv['works'] else 'wirkt NICHT'}")
    if r.level_min_test:
        say(f"Stufe {LOW_TEST_LEVEL} (~{LOW_TEST_LEVEL * 100} U/min): {_rpm(r.level_min_test)} U/min")
    if r.level_zero:
        stops = all(x == 0 for x in r.level_zero)
        say(f"Stufe 0: {_rpm(r.level_zero)} U/min "
            f"({'Lüfter stehen still' if stops else 'BIOS hält eine Mindestdrehzahl'})")
    if r.start_from_stop:
        starts = all(x > 0 for x in r.start_from_stop)
        say(f"Anlauf aus dem Stillstand: {_rpm(r.start_from_stop)} U/min "
            f"({'läuft an' if starts else 'läuft NICHT an – die Anlaufhilfe der Steuerung übernimmt das'})")
    for e in r.errors:
        say(f"Fehler: {e}")

    rec = r.recommendation
    if rec.get("mode") == "level":
        extra = f" im BIOS-Leistungsmodus '{rec['fan_mode']}'" if rec.get("fan_mode") else ""
        say(f"\n-> Stufenlose Steuerung möglich{extra}.")
        if rec.get("fan_mode") == "performance":
            say("   Hinweis: 'performance' erlaubt mehr Leistungsaufnahme (etwas mehr Wärme).")
    elif rec.get("mode") == "toggle":
        say("\n-> Dieses BIOS ignoriert feste Drehzahlen. Die Steuerung schaltet daher\n"
            "   zwischen BIOS-Automatik und Max-Lüfter um (mit Hysterese).")
    else:
        say("\n-> Keine Steuerungsart zeigt Wirkung. Bitte die Rohdaten melden.")
    say("\nRohdaten (zum Kopieren):")
    say(json.dumps(asdict(r), ensure_ascii=False))
