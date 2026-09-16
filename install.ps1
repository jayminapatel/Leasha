<#
    install.ps1 - Local Knowledge Graph V2 installer (Windows 10/11)

    Run from a NORMAL PowerShell window (admin not required):

        cd D:\SearchProject
        .\install.ps1

    The project is installed into the folder THIS SCRIPT lives in. It never
    depends on your current directory, so running it from C:\Windows\system32
    or anywhere else is safe.

    ONE question is asked: where to build the index. Skip it with -DataPath.

    On any failure you are asked what to do: Retry / Continue / Abort.
    Use -OnError Continue or -OnError Abort for unattended runs.

    Examples:
        .\install.ps1 -DataPath "E:\KnowledgeGraphData"
        .\install.ps1 -DataPath "E:\KnowledgeGraphData" -OnError Continue -SkipOptional
#>

[CmdletBinding()]
param(
    # Where the index (vectors + FTS + cache + models) is built.
    [string]$DataPath = "",

    # Where the app + venv live. Defaults to this script's own folder.
    [string]$ProjectPath = "",

    # What to do when a step fails: Ask (default), Continue, Abort.
    [ValidateSet("Ask", "Continue", "Abort")]
    [string]$OnError = "Ask",

    # Skip the optional components (rerank model, Ollama, mistral).
    [switch]$SkipOptional,

    # Run only the cheap checks (paths, disk, winget, required files) and stop.
    # Nothing is installed and nothing is downloaded. Takes seconds.
    [switch]$Preflight,

    # Minimum free GB required on the index drive.
    [int]$RequiredFreeGB = 150
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Braille spinners from winget/ollama need a UTF-8 console or they render as mojibake.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$script:Failures     = @()
$script:Skipped      = @()
$script:LogFile      = $null
$script:Transcribing = $false
$script:LastPythonExit = 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Stop-Logging {
    if ($script:Transcribing) {
        try { Stop-Transcript | Out-Null } catch { }
        $script:Transcribing = $false
    }
}

function Exit-Installer {
    param([int]$Code)
    if ($script:LogFile) {
        Write-Host ""
        Write-Host "  Full log: $script:LogFile" -ForegroundColor Gray
    }
    Stop-Logging
    exit $Code
}

function Write-Title($text) {
    Write-Host ""
    Write-Host "  $text" -ForegroundColor White
    Write-Host ("  " + ("-" * $text.Length)) -ForegroundColor DarkGray
}

function Write-SharedComputerNotice {
    # **Owner-approved wording. Do not edit without the owner.** The same
    # paragraph appears in README.md, and a test holds the two to each other -
    # a promise about privacy that says two different things in two places is
    # worse than one that says nothing.
    Write-Host "  Leasha and shared computers." -ForegroundColor White
    Write-Host "  Everything Leasha indexes and everything you search stays on this" -ForegroundColor Gray
    Write-Host "  computer - nothing is ever sent anywhere. On a computer with separate" -ForegroundColor Gray
    Write-Host "  Windows accounts, each account gets its own private index: you find" -ForegroundColor Gray
    Write-Host "  your files, others find theirs, and Windows keeps them apart. On a" -ForegroundColor Gray
    Write-Host "  computer where people share one login, Leasha works like the rest of" -ForegroundColor Gray
    Write-Host "  that login - anyone using it can find anything it can read. If that" -ForegroundColor Gray
    Write-Host "  matters in your home, give each person their own Windows account" -ForegroundColor Gray
    Write-Host "  before installing, or choose the folders Leasha indexes so shared" -ForegroundColor Gray
    Write-Host "  spaces stay shared and private ones stay out." -ForegroundColor Gray
    Write-Host "  Leasha's index contains copies of text from your files - treat the index as being as" -ForegroundColor Gray
    Write-Host "  sensitive as the most sensitive thing you index." -ForegroundColor Gray
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    # PS 5.1's Set-Content -Encoding UTF8 writes a BOM, which breaks some
    # .env parsers. Write it explicitly without one.
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $enc)
}

<#
    Invoke-PythonSnippet runs a short Python program in the project venv.

    It deliberately returns NOTHING and sets $script:LastPythonExit instead.

    A PowerShell function returns everything it emits to the pipeline, not just
    what follows `return`. An earlier version did:

        & $script:Python $tmp     # emits Python's stdout
        return $LASTEXITCODE      # appends the exit code

    which handed the caller @("embedding model ready, dim = 384", 0) rather than
    0 - so a completely successful model download was reported as a failure.
    Routing the child's output to the host keeps the pipeline clean.
#>
function Invoke-PythonSnippet {
    param([string]$Code)

    $tmp = Join-Path $env:TEMP ("lkg_" + [guid]::NewGuid().ToString("N") + ".py")
    $script:LastPythonExit = 1
    try {
        Write-Utf8NoBom -Path $tmp -Content $Code

        # Progress bars (tqdm, huggingface downloads) go to stderr. Under
        # $ErrorActionPreference = "Stop" that can be promoted to a terminating
        # error, so relax it for the duration of the child process only.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $script:Python $tmp 2>&1 | ForEach-Object {
                Write-Host "    $_" -ForegroundColor DarkGray
            }
            $code = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }

        if ($null -eq $code) { $code = 0 }
        $script:LastPythonExit = [int]$code
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

<#
    Invoke-Step runs one installation step.

      -Name      what is being done
      -Action    scriptblock; throw to signal failure
      -Fix       exact command or action that resolves the failure
      -Optional  failure will not block readiness
      -Verify    optional scriptblock returning $true if the step's goal is
                 already satisfied - used to treat "already installed, no
                 upgrade available" as success rather than an error
#>
function Invoke-Step {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Action,
        [Parameter(Mandatory)][string]$Fix,
        [switch]$Optional,
        [scriptblock]$Verify
    )

    if ($Optional -and $SkipOptional) {
        Write-Host "==> $Name" -ForegroundColor Cyan
        Write-Host "    SKIPPED (-SkipOptional)" -ForegroundColor DarkGray
        $script:Skipped += $Name
        return
    }

    $tag = if ($Optional) { " (optional)" } else { "" }

    while ($true) {
        Write-Host "==> $Name$tag" -ForegroundColor Cyan
        $failure = $null
        try {
            & $Action
        } catch {
            $failure = $_.Exception.Message
        }

        # A step can "fail" noisily and still have achieved its goal - the
        # classic case being winget returning a non-zero code because the
        # package is already installed and current.
        if ($failure -and $Verify) {
            try {
                if (& $Verify) {
                    Write-Host "    OK (already satisfied - ignoring: $failure)" -ForegroundColor Green
                    return
                }
            } catch { }
        }

        if (-not $failure) {
            Write-Host "    OK" -ForegroundColor Green
            return
        }

        Write-Host "    ERROR: $failure" -ForegroundColor Red
        Write-Host "    FIX:   $Fix" -ForegroundColor Yellow

        $decision = $OnError
        if ($decision -eq "Ask") {
            if ($Optional) {
                Write-Host "    This component is OPTIONAL - the app works without it." -ForegroundColor DarkGray
                $default = "C"
            } else {
                Write-Host "    This component is REQUIRED - the app will not start without it." -ForegroundColor DarkGray
                $default = "A"
            }
            $answer = Read-Host "    [R]etry after applying the fix / [C]ontinue anyway / [A]bort  (Enter = $default)"
            if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $default }
            $key = $answer.Trim().ToUpper().Substring(0, 1)

            # NOTE: 'continue' inside a switch continues the SWITCH, not the
            # enclosing while loop - so branch with if/else instead.
            if ($key -eq "R") {
                Write-Host "    Retrying..." -ForegroundColor DarkGray
                continue
            } elseif ($key -eq "A") {
                $decision = "Abort"
            } else {
                $decision = "Continue"
            }
        }

        if ($decision -eq "Abort") {
            Write-Host ""
            Write-Host "ABORTED at step: $Name" -ForegroundColor Red
            Write-Host "Apply the FIX above, then re-run this script - completed steps are skipped." -ForegroundColor Yellow
            Exit-Installer 1
        }

        $script:Failures += $Name
        return
    }
}

# ---------------------------------------------------------------------------
# Resolve the project location - NEVER the caller's current directory
# ---------------------------------------------------------------------------

if (-not $ProjectPath) {
    if ($PSScriptRoot) {
        $ProjectPath = $PSScriptRoot
    } else {
        throw "Cannot determine the script location. Re-run with -ProjectPath ""D:\SearchProject""."
    }
}
$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path

$script:Python = Join-Path $ProjectPath "venv\Scripts\python.exe"
$Python  = $script:Python
$ReqFile = Join-Path $ProjectPath "requirements.txt"
$Doctor  = Join-Path $ProjectPath "doctor.py"

# Start logging before anything can fail, so a failed run always leaves evidence.
# Transcripts live in logs\install\ alongside the app's own structured logs.
# See docs/TROUBLESHOOTING.md for what lives where.
$logDir = Join-Path $ProjectPath "logs\install"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$script:LogFile = Join-Path $logDir ("install-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
try {
    Start-Transcript -Path $script:LogFile -Force | Out-Null
    $script:Transcribing = $true
} catch {
    Write-Host "  (Could not start a transcript: $($_.Exception.Message))" -ForegroundColor DarkYellow
    $script:LogFile = $null
}

Write-Title "Local Knowledge Graph V2 - installer"
Write-Host "  Project location : $ProjectPath" -ForegroundColor Gray
Write-Host "  On error         : $OnError" -ForegroundColor Gray
Write-Host "  PowerShell       : $($PSVersionTable.PSVersion) on $([Environment]::OSVersion.VersionString)" -ForegroundColor Gray
if ($script:LogFile) {
    Write-Host "  Logging to       : $script:LogFile" -ForegroundColor Gray
}
if ($Preflight) {
    Write-Host "  Mode             : PREFLIGHT - checks only, nothing will be installed" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Preflight - the things that cannot be auto-fixed
# ---------------------------------------------------------------------------

Invoke-Step -Name "Locate requirements.txt and doctor.py" `
    -Fix "Both files must sit next to install.ps1 in $ProjectPath. Re-download them from the project spec." `
    -Action {
        if (-not (Test-Path -LiteralPath $ReqFile)) { throw "requirements.txt not found in $ProjectPath" }
        if (-not (Test-Path -LiteralPath $Doctor))  { throw "doctor.py not found in $ProjectPath" }
    }

Invoke-Step -Name "Check winget is available" `
    -Fix "Install 'App Installer' from the Microsoft Store, then re-run this script." `
    -Action {
        if (-not (Test-CommandExists "winget")) { throw "winget not found on PATH" }
    }

# ---------------------------------------------------------------------------
# The ONE question: where to build the index
# ---------------------------------------------------------------------------

# **An existing install is left exactly as it is.** If .env already names a
# DATA_PATH, that is the answer: no question, no prompt, no warning, no
# migration. Somebody re-running the installer to repair a venv must not be
# asked where their index lives, and must certainly not be defaulted onto a new
# location that would silently start a second one.
$ExistingDataPath = ""
$EnvFile = Join-Path $ProjectPath ".env"
if ((-not $DataPath) -and (Test-Path -LiteralPath $EnvFile)) {
    foreach ($line in (Get-Content -LiteralPath $EnvFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*DATA_PATH\s*=\s*(.+?)\s*$') {
            $candidate = $Matches[1].Trim().Trim('"')
            if ($candidate) {
                $ExistingDataPath = $candidate
                $DataPath = $candidate
            }
            break
        }
    }
}

if ($ExistingDataPath) {
    Write-Title "Index location"
    Write-Host "  Keeping the location already in .env: $ExistingDataPath" -ForegroundColor Gray
    Write-Host "  Nothing is moved and nothing is re-indexed." -ForegroundColor DarkGray
}

if (-not $DataPath) {
    Write-Title "Index location"
    Write-Host "  The index (vectors + full-text + cache + models) is large." -ForegroundColor Gray
    Write-Host "  Needs ~${RequiredFreeGB}GB free for a 100GB corpus, ideally on an SSD." -ForegroundColor Gray
    Write-Host ""
    Write-SharedComputerNotice
    Write-Host ""
    # **Per-account by default.** %LOCALAPPDATA% is ACL'd to this Windows
    # account, so two people on one machine get two private indexes with no
    # extra machinery - Windows' own permissions do the separating. Any other
    # path is still accepted; this is the default, not a restriction.
    $default = Join-Path $env:LOCALAPPDATA "Leasha"
    $answer  = Read-Host "  Index location [Enter for $default]"
    if ([string]::IsNullOrWhiteSpace($answer)) {
        $DataPath = $default
    } else {
        $DataPath = $answer.Trim().Trim('"')
    }
}

Invoke-Step -Name "Validate index location: $DataPath" `
    -Fix "Pick a path on an existing local drive with ${RequiredFreeGB}GB+ free, then re-run: .\install.ps1 -DataPath ""E:\KnowledgeGraphData""" `
    -Action {
        $root = [System.IO.Path]::GetPathRoot($DataPath)
        if (-not $root) { throw "'$DataPath' is not an absolute path (expected something like E:\KnowledgeGraphData)" }
        if (-not (Test-Path -LiteralPath $root)) { throw "Drive $root does not exist" }

        New-Item -ItemType Directory -Force -Path $DataPath | Out-Null

        # Prove it is actually writable, not just present
        $probe = Join-Path $DataPath ".write_test"
        Set-Content -LiteralPath $probe -Value "ok"
        Remove-Item -LiteralPath $probe -Force

        # Space check on the CHOSEN drive, not the app drive.
        # $root looks like "E:\" - take the letter; TrimEnd overloads are
        # unreliable on Windows PowerShell 5.1.
        $letter = $root.Substring(0, 1)
        $free   = (Get-PSDrive -Name $letter).Free / 1GB
        if ($free -lt $RequiredFreeGB) {
            throw ("Only {0:N0}GB free on $root - need ${RequiredFreeGB}GB for a 100GB corpus" -f $free)
        }
        Write-Host ("    {0:N0}GB free on $root" -f $free) -ForegroundColor DarkGray
    }

# ---------------------------------------------------------------------------
# Preflight stop - everything above is cheap and local. Everything below
# installs software or downloads gigabytes. This is the natural place to stop
# and confirm the setup is sane before committing to that.
# ---------------------------------------------------------------------------

if ($Preflight) {
    Write-Title "Preflight report"
    $checks = @(
        @{ Name = "winget";            Value = (Test-CommandExists "winget") },
        @{ Name = "Python launcher";   Value = (Test-CommandExists "py") },
        @{ Name = "git";               Value = (Test-CommandExists "git") },
        @{ Name = "ollama (optional)"; Value = (Test-CommandExists "ollama") },
        @{ Name = "venv already built"; Value = (Test-Path -LiteralPath $Python) },
        @{ Name = ".env already written"; Value = (Test-Path -LiteralPath (Join-Path $ProjectPath ".env")) }
    )
    foreach ($c in $checks) {
        $mark = if ($c.Value) { "yes" } else { "no " }
        Write-Host ("    {0,-22} {1}" -f $c.Name, $mark) -ForegroundColor Gray
    }
    Write-Host ""
    if ($script:Failures.Count -gt 0) {
        Write-Host "  PREFLIGHT FAILED - fix the items above before a real run." -ForegroundColor Red
        Exit-Installer 1
    }
    Write-Host "  PREFLIGHT PASSED - re-run without -Preflight to install." -ForegroundColor Green
    Exit-Installer 0
}

# ---------------------------------------------------------------------------
# Runtime installs
#
# winget returns a non-zero exit code when a package is already installed and
# no upgrade is available (-1978335189 = UPDATE_NOT_APPLICABLE). That is not a
# failure. Every install step therefore carries a -Verify block that checks
# whether the command is actually usable; if it is, the exit code is ignored.
# ---------------------------------------------------------------------------

Invoke-Step -Name "Install Python 3.12" `
    -Fix "Run manually: winget install --id Python.Python.3.12 -e   (or install from python.org and tick 'Add python.exe to PATH')" `
    -Verify { Update-SessionPath; Test-CommandExists "py" } `
    -Action {
        if (Test-CommandExists "py") {
            Write-Host "    Already present" -ForegroundColor DarkGray
            return
        }
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget exited with code $LASTEXITCODE" }
        Update-SessionPath
    }

Invoke-Step -Name "Install Git" `
    -Fix "Run manually: winget install --id Git.Git -e" `
    -Verify { Update-SessionPath; Test-CommandExists "git" } `
    -Action {
        if (Test-CommandExists "git") {
            Write-Host "    Already present" -ForegroundColor DarkGray
            return
        }
        winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget exited with code $LASTEXITCODE" }
        Update-SessionPath
    }

Update-SessionPath

# ---------------------------------------------------------------------------
# Project scaffolding
# ---------------------------------------------------------------------------

Invoke-Step -Name "Create project folders and virtual environment" `
    -Fix "Confirm Python works: py -3.12 --version   - then delete the venv folder and re-run." `
    -Action {
        foreach ($d in @("app", "logs")) {
            New-Item -ItemType Directory -Force -Path (Join-Path $ProjectPath $d) | Out-Null
        }
        foreach ($d in @("vectors", "fts", "cache", "models", "state")) {
            New-Item -ItemType Directory -Force -Path (Join-Path $DataPath $d) | Out-Null
        }

        if (-not (Test-Path -LiteralPath $Python)) {
            Push-Location $ProjectPath
            try {
                py -3.12 -m venv venv
                if ($LASTEXITCODE -ne 0) { throw "py -3.12 -m venv venv exited with code $LASTEXITCODE" }
            } finally {
                Pop-Location
            }
        } else {
            Write-Host "    venv already exists - reusing" -ForegroundColor DarkGray
        }

        if (-not (Test-Path -LiteralPath $Python)) { throw "venv creation failed - $Python not found" }

        $ver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
        Write-Host "    venv Python $ver" -ForegroundColor DarkGray
        if ([version]$ver -lt [version]"3.12") { throw "venv is Python $ver - 3.12+ required" }
    }

Invoke-Step -Name "Write .env with the chosen index location" `
    -Fix "Create $ProjectPath\.env by hand containing at least: DATA_PATH=$DataPath" `
    -Action {
        # VECTOR_PATH, FTS_DB, CACHE_PATH, MODEL_CACHE and STATE_PATH are
        # deliberately NOT written here. They used to be, pinned absolutely to
        # the chosen drive - and because .env always beats a default, that made
        # DATA_PATH meaningless: changing it moved nothing, because all five
        # subdirectories still resolved to the old location. config.py derives
        # them from DATA_PATH when they are absent, which is what lets the index
        # be moved. Anyone needing one somewhere unusual can still set it, and
        # then owns keeping it correct.
        $envText = @"
# Generated by install.ps1 on $(Get-Date -Format "yyyy-MM-dd HH:mm")
DATA_PATH=$DataPath
PROJECT_PATH=$ProjectPath
LOG_PATH=$ProjectPath\logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_ENABLED=true

# RERANK_MODEL is deliberately NOT set here.
#
# It was, as BAAI/bge-reranker-base, and that pinned every installed copy to
# the slowest of the four measured models - 9.2x slower than the default, for
# identical scores on the evaluation corpus. Changing the default in the code
# then did nothing for anybody who had run this installer, because a value in
# .env always wins. The measurement said "9.2x faster" and no machine got it.
#
# Leaving it unset lets the code default apply, so a model chosen on a later
# measurement reaches existing installs. Set it only to override deliberately:
#   RERANK_MODEL=BAAI/bge-reranker-base    slower, no better on what was tested

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=5
REQUIRED_FREE_GB=$RequiredFreeGB
"@
        Write-Utf8NoBom -Path (Join-Path $ProjectPath ".env") -Content $envText
    }

# ---------------------------------------------------------------------------
# Work order 202626130120 (0t) section 4: repair, not just install.
#
# The owner's venv on 2026-09-12 hit WinError 5 mid-uninstall because a
# running Leasha process held onnxruntime.dll open. That left stash
# directories pip could not finish removing (site-packages\~nnxruntime\
# and similar) and an onnxruntime package with no __init__.py - which still
# IMPORTS, as an empty namespace package, so the failure was invisible until
# something tried to call it. Both are checked and cleared before a single
# package is touched, so a repair run does not repeat the same failure.
# ---------------------------------------------------------------------------

Invoke-Step -Name "Check no Leasha process is holding the venv" `
    -Fix "Close Leasha - the window, and any 'python.exe -m app.cli' or 'python.exe -m app.main' command using this venv - then re-run this script." `
    -Action {
        if (Test-Path -LiteralPath $Python) {
            $holding = Get-Process -Name "python" -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -and ($_.Path -eq $Python) }
            if ($holding) {
                throw ("{0} process(es) are running from this venv's python.exe and " +
                       "would block a package reinstall (WinError 5)." -f @($holding).Count)
            }
        }
    }

Invoke-Step -Name "Clear stash directories left by a failed uninstall" `
    -Fix "Delete venv\Lib\site-packages\~* by hand, then re-run this script." `
    -Action {
        $siteDir = Join-Path $ProjectPath "venv\Lib\site-packages"
        if (Test-Path -LiteralPath $siteDir) {
            $stashes = Get-ChildItem -LiteralPath $siteDir -Filter "~*" -ErrorAction SilentlyContinue
            foreach ($stash in $stashes) {
                Write-Host ("    Removing stale stash: {0}" -f $stash.Name) -ForegroundColor DarkGray
                Remove-Item -LiteralPath $stash.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

Invoke-Step -Name "Install Python packages" `
    -Fix "Run it verbosely to see which package failed: `"$Python`" -m pip install -r `"$ReqFile`" --verbose   - every pin ships a Windows wheel, so no compiler is needed." `
    -Action {
        & $Python -m pip install --upgrade pip --quiet
        if ($LASTEXITCODE -ne 0) { throw "pip self-upgrade failed with code $LASTEXITCODE" }
        & $Python -m pip install -r $ReqFile
        if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.txt failed with code $LASTEXITCODE" }
    }

# ---------------------------------------------------------------------------
# Models - required search model first, optional rerank second
# ---------------------------------------------------------------------------

Invoke-Step -Name "Download embedding model bge-small-en-v1.5 (~130MB)" `
    -Fix "Needs internet once; the app is fully offline afterwards. Check connectivity or your proxy, then re-run - completed steps are skipped." `
    -Action {
        $code = @"
import os
os.environ['FASTEMBED_CACHE_PATH'] = r'$DataPath\models'
from fastembed import TextEmbedding
m = TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir=r'$DataPath\models')
v = list(m.embed(['warmup']))[0]
print('embedding model ready, dim =', len(v))
"@
        Invoke-PythonSnippet -Code $code
        if ($script:LastPythonExit -ne 0) {
            throw "model download or warm-up failed (python exit code $script:LastPythonExit)"
        }
    }

Invoke-Step -Name "Download rerank model bge-reranker-base (~1.1GB)" -Optional `
    -Fix "Reranking is a quality boost and a toggle in Settings. Re-run install.ps1 later to fetch it." `
    -Action {
        $code = @"
import os
os.environ['FASTEMBED_CACHE_PATH'] = r'$DataPath\models'
from fastembed.rerank.cross_encoder import TextCrossEncoder
m = TextCrossEncoder('BAAI/bge-reranker-base', cache_dir=r'$DataPath\models')
print('rerank scores:', list(m.rerank('warmup query', ['warmup document'])))
"@
        Invoke-PythonSnippet -Code $code
        if ($script:LastPythonExit -ne 0) {
            throw "model download or warm-up failed (python exit code $script:LastPythonExit)"
        }
    }

# ---------------------------------------------------------------------------
# Optional: DirectML GPU acceleration (CPU fallback always available)
# ---------------------------------------------------------------------------
#
# Work order 202626130120 (0t) section 3. This used to ask [y/N] before
# installing the DirectML wheel, which meant GPU support only ever worked
# because that answer happened to be given, and a later 'no' left the
# machine five times slower with nothing reminding anyone. There is no
# prompt any more: on a Windows machine with a display adapter, the pinned
# onnxruntime-directml wheel is installed unconditionally, last, with
# --force-reinstall --no-deps so it always wins the site-packages
# directory regardless of what the plain requirements.txt install did -
# see requirements.txt's own comment for why the two pins must match.
# The provider is verified immediately afterwards and the step fails
# loudly (not silently) if it is still absent.

$hasAdapter = $false

$code = @"
try:
    import sys
    if sys.platform != 'win32':
        print('False')
        sys.exit(0)
    import subprocess
    result = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command',
         'Get-CimInstance Win32_VideoController -ErrorAction Stop'],
        capture_output=True, text=True, timeout=5)
    print('True' if result.returncode == 0 and result.stdout else 'False')
except Exception:
    print('False')
"@

$tmp = Join-Path $env:TEMP ("adapter_check_" + [guid]::NewGuid().ToString("N") + ".py")
try {
    Write-Utf8NoBom -Path $tmp -Content $code
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Python $tmp 2>&1 | Out-String
        $hasAdapter = $output.Trim() -eq "True"
    } finally {
        $ErrorActionPreference = $previous
    }
} catch {
    $hasAdapter = $false
} finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

if ($hasAdapter -and -not $SkipOptional -and -not $Preflight) {
    Invoke-Step -Name "Install onnxruntime-directml (GPU acceleration)" -Optional `
        -Fix "Close Leasha first - a running process holding onnxruntime.dll is why this fails (WinError 5). Then: venv\Scripts\python.exe -m pip install --force-reinstall --no-deps onnxruntime-directml==1.24.4" `
        -Action {
            & $Python -m pip install --force-reinstall --no-deps onnxruntime-directml==1.24.4
            if ($LASTEXITCODE -ne 0) { throw "pip install onnxruntime-directml exited with code $LASTEXITCODE" }

            $providerCheck = & $Python -c "import onnxruntime; print('DmlExecutionProvider' in onnxruntime.get_available_providers())"
            if ($LASTEXITCODE -ne 0) { throw "onnxruntime could not be imported after installing the DirectML wheel" }
            if ($providerCheck.Trim() -ne "True") {
                throw "onnxruntime-directml installed, but DmlExecutionProvider is still not available - the wheel did not take effect"
            }
        }
} elseif (-not $Preflight) {
    Write-Host "  No display adapter detected - onnxruntime-directml is not installed; the processor is used." -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# Optional: Ollama (RAG answers + entity extraction only - never in search)
# ---------------------------------------------------------------------------

Invoke-Step -Name "Install Ollama" -Optional `
    -Fix "Skip it, or install later: winget install --id Ollama.Ollama -e" `
    -Verify { Update-SessionPath; Test-CommandExists "ollama" } `
    -Action {
        if (Test-CommandExists "ollama") {
            Write-Host "    Already present" -ForegroundColor DarkGray
            return
        }
        winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget exited with code $LASTEXITCODE" }
        Update-SessionPath
    }

Invoke-Step -Name "Download LLM model: mistral (~4.1GB)" -Optional `
    -Fix "The previous run stalled at 'pulling manifest', which almost always means the Ollama background service was not up yet. Start it (run 'ollama serve' in another window, or launch the Ollama tray app) and then run: ollama pull mistral" `
    -Action {
        if (-not (Test-CommandExists "ollama")) { throw "Ollama is not installed - nothing to pull" }

        # A freshly installed Ollama has no service listening yet, so the pull
        # hangs on 'pulling manifest' and dies. Wait for the API first.
        $ready = $false
        for ($i = 1; $i -le 30; $i++) {
            try {
                Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null
                $ready = $true
                break
            } catch {
                if ($i -eq 1) {
                    Write-Host "    Waiting for the Ollama service to start (up to 60s)..." -ForegroundColor DarkGray
                    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
                }
                Start-Sleep -Seconds 2
            }
        }
        if (-not $ready) {
            throw "Ollama is installed but nothing is listening on 127.0.0.1:11434 after 60s"
        }

        ollama pull mistral
        if ($LASTEXITCODE -ne 0) { throw "ollama pull mistral exited with code $LASTEXITCODE" }
    }

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

Write-Title "Final verification (doctor.py)"
$doctorExit = 0
if (Test-Path -LiteralPath $Doctor) {
    Push-Location $ProjectPath
    try {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            # Write-Host, not bare output: the transcript recorded nothing at
            # all from a plain `& $Python $Doctor`, which hid the one report
            # that matters most.
            & $Python $Doctor 2>&1 | ForEach-Object { Write-Host $_ }
            $doctorExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }
    } finally {
        Pop-Location
    }
if ($null -eq $doctorExit) { $doctorExit = 0 }
} else {
    Write-Host "  doctor.py missing - cannot verify." -ForegroundColor Red
    $doctorExit = 1
}

# ---------------------------------------------------------------------------
# Tab completion - the one optional question, asked after everything works
# ---------------------------------------------------------------------------
#
# **Asked, never assumed.** A PowerShell profile is somebody's own file, and
# writing to it uninvited is the kind of thing that gets an application
# uninstalled. Declining costs a convenience and nothing else, and
# `leasha completions install --path $PROFILE --remove` undoes it later.
#
# No administrator rights: a profile lives under the user's own Documents,
# which is the same rule `add-to-path.ps1` follows.

# `-SkipOptional` already means "do not ask me about the extras", and this is
# one; `-Preflight` checks without changing anything, so it must not write to a
# profile either.
if (-not $SkipOptional -and -not $Preflight) {
    Write-Title "Tab completion (optional)"
    Write-Host "  Press Tab after `leasha ` to complete filters and their values" -ForegroundColor Gray
    Write-Host "  from what is actually in your index. Adds two lines to your" -ForegroundColor Gray
    Write-Host "  PowerShell profile; nothing else on the machine changes." -ForegroundColor Gray
    Write-Host ""
    $answer = Read-Host "  Set it up? [y/N]"
    if ($answer -match '^(y|yes)$') {
        Invoke-Step -Name "Install tab completion into `$PROFILE" -Optional `
            -Fix "Run it by hand later: venv\Scripts\python.exe -m app.cli completions install --path `$PROFILE" `
            -Action {
                & "$ProjectPath\venv\Scripts\python.exe" -m app.cli `
                    completions install --path $PROFILE
                if ($LASTEXITCODE -ne 0) { throw "completions install exited $LASTEXITCODE" }
            }
    } else {
        Write-Host "  Skipped. `leasha completions install --path `$PROFILE` sets it up later." -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# leasha:// links - the second optional question. Adoptions section 7a.
# ---------------------------------------------------------------------------
#
# **Asked, never assumed**, for the same reason tab completion is: registering
# a URL scheme changes how the whole machine treats a kind of link, and doing
# that to somebody without telling them is how an application gets uninstalled.
#
# **Per-user, so no administrator rights** - HKCU\Software\Classes, the same
# rule `add-to-path.ps1` and the completer both follow. `leasha open
# unregister` removes it.
#
# **There is no uninstaller in this repository yet** - packaging is Layer 9 -
# so the removal is a command rather than something that happens for you. The
# order's note says so rather than promising a script nobody has written; the
# hook belongs to L9 and `unregister()` is waiting for it.

if (-not $SkipOptional -and -not $Preflight) {
    Write-Title "leasha:// links (optional)"
    Write-Host "  Lets a shortcut or a link open Leasha on a search:" -ForegroundColor Gray
    Write-Host "    leasha://search?q=safety%20report" -ForegroundColor Gray
    Write-Host "  Adds one key under your own registry. Remove it with" -ForegroundColor Gray
    Write-Host "  '.\leasha open unregister'." -ForegroundColor Gray
    Write-Host ""
    $answer = Read-Host "  Set it up? [y/N]"
    if ($answer -match '^(y|yes)$') {
        Invoke-Step -Name "Register the leasha:// scheme" -Optional `
            -Fix "Run it by hand later: .\leasha open register" `
            -Action {
                & "$ProjectPath\venv\Scripts\python.exe" -m app.cli open register
                if ($LASTEXITCODE -ne 0) { throw "open register exited $LASTEXITCODE" }
            }
    } else {
        Write-Host "  Skipped. '.\leasha open register' sets it up later." -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# Nightly system loop - the third optional question. Order 0m section 5b.
# ---------------------------------------------------------------------------
#
# **Never silently, the work order's own words.** A daily task that indexes a
# scale fixture and runs the built-in evaluation is a real, if small, cost
# every night - CPU, a few minutes, a log file growing by one line - and
# deciding that for somebody is exactly what tab completion and leasha://
# links above both ask first.
#
# **Per-user, no administrator rights.** `schtasks /create` without `/ru`
# registers under the account running this installer, the same boundary the
# tab-completion and leasha:// steps both keep - `Get-ScheduledTask` and
# `Unregister-ScheduledTask` both see it without elevation because it is not
# a machine-wide task.
#
# `/sc daily /st 02:00`: two in the morning, so it runs unattended rather
# than fighting the owner for CPU during the day; `pythonw.exe` so no console
# window flashes open once a day for no reason - the exact fix `leasha.cmd`
# already applies to the GUI launch, for the same reason.

if (-not $SkipOptional -and -not $Preflight) {
    Write-Title "Nightly system loop (optional)"
    Write-Host "  Once a day, indexes a small fixture corpus and runs the built-in" -ForegroundColor Gray
    Write-Host "  evaluation, so a regression shows up within a day instead of at the" -ForegroundColor Gray
    Write-Host "  next release. Appends one line to logs\nightly.log; 'leasha doctor'" -ForegroundColor Gray
    Write-Host "  shows when it last passed. Runs under your own account, no admin" -ForegroundColor Gray
    Write-Host "  rights needed. Remove it later with:" -ForegroundColor Gray
    Write-Host "    schtasks /delete /tn `"Leasha nightly system loop`" /f" -ForegroundColor Gray
    Write-Host ""
    $answer = Read-Host "  Set it up? [y/N]"
    if ($answer -match '^(y|yes)$') {
        Invoke-Step -Name "Register the nightly scheduled task" -Optional `
            -Fix ("Run it by hand later: schtasks /create /tn `"Leasha nightly system loop`" " +
                  "/tr `"$ProjectPath\venv\Scripts\pythonw.exe $ProjectPath\tools\nightly.py`" " +
                  "/sc daily /st 02:00 /f") `
            -Action {
                $taskCommand = "`"$ProjectPath\venv\Scripts\pythonw.exe`" `"$ProjectPath\tools\nightly.py`""
                & schtasks /create /tn "Leasha nightly system loop" /tr $taskCommand `
                    /sc daily /st 02:00 /f
                if ($LASTEXITCODE -ne 0) { throw "schtasks /create exited $LASTEXITCODE" }
            }
    } else {
        Write-Host "  Skipped. Set it up by hand later:" -ForegroundColor DarkGray
        Write-Host "    schtasks /create /tn `"Leasha nightly system loop`" /tr `"$ProjectPath\venv\Scripts\pythonw.exe $ProjectPath\tools\nightly.py`" /sc daily /st 02:00 /f" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Title "Summary"
Write-Host "  Project : $ProjectPath" -ForegroundColor Gray
Write-Host "  Index   : $DataPath" -ForegroundColor Gray

if ($script:Skipped.Count -gt 0) {
    Write-Host "  Skipped by request:" -ForegroundColor DarkGray
    $script:Skipped | ForEach-Object { Write-Host "    - $_" -ForegroundColor DarkGray }
}

if ($script:Failures.Count -gt 0) {
    Write-Host ""
    Write-Host "  COMPLETED WITH $($script:Failures.Count) FAILURE(S):" -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "    - $_" -ForegroundColor Red }
    Write-Host "  Apply the FIX lines above, then re-run this script." -ForegroundColor Yellow
    Exit-Installer 1
}

if ($doctorExit -ne 0) {
    Write-Host ""
    Write-Host "  Every install step succeeded, but doctor.py reports the environment is NOT READY." -ForegroundColor Yellow
    Write-Host "  Apply the FIX lines it printed, then re-run: `"$Python`" doctor.py" -ForegroundColor Yellow
    Exit-Installer 1
}

Write-Host ""
Write-Host "  ALL STEPS SUCCEEDED - environment verified." -ForegroundColor Green
Write-Host ""
Write-Host "  Start it with:" -ForegroundColor Green
Write-Host "    cd $ProjectPath"
Write-Host "    .\leasha"
Write-Host ""
Write-Host "  The .\ is required by PowerShell, which does not run commands from" -ForegroundColor Gray
Write-Host "  the current folder. To drop it and use 'leasha' from anywhere:" -ForegroundColor Gray
Write-Host "    .\add-to-path.ps1" -ForegroundColor Gray
Write-Host ""
Write-Host "  Worth running once each:" -ForegroundColor Green
Write-Host "    .\leasha commands            the filters you can type in the search box"
Write-Host "    .\leasha evaluate --builtin  proves search works, in about ten seconds"
Write-Host "    .\leasha formats             which file types are indexed, and which are off"
Write-Host "    .\leasha open register       make leasha:// links open Leasha"
Exit-Installer 0
