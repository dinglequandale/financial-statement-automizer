@echo off
setlocal
rem ---------------------------------------------------------------------------
rem  Financial Statement Automizer -- double-click this to open the window.
rem  Nothing here needs editing.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py
if "%PY%"=="" (where python >nul 2>nul && set PY=python)

if "%PY%"=="" (
  echo.
  echo   Python is not installed on this computer, so the tool cannot start.
  echo.
  echo   Install it once from https://www.python.org/downloads/ ,
  echo   tick "Add Python to PATH" during setup, then run Install.cmd
  echo   in this folder, and this launcher will work from then on.
  echo.
  pause
  exit /b 1
)

%PY% -m fsa.gui
if errorlevel 1 (
  echo.
  echo   The tool did not start. If this is the first time, run Install.cmd
  echo   in this folder once, then try again.
  echo.
  pause
)
endlocal
