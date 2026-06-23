@echo off
title PC Caster

REM ── Check Python is available ────────────────────────────────────────────────
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo.
    echo  Python not found.
    echo  Please install Python from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM ── Install dependencies ─────────────────────────────────────────────────────
echo Installing/checking dependencies...
pip install -r requirements.txt --quiet

REM ── Install the headless browser used by the .m3u8 finder (one-time) ─────────
echo Checking headless browser for stream scanning...
python -m playwright install chromium

REM ── Generate brand icons if missing ──────────────────────────────────────────
IF NOT EXIST "assets\app_icon.ico" (
    echo Generating brand icons...
    python make_icons.py
)

REM ── Launch the app windowless (no lingering console) ────────────────────────
echo Starting PC Caster...
start "" wscript.exe "PC Caster.vbs"
REM The console closes now; the app keeps running on its own.
