#Requires -Version 5.1
<#
.SYNOPSIS
    Entfernt die Victus-Lüftersteuerung. Mit -KeepConfig bleibt
    C:\ProgramData\VictusFan (Konfiguration + Log) erhalten.
#>
param([switch]$KeepConfig)

$ErrorActionPreference = "Continue"
$TaskName   = "VictusFanControl"
$InstallDir = Join-Path $env:ProgramFiles "VictusFan"
$DataDir    = Join-Path $env:ProgramData "VictusFan"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($KeepConfig) { $argList += "-KeepConfig" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    exit
}

Write-Host "Stoppe und entferne geplante Aufgabe ..."
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

foreach ($lnk in @(
    (Join-Path ([Environment]::GetFolderPath("CommonStartup")) "Victus Lüftersteuerung.lnk"),
    (Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "Victus Lüftersteuerung.lnk"))) {
    Remove-Item -Force $lnk -ErrorAction SilentlyContinue
}

# Autostart-Eintrag des Tray-Icons (pro Benutzer)
foreach ($key in "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run",
                 "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run") {
    Remove-ItemProperty -Path $key -Name "Victus Lüftersteuerung" -ErrorAction SilentlyContinue
}

$binDir = Join-Path $InstallDir "bin"
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$newPath = (($machinePath -split ";") | Where-Object { $_ -and $_ -ne $binDir }) -join ";"
[Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")

Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
if (-not $KeepConfig) { Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue }

Write-Host "Victus-Lüftersteuerung wurde entfernt."
Read-Host "Enter zum Schließen"
