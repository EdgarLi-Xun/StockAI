@echo off
cd /d "%~dp0"
if not exist ".venv\stockai-web-installed" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" -WebOnly
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0run.py"
