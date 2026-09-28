@echo off
setlocal
title DiskSentry (from source)
cd /d "%~dp0"
echo   DiskSentry - running from source as a native desktop app window.
echo   If the window shell is unavailable it falls back to your browser.
echo   Press Ctrl+C here to stop.
echo.
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% -m pip install -r requirements.txt >nul 2>nul
%PY% run.py
pause
