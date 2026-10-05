<#
    build.ps1 - build Leasha's Windows installer (order 202626082213 section 4.1).

    From the project folder, in a normal PowerShell window:

        .\packaging\build.ps1            # build into build\installer
        .\packaging\build.ps1 -Release   # and copy it to the Releases folder on Google Drive

    Steps: PyInstaller (packaging\leasha.spec) makes build\dist\Leasha, a folder with
    Leasha.exe and leasha-cli.exe; a quick check runs leasha-cli.exe; Inno Setup
    (packaging\installer.iss) wraps the folder as Leasha-Setup-<version>.exe and its
    SHA256 is printed. Needs PyInstaller in the venv and Inno Setup 6
    (winget install --id JRSoftware.InnoSetup -e).

    ASCII only, saved UTF-8 with a BOM: non-negotiable 7.
#>
[CmdletBinding()]
param([switch]$Release)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "venv\Scripts\python.exe"
$Version = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -Raw).Trim()
$Dist = Join-Path $Root "build\dist"
$Work = Join-Path $Root "build\work"
$Out = Join-Path $Root "build\installer"

Write-Host "Leasha $Version" -ForegroundColor Cyan

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller (packagingequirements-build.txt)..." -ForegroundColor Cyan
    & $Python -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Could not install PyInstaller (exit $LASTEXITCODE)." }
}

Write-Host "1/4 PyInstaller..." -ForegroundColor Cyan
& $Python -m PyInstaller (Join-Path $PSScriptRoot "leasha.spec") --noconfirm --clean `
    --distpath $Dist --workpath $Work
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE). Its output above says why." }

Write-Host "2/4 Checking the build runs..." -ForegroundColor Cyan
$Cli = Join-Path $Dist "Leasha\leasha-cli.exe"
& $Cli -c "import app.core.version as v, PySide6.QtCore as q; print('leasha', v.version(), 'qt', q.qVersion())"
if ($LASTEXITCODE -ne 0) { throw "The built leasha-cli.exe did not run (exit $LASTEXITCODE)." }

# A settings file left in the build by a test would be installed, and the
# installer would then keep it rather than write the one the user chose.
if (Test-Path -LiteralPath (Join-Path $Dist "Leasha\_internal\.env")) {
    throw "build\dist\Leasha\_internal\.env exists (left by a test?). Delete it and run this again."
}

Write-Host "3/4 Inno Setup..." -ForegroundColor Cyan
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Iscc) {
    throw "Inno Setup 6 is not installed. Run: winget install --id JRSoftware.InnoSetup -e"
}
& $Iscc "/DAppVersion=$Version" "/DSourceDir=$Dist\Leasha" "/DOutputDir=$Out" `
    (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)." }

Write-Host "4/4 Done." -ForegroundColor Cyan
$Setup = Join-Path $Out "Leasha-Setup-$Version.exe"
$Hash = (Get-FileHash -LiteralPath $Setup -Algorithm SHA256).Hash
$SizeMB = [math]::Round((Get-Item -LiteralPath $Setup).Length / 1MB)
Write-Host "  $Setup ($SizeMB MB)"
Write-Host "  SHA256 $Hash"

if ($Release) {
    $Shelf = Join-Path "D:\Local\GDrive\Leasha\Releases" $Version
    New-Item -ItemType Directory -Force -Path $Shelf | Out-Null
    Copy-Item -LiteralPath $Setup -Destination $Shelf -Force
    Set-Content -LiteralPath (Join-Path $Shelf "Leasha-Setup-$Version.sha256") -Value "$Hash  Leasha-Setup-$Version.exe" -Encoding ascii
    Write-Host "  Copied to $Shelf" -ForegroundColor Green
}
