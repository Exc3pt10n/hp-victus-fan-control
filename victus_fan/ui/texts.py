"""Texte der Oberfläche: Profilnamen, Hinweise, Tray-Tooltip und Update-Status."""

from __future__ import annotations

import time

from .theme import ACCENT, OK, WARN, C, fmt_temp

# --------------------------------------------------------------------------
# Texte
# --------------------------------------------------------------------------
# Eine Skala für die Lüfter: die Profile plus "Maximum" (Steuerung "max").
PROFILE_LABELS = {"auto": "Automatisch", "quiet": "Leise", "balanced": "Ausgewogen",
                  "performance": "Stark"}
MAX_LABEL = "Maximum"
FAN_HINTS = {
    "auto": "Ausgewogen am Netzteil, Leise im Akkubetrieb.",
    "quiet": "Lüfter stehen unter 50 °C still und laufen danach sanft an.",
    "balanced": "Leise im Alltag, kühlt unter Last kräftig.",
    "performance": "Kühlt früh und kräftig – für Spiele und Rendering.",
    "max": "Beide Lüfter laufen dauerhaft mit voller Drehzahl (≈ 5400 U/min).",
}
PAUSED_HINT = "Die Lüftersteuerung ist aus – das BIOS regelt die Lüfter wie ab Werk."



def profile_label(name: str) -> str:
    return PROFILE_LABELS.get(name, name)


def fan_choice(override: dict) -> str:
    """Gewählte Stufe auf der Lüfter-Skala: Profil oder "max"."""
    return "max" if override.get("mode") == "max" else override.get("profile", "auto")



def tray_tooltip(s: dict | None, running: bool) -> str:
    """Text beim Überfahren des Tray-Icons (Windows kürzt nach 127 Zeichen)."""
    if not running or not s:
        return "Victus Lüftersteuerung\nDienst läuft nicht"
    gpu = "schläft" if s.get("gpu_asleep") else fmt_temp(s.get("gpu_temp"))
    lines = [f"CPU {fmt_temp(s.get('cpu_temp'))} · GPU {gpu}"]
    rpm = s.get("fan_rpm") or []
    if rpm:
        fans = " · ".join(f"{name} {value}" for name, value in zip(("CPU", "GPU"), rpm))
        lines.append(f"Lüfter {fans} U/min")
    output = s.get("output", "")
    if s.get("mode") == "paused":
        lines.append("Lüftersteuerung aus (BIOS regelt)")
    elif output == "MAX":
        lines.append(MAX_LABEL)
    elif s.get("warning"):
        lines.append("⚠ Lüfter folgen nicht")
    else:
        lines.append(f"{profile_label(s.get('profile', ''))} · Vorgabe {s.get('fan_percent', 0):.0f} %")
    return "\n".join(["Victus Lüftersteuerung"] + lines)[:127]


UPDATE_TEXT = {
    "current": "Aktuell",
    "available": "Update verfügbar",
    "installing": "Update wird installiert …",
    "updated": "Aktualisiert",
    "error": "Update-Prüfung fehlgeschlagen",
}


def update_summary(status: dict, installed: dict | None = None) -> tuple[str, str]:
    """Kurztext + Farbe für den Update-Status."""
    installed = installed or {}
    state = status.get("state")
    if state == "installing":
        if status.get("latest_sha") and status.get("latest_sha") == installed.get("sha"):
            # Installation fertig, der Updater konnte es nur nicht mehr eintragen.
            state = "updated"
            status = dict(status, updated_at=installed.get("installed_at", ""))
        else:
            try:
                started = time.mktime(time.strptime(status.get("checked_at", ""), "%Y-%m-%d %H:%M"))
            except ValueError:
                started = None
            if started is not None and time.time() - started > 45 * 60:
                return f"Update hängt seit {status['checked_at']} – siehe update.log", WARN
    if not state:
        return "Noch nicht nach Updates gesucht", C.MUTED
    text = UPDATE_TEXT.get(state, state)
    if state == "updated" and status.get("updated_at"):
        text += f" am {status['updated_at']}"
    elif status.get("checked_at") and state in ("current", "error", "available"):
        text += f" · geprüft {status['checked_at']}"
    color = {"error": WARN, "available": ACCENT, "installing": ACCENT, "updated": OK}.get(state, C.MUTED)
    return text, color
