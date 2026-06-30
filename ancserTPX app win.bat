@echo off
title ancserTPX app
color 0A

echo.
echo  ========================================
echo   ancserTPX app (native window, no Chrome)
echo  ========================================
echo.

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found! Run "ancserTPX install win.bat" first.
    pause
    exit /b 1
)

:: Kill old web/terminal/app instances and occupied app ports
echo  Stopping old ancserTPX instances...
powershell -ExecutionPolicy Bypass -File "%~dp0backend\kill_old.ps1"
timeout /t 2 /nobreak >nul

:: Clear bytecode cache
echo  Clearing bytecode cache...
for /d /r "backend" %%d in (__pycache__) do (
    if exist "%%d" rd /s /q "%%d" >nul 2>&1
)

:: Install deps if needed (includes pywebview)
if not exist ".deps_installed" (
    echo  Installing dependencies...
    pip install -r backend\requirements.txt >nul 2>&1
    echo done > .deps_installed
)

:: Ensure pywebview present even if .deps_installed predates this feature
python -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo  Installing pywebview...
    pip install "pywebview>=5.3" >nul 2>&1
)

echo.
echo  ============================================
echo   Launching ancserTPX in a native window...
echo   Close the window to stop. This console can
echo   stay minimized.
echo  ============================================
echo.

python app.py

echo.
echo  ancserTPX app stopped.
pause
