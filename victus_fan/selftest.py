"""Hardware-Test: prüft, welche Lüfterbefehle tatsächlich wirken.

Das BIOS mancher Victus-Modelle nimmt Befehle an (Rückgabecode 0), setzt sie
aber nicht um. Deshalb wird hier nicht dem Rückgabecode vertraut, sondern
die tatsächlich gemessene Drehzahl verglichen.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from statistics import mean

from .state import read_override, read_status, write_override

MIN_DIFF_RPM = 700


# Reihenfolge = Vorzug: "default" und "cool" erhöhen die Leistungsaufnahme nicht,
# "performance" schon.
TEST_FAN_MODES = ("default", "cool", "performance")
LOW_TEST_LEVEL = 11  # ca. 1100 U/min - entspricht etwa der Anlaufdrehzahl der Profile


@dataclass
class Result:
    baseline: list[int] = field(default_factory=list)
    max_on: list[int] = field(default_factory=list)
    level_high: list[int] = field(default_factory=list)
    level_low: list[int] = field(default_factory=list)
    level_high_set: list[int] = field(default_factory=list)
    level_low_set: list[int] = field(default_factory=list)
    max_works: bool = False
    level_works: bool = False
    # Erweiterter Test: BIOS-Leistungsmodi
    fan_modes: dict = field(default_factory=dict)      # Modus -> Leerlauf-Drehzahl
    mode_levels: dict = field(default_factory=dict)    # Modus -> {high, low, works}
    best_fan_mode: str = ""
    level_zero: list[int] = field(default_factory=list)
    level_min_test: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    recommendation: dict = field(default_factory=dict)


def _avg(values: list[int]) -> float:
    return mean(values) if values else 0.0


def _wait_and_measure(backend, seconds: float, label: str, say) -> list[int]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        rpm = backend.read_fan_rpm()
        say(f"\r  {label}: {' / '.join(map(str, rpm)) or '-'} U/min   "
            f"(noch {int(end - time.monotonic())} s)   ", end="")
        time.sleep(1)
    rpm = backend.read_fan_rpm()
    say(f"\r  {label}: {' / '.join(map(str, rpm)) or '-'} U/min" + " " * 20)
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


def run(backend, settle: float = 15.0, say=print, extended: bool = True) -> Result:
    r = Result()
    has_levels = hasattr(backend, "set_levels")

    say("\n1) Ausgangslage (BIOS-Automatik)")
    try:
        backend.restore_auto()
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"restore_auto: {exc}")
    r.baseline = _wait_and_measure(backend, settle / 2, "BIOS", say)

    say("\n2) Max-Lüfter einschalten")
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

    say("\n3) Feste Drehzahl hoch / niedrig vorgeben")
    try:
        if has_levels:
            top = [max(1, x // 100) for x in r.max_on] if r.max_works and r.max_on else \
                [backend.max_cpu, backend.max_gpu]
            top = (top + top)[:2]
            r.level_high_set = [round(x * 0.85) for x in top]
            r.level_low_set = [round(x * 0.4) for x in top]
            backend.set_levels(r.level_high_set)
            r.level_high = _wait_and_measure(backend, settle, f"Stufe {r.level_high_set}", say)
            backend.set_levels(r.level_low_set)
            r.level_low = _wait_and_measure(backend, settle, f"Stufe {r.level_low_set}", say)
        else:
            backend.supports_level = True
            backend.set_percent(85)
            r.level_high = _wait_and_measure(backend, settle, "85 %", say)
            backend.set_percent(40)
            r.level_low = _wait_and_measure(backend, settle, "40 %", say)
        r.level_works = _avg(r.level_high) - _avg(r.level_low) >= MIN_DIFF_RPM
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"Drehzahlvorgabe: {exc}")
    finally:
        try:
            backend.restore_auto()
        except Exception:  # noqa: BLE001
            pass

    if extended and hasattr(backend, "set_fan_mode") and r.level_high_set:
        _run_fan_modes(backend, r, settle, say)

    rec: dict = {}
    if r.level_works:
        rec["mode"] = "level"
    elif r.best_fan_mode:
        rec["mode"] = "level"
        rec["fan_mode"] = r.best_fan_mode
    elif r.max_works:
        rec["mode"] = "toggle"
    if r.fan_modes and r.level_works:
        # Feste Drehzahlen wirkten schon vor dem Modus-Test - das kann aber daran
        # liegen, dass vorher bereits ein Modus gesetzt war (nach dem Systemstart
        # ignoriert das BIOS sie ggf.). "default" einmal zu setzen schaltet sie
        # zuverlässig frei, ohne die Leistungsaufnahme zu ändern.
        rec["fan_mode"] = "default"
    elif r.fan_modes and not r.best_fan_mode:
        rec["fan_mode"] = ""
    if r.max_works and len(r.max_on) >= 2 and has_levels:
        rec["max_level_cpu"] = max(1, r.max_on[0] // 100)
        rec["max_level_gpu"] = max(1, r.max_on[1] // 100)
    if r.level_min_test:
        # Hebt das BIOS kleine Stufen an, ist das die echte Untergrenze.
        floor = max(r.level_min_test) // 100
        rec["min_level"] = floor if floor > LOW_TEST_LEVEL + 3 else 0
    r.recommendation = rec
    return r


def _run_fan_modes(backend, r: Result, settle: float, say) -> None:
    try:
        for mode in TEST_FAN_MODES:
            say(f"\n4) BIOS-Leistungsmodus {mode}")
            try:
                backend.set_fan_mode(mode)
            except Exception as exc:  # noqa: BLE001
                r.errors.append(f"set_fan_mode({mode}): {exc}")
                continue
            r.fan_modes[mode] = _wait_and_measure(backend, settle, "Automatik", say)
            backend.set_levels(r.level_high_set)
            high = _wait_and_measure(backend, settle, f"Stufe {r.level_high_set}", say)
            backend.set_levels(r.level_low_set)
            low = _wait_and_measure(backend, settle, f"Stufe {r.level_low_set}", say)
            works = _avg(high) - _avg(low) >= MIN_DIFF_RPM
            r.mode_levels[mode] = {"high": high, "low": low, "works": works}
            if works and not r.best_fan_mode:
                r.best_fan_mode = mode

        if r.best_fan_mode:
            say(f"\n5) Niedrige Stufen im Modus {r.best_fan_mode}")
            backend.set_fan_mode(r.best_fan_mode)
            backend.set_levels([LOW_TEST_LEVEL, LOW_TEST_LEVEL])
            r.level_min_test = _wait_and_measure(backend, settle, f"Stufe [{LOW_TEST_LEVEL}, {LOW_TEST_LEVEL}]", say)
            backend.set_levels([0, 0])
            r.level_zero = _wait_and_measure(backend, settle, "Stufe [0, 0]", say)
    except Exception as exc:  # noqa: BLE001
        r.errors.append(f"Leistungsmodus-Test: {exc}")
    finally:
        try:
            backend.set_fan_mode("default")
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"set_fan_mode(default): {exc}")
        try:
            backend.restore_auto()
        except Exception:  # noqa: BLE001
            pass


def report(r: Result, say=print) -> None:
    say("\n=== Ergebnis ===")
    say(f"Max-Lüfter an/aus:     {'wirkt' if r.max_works else 'wirkt NICHT'}")
    say(f"Feste Drehzahlvorgabe: {'wirkt' if r.level_works else 'wirkt NICHT'}")
    for mode, rpm in r.fan_modes.items():
        diff = _avg(rpm) - _avg(r.baseline)
        lv = r.mode_levels.get(mode, {})
        say(f"Modus {mode:<12} Automatik {' / '.join(map(str, rpm))} U/min "
            f"({'+' if diff >= 0 else ''}{diff:.0f}), feste Drehzahl: "
            f"{'wirkt' if lv.get('works') else 'wirkt NICHT'}")
    if r.level_min_test:
        say(f"Stufe {LOW_TEST_LEVEL} (~{LOW_TEST_LEVEL * 100} U/min): "
            f"{' / '.join(map(str, r.level_min_test))} U/min")
    if r.level_zero:
        stops = all(x == 0 for x in r.level_zero)
        say(f"Stufe 0: {' / '.join(map(str, r.level_zero))} U/min "
            f"({'Lüfter stehen still' if stops else 'BIOS hält eine Mindestdrehzahl'})")
    if r.fan_modes:
        say("Hinweis: Der BIOS-Leistungsmodus wurde auf 'default' zurückgesetzt. Falls du im\n"
            "OMEN Gaming Hub einen anderen Modus nutzt, stelle ihn dort ggf. neu ein.")
    for e in r.errors:
        say(f"Fehler: {e}")
    rec = r.recommendation
    if rec.get("mode") == "level" and rec.get("fan_mode") == "performance":
        say("\n-> Stufenlose Steuerung möglich, aber nur im BIOS-Modus 'performance'.\n"
            "   Dieser Modus erlaubt auch mehr Leistungsaufnahme (etwas mehr Wärme).")
    elif rec.get("mode") == "level" and rec.get("fan_mode"):
        say(f"\n-> Stufenlose Steuerung möglich im BIOS-Modus '{rec['fan_mode']}'.")
    elif rec.get("mode") == "level":
        say("\n-> Stufenlose Steuerung möglich.")
    elif rec.get("mode") == "toggle":
        say("\n-> Dieses BIOS ignoriert feste Drehzahlen. Die Steuerung schaltet daher\n"
            "   zwischen BIOS-Automatik und Max-Lüfter um (mit Hysterese).")
    else:
        say("\n-> Keine der Steuerungsarten zeigt Wirkung. Bitte die Ausgabe unten melden.")
    say("\nRohdaten (zum Kopieren):")
    say(json.dumps(asdict(r), ensure_ascii=False))
