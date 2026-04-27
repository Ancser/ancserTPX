@echo off
:: Kill all processes listening on port 8001
:: Called by start-Win11.bat, can also be run standalone

echo  Killing processes on port 8001...

:: Use PowerShell — much more reliable than batch for/taskkill
powershell -Command "Get-NetTCPConnection -LocalPort 8001 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host '  Killing PID' $_; Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"

:: Also kill any python.exe running uvicorn (catches orphans)
powershell -Command "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'uvicorn' } | ForEach-Object { Write-Host '  Killing uvicorn PID' $_.ProcessId; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

:: Wait for OS to release sockets
timeout /t 3 /nobreak >nul

:: Verify
powershell -Command "if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) { Write-Host '  [FAIL] Port 8001 still occupied!'; exit 1 } else { Write-Host '  [OK] Port 8001 is free'; exit 0 }"
