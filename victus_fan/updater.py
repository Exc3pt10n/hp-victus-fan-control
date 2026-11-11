"""Auto-Update über GitHub.

Ablauf (läuft als geplante Aufgabe "VictusFanUpdate" mit SYSTEM-Rechten):

1. Neuesten Commit des Branches über die GitHub-API abfragen
2. Mit dem installierten Stand (installed.json) vergleichen
3. Bei Abweichung den Quellcode als ZIP laden und dessen eigenen Installer
   unbeaufsichtigt ausführen (install.ps1 -Unattended)

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


def resolve_branch(repo: str, branch: str) -> str:
    if branch:
        return branch
    info = json.loads(_get(f"{API}/repos/{repo}"))
    return info["default_branch"]


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


def check_and_update(cfg: dict, force: bool = False, check_only: bool | None = None) -> dict:
    """Prüft auf ein Update und installiert es ggf. Gibt den neuen Status zurück."""
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
        repo = cfg["repo"]
        branch = resolve_branch(repo, cfg.get("branch", ""))
        latest = latest_commit(repo, branch)
    except (UpdateError, KeyError, ValueError) as exc:
        log.warning("Update-Prüfung fehlgeschlagen: %s", exc)
        return write_status(state="error", message=str(exc))

    available = force or latest["sha"] != current.get("sha")
    base = dict(branch=branch, latest_sha=latest["sha"], latest_date=latest["date"],
                latest_message=latest["message"], installed_sha=current.get("sha", ""))
    if not available:
        log.info("Aktuell (%s)", latest["sha"][:7])
        return write_status(state="current", message="Aktuell", **base)
    if check_only:
        log.info("Update verfügbar: %s", latest["sha"][:7])
        return write_status(state="available", message=f"Update verfügbar: {latest['message']}", **base)
    if sys.platform != "win32":
        return write_status(state="available", message="Automatische Installation nur unter Windows", **base)

    write_status(state="installing", message="Update wird installiert …", **base)
    tmp = Path(tempfile.mkdtemp(prefix="victusfan-update-"))
    try:
        src = download_source(repo, latest["sha"], tmp)
        run_installer(src, latest["sha"], branch)
    except (UpdateError, OSError, subprocess.SubprocessError) as exc:
        log.error("Update fehlgeschlagen: %s", exc)
        return write_status(state="error", message=f"Update fehlgeschlagen: {exc}", **base)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("Update installiert: %s", latest["sha"][:7])
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
