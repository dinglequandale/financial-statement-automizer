@echo off
setlocal
rem ---------------------------------------------------------------------------
rem  One-time setup. Run this once, then use "Launch Automizer.cmd" from then on.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py
if "%PY%"=="" (where python >nul 2>nul && set PY=python)

if "%PY%"=="" (
  echo.
  echo   Python is not installed. Get it from https://www.python.org/downloads/
  echo   and tick "Add Python to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

echo Installing. This takes a minute and only happens once.
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install -e .
if errorlevel 1 (
  echo.
  echo   Setup did not finish. Send the text above to whoever maintains the tool.
  echo.
  pause
  exit /b 1
)

echo.
echo   Done. Double-click "Launch Automizer.cmd" to start.
echo.
echo   One more thing: the suggestions need a key. Create a file called .env
echo   in this folder containing one line:
echo.
echo       ANTHROPIC_API_KEY=sk-ant-...
echo.
echo   Without it everything still runs; you just fill in more rows by hand.
echo.
pause
endlocal
