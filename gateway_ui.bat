@echo off
setlocal
title BMS Gateway UI
cd /d "%~dp0"

rem --- load shared database settings (the UI reads/writes config.bat too) ---
call config.bat

set "PYW="
where pyw >nul 2>nul && set "PYW=pyw"
if not defined PYW where pythonw >nul 2>nul && set "PYW=pythonw"
if not defined PYW where py >nul 2>nul && set "PYW=py"
if not defined PYW where python >nul 2>nul && set "PYW=python"

if not defined PYW (
  echo [!] Python not found - install Python to run the gateway UI.
  echo.
  pause
  exit /b
)

rem  pyw/pythonw open the window without a console box
start "" %PYW% gateway_ui.py
exit /b
