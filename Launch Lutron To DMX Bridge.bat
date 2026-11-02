@echo off
rem Double-click this file (Windows) to start Lutron To DMX Bridge from this folder: it opens a log window and the
rem web GUI in your browser. Needs Python 3.10+ from https://www.python.org/downloads/ (tick "Add to PATH").
rem After updating the code, delete the .venv folder so the requirements are installed again.
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
set "PYW=.venv\Scripts\pythonw.exe"

if not exist "%PY%" (
  echo First run: setting up Lutron To DMX Bridge, this takes a minute...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if errorlevel 1 goto nopython
)
if not exist ".venv\.lutron-to-dmx-bridge-installed" (
  echo Installing Lutron To DMX Bridge and its requirements...
  "%PY%" -m pip install --quiet -e .
  if errorlevel 1 goto pipfail
  echo ok> ".venv\.lutron-to-dmx-bridge-installed"
)

"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo This Python has no tkinter, so there is no log window; the log shows here instead.
  "%PY%" -m lutron_to_dmx_bridge.launcher %*
  pause
  exit /b
)
start "" "%PYW%" -m lutron_to_dmx_bridge.launcher %*
exit /b

:nopython
echo.
echo Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH") and double-click this file again.
pause
exit /b 1

:pipfail
echo.
echo Installing the requirements failed. Are you online?
pause
exit /b 1
