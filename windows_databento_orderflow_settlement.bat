@echo off
setlocal
title ancserTPX Databento MBO Settlement
cd /d "%~dp0"

set "LOG_DIR=%~dp0..\ancserMarketData\runtime\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

rem Only a zero-cost subscription quote is accepted.  The Python job also
rem verifies this independently before any transfer.
python "%~dp0scripts\databento_orderflow_settlement.py" --download --max-cost 0 --lookback-trading-days 5 >> "%LOG_DIR%\databento_orderflow_settlement.log" 2>&1
set "RESULT=%errorlevel%"
exit /b %RESULT%
