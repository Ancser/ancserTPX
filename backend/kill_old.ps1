# Kill all python uvicorn processes to prevent dual-engine trading
$killed = 0

# Method 1: Kill python processes running uvicorn
Get-WmiObject Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'uvicorn' } |
    ForEach-Object {
        Write-Host "  Killing uvicorn PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }

# Method 2: Kill anything listening on ports 8001-8010
8001..8010 | ForEach-Object {
    $port = $_
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique |
        Where-Object { $_ -ne 0 } |
        ForEach-Object {
            Write-Host "  Killing PID $_ on port $port"
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
            $killed++
        }
}

if ($killed -eq 0) {
    Write-Host "  No old processes found"
}
