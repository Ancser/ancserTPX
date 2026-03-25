# ancserTPX Launcher (PowerShell)
Write-Host ""
Write-Host "  ========================================" -ForegroundColor Cyan
Write-Host "   ancserTPX - NQ Futures Trading System" -ForegroundColor Cyan
Write-Host "  ========================================" -ForegroundColor Cyan
Write-Host ""

Set-Location $PSScriptRoot

# Start server in background
Write-Host "  Starting backend on http://localhost:8000" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host ""

# Open browser after 2 seconds
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process "http://localhost:8000"
} | Out-Null

# Run server
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
