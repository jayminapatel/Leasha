<#
    Put `leasha` on your PATH, so it works from any folder.

        .\add-to-path.ps1          add it
        .\add-to-path.ps1 -Remove  take it off again

    **Why this exists.** PowerShell does not run commands from the current
    directory - a deliberate protection against a malicious `ls.exe` dropped in
    a folder you happen to be standing in. So `leasha` fails where `.\leasha`
    works, and the error message is confusing the first time you meet it.

    This edits the **user** PATH, not the machine one: no administrator rights,
    nothing changed for anybody else who uses this computer, and it is undone by
    running this with -Remove.

    ASCII only and saved with a BOM, like every other .ps1 here: PowerShell 5.1
    decodes a BOM-less file as ANSI, and one smart quote killed the installer at
    parse time once already.
#>

[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
# This script's own folder goes on PATH, because leasha.cmd sits beside it.
$folder = Split-Path -Parent $MyInvocation.MyCommand.Path

$current = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $current) { $current = "" }

# Split and compare case-insensitively with trailing slashes ignored, so running
# this twice does not add a second copy.
$parts = @($current -split ";" | Where-Object { $_ -ne "" })
$normalised = $parts | ForEach-Object { $_.TrimEnd("\") }
$target = $folder.TrimEnd("\")
$present = $normalised -contains $target

if ($Remove) {
    if (-not $present) {
        Write-Host "Not on your PATH; nothing to remove." -ForegroundColor Yellow
        exit 0
    }
    $kept = @()
    for ($i = 0; $i -lt $parts.Count; $i++) {
        if ($normalised[$i] -ne $target) { $kept += $parts[$i] }
    }
    [Environment]::SetEnvironmentVariable("Path", ($kept -join ";"), "User")
    Write-Host "Removed $folder from your PATH." -ForegroundColor Green
    Write-Host "Open a new terminal for it to take effect." -ForegroundColor Gray
    exit 0
}

if ($present) {
    Write-Host "$folder is already on your PATH." -ForegroundColor Green
    Write-Host "If 'leasha' still is not found, open a NEW terminal - an" -ForegroundColor Gray
    Write-Host "already-open one keeps the PATH it started with." -ForegroundColor Gray
    exit 0
}

$updated = if ($current -eq "") { $folder } else { "$current;$folder" }
[Environment]::SetEnvironmentVariable("Path", $updated, "User")

# Also update this session, so it works immediately rather than after a restart.
$env:Path = "$env:Path;$folder"

Write-Host ""
Write-Host "  Added $folder to your PATH." -ForegroundColor Green
Write-Host ""
Write-Host "  You can now run these from anywhere:" -ForegroundColor Green
Write-Host "    leasha                     open the window"
Write-Host "    leasha commands            the filters you can type"
Write-Host "    leasha evaluate --builtin  does search actually work?"
Write-Host "    leasha stats               what is in the index"
Write-Host ""
Write-Host "  Open a NEW terminal for other sessions to see it." -ForegroundColor Gray
Write-Host "  Undo at any time: .\add-to-path.ps1 -Remove" -ForegroundColor Gray
