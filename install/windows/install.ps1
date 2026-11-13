#Requires -Version 5.1
<#
.SYNOPSIS
    Installiert oder aktualisiert die Victus-Lüftersteuerung (Windows 11).

.DESCRIPTION
    - prüft/installiert Python 3.11+
    - legt eine eigene Python-Umgebung unter "C:\Program Files\VictusFan" an
    - kopiert die Konfiguration nach C:\ProgramData\VictusFan (bestehende bleibt)
    - geplante Aufgabe "VictusFanControl": Lüftersteuerung, startet mit Windows,
      läuft unsichtbar als SYSTEM, startet sich bei Absturz neu
    - geplante Aufgabe "VictusFanUpdate": sucht beim Start und alle 6 Stunden
      nach Updates und installiert sie
    - Tray-Icon im Autostart und im Startmenü, Befehl "victus-fan" im PATH

.PARAMETER Unattended
    Für den Auto-Updater: keine Rückfragen, Tray-Icon und Updater laufen
    weiter, der Dienst wird auf jeden Fall wieder gestartet.

.PARAMETER NoWait
    Am Ende nicht auf Enter warten und das Tray-Icon nicht starten (z. B. für
    automatische Tests).
#>
param(
    [switch]$SkipDiagnose,
    [switch]$Unattended,
    [switch]$NoWait,
    [string]$SourceSha = "",
    [string]$SourceBranch = ""
)

$ErrorActionPreference = "Stop"
$TaskName   = "VictusFanControl"
$UpdateTask = "VictusFanUpdate"
$InstallDir = Join-Path $env:ProgramFiles "VictusFan"
$DataDir    = Join-Path $env:ProgramData "VictusFan"
$RepoDir    = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$UsersSid   = "*S-1-5-32-545"   # VORDEFINIERT\Benutzer (sprachunabhängig)

# --- Als Administrator neu starten ---------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($Unattended) { throw "Administratorrechte erforderlich" }
    Write-Host "Starte mit Administratorrechten neu ..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($SkipDiagnose) { $argList += "-SkipDiagnose" }
    if ($NoWait) { $argList += "-NoWait" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit
}

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

# Python gibt UTF-8 aus - die Konsole soll es auch so lesen (sonst kaputte Umlaute).
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# Externe Programme aufrufen, ohne dass Ausgaben auf stderr (z. B. pip-Warnungen)
# unter Windows PowerShell 5.1 als Fehler gewertet werden.
# Invoke-Native: Ausgabe -> Konsole, Rückgabe = nur der Exit-Code.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) {
            & $Exe @Arguments 2>&1 | Out-Null
        } else {
            & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
        }
        return [int]$LASTEXITCODE
    } catch {
        return 9009
    } finally {
        $ErrorActionPreference = $old
    }
}

# Get-NativeOutput: letzte Ausgabezeile oder $null bei Fehler.
function Get-NativeOutput {
    param([string]$Exe, [string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Arguments 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        return "$($out | Select-Object -Last 1)".Trim()
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $old
    }
}

function Stop-VictusProcesses([string]$Pattern) {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like $Pattern } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# --- Python finden -------------------------------------------------------
function Test-Python([string]$Exe) {
    if (-not $Exe -or -not (Test-Path $Exe)) { return $false }
    (Invoke-Native $Exe @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)") -Quiet) -eq 0
}

function Find-Python {
    $candidates = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($v in "3.13", "3.12", "3.11") {
            $out = Get-NativeOutput "py.exe" @("-$v", "-c", "import sys; print(sys.executable)")
            if ($out) { $candidates += $out }
        }
    }
    foreach ($name in "python.exe", "python3.exe") {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $candidates += $cmd.Source }
    }
    foreach ($exe in $candidates) { if (Test-Python $exe) { return $exe } }
    return $null
}

$venv        = Join-Path $InstallDir "venv"
$venvPython  = Join-Path $venv "Scripts\python.exe"
$venvPythonw = Join-Path $venv "Scripts\pythonw.exe"

if (-not (Test-Python $venvPython)) {
    if ($Unattended) { throw "Python-Umgebung fehlt oder ist defekt - bitte install.cmd manuell ausführen" }
    Step "Suche Python 3.11 oder neuer"
    $python = Find-Python
    if (-not $python) {
        if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            throw "Python 3.11+ nicht gefunden und winget fehlt. Bitte Python von https://www.python.org installieren (Haken bei 'Add to PATH' setzen) und erneut starten."
        }
        Write-Host "Python nicht gefunden - installiere Python 3.12 systemweit über winget ..."
        $null = Invoke-Native "winget.exe" @("install", "--id", "Python.Python.3.12", "--scope", "machine", "--silent",
            "--accept-package-agreements", "--accept-source-agreements")
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $python = Find-Python
        if (-not $python) { throw "Python-Installation fehlgeschlagen. Bitte manuell installieren." }
    }
    Write-Host "Verwende $python"
}

# --- Laufenden Dienst stoppen --------------------------------------------
Step "Stoppe laufende Lüftersteuerung"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
if ($Unattended) {
    # Nur den Dienst - Tray-Icon und Updater laufen weiter.
    Stop-VictusProcesses "*victus_fan run*"
} else {
    Stop-VictusProcesses "*victus_fan run*"
    Stop-VictusProcesses "*victus_fan gui*"
}

$serviceRegistered = $false
try {
    # --- Virtuelle Umgebung + Paket --------------------------------------
    Step "Installiere nach $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if (-not (Test-Python $venvPython)) {
        if ((Invoke-Native $python @("-m", "venv", $venv)) -ne 0) { throw "venv konnte nicht angelegt werden" }
    }
    $pip = @("-m", "pip", "--disable-pip-version-check")
    if (-not $Unattended) {
        $null = Invoke-Native $venvPython ($pip + @("install", "--upgrade", "--quiet", "pip"))
    }

    # Zweistufig, damit ein abgebrochener Download (Netz weg, Akku leer) die
    # laufende Installation nicht beschädigt:
    # 1. Alle Pakete in einen eigenen Ordner laden bzw. bauen - nur hier wird das
    #    Netz gebraucht, die Python-Umgebung bleibt unberührt.
    # 2. Lokal aus diesem Ordner installieren und prüfen. Schlägt das fehl, wird
    #    die vorherige Version aus ihrem aufgehobenen Paketordner wiederhergestellt.
    $wheelDir = Join-Path $InstallDir "wheels"
    $stage    = Join-Path $InstallDir "wheels.new"
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    if ((Invoke-Native $venvPython ($pip + @("wheel", "--quiet", "--wheel-dir", $stage, "$RepoDir"))) -ne 0) {
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
        throw "Pakete konnten nicht geladen werden - die bisherige Version bleibt unverändert"
    }
    $newWheel = Get-ChildItem $stage -Filter "victus_fan-*.whl" | Select-Object -First 1
    if (-not $newWheel) { throw "Paket der Lüftersteuerung wurde nicht gebaut" }
    $local = $pip + @("install", "--quiet", "--no-index", "--find-links")
    $ok = (Invoke-Native $venvPython ($local + @($stage, "--upgrade", $newWheel.FullName))) -eq 0
    if ($ok) {
        # Gleiche Versionsnummer (Test-Branch): trotzdem den neuen Stand einspielen
        $ok = (Invoke-Native $venvPython ($local + @($stage, "--no-deps", "--force-reinstall", $newWheel.FullName))) -eq 0
    }
    if ($ok) {
        $ok = (Invoke-Native $venvPython @("-c", "import victus_fan.cli, victus_fan.service, victus_fan.updater") -Quiet) -eq 0
    }
    if (-not $ok) {
        $oldWheel = $null
        if (Test-Path $wheelDir) { $oldWheel = Get-ChildItem $wheelDir -Filter "victus_fan-*.whl" | Select-Object -First 1 }
        if ($oldWheel) {
            Write-Warning "Installation fehlgeschlagen - stelle die vorherige Version wieder her"
            $null = Invoke-Native $venvPython ($local + @($wheelDir, "--no-deps", "--force-reinstall", $oldWheel.FullName))
            $null = Invoke-Native $venvPython ($local + @($wheelDir, $oldWheel.FullName))
        }
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
        throw "pip install fehlgeschlagen"
    }
    # Pakete dieser Version für eine spätere Wiederherstellung aufheben
    Remove-Item -Recurse -Force $wheelDir -ErrorAction SilentlyContinue
    Move-Item $stage $wheelDir
    if ((Invoke-Native $venvPython @("-c", "import tkinter") -Quiet) -ne 0) {
        Write-Warning "tkinter fehlt - das Fenster der Oberfläche wird nicht funktionieren. Python von python.org mit 'tcl/tk and IDLE' installieren."
    }

    # --- Konfiguration ----------------------------------------------------
    Step "Konfiguration in $DataDir"
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    # Ordnerrechte ausdrücklich setzen (statt von ProgramData zu erben, wo Benutzer
    # eigene Dateien anlegen dürfen): SYSTEM und Administratoren voll, Benutzer
    # nur lesen. Schreiben dürfen sie nur override.json und config.toml (unten).
    if ((Invoke-Native "icacls.exe" @($DataDir, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F", "${UsersSid}:(OI)(CI)RX") -Quiet) -ne 0) {
        Write-Warning "Ordnerrechte für $DataDir konnten nicht gesetzt werden"
    }
    $configFile = Join-Path $DataDir "config.toml"
    if (-not (Test-Path $configFile)) {
        Copy-Item (Join-Path $RepoDir "config.example.toml") $configFile
        Write-Host "config.toml angelegt"
    } else {
        # Ältere Dateien auf den aktuellen Aufbau bringen: eigene Werte bleiben, veraltete
        # Einträge fallen weg, die alte Datei wird als config.toml.bak gesichert.
        $template = Join-Path $RepoDir "config.example.toml"
        if ((Invoke-Native $venvPython @("-m", "victus_fan", "--config", $configFile, "migrate-config",
                "--template", $template)) -ne 0) {
            Write-Warning "Konfiguration konnte nicht aktualisiert werden - die bisherige bleibt unverändert"
        }
    }
    # Normale Benutzer dürfen Profil/Modus umschalten und die Konfiguration bearbeiten.
    $override = Join-Path $DataDir "override.json"
    if (-not (Test-Path $override)) { Set-Content -Path $override -Value '{"profile": "auto", "mode": "normal"}' -Encoding UTF8 }
    $null = Invoke-Native "icacls.exe" @($override, "/grant", "${UsersSid}:(M)") -Quiet
    $null = Invoke-Native "icacls.exe" @($configFile, "/grant", "${UsersSid}:(M)") -Quiet

    # --- Befehl "victus-fan" bereitstellen -------------------------------
    $binDir = Join-Path $InstallDir "bin"
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    Set-Content -Path (Join-Path $binDir "victus-fan.cmd") -Encoding ASCII -Value "@`"$venvPython`" -m victus_fan %*"
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if (($machinePath -split ";") -notcontains $binDir) {
        [Environment]::SetEnvironmentVariable("Path", "$machinePath;$binDir", "Machine")
        Write-Host "$binDir zum PATH hinzugefügt (neue Konsole öffnen)"
    }

    # --- Tray-Icon: Startmenü ----------------------------------------------
    # Den Autostart bei der Anmeldung richtet das Tray-Icon selbst pro Benutzer ein
    # (HKCU\...\Run, abschaltbar unter Extras > "Mit Windows starten").
    Step "Richte Startmenü-Eintrag ein"
    $iconFile = Join-Path $InstallDir "victus-fan.ico"
    $null = Invoke-Native $venvPython @("-c", "import sys; from victus_fan.ui.icons import make_fan_image; make_fan_image(256).save(sys.argv[1], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(256,256)])", $iconFile) -Quiet
    $shell = New-Object -ComObject WScript.Shell
    $menuLnk    = Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "Victus Lüftersteuerung.lnk"
    # Frühere Versionen legten eine Verknüpfung im Autostart aller Benutzer an.
    $oldStartupLnk = Join-Path ([Environment]::GetFolderPath("CommonStartup")) "Victus Lüftersteuerung.lnk"
    Remove-Item -Force $oldStartupLnk -ErrorAction SilentlyContinue
    foreach ($item in @(,@($menuLnk, "-m victus_fan gui"))) {
        $lnk = $shell.CreateShortcut($item[0])
        $lnk.TargetPath = $venvPythonw
        $lnk.Arguments = $item[1]
        $lnk.WorkingDirectory = $InstallDir
        $lnk.IconLocation = $iconFile
        $lnk.Description = "Victus Lüftersteuerung"
        $lnk.Save()
    }

    # --- Hardware prüfen (nur bei manueller Installation) ----------------
    if (-not $SkipDiagnose -and -not $Unattended) {
        Step "Prüfe Hardware-Unterstützung"
        if ((Invoke-Native $venvPython @("-m", "victus_fan", "diagnose")) -ne 0) {
            throw "Diagnose fehlgeschlagen - Dienst wird nicht eingerichtet. Details siehe oben."
        }
    }

    # --- Geplante Aufgabe: Lüftersteuerung -------------------------------
    Step "Richte Hintergrunddienst '$TaskName' ein"
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $action   = New-ScheduledTaskAction -Execute $venvPythonw -Argument "-m victus_fan run" -WorkingDirectory $InstallDir
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
        -Principal $taskPrincipal -Settings $settings -Force `
        -Description "Intelligente Lüftersteuerung für HP Victus" | Out-Null
    $serviceRegistered = $true

    # --- Geplante Aufgabe: Auto-Update -----------------------------------
    # Nicht neu registrieren, während der Updater selbst läuft (-Unattended):
    # das könnte die laufende Update-Aufgabe beenden.
    if ($Unattended -and (Get-ScheduledTask -TaskName $UpdateTask -ErrorAction SilentlyContinue)) {
        Write-Host "Auto-Update-Aufgabe vorhanden - bleibt unverändert"
    } else {
        Step "Richte Auto-Update '$UpdateTask' ein"
        $updAction  = New-ScheduledTaskAction -Execute $venvPythonw -Argument "-m victus_fan update --auto" -WorkingDirectory $InstallDir
        $atStartup  = New-ScheduledTaskTrigger -AtStartup
        $atStartup.Delay = "PT10M"
        $repeat     = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(6) -RepetitionInterval (New-TimeSpan -Hours 6)
        $updSettings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RunOnlyIfNetworkAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $UpdateTask -Action $updAction -Trigger @($atStartup, $repeat) `
            -Principal $taskPrincipal -Settings $updSettings -Force `
            -Description "Sucht nach Updates der Victus-Lüftersteuerung und installiert sie" | Out-Null
    }

    # --- Installierten Stand merken (Updater + Tray-Neustart) ------------
    $version = Get-NativeOutput $venvPython @("-c", "import victus_fan; print(victus_fan.__version__)")
    [ordered]@{
        version      = "$version"
        sha          = $SourceSha
        branch       = $SourceBranch
        installed_at = (Get-Date -Format "yyyy-MM-dd HH:mm")
        source       = $(if ($Unattended) { "auto-update" } else { "manuell" })
    } | ConvertTo-Json | Set-Content -Path (Join-Path $InstallDir "installed.json") -Encoding UTF8

    # --- Eintrag in "Installierte Apps" (Einstellungen > Apps) ------------
    Step "Trage die Lüftersteuerung in die Windows-App-Liste ein"
    $uninstallScript = Join-Path $InstallDir "uninstall.ps1"
    Copy-Item (Join-Path $PSScriptRoot "uninstall.ps1") $uninstallScript -Force
    $uninstallCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallScript`""
    $sizeKb = [int]((Get-ChildItem $InstallDir -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum / 1KB)
    $appKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VictusFanControl"
    New-Item -Path $appKey -Force | Out-Null
    $values = [ordered]@{
        DisplayName          = "Victus Lüftersteuerung"
        DisplayVersion       = "$version"
        Publisher            = "hp-victus-fan-control (GitHub)"
        DisplayIcon          = $iconFile
        InstallLocation      = $InstallDir
        InstallDate          = (Get-Date -Format "yyyyMMdd")
        URLInfoAbout         = "https://github.com/Exc3pt10n/hp-victus-fan-control"
        UninstallString      = $uninstallCmd
        QuietUninstallString = "$uninstallCmd -Quiet"
    }
    foreach ($name in $values.Keys) {
        New-ItemProperty -Path $appKey -Name $name -Value $values[$name] -PropertyType String -Force | Out-Null
    }
    foreach ($name in "NoModify", "NoRepair") {
        New-ItemProperty -Path $appKey -Name $name -Value 1 -PropertyType DWord -Force | Out-Null
    }
    New-ItemProperty -Path $appKey -Name "EstimatedSize" -Value $sizeKb -PropertyType DWord -Force | Out-Null
}
finally {
    # Die Lüftersteuerung immer wieder starten - auch wenn ein Update scheitert.
    if ($serviceRegistered -or (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
}

if ($Unattended) {
    Write-Host "Update installiert."
    exit 0
}
if ($NoWait) {
    Write-Host "Installation abgeschlossen."
    exit 0
}

Start-Sleep -Seconds 5
# Tray-Icon im Kontext des angemeldeten Benutzers (nicht als Admin) starten
Start-Process explorer.exe -ArgumentList "`"$menuLnk`""
Step "Fertig"
$null = Invoke-Native $venvPython @("-m", "victus_fan", "status")
Write-Host ""
Write-Host "Die Lüftersteuerung läuft jetzt im Hintergrund und startet automatisch mit Windows."
Write-Host "Das Tray-Icon (unten rechts, ggf. unter '^') zeigt die höchste Temperatur;"
Write-Host "Klick darauf öffnet das Fenster, Rechtsklick das Menü."
Write-Host "Updates werden automatisch installiert (beim Start und alle 6 Stunden)."
Write-Host "Befehle (neue Konsole):  victus-fan status | victus-fan profile quiet|balanced|performance|auto"
Write-Host "Konfiguration:           $configFile"
Write-Host "Log:                     $(Join-Path $DataDir 'victus-fan.log')"
Write-Host ""
Read-Host "Enter zum Schließen"
