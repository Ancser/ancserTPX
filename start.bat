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

:: ── Try to kill old processes (best effort) ──
echo  Attempting to free old ports...
powershell -Command "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'uvicorn' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 2 /nobreak >nul

:: ── Find a free port starting from 8001 ──
set PORT=8001

:check_port
powershell -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>&1
if errorlevel 1 (
    echo  Port %PORT% occupied, trying next...
    set /a PORT+=1
    if %PORT% GTR 8010 (
        echo  [ERROR] Ports 8001-8010 all occupied! Restart PC or manually kill processes.
        pause
        exit /b 1
    )
    goto check_port
)
echo  Using port %PORT%

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
echo  ============================================
echo   Server starting on port %PORT%
echo   Web UI: http://localhost:%PORT%
echo  ============================================
echo  Press Ctrl+C to stop
echo.

start "" http://localhost:%PORT%

python -m uvicorn backend.main:app --host 0.0.0.0 --port %PORT%

pause
