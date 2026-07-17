@echo off
title ancserTPX terminal
color 0A

echo.
echo  ========================================
echo   ancserTPX terminal
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
echo   Starting terminal-only LIVE engine
echo   Uses .env credentials, default account,
echo   and the last used live preset.
echo   Use Ctrl+C to stop
echo  ============================================
echo.

python -m backend.terminal_live

echo.
echo  ancserTPX terminal stopped.
pause
