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
    echo  [ERROR] Python not found!
    pause
    exit /b 1
)

:: ── Kill old uvicorn processes ──
echo  Killing old uvicorn processes...
powershell -ExecutionPolicy Bypass -File "%~dp0kill_old.ps1"
timeout /t 3 /nobreak >nul

:: ── Find a free port ──
set PORT=8001

:check_port
powershell -Command "exit ([int][bool](Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue))"
if %errorlevel% == 1 (
    echo  Port %PORT% occupied, trying next...
    set /a PORT+=1
    if %PORT% GTR 8010 (
        echo  [ERROR] Ports 8001-8010 all occupied!
        pause
        exit /b 1
    )
    goto check_port
)
echo  [OK] Using port %PORT%

:: ── Clear cache ──
echo  Clearing bytecode cache...
for /d /r "backend" %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" >nul 2>&1
)

:: ── Clear zone data ──
if exist "data\live_zones.json" (
    echo  Resetting zone cache...
    echo {"saved_at":"","active_zone_id":null,"zones":[]}> "data\live_zones.json"
)

:: ── Install deps ──
if not exist ".deps_installed" (
    echo  Installing dependencies...
    pip install fastapi uvicorn httpx python-dotenv pydantic >nul 2>&1
    echo done > .deps_installed
)

:: ── Start ──
echo.
echo  ============================================
echo   Server starting on port %PORT%
echo   Web UI: http://localhost:%PORT%
echo   Use Ctrl+C to stop (do NOT close with X)
echo  ============================================
echo.

start "" http://localhost:%PORT%

python -m uvicorn backend.main:app --host 0.0.0.0 --port %PORT%

echo.
echo  Server stopped.
pause
