@echo off
setlocal
cd /d "%~dp0"

rem Compatibility wrapper: the native app is started by the hidden VBS host.
rem The API remains loopback-only at 127.0.0.1; no browser tab or server CMD
rem is opened by the application itself.
wscript.exe "%~dp0windows app.vbs"
exit /b %errorlevel%
