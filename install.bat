@echo off
title ancserTPX - Environment Setup
color 0A

echo.
echo  ========================================
echo   ancserTPX - One-Click Install
echo  ========================================
echo.

cd /d "%~dp0"

:: ── Check Python ──
echo  [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Python not found!
    echo  Download: https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo         Python %%v found

:: ── Check pip ──
echo  [2/4] Checking pip...
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] pip not found! Reinstall Python with pip enabled.
    pause
    exit /b 1
)
echo         pip OK

:: ── Install dependencies ──
echo  [3/4] Installing dependencies...
python -m pip install -r backend\requirements.txt --quiet
if errorlevel 1 (
    echo  [ERROR] Failed to install dependencies!
    pause
    exit /b 1
)
echo         All packages installed

:: ── Check .env ──
echo  [4/4] Checking .env...
if exist ".env" (
    echo         .env found
) else (
    echo         .env not found - creating from template...
    copy .env.example .env >nul 2>&1
    echo.
    echo  ============================================
    echo   IMPORTANT: Edit .env with your credentials
    echo   Open .env and fill in:
    echo     TOPSTEPX_USERNAME=your_email
    echo     TOPSTEPX_API_KEY=your_key
    echo  ============================================
    echo.
)

echo.
echo  ========================================
echo   Setup complete!
echo   Run start.bat to launch ancserTPX
echo  ========================================
echo.
pause
