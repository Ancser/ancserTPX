@echo off
title ancserTPX - NQ Futures Trading System
color 0A

echo.
echo  ========================================
echo   ancserTPX - NQ Futures Trading System
echo  ========================================
echo.

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found! Please install Python 3.10+
    pause
    exit /b 1
)

:: ── Kill old processes on port 8001 ──
echo  Cleaning up old processes on port 8001...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001.*LISTENING" 2^>nul') do (
    taskkill /F /PID %%a >nul 2>&1
)
:: Wait for port to fully release
timeout /t 2 /nobreak >nul

:: ── Clear Python bytecode cache ──
echo  Clearing bytecode cache...
for /d /r "backend" %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" >nul 2>&1
)

:: ── Clear stale zone data ──
if exist "data\live_zones.json" (
    echo  Resetting zone cache...
    echo {"saved_at":"","active_zone_id":null,"zones":[]}> "data\live_zones.json"
)

:: Install dependencies if needed
if not exist ".deps_installed" (
    echo  Installing dependencies...
    pip install fastapi uvicorn httpx python-dotenv pydantic >nul 2>&1
    echo done > .deps_installed
)

:: Start server and open browser
echo.
echo  Starting backend server...
echo  Web UI: http://localhost:8001
echo  Press Ctrl+C to stop
echo.

start "" http://localhost:8001

:: NOTE: --reload REMOVED for safety. Auto-reload kills live engine state
:: (pending orders, SL/TP, position tracking) when .py files change.
:: Restart manually with start.bat after code changes.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8001

pause
