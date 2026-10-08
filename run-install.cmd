@echo off
rem ---------------------------------------------------------------------------
rem  run-install.cmd - the reliable way to start the installer.
rem
rem  Double-click it, or run it from any prompt. It does three things a bare
rem  ".\install.ps1" does not:
rem
rem    1. Bypasses the execution policy for this one process, so a Restricted
rem       or AllSigned machine policy cannot block the script.
rem    2. Parse-checks install.ps1 with the real PowerShell parser FIRST, so a
rem       syntax or encoding fault is reported instead of failing silently.
rem    3. Keeps the window open at the end so nothing scrolls away.
rem
rem  Any arguments are passed straight through, e.g.
rem     run-install.cmd -Preflight
rem     run-install.cmd -DataPath "E:\KnowledgeGraphData" -SkipOptional
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

echo.
echo === Step 1 of 2: parse-checking the scripts
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\parse-check.ps1"
rem  "if errorlevel 1" is true for exit code 1 OR HIGHER - any failure, not only 1.
if errorlevel 1 goto :parsefail

echo.
echo === Step 2 of 2: running the installer
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo Installer finished successfully.
) else (
    echo Installer exited with code %RC%. See the log path printed above.
)
echo.
pause
exit /b %RC%

:parsefail
echo.
echo ============================================================
echo  The scripts did not parse. The installer was NOT started.
echo  Details: logs\parse-check.log
echo ============================================================
echo.
pause
exit /b 1
