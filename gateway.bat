@echo off
setlocal
title BMS Gateway - central to HIS
cd /d "%~dp0"

rem --- load the shared database settings ---
call config.bat

rem --- find Python ---
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [!] Python not found - install Python to run the gateway.
  echo.
  pause
  exit /b
)

:menu
cls
echo ==========================================
echo   BMS Gateway   central DB  --^>  HIS
echo ==========================================
echo   Central : %BMS_DB_DRIVER%  %BMS_DB_USER%@%BMS_DB_HOST%:%BMS_DB_PORT%/%BMS_DB_NAME%  table=%BMS_DB_TABLE%
echo   HIS     : %BMS_HIS_DRIVER%  %BMS_HIS_USER%@%BMS_HIS_HOST%:%BMS_HIS_PORT%/%BMS_HIS_NAME%  table=%BMS_HIS_TABLE%
echo   (edit config.bat to change these)
echo.
echo   -- appointment queue (%BMS_DB_TABLE% --^> oapp) --
echo   [1] status         check config + connect both databases
echo   [2] preview        show what would be written (NO writes)
echo   [3] sync           write pending rows into HIS now
echo.
echo   -- open visit queue (refer_open_visit --^> ovst) --
echo   [4] visit preview  show which visits would be opened (NO writes)
echo   [5] visit sync     open the visits now (INSERT into ovst)
echo.
echo   [6] service        sync BOTH queues every 60 seconds (Ctrl+C to stop)
echo   [7] UI             open the settings + monitor window
echo   [0] exit
echo.
set /p "CH=Select: "
echo.

if "%CH%"=="1" %PY% gateway.py status
if "%CH%"=="2" %PY% gateway.py preview
if "%CH%"=="3" %PY% gateway.py sync
if "%CH%"=="4" %PY% gateway.py visit-preview
if "%CH%"=="5" %PY% gateway.py visit-sync
if "%CH%"=="6" %PY% gateway.py sync-all --watch 60
if "%CH%"=="7" start "" %PY% gateway_ui.py
if "%CH%"=="0" exit /b

echo.
pause
goto menu
