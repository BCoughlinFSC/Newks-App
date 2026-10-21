@echo off
REM ===================================================================
REM  Meal Card Reload Bot - first-time setup.  DOUBLE-CLICK THIS FILE.
REM ===================================================================
REM  Why this .cmd exists instead of just running setup.ps1 directly:
REM  Windows blocks PowerShell scripts that came from the internet, so
REM  double-clicking the .ps1 does nothing or shows a red error. A .cmd
REM  is not blocked, and -ExecutionPolicy Bypass applies to this single
REM  run only - it changes no setting on the machine.
REM ===================================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
  echo.
  echo Setup did not finish. Read the messages above.
  pause
)
