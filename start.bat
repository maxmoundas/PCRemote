@echo off
setlocal
cd /d "%~dp0"
title PC Remote

where python >nul 2>nul || (
  echo Python 3.10+ is required. Install it from https://www.python.org/downloads/ ^(tick "Add to PATH"^).
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up...
  python -m venv .venv || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (pause & exit /b 1)
)

".venv\Scripts\python.exe" -m pcremote %*
pause
