@echo off
setlocal
cd /d "%~dp0"
echo === Jiangjin pressure : watch mode, one sample per 20 min ===
echo Press Ctrl+C in this window to stop.
if exist ".venv\Scripts\python.exe" goto use_venv
python pressure.py --watch --png
goto done

:use_venv
".venv\Scripts\python.exe" pressure.py --watch --png

:done
echo.
echo Watch stopped.
pause
exit /b 0
