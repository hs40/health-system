@echo off
REM ============================================================
REM Health System - daily sync (Garmin -> local -> Tredict)
REM Called by Windows Task Scheduler; can also be double-clicked.
REM Arguments are passed through, e.g.:
REM   daily.cmd --profile activity
REM   daily.cmd --profile health
REM   daily.cmd --profile weekly
REM   daily.cmd --profile all --dry-run
REM NOTE: keep this file ASCII-only. cmd.exe reads .cmd as GBK,
REM       so UTF-8 Chinese comments would break parsing.
REM ============================================================
setlocal
set "REPO=%~dp0"
set "PY=%REPO%backend\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] venv python not found: %PY%
  echo         Run: cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\pip install -e .
  exit /b 1
)
"%PY%" -u "%REPO%backend\scripts\daily_sync.py" %*
exit /b %ERRORLEVEL%
