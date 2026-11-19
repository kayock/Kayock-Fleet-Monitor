@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo.
    echo Run setup_windows.bat first, then start_monitor.bat again.
    echo.
    pause
    exit /b 1
)

echo Starting Kayock Fleet Monitor (Windows) ...
echo LAN access for the other machines: edit monitor_config.json
echo (set "host": "0.0.0.0") or run with --host 0.0.0.0
echo (default URL http://127.0.0.1:5000 - the console below prints the exact URL if the port was busy)
echo (press Ctrl+C in this window to stop it)
echo.
".venv\Scripts\python.exe" run_monitor.py
if errorlevel 1 (
    echo.
    echo [ERROR] The monitor exited unexpectedly. See the messages above.
    echo Common fixes: make sure no other app occupies port 5000,
    echo or run: .venv\Scripts\python.exe run_monitor.py --port 5050
    echo.
    pause
)
