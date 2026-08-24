<#
    parse-check.ps1 - static syntax check for the project's PowerShell scripts.

    Uses the real PowerShell parser, so it catches exactly what would stop a
    script from running - including the encoding fault that produced a silent
    failure with no log at all: a UTF-8 file saved without a BOM, whose em
    dashes Windows PowerShell 5.1 decoded as cp1252, turning byte 0x94 into a
    smart quote that PowerShell treats as a string delimiter.

    Results go to logs\parse-check.log so a failure always leaves evidence.

    Usage:
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\parse-check.ps1
#>

[CmdletBinding()]
param(
    [string[]]$Path
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if (-not $Path -or $Path.Count -eq 0) {
    $Path = @(
        (Join-Path $root "install.ps1"),
        (Join-Path $root "scripts\parse-check.ps1")
    )
}

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "parse-check.log"

$report = New-Object System.Collections.ArrayList
function Add-Line($text, $colour) {
    Write-Host $text -ForegroundColor $colour
    [void]$report.Add($text)
}

Add-Line "parse-check $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" "Gray"
Add-Line "PowerShell $($PSVersionTable.PSVersion)" "Gray"
Add-Line "" "Gray"

$failed = 0

foreach ($file in $Path) {
    $name = Split-Path -Leaf $file

    if (-not (Test-Path -LiteralPath $file)) {
        Add-Line "[MISSING] $name" "Red"
        $failed++
        continue
    }

    # --- Encoding check -----------------------------------------------------
    # Windows PowerShell 5.1 decodes a BOM-less file using the ANSI codepage,
    # not UTF-8. A non-ASCII byte in such a file is a parse hazard, so require
    # either a BOM or a pure-ASCII body.
    $bytes = [System.IO.File]::ReadAllBytes($file)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $body = if ($hasBom) { $bytes[3..($bytes.Length - 1)] } else { $bytes }
    $nonAscii = 0
    foreach ($b in $body) { if ($b -gt 127) { $nonAscii++ } }

    if ($nonAscii -gt 0 -and -not $hasBom) {
        Add-Line "[ENCODING] $name - $nonAscii non-ASCII byte(s) and NO UTF-8 BOM." "Red"
        Add-Line "           Windows PowerShell 5.1 will read this as the ANSI codepage" "Yellow"
        Add-Line "           and may mis-parse it. Save as UTF-8 with BOM, or use ASCII only." "Yellow"
        $failed++
    } else {
        $enc = if ($hasBom) { "UTF-8 BOM" } else { "ASCII" }
        Add-Line "[ENCODING] $name - OK ($enc, $nonAscii non-ASCII byte(s))" "DarkGray"
    }

    # --- Syntax check -------------------------------------------------------
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors)

    if ($errors -and $errors.Count -gt 0) {
        Add-Line "[SYNTAX]   $name - $($errors.Count) parse error(s):" "Red"
        foreach ($e in $errors) {
            Add-Line ("             line {0}, col {1}: {2}" -f `
                $e.Extent.StartLineNumber, $e.Extent.StartColumnNumber, $e.Message) "Red"
        }
        $failed++
    } else {
        Add-Line "[SYNTAX]   $name - OK ($($tokens.Count) tokens)" "Green"
    }
}

Add-Line "" "Gray"
if ($failed -gt 0) {
    Add-Line "FAILED - $failed problem(s). Do not run the installer until these are fixed." "Red"
} else {
    Add-Line "ALL SCRIPTS PARSE CLEANLY." "Green"
}

$report | Set-Content -LiteralPath $logFile -Encoding UTF8
Write-Host ""
Write-Host "  Written to: $logFile" -ForegroundColor Gray

exit $(if ($failed -gt 0) { 1 } else { 0 })
