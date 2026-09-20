<#
    install-nightly.ps1 - opt in to (or out of) the nightly system loop.

    Order 0m section 5b. Registers `tools\nightly.py` as a Windows scheduled
    task so the owner does not have to remember to run it. NEVER run by
    anything else: registering a recurring task is a standing configuration
    change on this machine, so it happens only when the owner runs this
    script, once, on purpose. Nothing in the installer, the tests or the
    application calls it.

    The nightly indexes a fixture corpus, runs the built-in evaluation and
    checks the pinned performance floors, then appends ONE line to
    logs\nightly.log. `leasha doctor` reads that line and shows when the
    nightly last passed.

    Usage (from the project folder):

        .\scripts\install-nightly.ps1 -WhatIf         show what would be registered; change nothing
        .\scripts\install-nightly.ps1                 register it (asks first)
        .\scripts\install-nightly.ps1 -Time 03:30     register it for another time of day
        .\scripts\install-nightly.ps1 -Status         is it registered, when did it last run
        .\scripts\install-nightly.ps1 -Uninstall      remove it (asks first)
        .\scripts\install-nightly.ps1 -Uninstall -WhatIf

    -DryRun is the same as -WhatIf. -Confirm:$false skips the question, for a
    script that has already asked (type it at a PowerShell prompt; `powershell
    -File` passes '$false' as text and Windows PowerShell 5.1 rejects it).

    Runs under YOUR account, only while you are logged on (no password is
    stored and no administrator rights are needed). If the machine was off or
    asleep at the time, it runs when it is next available. It stops itself
    after 3 hours. It uses pythonw.exe, so no console window opens.

    Exit codes: 0 done (or nothing to do), 1 could not do it (this includes an
    argument PowerShell itself rejects, such as -Time 25:99), 2 contradictory
    switches (-Uninstall with -Status).

    ASCII only, saved UTF-8 with a BOM (non-negotiable #7).
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$DryRun,
    [ValidatePattern('^([01]?[0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$Time = "02:00",
    [string]$ProjectPath = "",
    [string]$TaskName = "Leasha nightly system loop"
)

$ErrorActionPreference = "Stop"

if ($Uninstall -and $Status) {
    Write-Host "Choose one of -Uninstall or -Status, not both." -ForegroundColor Red
    exit 2
}

if (-not $ProjectPath) { $ProjectPath = Split-Path -Parent $PSScriptRoot }
$ProjectPath = [System.IO.Path]::GetFullPath($ProjectPath)

$pythonw = Join-Path $ProjectPath "venv\Scripts\pythonw.exe"
$script = Join-Path $ProjectPath "tools\nightly.py"
$logFile = Join-Path $ProjectPath "logs\nightly.log"

# -DryRun is the friendlier spelling of -WhatIf; both leave the machine alone.
$dry = $DryRun.IsPresent -or $WhatIfPreference

function Get-Existing {
    # Reading a task changes nothing, so it is safe in a dry run.
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return $null }
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Show-LastNightly {
    if (Test-Path -LiteralPath $logFile) {
        $last = Get-Content -LiteralPath $logFile -Tail 1
        Write-Host "  Last line of logs\nightly.log: $last"
    } else {
        Write-Host "  logs\nightly.log does not exist yet - the nightly has not run."
    }
}

# --- -Status ----------------------------------------------------------------

if ($Status) {
    $task = Get-Existing
    if (-not $task) {
        Write-Host "Not registered: no scheduled task called '$TaskName'."
        Write-Host "  Register it with: .\scripts\install-nightly.ps1   (add -WhatIf to look first)"
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Registered: '$TaskName' ($($task.State))"
        Write-Host "  Last run   : $($info.LastRunTime)   result: $($info.LastTaskResult)"
        Write-Host "  Next run   : $($info.NextRunTime)"
    }
    Show-LastNightly
    exit 0
}

# --- -Uninstall -------------------------------------------------------------

if ($Uninstall) {
    $task = Get-Existing
    if (-not $task) {
        Write-Host "Nothing to remove: no scheduled task called '$TaskName'."
        exit 0
    }
    if ($dry) {
        Write-Host "What if: would remove the scheduled task '$TaskName'. Nothing was changed."
        exit 0
    }
    if ($PSCmdlet.ShouldProcess($TaskName, "Remove the scheduled task")) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the scheduled task '$TaskName'. logs\nightly.log is kept."
    } else {
        Write-Host "Not removed."
    }
    exit 0
}

# --- install ----------------------------------------------------------------

$problems = @()
if (-not (Test-Path -LiteralPath $pythonw)) { $problems += "missing $pythonw - run run-install.cmd first" }
if (-not (Test-Path -LiteralPath $script)) { $problems += "missing $script" }
if ($problems.Count -gt 0 -and -not $dry) {
    foreach ($p in $problems) { Write-Host "Cannot register: $p" -ForegroundColor Red }
    exit 1
}

Write-Host "Nightly system loop"
Write-Host "  Task name  : $TaskName"
Write-Host "  Runs       : every day at $Time, under your own account, while you are logged on"
Write-Host "  Command    : `"$pythonw`" `"$script`""
Write-Host "  Working in : $ProjectPath"
Write-Host "  Result     : one line appended to $logFile"
Write-Host "  Remove it  : .\scripts\install-nightly.ps1 -Uninstall"
foreach ($p in $problems) { Write-Host "  Note       : $p" -ForegroundColor Yellow }

$existing = Get-Existing
if ($existing) { Write-Host "  Already registered - this would replace it." }

if ($dry) {
    Write-Host "What if: would register the scheduled task '$TaskName'. Nothing was changed."
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register a daily scheduled task at $Time")) {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $ProjectPath
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Leasha nightly system loop (tools\nightly.py). Remove with scripts\install-nightly.ps1 -Uninstall." `
        -Force | Out-Null
    Write-Host "Registered '$TaskName' for $Time daily."
    Show-LastNightly
} else {
    Write-Host "Not registered."
}
exit 0
