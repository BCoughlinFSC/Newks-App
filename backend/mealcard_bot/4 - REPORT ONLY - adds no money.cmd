@echo off
REM Export Paytronix loads and build both payroll workbooks. No wallet adjustments.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run Meal Card Reload.ps1" -ReportOnly
if errorlevel 1 (
  echo.
  echo Report generation failed. Do not rerun the reload to retry a report.
  pause
)
