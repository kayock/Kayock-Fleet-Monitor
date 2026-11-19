@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo   Kayock Fleet Monitor - first-time setup (Windows)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo.
    echo Install Python 3.9 or newer from https://www.python.org/downloads/
    echo and make sure to tick "Add python.exe to PATH" during installation.
    echo Then run this script again.
    echo.
    pause
    exit /b 1
)

python --version

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        echo Make sure you can write to this folder and try again.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    echo Check your Internet connection, then run this script again.
    pause
    exit /b 1
)

echo.
echo Setup complete. Start the monitor with start_monitor.bat
echo.
pause
exit /b 0
