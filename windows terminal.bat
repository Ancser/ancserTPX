@echo off
title windows terminal
color 0A

echo.
echo  ========================================
echo   windows terminal
echo  ========================================
echo.

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found!
    exit /b 1
)

:: Stop known legacy Web/Terminal workers. Never touch the desktop App or 8001.
echo  Stopping existing ancserTPX Web/Terminal workers...
powershell -ExecutionPolicy Bypass -File "%~dp0backend\stop_legacy_instances.ps1"
timeout /t 2 /nobreak >nul

:: Clear cache
echo  Clearing bytecode cache...
for /d /r "backend" %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" >nul 2>&1
)

:: Reset zone cache
if exist "%~dp0..\ancserMarketData\runtime\state\live_zones.json" (
    echo  Resetting zone cache...
    echo {"saved_at":"","active_zone_id":null,"zones":[]}> "%~dp0..\ancserMarketData\runtime\state\live_zones.json"
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
echo  windows terminal stopped.
