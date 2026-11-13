"""Auto-Update über GitHub.

Ablauf (läuft als geplante Aufgabe "VictusFanUpdate" mit SYSTEM-Rechten):

1. Neueste Version abfragen: das letzte GitHub-Release bzw. den höchsten Tag
   ``vX.Y.Z`` (angelegt vom Workflow nach einem Merge in main). Nur mit einem
   Test-Branch (``victus-fan update --branch NAME``, nur als Administrator) wird
   stattdessen jeder neue Commit eines Branches verfolgt.
2. Mit dem installierten Stand (installed.json) vergleichen - installiert wird
   nur eine neuere Version, nie eine ältere.
3. Den Quellcode als ZIP laden und dessen eigenen Installer unbeaufsichtigt
   ausführen (install.ps1 -Unattended)

Sicherheit: Weil der Updater als SYSTEM fremden Code ausführt, kommen Quelle
(``REPO``) und Test-Branch nie aus der config.toml - die dürfen normale
Benutzer bearbeiten. Das Repository steht fest im Code, der Test-Branch in
einer Datei im Programmordner (nur Administratoren).

Der Installer stoppt nur den Dienst, aktualisiert die Python-Umgebung und
startet den Dienst wieder. Das Tray-Icon bemerkt die neue installed.json und
startet sich selbst neu.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import __version__, paths

log = logging.getLogger(__name__)

REPO = "Exc3pt10n/hp-victus-fan-control"
API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"
USER_AGENT = f"victus-fan-updater/{__version__}"
TIMEOUT = 30


class UpdateError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UpdateError("GitHub-Abfragelimit erreicht - nächster Versuch später") from exc
        if exc.code == 404:
            raise UpdateError(f"Nicht gefunden: {url}") from exc
        raise UpdateError(f"GitHub antwortet mit Fehler {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Keine Verbindung zu GitHub ({exc})") from exc


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """'v2.3.0' / '2.3.0' -> (2, 3, 0); alles andere -> None."""
    if not text:
        return None
    parts = text.strip().lstrip("vV").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def latest_release(repo: str) -> dict:
    """Neueste freigegebene Version: letztes Release, sonst höchster vX.Y.Z-Tag."""
    tag, title, date = None, "", ""
    try:
        data = json.loads(_get(f"{API}/repos/{repo}/releases/latest"))
        if parse_version(data.get("tag_name")):
            tag, title, date = data["tag_name"], data.get("name") or "", data.get("published_at") or ""
    except UpdateError as exc:
        if "Nicht gefunden" not in str(exc):
            raise
    if tag is None:
        tags = [t["name"] for t in json.loads(_get(f"{API}/repos/{repo}/tags?per_page=100"))
                if parse_version(t.get("name"))]
        if not tags:
            raise UpdateError("Noch keine freigegebene Version (Tag vX.Y.Z) vorhanden")
        tag = max(tags, key=parse_version)
    commit = latest_commit(repo, tag)
    return {"tag": tag, "version": ".".join(map(str, parse_version(tag))), "sha": commit["sha"],
            "date": date or commit["date"], "message": title or f"Version {tag.lstrip('vV')}"}


def latest_commit(repo: str, branch: str) -> dict:
    data = json.loads(_get(f"{API}/repos/{repo}/commits/{urllib.parse.quote(branch, safe='')}"))
    return {
        "sha": data["sha"],
        "date": data.get("commit", {}).get("committer", {}).get("date", ""),
        "message": (data.get("commit", {}).get("message") or "").splitlines()[0][:120],
    }


def download_source(repo: str, sha: str, dest: Path) -> Path:
    """Lädt den Quellcode eines Commits und gibt das entpackte Wurzelverzeichnis zurück."""
    archive = dest / "source.zip"
    archive.write_bytes(_get(f"{CODELOAD}/{repo}/zip/{sha}", accept="application/zip"))
    with zipfile.ZipFile(archive) as zf:
        bad = [n for n in zf.namelist() if n.startswith(("/", "\\")) or ".." in Path(n).parts]
        if bad:
            raise UpdateError(f"Ungültiges Archiv ({bad[0]})")
        zf.extractall(dest / "src")
    roots = [p for p in (dest / "src").iterdir() if p.is_dir()]
    if len(roots) != 1 or not (roots[0] / "install" / "windows" / "install.ps1").exists():
        raise UpdateError("Archiv enthält keinen Windows-Installer")
    return roots[0]


# --------------------------------------------------------------------------
# Lokaler Zustand
# --------------------------------------------------------------------------
def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def installed() -> dict:
    return read_json(paths.INSTALL_INFO)


def test_branch() -> str:
    """Test-Branch aus dem Programmordner ("" = nur freigegebene Versionen)."""
    try:
        name = paths.UPDATE_BRANCH_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    # Nur übliche Branch-Namen (keine Pfade/URLs)
    if name and all(c.isalnum() or c in "-_./" for c in name) and ".." not in name:
        return name
    return ""


def set_test_branch(name: str) -> None:
    """Test-Branch setzen bzw. mit "" entfernen (Administratorrechte nötig)."""
    if name:
        paths.UPDATE_BRANCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.UPDATE_BRANCH_FILE.write_text(name + "\n", encoding="utf-8")
    else:
        paths.UPDATE_BRANCH_FILE.unlink(missing_ok=True)


def write_status(**fields) -> dict:
    status = read_json(paths.UPDATE_STATUS)
    status.update(fields, checked_at=time.strftime("%Y-%m-%d %H:%M"))
    try:
        paths.UPDATE_STATUS.parent.mkdir(parents=True, exist_ok=True)
        paths.UPDATE_STATUS.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("Update-Status nicht schreibbar: %s", exc)
    return status


# --------------------------------------------------------------------------
# Ablauf
# --------------------------------------------------------------------------
def run_installer(src: Path, sha: str, branch: str) -> None:
    script = src / "install" / "windows" / "install.ps1"
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(script), "-Unattended", "-SourceSha", sha, "-SourceBranch", branch]
    log.info("Starte Installer: %s", " ".join(cmd))
    # Ausgabe direkt in eine Datei statt über Pipes: Pipes können hängen, wenn ein
    # gestarteter Prozess sie geerbt hat und weiterläuft.
    paths.UPDATE_INSTALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with paths.UPDATE_INSTALL_LOG.open("w", encoding="utf-8", errors="replace") as out:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                              timeout=1800, creationflags=0x08000000)  # CREATE_NO_WINDOW
    log.info("Installer beendet mit Code %s (Ausgabe: %s)", proc.returncode, paths.UPDATE_INSTALL_LOG)
    if proc.returncode != 0:
        raise UpdateError(f"Installer fehlgeschlagen (Code {proc.returncode}) - siehe {paths.UPDATE_INSTALL_LOG.name}")


def check_and_update(cfg: dict, force: bool = False, check_only: bool | None = None,
                     branch: str | None = None) -> dict:
    """Prüft auf ein Update und installiert es ggf. Gibt den neuen Status zurück.

    ``cfg`` ist die [update]-Sektion (nur enabled/check_only werden genutzt).
    ``branch`` None = Test-Branch aus dem Programmordner."""
    if branch is None:
        branch = test_branch()
    if check_only is None:
        check_only = bool(cfg.get("check_only"))
    current = installed()
    previous = read_json(paths.UPDATE_STATUS)
    if previous.get("state") == "installing" and previous.get("latest_sha") \
            and previous.get("latest_sha") == current.get("sha"):
        # Ein früherer Lauf wurde nach erfolgreicher Installation unterbrochen.
        write_status(state="updated", installed_sha=current.get("sha"),
                     updated_at=current.get("installed_at", ""),
                     message=f"Aktualisiert: {previous.get('latest_message', '')}")
    try:
        repo = REPO
        if branch:
            # Sonderfall (nur zum Testen): jedem neuen Commit eines Branches folgen
            ref = branch
            latest = latest_commit(repo, ref)
            latest["version"] = ""
            newer = latest["sha"] != current.get("sha")
        else:
            latest = latest_release(repo)
            ref = latest["tag"]
            have = parse_version(current.get("version")) or parse_version(__version__)
            newer = latest["sha"] != current.get("sha") and (have is None or parse_version(ref) > have)
    except (UpdateError, KeyError, ValueError, TypeError) as exc:
        log.warning("Update-Prüfung fehlgeschlagen: %s", exc)
        return write_status(state="error", message=str(exc))

    available = force or newer
    base = dict(branch=ref, latest_version=latest["version"], latest_sha=latest["sha"],
                latest_date=latest["date"], latest_message=latest["message"],
                installed_sha=current.get("sha", ""))
    if not available:
        log.info("Aktuell (%s)", latest["version"] or latest["sha"][:7])
        return write_status(state="current", message="Aktuell", **base)
    if check_only:
        log.info("Update verfügbar: %s", latest["version"] or latest["sha"][:7])
        return write_status(state="available", message=f"Update verfügbar: {latest['message']}", **base)
    if sys.platform != "win32":
        return write_status(state="available", message="Automatische Installation nur unter Windows", **base)

    write_status(state="installing", message="Update wird installiert …", **base)
    tmp = Path(tempfile.mkdtemp(prefix="victusfan-update-"))
    try:
        src = download_source(repo, latest["sha"], tmp)
        run_installer(src, latest["sha"], ref)
    except (UpdateError, OSError, subprocess.SubprocessError) as exc:
        log.error("Update fehlgeschlagen: %s", exc)
        return write_status(state="error", message=f"Update fehlgeschlagen: {exc}", **base)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("Update installiert: %s", latest["version"] or latest["sha"][:7])
    base["installed_sha"] = latest["sha"]
    return write_status(state="updated", message=f"Aktualisiert: {latest['message']}",
                        updated_at=time.strftime("%Y-%m-%d %H:%M"), **base)


def setup_logging() -> None:
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    try:
        paths.UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            paths.UPDATE_LOG, maxBytes=500_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
