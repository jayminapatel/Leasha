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

if (-not $DataPath) {
    Write-Title "Index location"
    Write-Host "  The index (vectors + full-text + cache + models) is large." -ForegroundColor Gray
    Write-Host "  Needs ~${RequiredFreeGB}GB free for a 100GB corpus, ideally on an SSD." -ForegroundColor Gray
    Write-Host ""
    $default = "D:\KnowledgeGraphData"
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
        $envText = @"
# Generated by install.ps1 on $(Get-Date -Format "yyyy-MM-dd HH:mm")
DATA_PATH=$DataPath
VECTOR_PATH=$DataPath\vectors
FTS_DB=$DataPath\fts\knowledge.db
CACHE_PATH=$DataPath\cache
MODEL_CACHE=$DataPath\models
STATE_PATH=$DataPath\state
PROJECT_PATH=$ProjectPath
LOG_PATH=$ProjectPath\logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_MODEL=BAAI/bge-reranker-base
RERANK_ENABLED=true

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=5
REQUIRED_FREE_GB=$RequiredFreeGB
"@
        Write-Utf8NoBom -Path (Join-Path $ProjectPath ".env") -Content $envText
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
Exit-Installer 0
