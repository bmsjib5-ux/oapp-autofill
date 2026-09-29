@echo off
setlocal
title BMS Appointment Autofill v3
cd /d "%~dp0"

rem --- load the shared database settings (edit config.bat, not this file) ---
call config.bat

set "PORT=8137"
set "PAGE=appointment-autofill-v3.html"
set "URL=http://localhost:%PORT%/%PAGE%"
set "API=http://localhost:%PORT%/save_appointment.php"

echo ==========================================
echo   BMS Appointment Autofill v3
echo ==========================================
echo.

rem --- Is the port already taken by an old server window? ---
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>nul && (
  echo [!] Port %PORT% is ALREADY IN USE by another server.
  echo     An old start.bat window is probably still running.
  echo     Close it ^(press Ctrl+C in that window^) and run this again,
  echo     otherwise the browser may talk to the OLD server.
  echo.
  pause
  exit /b
)

rem --- Pick an engine. All three serve the page AND the API. ---
rem     server.exe -> standalone build  (needs nothing installed; preferred)
rem     python     -> server.py         (same API in Python, no PHP needed)
rem     php        -> php -S            (fallback)
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"

set "KIND="
if exist "%~dp0server.exe" set "KIND=exe"
if not defined KIND if exist "%~dp0dist\server.exe" set "KIND=exedist"
if not defined KIND if defined PY if exist "%~dp0server.py" set "KIND=python"
if not defined KIND where php >nul 2>nul && set "KIND=php"

if not defined KIND (
  echo [!] No engine found - opening the file directly in the browser.
  echo     The API will NOT work in file:// mode. Install PHP or Python,
  echo     or put server.exe next to this file.
  start "" "%~dp0%PAGE%"
  echo.
  pause
  exit /b
)

echo App URL     : %URL%
echo Register    : http://localhost:%PORT%/appointment-register.html
echo Appt center : http://localhost:%PORT%/appointment-center.html
echo Lab import  : http://localhost:%PORT%/lab-import-dashboard.html
echo Insight     : http://localhost:%PORT%/insight-dashboard.html
if /I "%KIND%"=="php" (
  echo Save API    : %API%
  echo Central DB  : %BMS_DB_DRIVER% %BMS_DB_USER%@%BMS_DB_HOST%:%BMS_DB_PORT%/%BMS_DB_NAME%  table=%BMS_DB_TABLE%
)
echo.
echo Gateway     : run gateway.bat  ^(central DB -^> HIS, written in Python^)
echo Stop server : press Ctrl+C in this window
echo.

if /I "%KIND%"=="exedist" (
  echo Sync latest web files into dist\ ...
  copy /Y "%~dp0appointment-autofill-v3.html" "%~dp0dist\appointment-autofill-v3.html" >nul
  copy /Y "%~dp0appointment-register.html" "%~dp0dist\appointment-register.html" >nul
  copy /Y "%~dp0appointment-center.html" "%~dp0dist\appointment-center.html" >nul
  copy /Y "%~dp0lab-import-dashboard.html" "%~dp0dist\lab-import-dashboard.html" >nul
  copy /Y "%~dp0insight-dashboard.html" "%~dp0dist\insight-dashboard.html" >nul
  copy /Y "%~dp0server.py" "%~dp0dist\server.py" >nul
  copy /Y "%~dp0gateway.py" "%~dp0dist\gateway.py" >nul
)

rem --- Open the browser after a short delay (server needs a moment) ---
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%URL%'"

rem --- Run the web page + API (Ctrl+C to stop) ---
if /I "%KIND%"=="php" (
  echo Engine: PHP built-in server - web page AND API in one.
  echo.
  php -S localhost:%PORT%
)
if /I "%KIND%"=="python" (
  echo Engine: Python server.py - web page AND API in one ^(no PHP needed^).
  echo.
  %PY% "%~dp0server.py" %PORT%
)
if /I "%KIND%"=="exe" (
  echo Engine: server.exe - web page AND API in one ^(nothing to install^).
  echo.
  "%~dp0server.exe" %PORT%
)
if /I "%KIND%"=="exedist" (
  echo Engine: dist\server.exe - web page AND API in one ^(nothing to install^).
  echo.
  "%~dp0dist\server.exe" %PORT%
)

echo.
echo Server stopped.
pause
