"""Kommandozeile: victus-fan run | status | profile | diagnose | curve"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from pathlib import Path

from . import __version__, paths
from .backends import BACKENDS, BackendError, create_backend
from .config import Config, load_config
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


def _r(value):
    return None if value is None else round(value, 1)


def load_config_or_defaults(path) -> tuple[Config, Exception | None]:
    """Konfiguration laden; ist sie fehlerhaft, die Standardwerte (und den Fehler).

    Dienst, Updater und "restore" dürfen an einer kaputten config.toml nie
    scheitern: Der Dienst regelt dann mit Standardwerten weiter (und übernimmt
    die korrigierte Datei ohne Neustart), und vor allem muss der Updater eine
    neue Version installieren können, die den Fehler vielleicht behebt.
    """
    try:
        return load_config(path), None
    except Exception as exc:  # noqa: BLE001
        return Config(path=Path(path) if path else None), exc


def _config(args) -> Config:
    """Für Befehle von Hand: bei fehlerhafter Datei Standardwerte plus Hinweis."""
    cfg, error = load_config_or_defaults(args.config)
    if error is not None:
        print(f"Hinweis: Konfiguration fehlerhaft ({error}) - nutze Standardwerte")
    return cfg


def cmd_run(args) -> int:
    from .service import FanService

    cfg, error = load_config_or_defaults(args.config)
    log_file = Path(args.log_file) if args.log_file else paths.LOG_FILE
    _setup_logging("DEBUG" if args.verbose else cfg.general["log_level"], log_file)
    if error is not None:
        logging.getLogger(__name__).error("Konfiguration fehlerhaft (%s) - nutze Standardwerte", error)
    backend = create_backend(args.backend or cfg.general["backend"], cfg)
    service = FanService(cfg, backend, status_file=paths.STATUS_FILE,
                         override_file=paths.OVERRIDE_FILE, dry_run=args.dry_run)
    if error is not None:
        service.config_error = f"{error} – es gelten die Standardwerte."
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
    if status.get("fan_mode"):
        print(f"BIOS-Modus:   {status['fan_mode']}")
    print(f"Backend:      {status['backend']}")
    print(f"Profil:       {status['profile']} ({status['profile_reason']})")
    print(f"CPU:          {status['cpu_temp']} °C (geglättet {status['cpu_smoothed']})")
    print(f"GPU:          {status['gpu_temp']} °C (geglättet {status['gpu_smoothed']})")
    print(f"Bedarf:       {status['demand_percent']} %")
    print(f"Lüfter:       {status['output']}{'  KRITISCH' if status['critical'] else ''}")
    print(f"Drehzahl:     {rpm}")
    return 0


def cmd_profile(args) -> int:
    cfg = _config(args)
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
    print({"normal": "Lüftersteuerung an - die Lüfter folgen dem gewählten Profil.",
           "max": "Lüfter laufen auf voller Drehzahl (zurück mit: victus-fan mode normal).",
           "paused": "Pausiert - das BIOS regelt (zurück mit: victus-fan mode normal)."}[args.name])
    return 0




def cmd_gui(args) -> int:
    from .gui import main as gui_main
    return gui_main(show=not args.hidden, config_path=args.config)


def cmd_diagnose(args) -> int:
    cfg = _config(args)
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
        cpu_temp=_r(backend.read_cpu_temp()),
        gpu_temp=_r(backend.read_gpu_temp()),
        on_battery=backend.on_battery(),
        fan_rpm=backend.read_fan_rpm(),
    )
    print(json.dumps(info, indent=2, ensure_ascii=False, default=str))
    if not info["supports_level"]:
        print("\nHinweis: Nur 'Max an/aus' möglich - die Steuerung schaltet mit Hysterese "
              "zwischen BIOS-Automatik und voller Drehzahl.")
    return 0


def cmd_restore(args) -> int:
    cfg = _config(args)
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
    from .config import save_settings

    def finish(code: int) -> int:
        if args.wait:
            input("\nEnter zum Schließen ...")
        return code

    cfg = _config(args)
    print("Hardware-Test der Lüftersteuerung (dauert ca. 1,5 Minuten).")
    print("Die Lüfter werden dabei hörbar schneller und langsamer.\n")
    try:
        backend = create_backend(args.backend or cfg.general["backend"], cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"FEHLER beim Zugriff auf die Hardware: {exc}")
        print("Bitte als Administrator ausführen.")
        return finish(1)

    previous_mode = selftest.pause_service(print)
    try:
        result = selftest.run(backend, settle=args.settle)
    finally:
        try:
            write_override(mode=previous_mode)
        except OSError:
            pass
    selftest.report(result)

    rec = result.recommendation
    if args.apply and rec.get("mode"):
        try:
            # Geprüft speichern: ein unplausibler Messwert darf die Datei nicht ungültig machen.
            from .config import HARDWARE_TEST_VERSION
            save_settings(args.config, {"windows": dict(rec, hardware_test=HARDWARE_TEST_VERSION)},
                          note="vom Hardware-Test gesetzt")
            print(f"\nEinstellungen in {args.config} gespeichert: {rec}")
            print("Der Dienst übernimmt sie automatisch innerhalb weniger Sekunden.")
        except Exception as exc:  # noqa: BLE001
            print(f"\nKonnte die Konfiguration nicht speichern: {exc}")
            return finish(1)
    elif rec.get("mode"):
        print(f"\nEmpfohlen für [windows] in der config.toml: {rec}  (automatisch mit --apply)")
    return finish(0 if rec.get("mode") else 2)


def cmd_update(args) -> int:
    from . import updater

    updater.setup_logging()
    if args.branch is not None:
        name = "" if args.branch in ("", "release") else args.branch
        try:
            updater.set_test_branch(name)
        except OSError as exc:
            print(f"Nicht gespeichert ({exc}) - dafür sind Administratorrechte nötig.")
            return 1
        print(f"Updates folgen jetzt jedem Commit von '{name}' (nur zum Testen)." if name
              else "Updates: nur freigegebene Versionen.")
    cfg, error = load_config_or_defaults(args.config)
    if error is not None:
        logging.getLogger(__name__).warning("Konfiguration fehlerhaft (%s) - nutze Standardwerte", error)
    if args.auto and not cfg.update.get("enabled", True):
        logging.getLogger(__name__).info("Auto-Update ist deaktiviert ([update] enabled = false)")
        return 0
    current = updater.installed()
    print(f"Installiert: {__version__} ({(current.get('sha') or 'unbekannt')[:7]})")
    status = updater.check_and_update(cfg.update, force=args.force,
                                      check_only=True if args.check else None)
    print(status.get("message", ""))
    return 1 if status.get("state") == "error" else 0


def cmd_sensors(args) -> int:
    import json as _json

    from . import sensors
    from .config import save_settings

    def finish(code: int) -> int:
        if args.wait:
            input("\nEnter zum Schließen ...")
        return code

    bios = None
    if sys.platform == "win32":
        try:
            from .backends.windows import HpBios
            bios = HpBios()
        except Exception as exc:  # noqa: BLE001
            print(f"Hinweis: HP-BIOS nicht erreichbar ({exc}) - für BIOS-Sensoren als Administrator starten.")

    print("Suche Temperaturquellen ...")
    found = sensors.discover(bios)
    if not found:
        print("Keine Temperaturquelle gefunden.")
        return finish(1)
    for s in found.values():
        print(f"  {s.label:<55} {s.read()} °C")

    if args.no_load:
        return finish(0)
    print("\nJetzt wird etwa 30 Sekunden lang gemessen, davon 20 s mit voller CPU-Last.")
    print("Bitte währenddessen nichts anderes Rechenintensives starten.\n")
    results = sensors.measure(found, load=args.load)
    best = sensors.recommend(results)
    sensors.report(results, best)
    print("\nRohdaten (zum Kopieren):")
    print(_json.dumps(results, ensure_ascii=False))

    if best and args.apply:
        try:
            save_settings(args.config, {"windows": {"cpu_sensor": best["id"]}},
                          note="von 'victus-fan sensors' gewählt")
            print(f"\nIn {args.config} als CPU-Quelle gespeichert - der Dienst übernimmt sie sofort.")
        except (OSError, ValueError) as exc:
            print(f"\nKonnte die Konfiguration nicht speichern: {exc}")
            return finish(1)
    elif best:
        print("Übernehmen mit: victus-fan sensors --apply")
    return finish(0)


def cmd_migrate_config(args) -> int:
    from .migrate import migrate_config

    try:
        result = migrate_config(args.config, args.template)
    except Exception as exc:  # noqa: BLE001 - Installer soll weiterlaufen, alte Datei bleibt
        print(f"Konfiguration nicht aktualisiert: {exc}")
        return 1
    print(result.summary())
    return 0


def cmd_curve(args) -> int:
    cfg = _config(args)
    profile = cfg.profile(args.name or cfg.general["profile"])
    print(f"Profil {profile.name}:   °C   CPU-Kurve  GPU-Kurve")
    for t in range(30, 101, 5):
        c, g = interpolate(profile.cpu_curve, t), interpolate(profile.gpu_curve, t)
        print(f"{'':16}{t:3d}   {c:5.0f} %    {g:5.0f} %   {'#' * int(max(c, g) / 4)}")
    return 0


def _utf8_console() -> None:
    # Umlaute korrekt ausgeben, auch wenn die Ausgabe umgeleitet wird
    # (PowerShell erwartet sonst eine andere Codepage).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(prog="victus-fan", description="Intelligente Lüftersteuerung für HP Victus")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-c", "--config", default=str(paths.CONFIG_FILE), help="Pfad zur config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="Steuerung starten (Vordergrund; der Dienst nutzt das auch)")
    p.add_argument("--backend", choices=BACKENDS)
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

    p = sub.add_parser("mode", help="normal (Steuerung an) | max (Lüfter auf Maximum) | paused (Steuerung aus, BIOS regelt)")
    p.add_argument("name", choices=MODES)
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("gui", help="Tray-Icon und Fenster starten")
    p.add_argument("--hidden", action="store_true", help="nur Tray-Icon, Fenster nicht öffnen")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("diagnose", help="Hardware-Unterstützung prüfen")
    p.add_argument("--backend", choices=BACKENDS)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("restore", help="Lüfter wieder dem BIOS überlassen (Max aus, Automatik)")
    p.add_argument("--backend", choices=BACKENDS)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("selftest", help="Hardware-Test: prüfen, welche Lüfterbefehle wirklich wirken")
    p.add_argument("--backend", choices=BACKENDS)
    p.add_argument("--apply", action="store_true", help="Ergebnis in die config.toml übernehmen")
    p.add_argument("--settle", type=float, default=15.0, help="Wartezeit je Schritt in Sekunden")
    p.add_argument("--wait", action="store_true", help="am Ende auf Enter warten")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("update", help="nach Updates suchen und installieren (Adminrechte)")
    p.add_argument("--check", action="store_true", help="nur prüfen, nicht installieren")
    p.add_argument("--force", action="store_true", help="auch ohne neue Version neu installieren")
    p.add_argument("--auto", action="store_true", help="für die geplante Aufgabe: beachtet [update] enabled")
    p.add_argument("--branch", metavar="NAME",
                   help="nur zum Testen: jedem Commit dieses Branches folgen; 'release' = wieder nur Versionen")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("sensors", help="Temperaturquellen auflisten und unter CPU-Last vergleichen")
    p.add_argument("--no-load", action="store_true", help="nur auflisten, keine Lastmessung")
    p.add_argument("--load", type=float, default=20.0, help="Dauer der CPU-Last in Sekunden")
    p.add_argument("--apply", action="store_true", help="beste Quelle als CPU-Sensor speichern")
    p.add_argument("--wait", action="store_true", help="am Ende auf Enter warten")
    p.set_defaults(func=cmd_sensors)

    p = sub.add_parser("migrate-config", help="config.toml auf den aktuellen Aufbau bringen (Installer)")
    p.add_argument("--template", required=True, help="Pfad zur config.example.toml")
    p.set_defaults(func=cmd_migrate_config)

    p = sub.add_parser("curve", help="Lüfterkurve eines Profils anzeigen")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_curve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
