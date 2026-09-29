@echo off
setlocal
title Build BMS exe
cd /d "%~dp0"

echo ==========================================
echo   Build BMS Appointment Autofill
echo ==========================================
echo.

where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [!] Python not found - cannot build.
  pause
  exit /b
)

%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo [!] PyInstaller not installed. Run:  %PY% -m pip install pyinstaller
  pause
  exit /b
)

set "OPTS=--noconfirm --clean --onefile --hidden-import pymysql --hidden-import psycopg2 --hidden-import cryptography.hazmat.primitives.ciphers.aead"

rem  A running server.exe/gateway.exe can't be overwritten -> PyInstaller produces
rem  a CORRUPT exe (fails at boot: "No module named pyimod02_importers").
rem  Close any running instances first.
taskkill /f /im server.exe     >nul 2>nul
taskkill /f /im gateway.exe    >nul 2>nul
taskkill /f /im gateway_ui.exe >nul 2>nul

echo [1/4] server.exe     ^(web page + API^)
%PY% -m PyInstaller %OPTS% --console   --name server     server.py     >nul 2>&1 || echo    FAILED

echo [2/4] gateway.exe    ^(CLI sync^)
%PY% -m PyInstaller %OPTS% --console   --name gateway    gateway.py    >nul 2>&1 || echo    FAILED

echo [3/4] gateway_ui.exe ^(monitor + settings^)
%PY% -m PyInstaller %OPTS% --windowed  --name gateway_ui gateway_ui.py >nul 2>&1 || echo    FAILED

echo [4/4] copying page + config into dist
rem  IMPORTANT: the .html files are NOT compiled into the exe - they must be copied,
rem  otherwise dist keeps serving an old version of the tool.
for %%F in (appointment-autofill-v3.html appointment-register.html appointment-center.html lab-import-dashboard.html insight-dashboard.html slot-admin.html) do (
  if exist "%%F" copy /y "%%F" "dist\" >nul
)
rem  slip.js = the appointment-slip builder, shared by the autofill page and the
rem  appointment center. It is loaded with <script src="slip.js">, so if it is
rem  missing from dist the Print button dies at the counter. Keep it in sync.
if exist "slip.js" (copy /y "slip.js" "dist\" >nul) else (echo    [!] slip.js MISSING - printing will fail)
rem  .php files are NOT copied into dist on purpose: dist runs server.exe / server.py
rem  (no php engine) which answer these API paths natively. Left out to keep dist lean
rem  and avoid shipping unused PHP source. (If you ever run "php -S" inside dist, copy
rem  save_appointment.php / sql_proxy.php / bmscrypt.php there by hand.)
if exist "config.bat" copy /y "config.bat" "dist\" >nul
if exist "config.key" copy /y "config.key" "dist\" >nul
if exist "dist_start.bat" copy /y "dist_start.bat" "dist\start.bat" >nul

rem  Python fallback: if server.exe ever fails (bad build/antivirus), start.bat
rem  runs server.py instead - but only on a PC that has Python installed.
for %%F in (server.py gateway.py gateway_ui.py bmscrypt.py) do (
  if exist "%%F" copy /y "%%F" "dist\" >nul
)

rem  clean build leftovers
rmdir /s /q build >nul 2>&1
del /q server.spec gateway.spec gateway_ui.spec >nul 2>&1
rmdir /s /q __pycache__ >nul 2>&1

echo.
echo Done. Files in dist:
dir /b dist
echo.
echo Copy the whole "dist" folder to the target PC and run start.bat there.
echo.
pause
