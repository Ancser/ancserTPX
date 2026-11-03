param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^\d{2}:\d{2}$')][string]$StartTime
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
# Windows Pacific wall time follows DST; Python independently checks the LA
# window. Logon may catch up within ten minutes, with one transactional claim.
$trigger = @((New-ScheduledTaskTrigger -Daily -At $StartTime), (New-ScheduledTaskTrigger -AtLogOn -User $userName))
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'ancserTPX Daily Terminal' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
