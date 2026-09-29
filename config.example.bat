@echo off
rem ============================================================
rem   DATABASE SETTINGS  --  edit HERE once.
rem   Used by both start.bat (web app + save API) and
rem   gateway.bat (central -> HIS sync, gateway.py).
rem
rem   DRIVER can be:   mysql   or   pgsql
rem   (do NOT put setlocal in this file - callers need the vars)
rem
rem   SECRETS (user / password / api key)
rem   -----------------------------------
rem   They are stored ENCRYPTED in the *_ENC lines (AES-256-GCM,
rem   key file: config.key). Manage them with the UI
rem   (gateway_ui.bat) or run:  python bmscrypt.py encrypt-config
rem   A plain value is used only when its *_ENC line is empty.
rem   Keep config.key on this machine - never commit or share it.
rem ============================================================

rem  --- 1) CENTRAL DB (source: appointment queue from the tool) ---
set "BMS_DB_DRIVER=mysql"
set "BMS_DB_HOST="
set "BMS_DB_PORT=3306"
set "BMS_DB_NAME="
set "BMS_DB_USER="
set "BMS_DB_PASS="
set "BMS_DB_TABLE=appointment_queue"

rem  --- 2) HIS DB (destination: HOSxP oapp table) ---
set "BMS_HIS_DRIVER=pgsql"
set "BMS_HIS_HOST="
set "BMS_HIS_PORT=6432"
set "BMS_HIS_NAME="
set "BMS_HIS_USER="
set "BMS_HIS_PASS="
set "BMS_HIS_TABLE=oapp"

rem  HOSxP login name written to lab_app_head.order_staff / lab_app_order.staff
rem  (leave empty to fall back to the recorder name from the tool)
set "BMS_HIS_STAFF="

rem  Optional: require Authorization: Bearer <token> on the save API
set "BMS_API_KEY="

set "BMS_DB_USER_ENC="
set "BMS_DB_PASS_ENC="
set "BMS_HIS_USER_ENC="
set "BMS_HIS_PASS_ENC="
set "BMS_API_KEY_ENC="

set "BMS_AUTO_SYNC=1"
set "BMS_AUTO_INTERVAL=10"

rem  --- 3) MOPH Alert: send appointment card to patient LINE OA / Mor Prom app ---
rem  OFF by default (BMS_MOPH_ALERT=0). Easiest to edit in gateway_ui (Settings tab, box 3).
rem  AUTH=key  : CLIENT_KEY + SECRET_KEY from moph-alert.moph.go.th (MOPH Alert v3.1)
rem  AUTH=user : USER + PASS + HOSPCODE of MOPH Account Center with role MOPH_Alert_API (v2)
rem  ENV = uat or prod. Plain values move into the *_ENC lines with:
rem      python bmscrypt.py encrypt-config
rem  Before turning on:  gateway alert-test --cid YOUR_OWN_13_DIGIT_ID
rem                      gateway alert-preview
set "BMS_MOPH_ALERT=0"
set "BMS_MOPH_ALERT_ENV=uat"
set "BMS_MOPH_ALERT_AUTH="
set "BMS_MOPH_ALERT_CLIENT_KEY="
set "BMS_MOPH_ALERT_CLIENT_KEY_ENC="
set "BMS_MOPH_ALERT_SECRET_KEY="
set "BMS_MOPH_ALERT_SECRET_KEY_ENC="
set "BMS_MOPH_ALERT_HOSPCODE="
set "BMS_MOPH_ALERT_USER="
set "BMS_MOPH_ALERT_USER_ENC="
set "BMS_MOPH_ALERT_PASS="
set "BMS_MOPH_ALERT_PASS_ENC="
set "BMS_MOPH_ALERT_PHONE="

set "BMS_SYNC_BACK_DAYS=0"
