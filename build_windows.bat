@echo off
setlocal enableextensions
title DiskSentry - Windows EXE Builder
cd /d "%~dp0"

echo.
echo    DiskSentry //////////////////////////////////////////////
echo    Storage Telemetry Console - Windows executable builder
echo    /////////////////////////////////////////////////////////
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if not %errorlevel%==0 (
        echo    [X] Python not found.
        echo        1. Install Python 3.10+ from https://www.python.org/downloads/
        echo        2. IMPORTANT: tick "Add python.exe to PATH" during install
        echo        3. Run this file again.
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

echo    [1/4] Installing dependencies (flask, psutil, waitress, pywebview, pyinstaller)...
%PY% -m pip install --upgrade pip >nul 2>nul
%PY% -m pip install -r requirements.txt pyinstaller
if not %errorlevel%==0 (
    echo    [X] Dependency installation failed. Check your internet connection.
    pause
    exit /b 1
)

echo    [2/4] Building DiskSentry.exe with PyInstaller ...
%PY% -m PyInstaller --clean --noconfirm DiskSentry.spec
if not %errorlevel%==0 (
    echo    [X] Build failed. Scroll up for the error message.
    pause
    exit /b 1
)

echo    [3/4] Collecting artifact ...
if not exist build_out mkdir build_out
copy /Y "dist\DiskSentry.exe" "build_out\DiskSentry.exe" >nul

echo    [4/4] Done.
echo.
echo    /////////////////////////////////////////////////////////
echo    [+] BUILT:  build_out\DiskSentry.exe
echo.
echo    HOW TO USE
echo    - Double-click DiskSentry.exe  (accept the Administrator prompt -
echo      SMART / TBW reading requires elevation on Windows).
echo    - DiskSentry opens as a REAL DESKTOP APP WINDOW (its own taskbar
echo      icon, no browser, no address bar, no console window).
echo    - CLOSE THE WINDOW to quit the app (TBW snapshot is saved).
echo    - If the desktop shell cannot start on your machine, DiskSentry
echo      automatically opens your browser instead - nothing is lost.
echo    - OPTIONAL but recommended for full SMART tables + TBW:
echo      install smartmontools  https://www.smartmontools.org/wiki/Download
echo      then click RESCAN inside DiskSentry.
echo    - Diagnostics log (only for the exe): %%LOCALAPPDATA%%\DiskSentry\disksentry.log
echo    /////////////////////////////////////////////////////////
echo.
pause
