#Requires -Version 5.1
<#
.SYNOPSIS
    Entfernt die Victus-Lüftersteuerung vollständig und gibt die Lüfter an
    das BIOS zurück. Aufrufbar über Einstellungen > Apps > Installierte Apps,
    über install\windows\uninstall.cmd oder direkt.

.PARAMETER KeepConfig
    C:\ProgramData\VictusFan (Konfiguration und Logs) behalten.

.PARAMETER Quiet
    Ohne Rückfrage am Ende (für die stille Deinstallation).
#>
param([switch]$KeepConfig, [switch]$Quiet)

$ErrorActionPreference = "Continue"
$TaskName   = "VictusFanControl"
$InstallDir = Join-Path $env:ProgramFiles "VictusFan"
$DataDir    = Join-Path $env:ProgramData "VictusFan"
$AppKey     = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VictusFanControl"

function Restart-Self([string]$Path, [switch]$Elevate) {
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Path`"")
    if ($KeepConfig) { $argList += "-KeepConfig" }
    if ($Quiet) { $argList += "-Quiet" }
    if ($Elevate) {
        Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    } else {
        Start-Process powershell.exe -ArgumentList $argList -Wait
    }
}

# --- Als Administrator neu starten ---------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Restart-Self $PSCommandPath -Elevate
    exit
}

# --- Liegt das Skript im Installationsordner, von einer Kopie aus laufen ---
# (der Ordner wird gleich gelöscht)
if ($PSCommandPath -and $PSCommandPath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase)) {
    $copy = Join-Path $env:TEMP "victusfan-uninstall.ps1"
    Copy-Item $PSCommandPath $copy -Force
    Restart-Self $copy
    exit
}

Write-Host "Victus-Lüftersteuerung wird entfernt ..." -ForegroundColor Cyan

Write-Host "Stoppe Lüftersteuerung, Tray-Icon und Updater ..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*victus_fan*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "VictusFanUpdate" -Confirm:$false -ErrorAction SilentlyContinue

$venvPython = Join-Path $InstallDir "venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Host "Gebe Lüfter an das BIOS zurück ..."
    & $venvPython -m victus_fan restore
}

Write-Host "Entferne Verknüpfungen, Autostart und App-Eintrag ..."
foreach ($lnk in @(
    (Join-Path ([Environment]::GetFolderPath("CommonStartup")) "Victus Lüftersteuerung.lnk"),
    (Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "Victus Lüftersteuerung.lnk"))) {
    Remove-Item -Force $lnk -ErrorAction SilentlyContinue
}
foreach ($key in "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
                 "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run") {
    Remove-ItemProperty -Path $key -Name "Victus Lüftersteuerung" -ErrorAction SilentlyContinue
}
Remove-Item -Path $AppKey -Recurse -Force -ErrorAction SilentlyContinue

$binDir = Join-Path $InstallDir "bin"
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$newPath = (($machinePath -split ";") | Where-Object { $_ -and $_ -ne $binDir }) -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")

Write-Host "Lösche Programmdateien ..."
Start-Sleep -Seconds 1   # beendete Prozesse geben ihre Dateien frei
Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
if (-not $KeepConfig) {
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA "VictusFan") -ErrorAction SilentlyContinue
}

if (Test-Path $InstallDir) {
    Write-Warning "Einige Dateien in $InstallDir konnten nicht gelöscht werden - bitte nach einem Neustart von Hand löschen."
} else {
    Write-Host "Victus-Lüftersteuerung wurde entfernt." -ForegroundColor Green
}
if (-not $Quiet) { Read-Host "Enter zum Schließen" }
