@echo off
REM ===================================================================
REM  Meal Card Reload Bot - THE REAL RUN.  DOUBLE-CLICK THIS FILE.
REM
REM  *** THIS ONE ADDS MONEY TO THE CARDS. ***
REM
REM  Use "2 - TEST RUN - adds no money" first if you are unsure.
REM  This run offers you a dry run before it starts, too.
REM ===================================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run Meal Card Reload.ps1"
if errorlevel 1 (
  echo.
  echo The reload did not finish. Read the messages above.
  pause
)
