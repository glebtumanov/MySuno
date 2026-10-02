@echo off
rem MySuno launcher: starts the local server and opens the browser.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "vendor\ACE-Step-1.5\.venv\Scripts\python.exe" (
    echo Environment is not installed yet. Run setup.ps1 first:
    echo   powershell -ExecutionPolicy Bypass -File setup.ps1
    pause
    exit /b 1
)
"vendor\ACE-Step-1.5\.venv\Scripts\python.exe" -m mysuno
if errorlevel 1 pause
