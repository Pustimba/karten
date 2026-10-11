@echo off
rem Doppelklick startet die Zugbindung-App im Browser.
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  echo uv ^(Python-Paketmanager^) fehlt und wird jetzt einmalig installiert ...
  powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
echo Starte Zugbindung ... ^(beim ersten Start werden Pakete installiert^)
uv run --quiet zugbindung app
if errorlevel 1 pause
