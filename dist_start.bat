@echo off
setlocal
title BMS Appointment Autofill v3
cd /d "%~dp0"

set "PORT=8137"
set "PAGE=appointment-autofill-v3.html"
set "URL=http://localhost:%PORT%/%PAGE%"
set "REGURL=http://localhost:%PORT%/appointment-register.html"

echo ==========================================
echo   BMS Appointment Autofill v3
echo ==========================================
echo.

rem --- Is the port already taken by an old server window? ---
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>nul && (
  echo [!] Port %PORT% is ALREADY IN USE - an old window is probably still running.
  echo     Close it ^(Ctrl+C^) and run this again.
  echo.
  pause
  exit /b
)

rem --- Pick an engine. Prefer server.exe; fall back to Python if the exe is
rem     missing or broken (e.g. a bad build / antivirus). Both serve page + API. ---
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"

set "KIND="
if exist "%~dp0server.exe" set "KIND=exe"
if not defined KIND if defined PY if exist "%~dp0server.py" set "KIND=python"

if not defined KIND (
  echo [!] No engine found. Need server.exe in this folder,
  echo     or Python installed with server.py next to it.
  echo.
  pause
  exit /b
)

echo App URL     : %URL%
echo Register    : %REGURL%
echo Lab import  : http://localhost:%PORT%/lab-import-dashboard.html
echo Gateway     : run gateway_ui.exe  ^(central DB -^> HIS^)
echo Settings    : edit config.bat  ^(or use gateway_ui.exe^)
echo Stop server : press Ctrl+C in this window
echo.

start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%URL%'"

if /I "%KIND%"=="exe" (
  echo Engine: server.exe
  echo.
  server.exe %PORT%
  rem  If the exe dies instantly (bad build), errorlevel is non-zero -> try Python.
  if errorlevel 1 if defined PY if exist "%~dp0server.py" (
    echo.
    echo [!] server.exe failed to run - falling back to Python...
    echo.
    %PY% "%~dp0server.py" %PORT%
  )
)
if /I "%KIND%"=="python" (
  echo Engine: Python server.py  ^(server.exe not found^)
  echo.
  %PY% "%~dp0server.py" %PORT%
)

echo.
echo Server stopped.
pause
