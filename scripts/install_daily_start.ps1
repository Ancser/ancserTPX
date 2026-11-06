param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^\d{2}:\d{2}$')][string]$StartTime,
    # Print the daily trigger's StartBoundary instead of registering (tests).
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path -LiteralPath $ProjectRoot).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$windowlessPython = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $windowlessPython)) { throw 'pythonw.exe is required' }
$userName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ((Get-TimeZone).Id -ne 'Pacific Standard Time') {
    throw 'Set the Windows time zone to Pacific Time before enabling Daily start; the terminal uses America/Los_Angeles.'
}
$action = New-ScheduledTaskAction -Execute $windowlessPython -Argument '-m backend.daily_start --tick' -WorkingDirectory $projectPath
# Python independently checks the LA window. Logon may catch up within ten
# minutes, with one transactional claim.
# 1.4.1: New-ScheduledTaskTrigger writes StartBoundary with a UTC offset
# ("...T05:30:00-08:00"). Task Scheduler then fires at a fixed UTC instant, so
# after the 2026-11-01 DST change the 06:30 task fired at 05:30 PST, Python
# correctly found it not due, and no terminal started. A StartBoundary without
# an offset is local wall time and follows DST.
$daily = New-ScheduledTaskTrigger -Daily -At $StartTime
$daily.StartBoundary = [datetime]::Today.Add([TimeSpan]::Parse($StartTime)).ToString("yyyy-MM-dd'T'HH:mm:ss")
if ($DryRun) { Write-Output $daily.StartBoundary; return }
$trigger = @($daily, (New-ScheduledTaskTrigger -AtLogOn -User $userName))
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'ancserTPX Daily Terminal' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
