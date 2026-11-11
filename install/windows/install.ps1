#Requires -Version 5.1
<#
.SYNOPSIS
    Installiert die Victus-Lüftersteuerung als Hintergrunddienst (Windows 11).

.DESCRIPTION
    - prüft/installiert Python 3.11+
    - legt eine eigene Python-Umgebung unter "C:\Program Files\VictusFan" an
    - kopiert die Konfiguration nach C:\ProgramData\VictusFan
    - richtet eine geplante Aufgabe ein, die beim Systemstart unsichtbar als
      SYSTEM läuft und bei Absturz automatisch neu startet
    - macht den Befehl "victus-fan" in jeder Konsole verfügbar
#>
param(
    [switch]$SkipDiagnose
)

$ErrorActionPreference = "Stop"
$TaskName   = "VictusFanControl"
$InstallDir = Join-Path $env:ProgramFiles "VictusFan"
$DataDir    = Join-Path $env:ProgramData "VictusFan"
$RepoDir    = Resolve-Path (Join-Path $PSScriptRoot "..\..")

# --- Als Administrator neu starten ---------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Starte mit Administratorrechten neu ..."
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($SkipDiagnose) { $argList += "-SkipDiagnose" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit
}

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

# --- Python finden -------------------------------------------------------
function Find-Python {
    $candidates = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($v in "3.13", "3.12", "3.11") {
            try {
                $exe = & py.exe "-$v" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += $exe.Trim() }
            } catch {}
        }
    }
    foreach ($name in "python.exe", "python3.exe") {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $candidates += $cmd.Source }
    }
    foreach ($exe in $candidates) {
        & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $exe }
    }
    return $null
}

Step "Suche Python 3.11 oder neuer"
$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Python 3.11+ nicht gefunden und winget fehlt. Bitte Python von https://www.python.org installieren (Haken bei 'Add to PATH' setzen) und erneut starten."
    }
    Write-Host "Python nicht gefunden - installiere Python 3.12 systemweit über winget ..."
    winget install --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $python = Find-Python
    if (-not $python) { throw "Python-Installation fehlgeschlagen. Bitte manuell installieren." }
}
Write-Host "Verwende $python"

# --- Laufenden Dienst stoppen (bei Update) -------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Step "Stoppe vorhandene Installation"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*victus_fan*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# --- Virtuelle Umgebung + Paket ------------------------------------------
Step "Installiere nach $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$venv = Join-Path $InstallDir "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv konnte nicht angelegt werden" }
}
$venvPython  = Join-Path $venv "Scripts\python.exe"
$venvPythonw = Join-Path $venv "Scripts\pythonw.exe"
& $venvPython -m pip install --upgrade --quiet pip
& $venvPython -m pip install --upgrade --quiet "$RepoDir"
if ($LASTEXITCODE -ne 0) { throw "pip install fehlgeschlagen" }
& $venvPython -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "tkinter fehlt in dieser Python-Installation - das Fenster der Oberfläche wird nicht funktionieren. Python von python.org mit 'tcl/tk and IDLE' installieren."
}

# --- Konfiguration --------------------------------------------------------
Step "Konfiguration in $DataDir"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$configFile = Join-Path $DataDir "config.toml"
if (-not (Test-Path $configFile)) {
    Copy-Item (Join-Path $RepoDir "config.example.toml") $configFile
    Write-Host "config.toml angelegt"
} else {
    Write-Host "Vorhandene config.toml bleibt erhalten"
}
# Normale Benutzer dürfen das Profil umschalten (override.json) und den Status lesen.
$override = Join-Path $DataDir "override.json"
if (-not (Test-Path $override)) { Set-Content -Path $override -Value '{"profile": "auto"}' -Encoding UTF8 }
icacls $override /grant "*S-1-5-32-545:(M)" | Out-Null   # VORDEFINIERT\Benutzer
# Die Oberfläche öffnet die Konfiguration im Editor - Speichern ohne Adminrechte erlauben.
# Der Dienst übernimmt Änderungen automatisch (kein Neustart nötig).
icacls $configFile /grant "*S-1-5-32-545:(M)" | Out-Null

# --- Befehl "victus-fan" bereitstellen -----------------------------------
$binDir = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
Set-Content -Path (Join-Path $binDir "victus-fan.cmd") -Encoding ASCII -Value "@`"$venvPython`" -m victus_fan %*"
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
if (($machinePath -split ";") -notcontains $binDir) {
    [Environment]::SetEnvironmentVariable("Path", "$machinePath;$binDir", "Machine")
    Write-Host "$binDir zum PATH hinzugefügt (neue Konsole öffnen)"
}

# --- Tray-Icon: Autostart bei Anmeldung + Startmenü ----------------------
Step "Richte Tray-Icon und Startmenü-Eintrag ein"
$iconFile = Join-Path $InstallDir "victus-fan.ico"
& $venvPython -c "import sys; from victus_fan.gui import make_fan_image; make_fan_image(256).save(sys.argv[1], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(256,256)])" $iconFile
$shell = New-Object -ComObject WScript.Shell
$startupLnk = Join-Path ([Environment]::GetFolderPath("CommonStartup")) "Victus Lüftersteuerung.lnk"
$menuLnk    = Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "Victus Lüftersteuerung.lnk"
foreach ($item in @(@($startupLnk, "-m victus_fan gui --hidden"), @($menuLnk, "-m victus_fan gui"))) {
    $lnk = $shell.CreateShortcut($item[0])
    $lnk.TargetPath = $venvPythonw
    $lnk.Arguments = $item[1]
    $lnk.WorkingDirectory = $InstallDir
    $lnk.IconLocation = $iconFile
    $lnk.Description = "Victus Lüftersteuerung"
    $lnk.Save()
}

# --- Hardware prüfen -----------------------------------------------------
if (-not $SkipDiagnose) {
    Step "Prüfe Hardware-Unterstützung"
    & $venvPython -m victus_fan diagnose
    if ($LASTEXITCODE -ne 0) {
        throw "Diagnose fehlgeschlagen - Dienst wird nicht eingerichtet. Details siehe oben."
    }
}

# --- Geplante Aufgabe -----------------------------------------------------
Step "Richte Hintergrunddienst '$TaskName' ein"
$action   = New-ScheduledTaskAction -Execute $venvPythonw -Argument "-m victus_fan run" -WorkingDirectory $InstallDir
$triggers = @(New-ScheduledTaskTrigger -AtStartup)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Principal $taskPrincipal -Settings $settings -Force `
    -Description "Intelligente Lüftersteuerung für HP Victus" | Out-Null
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 5
# Tray-Icon im Kontext des angemeldeten Benutzers (nicht als Admin) starten
Start-Process explorer.exe -ArgumentList "`"$menuLnk`""
Step "Fertig"
& $venvPython -m victus_fan status
Write-Host ""
Write-Host "Die Lüftersteuerung läuft jetzt im Hintergrund und startet automatisch mit Windows."
Write-Host "Das Tray-Icon (unten rechts, ggf. unter '^') zeigt die höchste Temperatur;"
Write-Host "Klick darauf öffnet das Fenster, Rechtsklick das Menü."
Write-Host "Befehle (neue Konsole):  victus-fan status | victus-fan profile quiet|balanced|performance|auto"
Write-Host "Konfiguration:           $configFile"
Write-Host "Log:                     $(Join-Path $DataDir 'victus-fan.log')"
Write-Host ""
Read-Host "Enter zum Schließen"
