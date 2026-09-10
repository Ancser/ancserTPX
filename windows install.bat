@echo off
setlocal EnableDelayedExpansion
title windows install
color 0A

echo.
echo  ========================================
echo   windows install
echo  ========================================
echo.

cd /d "%~dp0"

set "PYEXE=python"
set "PY_VERSION=3.13.1"
set "PY_INSTALL_DIR=%LOCALAPPDATA%\Programs\Python\Python313"

:: ── [1/5] Locate or auto-install Python ──
echo  [1/5] Checking Python...
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
echo  Please open a NEW cmd window and run "windows install.bat" again.
pause
exit /b 1

:PY_OK
for /f "tokens=2" %%v in ('"%PYEXE%" --version 2^>^&1') do echo         Python %%v ready

:: ── [2/5] pip ──
echo  [2/5] Checking pip...
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

:: ── [3/5] Dependencies ──
echo  [3/5] Installing dependencies...
"%PYEXE%" -m pip install --upgrade pip --quiet
"%PYEXE%" -m pip install -r backend\requirements.txt --quiet
if errorlevel 1 (
    echo  [ERROR] Failed to install dependencies!
    pause
    exit /b 1
)
echo         All packages installed

:: The native desktop launcher needs pywebview even when an older install
:: already has a dependency marker.
"%PYEXE%" -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo         Installing native desktop window dependency...
    "%PYEXE%" -m pip install "pywebview>=5.3" --quiet
    if errorlevel 1 (
        echo  [ERROR] Failed to install pywebview.
        pause
        exit /b 1
    )
)

:: ── [4/5] .env ──
echo  [4/5] Checking .env...
if exist ".env" (
    echo         .env found
) else (
    echo         .env not found - creating blank...
    (
        echo TOPSTEPX_USERNAME=
        echo TOPSTEPX_API_KEY=
        echo EMAPMO_MESSENGER_ENABLED=false
        echo EMAPMO_DISCORD_WEBHOOK_URL=
        echo DISCORD_TOKEN=
        echo EMAPMO_DISCORD_CHANNEL_ID=
        echo EMAPMO_DISCORD_AUTH_MODE=bot
        echo EMAPMO_SIGNAL_HISTORY_DAYS=30
        echo EMAPMO_SIGNAL_CHART_BARS=96
        echo EMAPMO_SIGNAL_QUEUE_SIZE=8
        echo EMAPMO_SIGNAL_TIMEZONE=America/Chicago
    ) > .env
    echo.
    echo  ============================================
    echo   Credentials will be saved from the Web UI
    echo   Click CONNECT and enter your email + API key
    echo  ============================================
    echo.
)

:: [5/5] Canonical market-data root (E: mirroring is manual only)
echo  [5/5] Preparing MarketData roots...
set "MARKET_DATA_ROOT=%~dp0..\ancserMarketData"
if not exist "%MARKET_DATA_ROOT%" mkdir "%MARKET_DATA_ROOT%"
echo         Primary: %MARKET_DATA_ROOT%
schtasks /change /tn "ancserTPX MarketData Sync" /disable >nul 2>&1
echo         Hourly scheduled E: mirror is disabled.
echo         Run windows_market_data_sync.bat only when a manual mirror is wanted.

echo.
echo  ========================================
echo   Setup complete!
echo   Double-click "windows app.vbs" for the native WebView2 app
echo   ("windows web.bat" remains a compatibility shortcut)
echo   Run "windows terminal.bat" for terminal-only LIVE
echo  ========================================
echo.
pause
endlocal
