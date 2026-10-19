@echo off
setlocal
cd /d "%~dp0"
echo === Jiangjin pressure : fetch last 24 hours ===
if exist ".venv\Scripts\python.exe" goto use_venv
python pressure.py --hours 72 --view 24 --png
if errorlevel 1 goto failed
goto done

:use_venv
".venv\Scripts\python.exe" pressure.py --hours 72 --view 24 --png
if errorlevel 1 goto failed

:done
echo.
echo Done. Open: output\pressure.html
echo.
echo --- pushing water level archive ---
git add data/level
git diff --cached --quiet
if not errorlevel 1 goto nopush
git commit -q -m "data(level): local collect"
git push origin main
goto nopush2

:nopush
echo No new level data, nothing to push.

:nopush2
pause
exit /b 0

:failed
echo.
echo FAILED. See messages above.
pause
exit /b 1
