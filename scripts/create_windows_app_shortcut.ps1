[CmdletBinding()]
param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Join-Path $PSScriptRoot ".."
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$launcher = Join-Path $root "windows app.vbs"
$icon = Join-Path $root "frontend\static\favicon.ico"

if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Launcher not found: $launcher"
}
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw "Website icon not found: $icon"
}

$desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktop)) {
    throw "Windows Desktop folder could not be resolved"
}
$shortcutPath = Join-Path $desktop "ancserTPX.lnk"
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
if (-not (Test-Path -LiteralPath $wscript -PathType Leaf)) {
    $wscript = "wscript.exe"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $wscript
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "ancserTPX native desktop app"
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Output "Created $shortcutPath"
Write-Output "Icon $icon"
