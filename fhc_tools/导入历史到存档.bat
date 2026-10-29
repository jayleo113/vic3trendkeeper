@echo off
rem Full History Charts: fill the mod's chart data of your NEWEST save with the history the game kept
rem (GDP, prestige, literacy, SoL, population, radicals/loyalists since the start of the campaign).
rem Writes a NEW save "<name>_fhc.v3" to G:\Victoria3Mods\restored and copies it into "save games".
cd /d "%~dp0"
python -I -B "%~dp0import_history.py" --install
echo.
pause
