@echo off
title ancserTPX - NQ Futures Trading System
color 0A

echo.
echo  ========================================
echo   ancserTPX - NQ Futures Trading System
echo  ========================================
echo.
echo  Starting backend server...
echo  Web UI: http://localhost:8001
echo.

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found! Please install Python 3.10+
    pause
    exit /b 1
)

:: Install dependencies if needed
if not exist "backend\__pycache__" (
    echo  Installing dependencies...
    pip install fastapi uvicorn httpx python-dotenv pydantic >nul 2>&1
)

:: Start server and open browser
echo  Server starting on http://localhost:8001
echo  Press Ctrl+C to stop
echo.

start "" http://localhost:8001

python -m uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload

pause
