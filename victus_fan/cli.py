"""Kommandozeile: victus-fan run | status | profile | diagnose | curve"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from pathlib import Path

from . import __version__, paths
from .backends import BackendError, create_backend
from .config import load_config
from .controller import interpolate
from .state import MODES, read_status, write_override


def _setup_logging(level: str, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:  # unter pythonw.exe gibt es kein stderr
        handlers.append(logging.StreamHandler())
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers or [logging.NullHandler()])


def cmd_run(args) -> int:
    from .service import FanService

    cfg = load_config(args.config)
    log_file = Path(args.log_file) if args.log_file else paths.LOG_FILE
    _setup_logging("DEBUG" if args.verbose else cfg.general["log_level"], log_file)
    backend = create_backend(args.backend or cfg.general["backend"], cfg)
    service = FanService(cfg, backend, status_file=paths.STATUS_FILE,
                         override_file=paths.OVERRIDE_FILE, dry_run=args.dry_run)
    service.install_signal_handlers()
    service.run(verbose=args.verbose)
    return 0


def cmd_status(args) -> int:
    status = read_status()
    if status is None:
        print("Kein Status gefunden - läuft der Dienst?")
        return 1
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0
    rpm = ", ".join(f"{r} U/min" for r in status.get("fan_rpm") or []) or "-"
    note = "  (VERALTET - Dienst läuft nicht?)" if status["stale"] else ""
    note += "  (Testlauf)" if status.get("dry_run") else ""
    print(f"Stand:        {status['time']}{note}")
    print(f"Modus:        {status.get('mode', 'normal')}")
    print(f"Backend:      {status['backend']}")
    print(f"Profil:       {status['profile']} ({status['profile_reason']})")
    print(f"CPU:          {status['cpu_temp']} °C (geglättet {status['cpu_smoothed']})")
    print(f"GPU:          {status['gpu_temp']} °C (geglättet {status['gpu_smoothed']})")
    print(f"Bedarf:       {status['demand_percent']} %")
    print(f"Lüfter:       {status['output']}{'  KRITISCH' if status['critical'] else ''}")
    print(f"Drehzahl:     {rpm}")
    return 0


def cmd_profile(args) -> int:
    cfg = load_config(args.config)
    if args.name != "auto" and args.name not in cfg.raw["profiles"]:
        print(f"Unbekanntes Profil. Verfügbar: auto, {', '.join(cfg.raw['profiles'])}")
        return 2
    try:
        write_override(profile=args.name)
    except OSError as exc:
        print(f"Konnte {paths.OVERRIDE_FILE} nicht schreiben: {exc}")
        return 1
    print("Automatische Profilwahl (Netz/Akku) aktiv." if args.name == "auto"
          else f"Profil '{args.name}' aktiv (zurück mit: victus-fan profile auto).")
    return 0


def cmd_mode(args) -> int:
    try:
        write_override(mode=args.name)
    except OSError as exc:
        print(f"Konfiguration nicht schreibbar: {exc}")
        return 1
    print({"normal": "Intelligente Steuerung aktiv.",
           "max": "Lüfter laufen auf voller Drehzahl (zurück mit: victus-fan mode normal).",
           "paused": "Pausiert - das BIOS regelt (zurück mit: victus-fan mode normal)."}[args.name])
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    return gui_main(show=not args.hidden, config_path=args.config)


def cmd_diagnose(args) -> int:
    cfg = load_config(args.config)
    _setup_logging("DEBUG" if args.verbose else "INFO", None)
    try:
        backend = create_backend(args.backend or cfg.general["backend"], cfg)
    except BackendError as exc:
        print(f"FEHLER: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FEHLER beim Zugriff auf die Hardware: {exc!r}")
        print("Unter Windows: Eingabeaufforderung/PowerShell als Administrator starten.")
        return 1
    info = backend.describe()
    info.update(
        cpu_temp=backend.read_cpu_temp(),
        gpu_temp=backend.read_gpu_temp(),
        on_battery=backend.on_battery(),
        fan_rpm=backend.read_fan_rpm(),
    )
    print(json.dumps(info, indent=2, ensure_ascii=False, default=str))
    if not info["supports_level"]:
        print("\nHinweis: Nur 'Max an/aus' möglich - die Steuerung schaltet mit Hysterese "
              "zwischen BIOS-Automatik und voller Drehzahl.")
    return 0


def cmd_restore(args) -> int:
    cfg = load_config(args.config)
    try:
        backend = create_backend(args.backend or cfg.general["backend"], cfg)
        backend.shutdown()
    except Exception as exc:  # noqa: BLE001
        print(f"FEHLER: {exc}")
        return 1
    print("Lüftersteuerung an das BIOS zurückgegeben.")
    return 0


def cmd_selftest(args) -> int:
    from . import selftest
    from .config import update_config_values

    def finish(code: int) -> int:
        if args.wait:
            input("\nEnter zum Schließen ...")
        return code

    cfg = load_config(args.config)
    print("Hardware-Test der Lüftersteuerung (dauert ca. 2 Minuten).")
    print("Die Lüfter werden dabei hörbar schneller und langsamer.\n")
    try:
        backend = create_backend(args.backend or cfg.general["backend"], cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"FEHLER beim Zugriff auf die Hardware: {exc}")
        print("Bitte als Administrator ausführen.")
        return finish(1)

    previous_mode = selftest.pause_service(print)
    try:
        result = selftest.run(backend, settle=args.settle, extended=not args.quick)
    finally:
        try:
            write_override(mode=previous_mode)
        except OSError:
            pass
    selftest.report(result)

    rec = result.recommendation
    if args.apply and rec.get("mode"):
        try:
            update_config_values(args.config, "windows", rec, note="vom Hardware-Test gesetzt")
            print(f"\nEinstellungen in {args.config} gespeichert: {rec}")
            print("Der Dienst übernimmt sie automatisch innerhalb weniger Sekunden.")
        except Exception as exc:  # noqa: BLE001
            print(f"\nKonnte die Konfiguration nicht speichern: {exc}")
            return finish(1)
    elif rec.get("mode"):
        print(f"\nEmpfohlen für [windows] in der config.toml: {rec}  (automatisch mit --apply)")
    return finish(0 if rec.get("mode") else 2)


def cmd_curve(args) -> int:
    cfg = load_config(args.config)
    profile = cfg.profile(args.name or cfg.general["profile"])
    print(f"Profil {profile.name}:   °C   CPU-Kurve  GPU-Kurve")
    for t in range(30, 101, 5):
        c, g = interpolate(profile.cpu_curve, t), interpolate(profile.gpu_curve, t)
        print(f"{'':16}{t:3d}   {c:5.0f} %    {g:5.0f} %   {'#' * int(max(c, g) / 4)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="victus-fan", description="Intelligente Lüftersteuerung für HP Victus")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-c", "--config", default=str(paths.CONFIG_FILE), help="Pfad zur config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="Steuerung starten (Vordergrund; der Dienst nutzt das auch)")
    p.add_argument("--backend", choices=["auto", "windows", "linux", "simulate"])
    p.add_argument("--dry-run", action="store_true", help="nur messen und rechnen, nichts setzen")
    p.add_argument("--log-file")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="aktuellen Zustand des Dienstes anzeigen")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("profile", help="Profil festlegen: quiet | balanced | performance | auto")
    p.add_argument("name")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("mode", help="Modus: normal | max (volle Drehzahl) | paused (BIOS regelt)")
    p.add_argument("name", choices=MODES)
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("gui", help="Tray-Icon und Fenster starten")
    p.add_argument("--hidden", action="store_true", help="nur Tray-Icon, Fenster nicht öffnen")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("diagnose", help="Hardware-Unterstützung prüfen")
    p.add_argument("--backend", choices=["auto", "windows", "linux", "simulate"])
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("restore", help="Lüfter wieder dem BIOS überlassen (Max aus, Automatik)")
    p.add_argument("--backend", choices=["auto", "windows", "linux", "simulate"])
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("selftest", help="Hardware-Test: prüfen, welche Lüfterbefehle wirklich wirken")
    p.add_argument("--backend", choices=["auto", "windows", "linux", "simulate"])
    p.add_argument("--apply", action="store_true", help="Ergebnis in die config.toml übernehmen")
    p.add_argument("--settle", type=float, default=15.0, help="Wartezeit je Schritt in Sekunden")
    p.add_argument("--wait", action="store_true", help="am Ende auf Enter warten")
    p.add_argument("--quick", action="store_true", help="BIOS-Leistungsmodi nicht testen")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("curve", help="Lüfterkurve eines Profils anzeigen")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_curve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
