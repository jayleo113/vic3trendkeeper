@echo off
rem Repairs the history graphs of your newest save into a NEW save "<name>_tk.v3":
rem   expand capacities, trim engine back-fill, restore wiped history from older saves
rem   (searched in "save games" and G:\Victoria3Mods\history). The original save is not modified.
rem The result is written to G:\Victoria3Mods\restored and copied into "save games" so the game can load it.
cd /d "%~dp0"
python -I -B "%~dp0trend_restore.py" --auto --install
echo.
pause
