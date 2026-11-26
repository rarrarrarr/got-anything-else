<#
.SYNOPSIS
    Adds (or removes) a shortcut to gae.ahk in your Startup folder, so the
    "gae " hotstring is available after every sign-in.

.EXAMPLE
    .\install-startup.ps1              # install the shortcut and start the script now
.EXAMPLE
    .\install-startup.ps1 -Uninstall   # remove the shortcut
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

$script   = Join-Path $PSScriptRoot 'gae.ahk'
$startup  = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startup 'gae.lnk'

if ($Uninstall) {
    if (Test-Path $shortcut) {
        Remove-Item $shortcut
        Write-Host "Removed $shortcut"
    } else {
        Write-Host "No shortcut at $shortcut"
    }
    Write-Host 'A copy of gae.ahk that is already running keeps running until you exit it from the tray.'
    return
}

if (-not (Test-Path $script)) {
    throw "Can't find $script"
}

# Find AutoHotkey v2. Pointing the shortcut at the exe means it doesn't depend
# on the .ahk file association. Fall back to the association if not found.
$ahkExe = @(
    "$env:ProgramFiles\AutoHotkey\v2\AutoHotkey64.exe",
    "$env:ProgramFiles\AutoHotkey\v2\AutoHotkey.exe",
    "$env:LOCALAPPDATA\Programs\AutoHotkey\v2\AutoHotkey64.exe",
    "$env:LOCALAPPDATA\Programs\AutoHotkey\v2\AutoHotkey.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($shortcut)
if ($ahkExe) {
    $lnk.TargetPath = $ahkExe
    $lnk.Arguments  = "`"$script`""
} else {
    Write-Warning 'AutoHotkey v2 not found in the usual places; the shortcut will open gae.ahk through its file association.'
    $lnk.TargetPath = $script
}
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.Description      = 'gae hotstring (got-anything-else)'
$lnk.Save()

Write-Host "Created $shortcut"
Write-Host "  -> $($lnk.TargetPath) $($lnk.Arguments)"
Write-Host 'The shortcut points at this repo, so keep the repo where it is (or re-run this after moving it).'

if (-not $NoStart) {
    Start-Process -FilePath $shortcut
    Write-Host 'Started gae.ahk; type "gae " to try it.'
}
