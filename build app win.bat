@echo off
title ancserTPX - Build Installer
color 0A

echo.
echo  ========================================
echo   ancserTPX - Build desktop package
echo  ========================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found! Run "ancserTPX install win.bat" first.
    pause
    exit /b 1
)

echo  [1/3] Ensuring build deps (PyInstaller + app deps)...
pip install -r backend\requirements.txt >nul 2>&1
pip install "pyinstaller>=6.0" >nul 2>&1

echo  [2/3] Building (onedir, no console)...
pyinstaller ancserTPX.spec --noconfirm --clean
if errorlevel 1 (
    echo  [ERROR] Build failed.
    pause
    exit /b 1
)

echo  [3/3] Done.
echo.
echo  ============================================
echo   RUN THIS:  dist\ancserTPX\ancserTPX.exe
echo.
echo   Do NOT run the copy under build\ - that is
echo   PyInstaller scratch with no Python DLL and
echo   will error "Failed to load Python DLL".
echo.
echo   To distribute: zip the whole dist\ancserTPX
echo   folder. data\ and .env are created next to
echo   the exe on first run (credentials via the
echo   Web UI CONNECT button).
echo  ============================================
echo.

:: Open the correct output folder so the right exe is one click away.
if exist "dist\ancserTPX\ancserTPX.exe" start "" explorer "%~dp0dist\ancserTPX"

pause
