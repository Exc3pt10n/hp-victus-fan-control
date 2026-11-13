"""Logzeilen zerlegen und filtern (ohne Oberfläche, damit testbar)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

LINE_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d) (\d\d:\d\d:\d\d),\d+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
                     r"(?:[\w.]+: )?(.*)$")
LEVELS = {"DEBUG": "Debug", "INFO": "Info", "WARNING": "Warnung", "ERROR": "Fehler", "CRITICAL": "Fehler"}
MAX_LINES = 3000


@dataclass
class Entry:
    date: str          # "12.11."
    time: str          # "18:44:36"
    level: str         # "Info", "Warnung", "Fehler", "Debug"
    message: str       # ggf. mehrzeilig

    @property
    def important(self) -> bool:
        return self.level in ("Warnung", "Fehler")

    def text(self) -> str:
        return f"{self.date} {self.time}  {self.level:<7}  {self.message}"


def parse_log(text: str) -> list[Entry]:
    """Logtext in Einträge zerlegen; Folgezeilen gehören zum vorherigen Eintrag."""
    entries: list[Entry] = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            y, mo, d, t, level, msg = m.groups()
            entries.append(Entry(f"{d}.{mo}.", t, LEVELS[level], msg))
        elif line.strip() and entries:
            entries[-1].message += "\n" + line
        elif line.strip():
            entries.append(Entry("", "", "Info", line))
    return entries


def read_tail(path: Path, max_lines: int = MAX_LINES) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def filter_entries(entries: list[Entry], only_important: bool, search: str) -> list[Entry]:
    needle = search.strip().lower()
    return [e for e in entries
            if (not only_important or e.important) and (not needle or needle in e.text().lower())]
