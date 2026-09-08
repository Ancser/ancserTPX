@echo off
setlocal
title ancserTPX MarketData Sync
cd /d "%~dp0"
python "%~dp0scripts\market_data_sync.py" --once
exit /b %errorlevel%
