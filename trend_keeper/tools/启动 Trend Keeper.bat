@echo off
rem Starts the Trend Keeper window (no console). It waits for victoria3.exe and attaches automatically.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw -I -B "%~dp0trend_keeper.py" gui & exit /b)
start "" python -I -B "%~dp0trend_keeper.py" gui
