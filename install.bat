@echo off
setlocal EnableDelayedExpansion
title ancserTPX - Environment Setup
color 0A

echo.
echo  ========================================
echo   ancserTPX - One-Click Install
echo  ========================================
echo.

cd /d "%~dp0"

set "PYEXE=python"
set "PY_VERSION=3.13.1"
set "PY_INSTALL_DIR=%LOCALAPPDATA%\Programs\Python\Python313"

:: ── [1/4] Locate or auto-install Python ──
echo  [1/4] Checking Python...
%PYEXE% --version >nul 2>&1
if not errorlevel 1 goto PY_OK

:: Maybe Python was just installed in a previous run — check known path
if exist "%PY_INSTALL_DIR%\python.exe" (
    set "PYEXE=%PY_INSTALL_DIR%\python.exe"
    "!PYEXE!" --version >nul 2>&1
    if not errorlevel 1 goto PY_OK
)

echo         Python not found - auto-installing %PY_VERSION% (user scope, no admin needed)...
echo.

:: ── Try winget first (Win10 1809+ / Win11 default) ──
where winget >nul 2>&1
if not errorlevel 1 (
    echo         Using winget...
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-source-agreements --accept-package-agreements
    if exist "%PY_INSTALL_DIR%\python.exe" (
        set "PYEXE=%PY_INSTALL_DIR%\python.exe"
        goto PY_OK
    )
)

:: ── Fallback: download official installer via PowerShell ──
set "INSTALLER=%TEMP%\python-%PY_VERSION%-amd64.exe"
echo         winget unavailable - downloading installer from python.org...
powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = 'Tls12'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe' -OutFile '%INSTALLER%' -UseBasicParsing } catch { exit 1 }"
if not exist "%INSTALLER%" (
    echo.
    echo  [ERROR] Could not download Python installer.
    echo  Please install manually: https://www.python.org/downloads/
    echo  ^(check "Add Python to PATH" during install^)
    pause
    exit /b 1
)
echo         Running silent installer ^(user scope^)...
"%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 SimpleInstall=1
del "%INSTALLER%" >nul 2>&1

if exist "%PY_INSTALL_DIR%\python.exe" (
    set "PYEXE=%PY_INSTALL_DIR%\python.exe"
    goto PY_OK
)

echo.
echo  [ERROR] Python install ran but python.exe not found at expected path.
echo  Please open a NEW cmd window and run install.bat again.
pause
exit /b 1

:PY_OK
for /f "tokens=2" %%v in ('"%PYEXE%" --version 2^>^&1') do echo         Python %%v ready

:: ── [2/4] pip ──
echo  [2/4] Checking pip...
"%PYEXE%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo         pip missing - bootstrapping with ensurepip...
    "%PYEXE%" -m ensurepip --upgrade >nul 2>&1
    "%PYEXE%" -m pip --version >nul 2>&1
    if errorlevel 1 (
        echo  [ERROR] pip bootstrap failed.
        pause
        exit /b 1
    )
)
echo         pip OK

:: ── [3/4] Dependencies ──
echo  [3/4] Installing dependencies...
"%PYEXE%" -m pip install --upgrade pip --quiet
"%PYEXE%" -m pip install -r backend\requirements.txt --quiet
if errorlevel 1 (
    echo  [ERROR] Failed to install dependencies!
    pause
    exit /b 1
)
echo         All packages installed

:: ── [4/4] .env ──
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
endlocal
