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
REM **pythonw.exe, not python.exe** - the windowed build of the interpreter.
REM
REM python.exe is the console-subsystem binary, so Windows attached a terminal
REM to every session: an empty black rectangle sitting behind the application
REM for as long as it ran, which nobody asked for and nobody could close
REM without killing Leasha with it.
REM
REM pythonw has no stdout or stderr at all, so `setup_logging` skips the console
REM sink and Settings grows a "Recent activity" pane instead. Removing the
REM console without that would have removed the only running commentary there
REM was, which is the thing somebody looks at when the application seems stuck.
REM
REM `start ""` detaches, so this .cmd returns immediately rather than holding
REM its own console open for the life of the window - which would have defeated
REM the whole exercise.
REM
REM The fallback matters: an installation where pythonw is missing must still
REM open, with a console, rather than not open at all.
if exist "venv\Scripts\pythonw.exe" (
  start "" venv\Scripts\pythonw.exe -m app.main %*
  exit /b 0
)
venv\Scripts\python.exe -m app.main %*
exit /b %ERRORLEVEL%
