<#
    build.ps1 - build Leasha's Windows installer (order 202626082213 section 4.1).

    From the project folder, in a normal PowerShell window:

        .\packaging\build.ps1            # build into build\installer
        .\packaging\build.ps1 -Release   # and copy it to the Releases folder on Google Drive
        .\packaging\build.ps1 -Publish   # -Release, then publish it as the GitHub release

    Steps: PyInstaller (packaging\leasha.spec) makes build\dist\Leasha, a folder with
    Leasha.exe and leasha-cli.exe; a quick check runs leasha-cli.exe; Inno Setup
    (packaging\installer.iss) wraps the folder as Leasha-Setup-<version>.exe and its
    SHA256 is printed. Needs PyInstaller in the venv and Inno Setup 6
    (winget install --id JRSoftware.InnoSetup -e).

    ASCII only, saved UTF-8 with a BOM: non-negotiable 7.
#>
[CmdletBinding()]
param(
    [switch]$Release,
    # 2026-10-11 (owner): Leasha is distributed again, from GitHub Releases. -Publish
    # does everything -Release does, then puts the installer, a second copy of it
    # named Leasha-Setup.exe and the .sha256 on the GitHub release v<version>. The
    # fixed name is what keeps one version-free download link working for ever:
    # github.com/jayminapatel/Leasha/releases/latest/download/Leasha-Setup.exe,
    # the link leasha.co.uk/app carries. Needs gh, signed in as jayminapatel.
    [switch]$Publish,
    # 2026-10-08 review: leasha.spec now refuses to build when one of the optional
    # packages (av, pypff, reverse_geocoder, insightface) is missing from the venv,
    # so two builds of one commit ship the same thing. This switch is the deliberate
    # "a smaller Leasha is what I want"; the spec reads the variable set below.
    [switch]$AllowMissingOptional
)

# Stop on the first failed cmdlet. A native program (python, ISCC) never raises,
# so each one below is followed by its own $LASTEXITCODE check.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "venv\Scripts\python.exe"
$Version = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -Raw).Trim()
$Dist = Join-Path $Root "build\dist"
$Work = Join-Path $Root "build\work"
$Out = Join-Path $Root "build\installer"

Write-Host "Leasha $Version" -ForegroundColor Cyan

# -Publish is checked here, before the build, so a missing sign-in or tag costs
# seconds rather than a 15-minute build. Every probe decides by its exit code or
# its output; nothing feeds a native program's stderr to a redirect (see the
# PyInstaller probe below for why that matters in Windows PowerShell 5.1).
$Repo = "jayminapatel/Leasha"
$Tag = "v$Version"
if ($Publish) {
    $Release = $true
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "gh (the GitHub CLI) is not installed. Run: winget install --id GitHub.cli -e"
    }
    $Login = & gh api user --jq .login
    if ($LASTEXITCODE -ne 0 -or $Login -ne "jayminapatel") {
        throw "gh is not signed in as jayminapatel (it answered '$Login'). Run: gh auth login"
    }
    $AtTag = @(& git -C $Root tag --points-at HEAD)
    if ($AtTag -notcontains $Tag) {
        throw "This checkout is not at tag $Tag. A release is built from its tag: git checkout $Tag"
    }
    $Remote = & git -C $Root ls-remote --tags origin "refs/tags/$Tag"
    if ($LASTEXITCODE -ne 0 -or -not $Remote) {
        throw "Tag $Tag is not on GitHub yet. Run: git push origin $Tag"
    }
    # The release notes are this version's CHANGELOG section, as for 1.0.2 and 1.0.3.
    $Notes = New-Object System.Collections.Generic.List[string]
    $Inside = $false
    foreach ($Line in Get-Content -LiteralPath (Join-Path $Root "CHANGELOG.md") -Encoding UTF8) {
        if ($Line -match '^## \[') {
            if ($Inside) { break }
            $Inside = $Line.StartsWith("## [$Version]")
            continue
        }
        if ($Inside) { $Notes.Add($Line) }
    }
    if (-not ($Notes -join "").Trim()) {
        throw "CHANGELOG.md has no '## [$Version]' section to publish as the release notes."
    }
}

# One build at a time. A second build clears build\dist while the first is still
# packaging it, and neither finishes (2026-10-07: a 0.3.4 and a 0.3.5 build
# overlapped). The processes that started this one carry "build.ps1" in their own
# command lines too, so this process and its parents are left out.
$Mine = @{}
$Walk = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
while ($Walk -and -not $Mine.ContainsKey([int]$Walk.ProcessId)) {
    $Mine[[int]$Walk.ProcessId] = $true
    $Walk = Get-CimInstance Win32_Process -Filter "ProcessId=$($Walk.ParentProcessId)"
}
$Others = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
    Where-Object { -not $Mine.ContainsKey([int]$_.ProcessId) -and $_.CommandLine -match 'build\.ps1' })
if ($Others.Count -gt 0) {
    throw ("Another build is already running (process " + (($Others | ForEach-Object { $_.ProcessId }) -join ", ") +
           "). Let it finish, or close its window, then run this again.")
}

# 2026-10-08, found in review: this probe was `& $Python -c "import PyInstaller"
# 2>$null`. Under $ErrorActionPreference = "Stop", Windows PowerShell 5.1 turns
# anything a native command writes to a redirected stderr into a terminating
# NativeCommandError - so on a machine without PyInstaller the traceback ended
# the script here and the "install it" branch below never ran (pwsh 7 was fine).
# find_spec writes nothing to stderr either way; the exit code is the answer.
& $Python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller (packagingequirements-build.txt)..." -ForegroundColor Cyan
    & $Python -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "Could not install PyInstaller (exit $LASTEXITCODE)." }
}

if ($AllowMissingOptional) {
    $env:LEASHA_BUILD_ALLOW_MISSING = "1"
    Write-Host "Optional packages may be missing from this build (-AllowMissingOptional)." -ForegroundColor Yellow
} else {
    Remove-Item Env:\LEASHA_BUILD_ALLOW_MISSING -ErrorAction SilentlyContinue
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
# Inno Setup's three install locations: per-machine 32-bit (its installer's own
# default), per-machine 64-bit, and a per-user install under %LOCALAPPDATA%.
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $Iscc) {
    throw "Inno Setup 6 is not installed. Run: winget install --id JRSoftware.InnoSetup -e"
}
# The /D defines land in the #ifndef defaults at the top of installer.iss, so the
# same .iss also compiles by hand from the Inno Setup editor without editing it.
& $Iscc "/DAppVersion=$Version" "/DSourceDir=$Dist\Leasha" "/DOutputDir=$Out" `
    (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)." }

Write-Host "4/4 Done." -ForegroundColor Cyan
$Setup = Join-Path $Out "Leasha-Setup-$Version.exe"
# Printed, and with -Release written beside the file: an unsigned installer has
# nothing else a person can check a download against.
$Hash = (Get-FileHash -LiteralPath $Setup -Algorithm SHA256).Hash
$SizeMB = [math]::Round((Get-Item -LiteralPath $Setup).Length / 1MB)
Write-Host "  $Setup ($SizeMB MB)"
Write-Host "  SHA256 $Hash"

# HANDOFF, "Where a session runs": release builds travel to the laptop by Google
# Drive sync, never through git, so the shelf is the Drive folder outside the repo.
if ($Release) {
    $Shelf = Join-Path "D:\Local\GDrive\Leasha\Releases" $Version
    New-Item -ItemType Directory -Force -Path $Shelf | Out-Null
    Copy-Item -LiteralPath $Setup -Destination $Shelf -Force
    Set-Content -LiteralPath (Join-Path $Shelf "Leasha-Setup-$Version.sha256") -Value "$Hash  Leasha-Setup-$Version.exe" -Encoding ascii
    Write-Host "  Copied to $Shelf" -ForegroundColor Green
}

# The installer goes up twice: under its version, for the record, and as
# Leasha-Setup.exe, which GitHub's releases/latest/download/ address resolves to
# the newest release carrying that name. Drop the second copy and the website's
# download button quietly keeps serving the previous version.
if ($Publish) {
    Write-Host "Publishing $Tag on GitHub..." -ForegroundColor Cyan
    $Sum = Join-Path $Out "Leasha-Setup-$Version.sha256"
    Set-Content -LiteralPath $Sum -Value "$Hash  Leasha-Setup-$Version.exe" -Encoding ascii
    $Fixed = Join-Path $Out "Leasha-Setup.exe"
    Copy-Item -LiteralPath $Setup -Destination $Fixed -Force
    $NotesFile = Join-Path $Out "release-notes-$Version.md"
    [System.IO.File]::WriteAllLines($NotesFile, $Notes, (New-Object System.Text.UTF8Encoding($false)))
    $Existing = @(& gh release list --repo $Repo --limit 200 --json tagName --jq '.[].tagName')
    if ($LASTEXITCODE -ne 0) { throw "gh could not list the releases of $Repo (exit $LASTEXITCODE)." }
    if ($Existing -contains $Tag) {
        & gh release upload $Tag $Setup $Fixed $Sum --repo $Repo --clobber
    } else {
        & gh release create $Tag $Setup $Fixed $Sum --repo $Repo --title "Leasha $Version" `
            --notes-file $NotesFile --verify-tag --latest
    }
    if ($LASTEXITCODE -ne 0) { throw "gh could not publish $Tag (exit $LASTEXITCODE)." }
    Write-Host "  https://github.com/$Repo/releases/tag/$Tag" -ForegroundColor Green
    Write-Host "  Always the newest: https://github.com/$Repo/releases/latest/download/Leasha-Setup.exe" -ForegroundColor Green
}
