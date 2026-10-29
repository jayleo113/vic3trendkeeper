@echo off
rem Trend Keeper: remove the Startup shortcut and stop the running background helper.
cd /d "%~dp0"
python -I -B "%~dp0trend_keeper.py" uninstall-autostart
wmic process where "name='pythonw.exe' and commandline like '%%trend_keeper.py%%daemon%%'" call terminate >nul 2>nul
echo 已关闭开机自启，并停止了后台助手。
pause
