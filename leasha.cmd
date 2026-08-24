@echo off
REM Leasha - search everything on this machine.
REM
REM   leasha              open the window
REM   leasha --debug      open it and record the session to logs\sessions\
REM   leasha stats        anything else is passed to the command line tool
REM
REM Exists so nobody has to remember `venv\Scripts\python.exe -m app.main`.
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo Leasha is not installed yet. Run run-install.cmd first.
  exit /b 1
)

REM No arguments, or only --debug, means open the window. Anything else is a
REM command for the CLI - so `leasha stats` and `leasha formats` just work.
if "%~1"=="" goto gui
if "%~1"=="--debug" goto gui

venv\Scripts\python.exe -m app.cli %*
exit /b %ERRORLEVEL%

:gui
venv\Scripts\python.exe -m app.main %*
exit /b %ERRORLEVEL%
