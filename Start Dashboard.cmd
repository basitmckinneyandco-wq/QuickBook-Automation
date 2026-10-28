@echo off
REM Double-click this file to open the QuickBooks automation dashboard.
cd /d "%~dp0"
echo Starting the QuickBooks automation dashboard...
if exist "Update Dashboard.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "Update Dashboard.ps1"
)
python -m qb_agent --config config.json serve --open
echo.
echo The dashboard has stopped.
pause
