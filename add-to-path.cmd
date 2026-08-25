@echo off
REM Put `leasha` on your PATH so it works from any folder.
REM
REM Double-click this, or run it from a terminal. It calls the PowerShell
REM script beside it with the execution policy bypassed for this one run -
REM the same approach run-install.cmd uses, and for the same reason: a default
REM Windows install refuses to run an unsigned .ps1 at all.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0add-to-path.ps1" %*
echo.
pause
