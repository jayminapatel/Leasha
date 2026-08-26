<#
    setup-github.ps1 - put this repository on GitHub, once, safely.

    Everything here was checked against the repository on 2026-08-26 before it
    was written: `.env` is already gitignored and holds no secrets, `venv`,
    `logs`, `Backup` and `Leasha` are ignored, and the whole history is 2.68 MiB
    across 144 commits. What remains is tidying and the push itself.

    **Reports by default. Changes nothing without -Apply.** Creating a remote
    repository and deleting files are both one-way enough to deserve being
    read first, which is the same rule the rest of this project applies to
    anything destructive.

    Usage:

        .\scripts\setup-github.ps1                     what it would do
        .\scripts\setup-github.ps1 -Apply              do it, private repo
        .\scripts\setup-github.ps1 -Apply -Public      do it, public repo
        .\scripts\setup-github.ps1 -Apply -SkipTidy    push without tidying

    Requires: git. GitHub CLI (gh) is used when present; without it the script
    stops before the push and prints the two commands to run by hand.
#>
[CmdletBinding()]
param(
    [switch] $Apply,
    [switch] $Public,
    [switch] $SkipTidy,
    [string] $RepoName = "SearchProject",
    [string] $Branch = ""
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project

$problems = New-Object System.Collections.Generic.List[string]
$actions  = New-Object System.Collections.Generic.List[string]

function Say([string] $text) { Write-Host $text }
function Head([string] $text) { Write-Host ""; Write-Host $text; Write-Host ("-" * $text.Length) }

Head "Where this is"
Say  "  project    $project"
Say  "  branch     $(git rev-parse --abbrev-ref HEAD)"
Say  "  commits    $(git rev-list --count HEAD)"
Say  "  pack size  $((git count-objects -vH | Select-String 'size-pack').ToString().Trim())"

# --- 1. nothing secret may leave the machine --------------------------------
#
# The check that matters most, and the cheapest to get wrong. `.env` names the
# corpus and the index location; it is gitignored today and this asserts that
# it still is, every time, rather than trusting a file nobody has read lately.
Head "Secrets"
if (git ls-files --error-unmatch .env 2>$null) {
    $problems.Add(".env is TRACKED - it must be gitignored before any push")
} else {
    Say "  .env is not tracked"
}
$secretish = git grep -I -l -i -E "password|secret|api[_-]?key|token|Bearer " -- ":!*.md" ":!docs/*" 2>$null
if ($secretish) {
    Say "  files mentioning a credential word (read them, most are harmless):"
    $secretish | ForEach-Object { Say "    $_" }
} else {
    Say "  no credential words in tracked non-documentation files"
}

# --- 2. things that should not ship -----------------------------------------
Head "Tidying"
$strays = @(
    @{ Path = "big.pst";            Why = "5MB fixture of the letter x, referenced by nothing" },
    @{ Path = "docs\feature_.md";   Why = "stray file, fails 3 tests in test_docs_versioned" },
    @{ Path = "forget-repo.py";     Why = "one-off workaround, superseded by repos --forget" }
)
foreach ($stray in $strays) {
    if (Test-Path $stray.Path) {
        Say "  remove  $($stray.Path)  - $($stray.Why)"
        $actions.Add("remove $($stray.Path)")
    }
}
# A Windows path used as a relative directory name. Something wrote it; it is
# not tracked, and it is confusing enough to be worth clearing out.
$literal = Join-Path $project "D:\KnowledgeGraphData"
if (Test-Path -LiteralPath $literal) {
    Say "  remove  D:\KnowledgeGraphData (a literal folder name inside the project)"
    $actions.Add("remove the literal path folder")
}

# --- 3. line endings --------------------------------------------------------
#
# **This project has a non-negotiable about .ps1 encoding**, and a collaborator
# with a different core.autocrlf is exactly how that gets broken silently. The
# index is all LF today; .gitattributes is what keeps it that way.
Head "Line endings"
if (Test-Path ".gitattributes") {
    Say "  .gitattributes exists"
} else {
    Say "  create .gitattributes - without it a different core.autocrlf can rewrite"
    Say "  every file on clone, and the installer's BOM rule is encoding-sensitive"
    $actions.Add("create .gitattributes")
}

# --- 4. uncommitted work ----------------------------------------------------
Head "Working tree"
$dirty = git status --porcelain
if ($dirty) {
    $count = ($dirty | Measure-Object).Count
    Say "  $count uncommitted change(s). They will NOT be pushed unless committed."
    $dirty | Select-Object -First 12 | ForEach-Object { Say "    $_" }
    if ($count -gt 12) { Say "    ... and $($count - 12) more" }
    $problems.Add("$count uncommitted change(s) - commit or stash before pushing")
} else {
    Say "  clean"
}

# --- 5. leftover git temp objects -------------------------------------------
$garbage = git count-objects -v 2>&1 | Select-String "garbage"
if ($garbage) {
    Say "  loose temp objects present (interrupted git operations) - gc will clear them"
    $actions.Add("git gc")
}

# --- report or act ----------------------------------------------------------
if (-not $Apply) {
    Head "This was a report"
    if ($problems.Count) {
        Say "  Fix first:"
        $problems | ForEach-Object { Say "    - $_" }
    }
    if ($actions.Count) {
        Say "  Would do:"
        $actions | ForEach-Object { Say "    - $_" }
    }
    Say ""
    Say "  Re-run with -Apply to make the changes and push."
    exit 0
}

if ($problems.Count) {
    Head "Refusing to continue"
    $problems | ForEach-Object { Say "  - $_" }
    Say ""
    Say "  Nothing has been changed."
    exit 1
}

Head "Applying"

if (-not $SkipTidy) {
    foreach ($stray in $strays) {
        if (Test-Path $stray.Path) {
            if (git ls-files --error-unmatch $stray.Path 2>$null) {
                git rm -q -- $stray.Path
            } else {
                Remove-Item -Force -- $stray.Path
            }
            Say "  removed $($stray.Path)"
        }
    }
    if (Test-Path -LiteralPath $literal) {
        Remove-Item -Recurse -Force -LiteralPath $literal
        Say "  removed the literal path folder"
    }
}

if (-not (Test-Path ".gitattributes")) {
    # ASCII only, and no BOM needed here - git reads it as bytes.
    $lines = @(
        "# Normalise to LF in the repository. Checkout follows the platform,",
        "# except where a file's encoding is load-bearing.",
        "* text=auto eol=lf",
        "",
        "# PowerShell 5.1 reads a BOM-less file as the ANSI codepage, which has",
        "# already killed the installer once. These stay exactly as committed.",
        "*.ps1 text eol=crlf working-tree-encoding=UTF-8",
        "*.cmd text eol=crlf",
        "",
        "# Binary, so git never tries to translate anything inside them.",
        "*.png binary",
        "*.jpg binary",
        "*.ico binary",
        "*.pst binary",
        "*.zip binary",
        "*.onnx binary"
    )
    Set-Content -Path ".gitattributes" -Value $lines -Encoding ascii
    git add .gitattributes
    Say "  created .gitattributes"
}

if ($actions -contains "git gc") {
    git gc --quiet --prune=now
    Say "  ran git gc"
}

if (git status --porcelain) {
    git commit -q -m "chore: tidy for publication - remove fixtures, add .gitattributes"
    Say "  committed the tidy"
}

# --- the remote -------------------------------------------------------------
Head "GitHub"

if ($Branch) {
    git branch -M $Branch
    Say "  branch renamed to $Branch"
}
$current = git rev-parse --abbrev-ref HEAD

if (git remote get-url origin 2>$null) {
    Say "  origin already exists: $(git remote get-url origin)"
    git push -u origin $current
    Say "  pushed $current"
    exit 0
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Say "  GitHub CLI (gh) is not installed, so this script stops here."
    Say ""
    Say "  Either install it - winget install GitHub.cli - and re-run,"
    Say "  or create the repository on github.com and then run:"
    Say ""
    Say "    git remote add origin https://github.com/<you>/$RepoName.git"
    Say "    git push -u origin $current"
    exit 0
}

$visibility = if ($Public) { "--public" } else { "--private" }
Say "  creating $RepoName ($(if ($Public) { 'public' } else { 'private' })) and pushing $current"
gh repo create $RepoName $visibility --source . --remote origin --push
Say ""
Say "  Done. From here: git push after each session."
