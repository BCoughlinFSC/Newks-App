@echo off
REM ===================================================================
REM  Meal Card Reload Bot - TEST RUN.  DOUBLE-CLICK THIS FILE.
REM
REM  Reads every card balance and works out what it WOULD load.
REM  It never fills in the Paytronix form and never adds money.
REM  Safe to run any time, including before a reload is due.
REM ===================================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0TEST RUN - adds no money.ps1"
if errorlevel 1 (
  echo.
  echo The test did not finish. Read the messages above.
  pause
)
