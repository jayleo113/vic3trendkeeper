@echo off
rem Full History Charts: export every country's yearly history of your NEWEST save to CSV
rem (G:\Victoria3Mods\exports, UTF-8 with BOM - opens directly in Excel).
cd /d "%~dp0"
python -I -B "%~dp0export_csv.py"
echo.
pause
