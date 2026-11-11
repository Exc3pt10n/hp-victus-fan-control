@echo off
rem Doppelklick-Installer: startet install.ps1 (fragt nach Administratorrechten)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
