# finish-cleanup.ps1
# Removes the orphaned worktree directories under .claude\worktrees that were
# verified as holding no content absent from git.
#
# Verification already performed:
#   - every file in each directory below was hashed with git hash-object
#     (same CRLF normalisation git uses) and confirmed present in the object
#     database, OR copied to _rescued-from-worktrees\<name>\ first.
#   - 336 files were rescued that way. Review that folder before deleting it.
#
# The eight worktrees sitting on still-unmerged branches are NOT in this list
# and are left untouched.

$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot '.claude\worktrees'

$safe = @(
  'friendly-wozniak-932cfc'
  'great-dirac-c14fcd'
  'lane-a-0907'
  'lane-b-0907'
  'lane-c-0907'
  'lane-d-0907'
  'lane-e-0907'
  'mcp-list-bd809b'
  'merge-indexing-settings-3775be'
  'outlook-indexing-resilience-76ed15'
  'outstanding-work-bugs-7edba5'
  'silly-swartz-d5b7bf'
  'ultra-modern-ui-design-b5b14a'
)

# Worktrees deliberately kept (still-unmerged branches):
#   folder-contents-0e7ad3, funny-goldwasser-26a1dc,
#   indexing-mechanism-ui-fixes-054aec, jeff-queue-behavior-18b8e8,
#   order-0i-0j-resume, priceless-pasteur-a24e3d,
#   recursing-margulis-7e1937, reverent-ramanujan-ffc0f4

if (-not (Test-Path $root)) {
    Write-Host "No .claude\worktrees directory found. Nothing to do."
    return
}

foreach ($name in $safe) {
    $path = Join-Path $root $name
    if (Test-Path $path) {
        Write-Host "Removing $name ..." -NoNewline
        Remove-Item -LiteralPath $path -Recurse -Force
        Write-Host " done"
    } else {
        Write-Host "Already gone: $name"
    }
}

Write-Host ""
Write-Host "Remaining under .claude\worktrees (kept on purpose):"
Get-ChildItem -LiteralPath $root -Directory | Select-Object -ExpandProperty Name

Write-Host ""
Write-Host "Next: git worktree prune, then review _rescued-from-worktrees\"
