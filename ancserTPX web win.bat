@echo off
title ancserTPX web
color 0A

echo.
echo  ========================================
echo   ancserTPX web
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

:: Kill old web/terminal instances and occupied app ports
echo  Stopping old ancserTPX instances...
powershell -ExecutionPolicy Bypass -File "%~dp0backend\kill_old.ps1"
timeout /t 2 /nobreak >nul

:: Find a free port
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

:: Clear cache
echo  Clearing bytecode cache...
for /d /r "backend" %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" >nul 2>&1
)

:: Reset zone cache
if exist "data\live_zones.json" (
    echo  Resetting zone cache...
    echo {"saved_at":"","active_zone_id":null,"zones":[]}> "data\live_zones.json"
)

:: Install deps if needed
if not exist ".deps_installed" (
    echo  Installing dependencies...
    pip install -r backend\requirements.txt >nul 2>&1
    echo done > .deps_installed
)

:: The EMAPMO messenger chart dependency was added after older installs had
:: already created .deps_installed. Check it explicitly so charts never vanish.
python -c "import matplotlib" >nul 2>&1
if errorlevel 1 (
    echo  Installing EMAPMO chart dependency...
    pip install "matplotlib>=3.8" >nul 2>&1
)

echo.
echo  ============================================
echo   ancserTPX web starting on port %PORT%
echo   Web UI: http://localhost:%PORT%
echo   Use Ctrl+C to stop
echo  ============================================
echo.

start "" http://localhost:%PORT%

python -m uvicorn backend.main:app --host 0.0.0.0 --port %PORT%

echo.
echo  ancserTPX web stopped.
pause
