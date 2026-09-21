@echo off
REM Double-click this file to open the QuickBooks automation dashboard.
cd /d "%~dp0"
echo Starting the QuickBooks automation dashboard...
python -m qb_agent --config config.json serve --open
echo.
echo The dashboard has stopped.
pause
