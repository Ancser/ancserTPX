# Stop known legacy ancserTPX Web/Terminal launchers before the native App
# starts.  This script deliberately does not scan or kill TCP ports: 8001 is
# owned by the native pythonw App and an unrelated local service must not be
# terminated by a project launcher.
$terminalPattern = '(?i)backend\.terminal_live|terminal_live\.py'
$webPattern = '(?i)backend\.main:app|(?:-m\s+)?backend\.main\b|backend[\\/]main\.py\b|uvicorn(?:\.exe)?\s+(?:backend\.)?main:app'
$launcherPattern = '(?i)ancserTPX[\\/ ]+(?:terminal|web) win\.bat'
$webPortPattern = '(?i)(?:--port|-p)\s*8001(?:\s|$)'
$stopped = 0

$processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$legacyWebBatchParentIds = @(
    $processes |
        Where-Object {
            $_.Name -ieq 'cmd.exe' -and
            [string]$_.CommandLine -match '(?i)ancserTPX[\\/ ]+web win\.bat'
        } |
        Select-Object -ExpandProperty ProcessId
)
$targets = @(
    $processes |
        Where-Object {
            $_.Name -in @('python.exe', 'pythonw.exe') -and
            (
                [string]$_.CommandLine -match $terminalPattern -or
                (
                    [string]$_.CommandLine -match $webPattern -and
                    (
                        [string]$_.CommandLine -match $webPortPattern -or
                        $legacyWebBatchParentIds -contains [int]$_.ParentProcessId
                    )
                )
            )
        }
)
$parentIds = @($targets | Select-Object -ExpandProperty ParentProcessId -Unique)

foreach ($process in $targets) {
    Write-Host "  Stopping legacy ancserTPX PID $($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped++
}

# Old BAT launchers ended with `pause`.  Close only a matching ancserTPX BAT
# parent, never an arbitrary cmd.exe owned by the user.
foreach ($process in $processes) {
    if (
        $process.Name -ieq 'cmd.exe' -and
        $parentIds -contains [int]$process.ProcessId -and
        [string]$process.CommandLine -match $launcherPattern
    ) {
        Write-Host "  Closing legacy ancserTPX CMD PID $($process.ProcessId)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }
}

if ($stopped -eq 0) {
    Write-Host "  No legacy ancserTPX Web/Terminal process found"
}
