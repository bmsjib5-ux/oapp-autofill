#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GATEWAY : ดึงนัดจาก "ฐานกลาง" -> เขียนเข้า "ฐาน HIS" (ตาราง oapp)
รองรับทั้ง PostgreSQL และ MySQL ทั้งสองฝั่ง

สถานะในฐานกลางที่ gateway หยิบไปทำ:
  ตาราง appointment_queue (คิวนัด)
    pending      นัดใหม่จากหน้าทำนัด    -> INSERT oapp + ใบสั่ง lab
    lab_pending  นัดที่มีอยู่แล้วใน HIS  -> เพิ่มเฉพาะใบสั่ง lab แนบ oapp_id เดิม (จากหน้าทะเบียนนัด)
  ตาราง refer_open_visit (คิวเปิด visit จากหน้าทะเบียนนัด refer)
    pending      คนไข้ส่งต่อที่ยังไม่มี VN -> INSERT ovst แล้วเขียน vn กลับเข้าคิว
                 (ลงนัดใน oapp ไม่ได้จนกว่าจะมี VN เพราะ oapp.vn เป็น NOT NULL)

ตั้งค่าผ่าน environment variables (ตั้งไว้ใน config.bat):
  ฐานกลาง : BMS_DB_DRIVER  (mysql|pgsql), BMS_DB_HOST/PORT/NAME/USER/PASS/TABLE
  ฐาน HIS : BMS_HIS_DRIVER (mysql|pgsql), BMS_HIS_HOST/PORT/NAME/USER/PASS/TABLE

วิธีใช้:
  python gateway.py status              ตรวจ config + เชื่อมต่อ 2 ฐาน + นับ pending
  python gateway.py preview             ดูว่าจะเขียนอะไรเข้า HIS (ไม่เขียนจริง)
  python gateway.py sync                เขียนเข้า HIS จริง + อัปเดตสถานะในฐานกลาง
  python gateway.py sync --watch 60     ทำงานวนทุก 60 วินาที (โหมด service)
  python gateway.py visit-preview       ดูว่าจะเปิด visit ให้ใครบ้าง (ไม่เขียนจริง)
  python gateway.py visit-sync          เปิด visit จริง (INSERT ovst)
  python gateway.py sync-all            ทำทั้งคิวนัดและคิวเปิด visit ในรอบเดียว
                                        (ใช้กับ --watch ได้ทั้งคู่)
  python gateway.py alert-preview       ดูว่าจะแจ้งนัดเข้าหมอพร้อมให้ใครบ้าง (ไม่ส่งจริง)
  python gateway.py alert-sync          แจ้งนัดเข้าหมอพร้อมตอนนี้ (sync ทำให้เองทุกรอบถ้า BMS_MOPH_ALERT=1)
  python gateway.py alert-test --cid N  ส่งการ์ดทดสอบไปที่เลขบัตรนี้ (--oapp ID = ใช้เนื้อหานัดจริงใบนั้น)
                                        ยืนยันตัวตนด้วย client-key/secret-key (v3.1) หรือ user/password (v2)
  ตัวเลือก: --limit N (ค่าเริ่มต้น 50) · --force (ข้ามการตรวจรหัสก่อนเขียน)

ก่อนเขียนจะตรวจก่อนว่ารหัส lab/x-ray ในคิวมีอยู่ในฐาน HIS ปลายทางจริงไหม
ถ้าตรงน้อยผิดปกติ (มักเพราะ BMS_HIS_* ชี้คนละฐานกับที่หน้าเว็บใช้) จะหยุดแล้วแจ้งสาเหตุ
"""

import os
import re
import sys
import json
import time
import datetime
import argparse
import uuid
import base64
import hashlib
import hmac
import http.client
import urllib.error
import urllib.parse
import urllib.request
from html import escape as html_escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import bmscrypt          # ถอดรหัส user/pass จาก config.bat (<NAME>_ENC)
except Exception:
    bmscrypt = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _secret(name):
    """อ่านค่าลับ: ใช้ <name>_ENC (เข้ารหัส) ก่อน ถ้าไม่มีค่อยใช้ <name> (plaintext)"""
    if bmscrypt is not None:
        return bmscrypt.env_secret(name)
    return os.getenv(name) or ""


def load_config_if_needed():
    """ถ้ายังไม่มี env (เช่นรัน gateway.exe ตรง ๆ ไม่ได้ผ่าน config.bat)
       -> อ่าน config.bat ที่วางอยู่ข้าง ๆ เข้ามาเอง"""
    if os.getenv("BMS_DB_NAME") or os.getenv("BMS_HIS_NAME"):
        return False                       # config.bat ตั้ง env มาแล้ว -> ใช้ของเดิม
    if bmscrypt is None:
        return False
    try:
        vals = bmscrypt.read_config()
    except Exception:
        return False
    for k, v in vals.items():
        os.environ.setdefault(k, v)
    return bool(vals)

OAPP_FIELDS = ["hn", "vn", "vstdate", "nextdate", "nexttime", "nexttime_end",
               "clinic", "depcode", "spclty", "doctor", "note",
               "app_cause", "app_user", "contact_point", "lab_list_text", "xray_list_text",
               "referin_vn",
               # ช่องคิวที่เลือกจากหน้าทำนัด (opd_qs_slot.opd_qs_slot_id)
               # ไม่ได้เลือก -> ไม่มีคีย์นี้ใน payload -> ไม่ถูกใส่ใน INSERT (ปล่อยเป็น NULL ตามเดิม)
               "opd_qs_slot_id"]
REQUIRED = ["hn", "vn", "clinic", "nextdate"]     # oapp: NOT NULL ในสคีมา HOSxP
# สถานะนัดที่เพิ่งสร้าง — อ้างอิงตาราง oapp_status ของ HOSxP
#   1=รอให้ถึงวันนัด  2=มารับบริการแล้ว  3=ไม่มาตามนัด  4=ยกเลิก
OAPP_STATUS_NEW = 1
# นัดที่ถูกยกเลิกใน HOSxP (แถวยังอยู่ แค่เปลี่ยนสถานะ) — dedup ต้องข้ามสถานะนี้
# ไม่งั้น "ยกเลิกแล้วนัดใหม่วันเดิม" จะถูกมองว่ามีนัดแล้ว -> insert ไม่ได้
# ปรับได้ด้วย env BMS_HIS_CANCEL_STATUS ถ้า HOSxP ชุดนี้ใช้รหัสยกเลิกคนละเลข
OAPP_STATUS_CANCELLED = int(os.getenv("BMS_HIS_CANCEL_STATUS") or 4)

# แพทย์ที่ลงในใบสั่ง lab (lab_app_head.doctor_code) — รพ. กำหนดให้ใช้รหัสเดียวคงที่
# แก้ได้ที่ config.bat ผ่าน BMS_HIS_LAB_DOCTOR ถ้าต้องการเปลี่ยน
LAB_DOCTOR_CODE = os.getenv("BMS_HIS_LAB_DOCTOR") or "9757"

# หยิบคิวย้อนหลังกี่วัน (0 = เฉพาะที่สร้างวันนี้ = พฤติกรรมเดิม)
# ถ้าวันไหน gateway ไม่ได้เปิด คิวของวันนั้นจะค้าง pending ถาวรเพราะไม่มีใครหยิบอีกเลย
# ตั้งถาวรที่ config.bat -> BMS_SYNC_BACK_DAYS=7  หรือสั่งเฉพาะครั้งด้วย  gateway sync --days 7
def _int_env(name, default, lo, hi):
    try:
        v = int(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))

SYNC_BACK_DAYS = _int_env("BMS_SYNC_BACK_DAYS", 0, 0, 365)

# ---- เปิด visit ให้คนไข้ refer (คิวจากหน้าทะเบียนนัด refer) ----
# คนไข้ที่ยังไม่มา รพ. จะไม่มี VN จึงลงนัดใน oapp ไม่ได้ (oapp.vn เป็น NOT NULL)
# ตารางคิวแยกจาก appointment_queue เพราะเป็นงานคนละอย่างและปลายทางคนละตาราง
VISIT_TABLE = os.getenv("BMS_DB_VISIT_TABLE") or "refer_open_visit"
# คำขอ "เพิ่มคิวนัด" จากหน้าทำนัด — ตารางแยกของตัวเองในฐานกลาง (คนละชนิดกับใบนัด)
SLOT_TABLE = os.getenv("BMS_DB_SLOT_TABLE") or "slot_add_queue"

# ค่าที่ต้องสร้างตอนเขียนเท่านั้น (ผูกกับเวลา / ต้องไม่ซ้ำ) — หน้าเว็บส่งมาเป็น null
OVST_GEN_FIELDS = ["hos_guid", "vn", "vstdate", "vsttime", "oqueue", "ovst_key"]
# ค่าที่หน้าเว็บส่งมาให้ ใช้ตามนั้น
OVST_FIXED_FIELDS = ["hn", "doctor", "hospmain", "hospsub", "pttype", "spclty",
                     "cur_dep", "main_dep", "visit_type", "staff"]
OVST_REQUIRED = ["hn", "vn", "vstdate", "vsttime"]

# คอลัมน์ที่ HOSxP เติมให้ทุกแถวแต่หน้าเว็บไม่ได้ส่งมา — ยืนยันจาก ovst จริงของ รพ.
# (วันที่สุ่มตรวจ ทุกแถวมีค่าครบทั้งชุด) ถ้าไม่เติม แถวที่เราเขียนจะผิดแผกจากแถวที่ HOSxP
# เขียนเอง แล้วรายงาน/หน้าจอที่กรองด้วยคอลัมน์เหล่านี้จะมองไม่เห็น visit ที่เราเปิด
OVST_DEFAULTS = {
    "ovstost": "00",            # รอผลตรวจ — สถานะระหว่างที่ยังตรวจไม่จบ (ตาราง ovstost)
    "cur_dep_busy": "N",
    "pt_subtype": 0,
    "pttypeno": "",
    "anonymous_visit": "N",
    "at_hospital": "Y",
    "pt_priority": 4,
}
# ตาราง referin = ทะเบียนรับส่งต่อของ visit (PK = vn) ไม่มีแถวนี้ HOSxP ไม่นับว่าเป็นผู้ป่วย
# รับส่งต่อ และแฟ้ม refer ของ 43 แฟ้มจะไม่เห็นเคสนี้ — คิวนี้เป็นผู้ป่วยส่งต่อทั้งหมด
# จึงสร้างพร้อมเปิด visit ไปเลย (รูปแบบตามแถวที่เจ้าหน้าที่คีย์มือไว้ให้ visit ของ gateway)
REFERIN_TABLE = "referin"
REFERIN_DEFAULTS = {
    "rfrcs": " ",            # สาเหตุการส่งต่อ — ต้นทางไม่ได้ส่งมา เว้นว่างแบบเดียวกับที่คีย์มือ
    "refer_point": "",
}

# สิทธิของ visit อยู่ในตาราง visit_pttype (คนละตารางกับ ovst.pttype ที่เป็นแค่สำเนาไว้ค้นเร็ว)
# ไม่มีแถวนี้ หน้าจอสิทธิ/การเงินของ HOSxP จะว่าง และส่งออก 43 แฟ้มไม่เห็นสิทธิ
# คอลัมน์ที่แถวจริงมีครบทุกแถว: vn, pttype, pttypeno, pttype_number (ยืนยันจาก 194/194 แถวในวันที่สุ่มตรวจ)
# ส่วน rcpt_amount/contract_id/finance_clear_ok ฯลฯ เป็นของฝั่งการเงิน เติมทีหลังตอนคิดเงิน ไม่ใช่ตอนเปิด visit
VISIT_PTTYPE_TABLE = "visit_pttype"
VISIT_PTTYPE_DEFAULTS = {
    "pttype_number": 1,             # ลำดับสิทธิใน visit — เปิดใหม่มีสิทธิเดียวเสมอ
    "pttype_service_charge": "Y",
}
# ค่าที่ลอกจากแถว ovst มาตรง ๆ เพื่อให้สองตารางไม่ขัดกันเอง
VISIT_PTTYPE_FROM_OVST = ["pttypeno", "hospmain", "hospsub"]

# ประเภทการมา (ovstist) ของคิวนี้ = '04' รับต่อจากสถานพยาบาลอื่น — คิวนี้มาจากระบบส่งต่อทั้งหมด
# เปลี่ยนได้ที่ config.bat ถ้า รพ. ใช้รหัสคนละตัว (ดูรายการได้จากตาราง ovstist ของ HOSxP)
OVST_REFER_IST = os.getenv("BMS_HIS_OVSTIST_REFER") or "04"
# ตรวจว่า HN มีบัตรในฐานปลายทางก่อนเปิด visit — ปิดได้ด้วย BMS_HIS_SKIP_PATIENT_CHECK=1
CHECK_PATIENT = (os.getenv("BMS_HIS_SKIP_PATIENT_CHECK") or "") not in ("1", "true", "yes")
# แผนกที่ออกใบสั่ง (lab_app_head.hospital_department_id)
LAB_HOSPITAL_DEPARTMENT_ID = int(os.getenv("BMS_HIS_LAB_DEPT") or 1)

C_OK, C_BAD, C_WARN, C_DIM, C_END = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"


def cfg_from(prefix, def_driver, def_table):
    drv = (os.getenv(prefix + "DRIVER") or def_driver).lower()
    if drv not in ("mysql", "pgsql"):
        drv = def_driver
    port = os.getenv(prefix + "PORT")
    table = re.sub(r"[^A-Za-z0-9_]", "", os.getenv(prefix + "TABLE") or def_table)
    return {
        "driver": drv,
        "host": os.getenv(prefix + "HOST") or "127.0.0.1",
        "port": int(port) if port and port.isdigit() else (5432 if drv == "pgsql" else 3306),
        "name": os.getenv(prefix + "NAME") or "",
        "user": _secret(prefix + "USER"),
        "pass": _secret(prefix + "PASS"),
        "table": table or def_table,
    }


# HOSxP รุ่นเก่าหลายที่ตั้งฐาน PostgreSQL เป็น WIN874 (ไทย) ซึ่งเก็บได้แค่ ASCII + ไทย
# อักขระอย่าง ·  —  “ ”  … จะทำให้ INSERT ล้มทั้งรายการ:
#   character with byte sequence 0xc2 0xb7 in encoding "UTF8" has no equivalent in encoding "WIN874"
# จึงแปลงเป็นตัวที่ WIN874 มี ก่อนส่งลงฐาน (ทำเฉพาะตอนฐานปลายทางเป็น WIN874/TIS620 เท่านั้น
# ฐาน UTF8 ปล่อยข้อความเดิมไว้ ไม่ไปแตะ)
_PUNCT = {
    "·": " - ", "•": " - ", "°": " องศา",
    "–": "-", "—": "-", "−": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ", "​": "",
    "→": "->", "←": "<-", "✓": "v", "✔": "v", "✗": "x",
    "×": "x", "÷": "/", "≤": "<=", "≥": ">=", "≠": "!=",
}


def win874_safe(v):
    """คืนข้อความที่ WIN874 เก็บได้แน่ ๆ — ตัวที่แปลงไม่ได้จะถูกตัดทิ้งแทนที่จะทำให้ทั้งแถวพัง"""
    if not isinstance(v, str):
        return v
    for k, r in _PUNCT.items():
        if k in v:
            v = v.replace(k, r)
    try:
        v.encode("cp874")
        return v
    except UnicodeEncodeError:
        return v.encode("cp874", "ignore").decode("cp874")


class DB:
    """ห่อ PostgreSQL / MySQL ให้ใช้เหมือนกัน (placeholder %s เหมือนกันทั้งคู่)"""

    def __init__(self, cfg, label):
        self.cfg, self.label, self.driver = cfg, label, cfg["driver"]
        if not cfg["name"]:
            raise ValueError("ยังไม่ได้ตั้งชื่อฐานข้อมูล (%sNAME)" % label)
        if self.driver == "pgsql":
            import psycopg2
            import psycopg2.extras
            self._dict = psycopg2.extras.RealDictCursor
            self.conn = psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["name"],
                                         user=cfg["user"], password=cfg["pass"], connect_timeout=10)
            self.conn.set_client_encoding("UTF8")          # กันภาษาไทยเพี้ยน
            try:                                           # ฐานเป็น WIN874/TIS620 ไหม -> ต้องกรองอักขระก่อนเขียน
                cur = self.conn.cursor()
                cur.execute("SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname=current_database()")
                self.enc = (cur.fetchone() or [""])[0] or ""
                cur.close()
            except Exception:
                self.conn.rollback()
                self.enc = ""
        else:
            import pymysql
            import pymysql.cursors
            self._dict = pymysql.cursors.DictCursor
            self.conn = pymysql.connect(host=cfg["host"], port=cfg["port"], database=cfg["name"],
                                        user=cfg["user"], password=cfg["pass"],
                                        charset="utf8mb4", connect_timeout=10)   # กันภาษาไทยเพี้ยน
            self.enc = "UTF8"                              # utf8mb4 เก็บได้ทุกตัว ไม่ต้องกรอง

    def _cursor(self, as_dict=False):
        if not as_dict:
            return self.conn.cursor()
        if self.driver == "pgsql":
            return self.conn.cursor(cursor_factory=self._dict)
        return self.conn.cursor(self._dict)

    def query(self, sql, params=()):
        cur = self._cursor(as_dict=True)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    def scalar(self, sql, params=()):
        cur = self._cursor()
        cur.execute(sql, params)
        r = cur.fetchone()
        cur.close()
        return None if r is None else r[0]

    def _safe(self, params):
        """ฐานปลายทางเป็น WIN874 -> แปลงอักขระที่เก็บไม่ได้ก่อน ไม่งั้น INSERT ล้มทั้งรายการ"""
        if str(getattr(self, "enc", "")).upper() not in ("WIN874", "TIS620"):
            return params
        if isinstance(params, dict):
            return {k: win874_safe(v) for k, v in params.items()}
        return tuple(win874_safe(v) for v in params)

    def execute(self, sql, params=()):
        cur = self._cursor()
        cur.execute(sql, self._safe(params))
        cur.close()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def approx_rows(c, table, dbname=None):
    """จำนวนแถวโดยประมาณ (เร็ว ไม่ต้อง full-scan) สำหรับตารางใหญ่อย่าง oapp
       pgsql -> pg_class.reltuples · mysql -> information_schema.tables.table_rows
       คืน (n, approx): approx=True ถ้าเป็นค่าประมาณ · ประมาณไม่ได้ (ยังไม่เคย ANALYZE ฯลฯ)
       จะ fallback ไป COUNT(*) จริง (approx=False) · อ่านไม่ได้เลย -> (None, False)"""
    try:
        if getattr(c, "driver", "") == "pgsql":
            n = c.scalar("SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass", (table,))
        else:
            n = c.scalar("SELECT table_rows FROM information_schema.tables "
                         "WHERE table_schema = %s AND table_name = %s",
                         (dbname or c.cfg.get("name"), table))
        if n is not None and int(n) > 0:
            return int(n), True
    except Exception:
        pass
    try:
        return c.scalar("SELECT COUNT(*) FROM %s" % table), False
    except Exception:
        return None, False


def count_today(c, table, datecol):
    """นับแถวที่ datecol = วันนี้ (คอลัมน์วันที่ของ oapp มัก index -> เร็วแม้ตารางใหญ่)
       CURRENT_DATE ใช้ได้ทั้ง pgsql/mysql · error/ไม่มีคอลัมน์ -> None"""
    try:
        return c.scalar("SELECT COUNT(*) FROM %s WHERE %s = CURRENT_DATE" % (table, datecol))
    except Exception:
        return None


def connect(cfg, label):
    try:
        return DB(cfg, label), None
    except Exception as e:
        return None, str(e)


# ===== oapp.app_user ต้องเป็น "ชื่อล็อกอิน" HOSxP (officer_login_name) ไม่ใช่ชื่อ-สกุล =====
# หน้าเว็บพยายามแปลงให้แล้ว แต่ถ้าแปลงไม่ได้ (หรือแถวเก่าที่ค้างในคิว) จะมาเป็นชื่อ-สกุล/ค่าว่าง
# ที่นี่ต่อฐาน HIS ตรง ไม่ติดปัญหาภาษาไทยเพี้ยนแบบ /api/sql จึงหาจาก officer ได้แม่นกว่า
_LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TITLE_RE = re.compile(r"^(นางสาว|นาง|นาย|น\.ส\.|นพ\.|พญ\.|ดร\.|ทพญ\.|ทพ\.|ภก\.|ภญ\.)\s*")
_OFFICER_LOGIN = {}          # ชื่อ (ตัดช่องว่าง) -> login ('' = หาแล้วไม่เจอ) — จำไว้ทั้งรอบ sync


def _nospace(v):
    return re.sub(r"\s+", "", str(v or ""))


def fix_app_user(hc, d, json_row):
    """แปลง d["app_user"] ให้เป็นชื่อล็อกอิน · คืน (ค่าที่ใช้, ข้อความเตือน)
       - เป็นล็อกอินอยู่แล้ว -> ใช้เลย
       - เป็นชื่อ-สกุล / ว่าง -> หาใน officer จากชื่อนั้น (หรือ appoint.recorder ใน json_row)
       - หาไม่เจอ -> ไม่ใส่ app_user (ไม่ยัดชื่อ-สกุลลงช่องที่ HOSxP ใช้เป็นล็อกอิน) แล้วเตือนไว้"""
    cur = str(d.get("app_user") or "").strip()
    if cur and _LOGIN_RE.match(cur):
        return cur, ""
    name = cur
    if not name:
        try:
            j = json.loads(json_row or "{}")
            name = str(((j.get("appoint") or {}).get("recorder")) or "").strip()
        except Exception:
            name = ""
    if not name:
        d.pop("app_user", None)
        return "", ""
    key = _nospace(name)
    if key not in _OFFICER_LOGIN:
        login = ""
        for q in dict.fromkeys([name, _TITLE_RE.sub("", name).strip()]):
            if not q:
                continue
            try:
                rows = hc.query("SELECT officer_login_name, officer_name FROM officer "
                                "WHERE REPLACE(officer_name, ' ', '') LIKE %s", ("%" + _nospace(q) + "%",))
            except Exception as e:
                try:
                    hc.rollback()          # pgsql: คิวรี่พลาดแล้วทรานแซกชันค้าง ต้องล้างก่อนทำแถวต่อ
                except Exception:
                    pass
                print("    %s[app_user]%s หาใน officer ไม่ได้: %s" % (C_WARN, C_END, e))
                rows = []
            rows = [x for x in rows if str(x.get("officer_login_name") or "").strip()]
            if rows:
                exact = [x for x in rows if _nospace(x.get("officer_name")) in (key, _nospace(q))]
                login = str((exact or rows)[0]["officer_login_name"]).strip()
                break
        _OFFICER_LOGIN[key] = login
    login = _OFFICER_LOGIN[key]
    if login:
        d["app_user"] = login
        return login, ""
    d.pop("app_user", None)
    return "", "ไม่พบชื่อล็อกอินของ \"%s\" ใน officer — ไม่ใส่ app_user" % name


def map_oapp(json_row, row_hn, row_vn):
    """แปลง json_row (จากเครื่องมือ) -> ค่าที่จะลงตาราง oapp"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        j = {}
    o = j.get("oapp") if isinstance(j, dict) and isinstance(j.get("oapp"), dict) else {}
    d = {}
    for k in OAPP_FIELDS:
        v = o.get(k)
        if k == "hn" and not v:
            v = row_hn
        if k == "vn" and not v:
            v = row_vn
        if v not in (None, ""):
            d[k] = v
    return d


def _num(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def xray_orders_from(json_row):
    """ดึงรายการรังสีจาก json_row — เอาเฉพาะที่จับคู่ xray_items ได้ (มี xray_items_code เป็นตัวเลข)
       ตัวที่ยังไม่ได้เลือกรหัสจะไม่ถูกเขียนลง oapp_xray แต่ยังอยู่ในข้อความ oapp.xray_list_text"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return []
    out, seen = [], set()
    for o in (j.get("orders") or []):
        if str(o.get("type") or "").lower() != "img":
            continue
        code = _num(o.get("xray_items_code"))
        if code is None or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": o.get("xray_items_name") or o.get("name") or ""})
    return out


def doctor_note_request_from(json_row, row_hn=None, row_vn=None):
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    n = j.get("doctor_note")
    if not isinstance(n, dict) or not n.get("insert_if_missing"):
        return None
    text = str(n.get("text") or "").strip()
    if not text:
        return None
    return {
        "hn": str(n.get("hn") or row_hn or "").strip(),
        "vn": str(n.get("vn") or row_vn or "").strip(),
        "vstdate": str(n.get("vstdate") or "").strip(),
        "vsttime": str(n.get("vsttime") or "").strip(),
        "doctor_code": str(n.get("doctor_code") or "").strip(),
        "doctor_name": str(n.get("doctor_name") or "").strip(),
        "staff": str(n.get("staff") or "").strip(),
        "text": text,
    }


_COLS = {}       # (id(conn), ชื่อตาราง) -> {ชื่อพิมพ์เล็ก: ชื่อจริง}


def table_columns(hc, table):
    """คอลัมน์จริงของตาราง — คืน dict {ชื่อพิมพ์เล็ก: ชื่อจริง}

       ห้ามเปลี่ยนเป็น set: _first_col() เอา "ชื่อจริง" ที่เป็นค่าใน dict ไปประกอบ SQL
       (เคยพลาดมาแล้ว — ทับด้วย set แล้ว cols[name] ระเบิดทุกแถวที่มี doctor_note)
       ตารางไม่มีจริง -> คืน dict ว่าง ผู้เรียกใช้ค่าว่างแทนคำตอบว่า "ไม่มี/ไม่รู้"
       แคชต่อคอนเนกชัน ไม่ต้องถาม information_schema ซ้ำทุกแถว"""
    key = (id(hc.conn), str(table).lower())
    if key in _COLS:
        return _COLS[key]
    try:
        rows = hc.query("""SELECT column_name
                           FROM information_schema.columns
                           WHERE table_name=%s""", (table,))
        cols = {str(r.get("column_name") or "").lower(): str(r.get("column_name") or "") for r in rows}
    except Exception:
        hc.rollback()
        cols = {}
    _COLS[key] = cols
    return cols


_HAS_COL = {}
def slot_add_request_from(json_row):
    """คำขอ "เพิ่มคิว" ที่หน้าเว็บส่งมา — {'slot_add': {...}} · ไม่ใช่คำขอแบบนี้คืน None"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_add")
    return a if isinstance(a, dict) else None


def _num_parts(number, n):
    """แยก 'E1001' (ลำดับ 1) -> ('E1', 3)  ·  'J003' (ลำดับ 3) -> ('J', 3)

       เลขคิวคือ prefix + ลำดับที่เติมศูนย์นำ แต่ prefix เองก็มีตัวเลขปนได้ (E1)
       จึงไล่จากความกว้างมากไปน้อย เอาอันแรกที่แปลงกลับเป็นลำดับได้ตรง
       (ต้องเอาความกว้างมากสุด ไม่งั้นพอเลขขึ้นหลักจะได้ J0010 แทน J010)"""
    s = str(number or "").strip()
    m = re.search(r"(\d+)$", s)
    if not m or n is None:
        return s, 0
    digits = m.group(1)
    for w in range(len(digits), 0, -1):
        tail = digits[-w:]
        try:
            if int(tail) == int(n):
                return s[:len(s) - w], w
        except ValueError:
            pass
    return s[:len(s) - len(digits)], len(digits)


def add_qs_slots(hc, req):
    """เพิ่มคิวว่างในตาราง opd_qs_slot ตามคำขอของพยาบาล (คิวเต็ม / ขอแทรกเคส)

       วิธี: "ลอกแบบ" คิวสุดท้ายของกลุ่มนั้นมาทั้งดุ้น แล้วขยับเลขคิวกับเวลาให้ต่อกัน
       ไม่ประดิษฐ์ค่าเอง เพราะคอลัมน์อย่าง slot_key / opd_queue_slot_type_id / tmpl_type_id
       มีความหมายกับตัวจัดคิวของ HOSxP ที่เราไม่ได้เป็นคนเขียน

       ต้องมีคิวเดิมอยู่ก่อนอย่างน้อย 1 ใบในกลุ่มนั้น (วัน+แพทย์+ตารางออกตรวจ)
       ไม่มีแปลว่ายังไม่ได้เปิดตารางออกตรวจวันนั้น -> ให้ไปเปิดใน HOSxP ก่อน ไม่เดาสร้างให้

       คืน (จำนวนที่เพิ่ม, ข้อความสรุป)"""
    date = str(req.get("schedule_date") or "").strip()[:10]
    doc = str(req.get("doctor_code") or "").strip()
    n = int(req.get("count") or 0)
    sched = req.get("opd_queue_schedule_id")
    if not date or not doc:
        raise ValueError("คำขอเพิ่มคิวไม่มีวันที่หรือรหัสแพทย์")
    if n < 1 or n > 50:
        raise ValueError("จำนวนคิวที่เพิ่มต้องอยู่ระหว่าง 1-50 (ขอมา %s)" % req.get("count"))

    where = "schedule_date=%s AND doctor_code=%s"
    args = [date, doc]
    if sched not in (None, "", 0):
        where += " AND opd_queue_schedule_id=%s"
        args.append(int(sched))
    # ล็อกทั้งกลุ่มไว้ก่อน กันสองคนกดเพิ่มคิวพร้อมกันแล้วได้เลขคิวชนกัน
    rows = hc.query("SELECT * FROM opd_qs_slot WHERE %s ORDER BY queue_slot_number_int DESC, opd_qs_slot_id DESC "
                    "LIMIT 1 FOR UPDATE" % where, tuple(args))
    if not rows:
        raise ValueError("วันที่ %s ยังไม่มีคิวของ %s ในระบบ — เปิดตารางออกตรวจใน HOSxP ก่อน" % (date, doc))
    last = rows[0]

    prefix, pad = _num_parts(last.get("queue_slot_number"), last.get("queue_slot_number_int"))
    dur = int(last.get("time_second") or 0)
    cur_no = int(last.get("queue_slot_number_int") or 0)
    cur_fin = last.get("finish_time")
    cols = table_columns(hc, "opd_qs_slot")

    made = []
    for _ in range(n):
        cur_no += 1
        num = (prefix + str(cur_no).zfill(pad)) if pad else (prefix + str(cur_no))
        # เวลา: ต่อจากคิวก่อนหน้า 1 วินาที ยาวเท่าเดิม · ไม่มีเวลาเดิมให้อ้างอิงก็ปล่อยว่าง
        st = fin = None
        if cur_fin is not None and dur > 0:
            base = datetime.datetime.combine(datetime.date(2000, 1, 1), cur_fin) + datetime.timedelta(seconds=1)
            st = base.time()
            fin = (base + datetime.timedelta(seconds=dur - 1)).time()
            cur_fin = fin
        d = {
            "opd_qs_slot_id": next_id(hc, "opd_qs_slot_id", "opd_qs_slot"),
            "schedule_date": date,
            "doctor_code": doc,
            "queue_slot_number": num,
            "queue_slot_number_int": cur_no,
            "slot_busy": "N",
            "slot_register": "N",
            "call_status": "N",
        }
        if st is not None:
            d["start_time"] = st
            d["finish_time"] = fin
            d["time_second"] = dur
            d["slot_key"] = "%s%s-%d" % (st.strftime("%H%M%S"), fin.strftime("%H%M%S"), cur_no)
        # ลอกค่าที่ผูกกับตารางออกตรวจมาจากคิวเดิมทั้งหมด
        for k in ("opd_queue_schedule_id", "opd_queue_slot_type_id", "opd_qs_schedule_tmpl_type_id", "opd_qs_room_id"):
            if last.get(k) is not None:
                d[k] = last[k]
        if "update_datetime" in cols:
            d["update_datetime"] = datetime.datetime.now().replace(microsecond=0)
        keys = [k for k in d.keys() if k.lower() in cols]
        hc.execute("INSERT INTO opd_qs_slot (%s) VALUES (%s)"
                   % (",".join(keys), ",".join(["%s"] * len(keys))), tuple(d[k] for k in keys))
        made.append(num)

    # โควตาในตารางสรุป — บวกตามจำนวนที่เพิ่ม ไม่งั้นหน้าจอ HOSxP จะโชว์โควตาไม่ตรงกับคิวที่มีจริง
    bumped = []
    try:
        t = last.get("opd_qs_schedule_tmpl_type_id")
        hc.execute("UPDATE opd_qs_slot_summary SET total_limit = COALESCE(total_limit,0)+%s "
                   "WHERE schedule_date=%s AND doctor_code=%s"
                   + (" AND opd_qs_schedule_tmpl_type_id=%s" if t is not None else ""),
                   tuple([n, date, doc] + ([t] if t is not None else [])))
        bumped.append("total_limit")
    except Exception as e:
        hc.rollback()
        raise ValueError("เพิ่มคิวแล้วแต่อัปเดต opd_qs_slot_summary ไม่ได้: %s" % e)
    if last.get("opd_queue_schedule_id") is not None:
        try:
            hc.execute("UPDATE opd_queue_schedule SET slot_count = COALESCE(slot_count,0)+%s,"
                       " available_count = COALESCE(available_count,0)+%s WHERE opd_queue_schedule_id=%s",
                       (n, n, last["opd_queue_schedule_id"]))
            bumped.append("slot_count")
        except Exception as e:
            hc.rollback()
            raise ValueError("เพิ่มคิวแล้วแต่อัปเดต opd_queue_schedule ไม่ได้: %s" % e)
    return len(made), "คิวใหม่ %s  (โควตา +%d: %s)" % (", ".join(made), n, "/".join(bumped) or "-")


def slot_remove_request_from(json_row):
    """คำขอ "ลดคิว" จากหน้า slot-admin — {'slot_remove': {...}} · ไม่ใช่คำขอแบบนี้คืน None"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_remove") if isinstance(j, dict) else None
    return a if isinstance(a, dict) else None


def _pending_slot_refs(cc):
    """opd_qs_slot_id ที่ใบนัดในฐานกลางซึ่งยัง pending เลือกไว้ (ยังไม่ถูกเขียนลง oapp)

       รอบ sync ทำคำขอคิวก่อนใบนัด ถ้าลบคิวที่ใบนัดค้างเลือกไว้ ใบนัดนั้นจะจองคิวไม่ได้
       อ่านไม่ได้ -> คืน None ให้ผู้เรียกหยุด (ไม่รู้ = ไม่ลบ)"""
    if cc is None:
        return None
    try:
        rows = cc.query("SELECT json_row FROM %s WHERE status='pending'" % cc.cfg["table"])
    except Exception:
        cc.rollback()
        return None
    out = set()
    for r in rows:
        try:
            j = json.loads(r.get("json_row") or "{}")
            o = j.get("oapp") if isinstance(j, dict) and isinstance(j.get("oapp"), dict) else {}
            v = int(o.get("opd_qs_slot_id") or 0)
        except Exception:
            continue
        if v > 0:
            out.add(v)
    return out


def _slot_is_free(r):
    """คิวว่างจริง — ยังไม่ถูกจอง (slot_register) / ไม่ busy / ยังไม่มี visit / ไม่มีใครผูก ref_key"""
    return (str(r.get("slot_register") or "").strip().upper() != "Y"
            and str(r.get("slot_busy") or "").strip().upper() != "Y"
            and not str(r.get("vn") or "").strip()
            and not str(r.get("ref_key") or "").strip())


def remove_qs_slots(hc, req, cc=None):
    """ลดคิว — ลบแถว opd_qs_slot ที่ "ยังว่าง" ตาม id ที่หน้า slot-admin เลือกไว้ (ขากลับของ add_qs_slots)

       ลบด้วย opd_qs_slot_id เท่านั้น (ไม่ลบด้วยวันที่+แพทย์) และเช็คซ้ำตอนลงมือ
       เพราะระหว่างที่คำขอรอคิว คิวอาจถูกจองไปแล้ว — คิวต่อไปนี้จะถูกข้าม ไม่ลบ:
         - slot_register='Y' / slot_busy='Y' / มี vn / มี ref_key
         - มี oapp.opd_qs_slot_id ชี้อยู่ (นัดใน HOSxP ทุกสถานะ)
         - ใบนัดในฐานกลางที่ยัง pending เลือกคิวนี้ไว้
         - วันที่ / แพทย์ / ตารางออกตรวจ ไม่ตรงกับคำขอ (กัน id หลงวัน)
       ลบแล้วลดโควตาใน opd_qs_slot_summary / opd_queue_schedule ตามจำนวนที่ลบได้จริง (ไม่ติดลบ)
       คืน (จำนวนที่ลบ, ข้อความสรุป)"""
    date = str(req.get("schedule_date") or "").strip()[:10]
    doc = str(req.get("doctor_code") or "").strip()
    sched = req.get("opd_queue_schedule_id")
    if not date or not doc:
        raise ValueError("คำขอลดคิวไม่มีวันที่หรือรหัสแพทย์")
    ids = []
    for v in (req.get("slot_ids") or []):
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in ids:
            ids.append(i)
    if not ids or len(ids) > 200:
        raise ValueError("จำนวนคิวที่ลดต้องอยู่ระหว่าง 1-200 (ขอมา %d)" % len(ids))
    held = _pending_slot_refs(cc)
    if held is None:
        raise ValueError("อ่านใบนัดที่ค้างในฐานกลางไม่ได้ — ไม่ลบคิว (กันลบคิวที่ใบนัดเลือกไว้)")

    ph = ",".join(["%s"] * len(ids))
    # ล็อกแถวก่อนเช็ค กันตัวจองคิว (gateway อีกตัว / หน้าจอ HOSxP) จองคิวเดียวกันระหว่างลบ
    rows = hc.query("SELECT opd_qs_slot_id, schedule_date, doctor_code, queue_slot_number, queue_slot_number_int,"
                    " slot_register, slot_busy, vn, ref_key, opd_queue_schedule_id, opd_qs_schedule_tmpl_type_id"
                    " FROM opd_qs_slot WHERE opd_qs_slot_id IN (%s) FOR UPDATE" % ph, tuple(ids))
    by_id = {int(r["opd_qs_slot_id"]): r for r in rows}
    refd = set()
    if "opd_qs_slot_id" in table_columns(hc, "oapp"):
        for x in hc.query("SELECT DISTINCT opd_qs_slot_id AS i FROM oapp WHERE opd_qs_slot_id IN (%s)" % ph, tuple(ids)):
            if x.get("i") is not None:
                refd.add(int(x["i"]))

    ok, skip = [], []
    for i in ids:
        r = by_id.get(i)
        if not r:
            skip.append("%s ไม่มีแล้ว" % i)
            continue
        num = str(r.get("queue_slot_number") or i).strip()
        if str(r.get("schedule_date") or "")[:10] != date or str(r.get("doctor_code") or "").strip() != doc:
            skip.append("%s คนละวัน/แพทย์" % num)
        elif sched not in (None, "", 0) and str(r.get("opd_queue_schedule_id") or "") != str(sched):
            skip.append("%s คนละตาราง" % num)
        elif not _slot_is_free(r):
            skip.append("%s จองแล้ว" % num)
        elif i in refd or i in held:
            skip.append("%s มีนัดอ้าง" % num)
        else:
            ok.append(r)
    if not ok:
        raise ValueError("ไม่มีคิวที่ลดได้ — " + ", ".join(skip[:8]))

    ok_ids = [int(r["opd_qs_slot_id"]) for r in ok]
    ph2 = ",".join(["%s"] * len(ok_ids))
    # เงื่อนไข "ว่าง" ซ้ำใน SQL อีกชั้น — ต่อให้ตรรกะข้างบนพลาด ก็ลบคิวที่จองแล้วไม่ได้
    hc.execute("DELETE FROM opd_qs_slot WHERE opd_qs_slot_id IN (%s)"
               " AND COALESCE(slot_register,'N')<>'Y' AND COALESCE(slot_busy,'N')<>'Y'"
               " AND COALESCE(vn,'')='' AND COALESCE(ref_key,'')=''" % ph2, tuple(ok_ids))
    left = {int(x["i"]) for x in hc.query("SELECT opd_qs_slot_id AS i FROM opd_qs_slot WHERE opd_qs_slot_id IN (%s)"
                                          % ph2, tuple(ok_ids))}
    gone = [r for r in ok if int(r["opd_qs_slot_id"]) not in left]
    if not gone:
        raise ValueError("ลบคิวไม่ได้ (เงื่อนไขคิวว่างไม่ผ่านตอนลบ)")
    n = len(gone)

    # โควตา — ลดตามจำนวนที่ลบได้จริง แยกตามชนิดตาราง/ตารางออกตรวจของคิวที่ลบ
    by_t, by_s = {}, {}
    for r in gone:
        t = r.get("opd_qs_schedule_tmpl_type_id")
        by_t[t] = by_t.get(t, 0) + 1
        sc = r.get("opd_queue_schedule_id")
        if sc is not None:
            by_s[sc] = by_s.get(sc, 0) + 1
    bumped = []
    try:
        for t, k in by_t.items():
            hc.execute("UPDATE opd_qs_slot_summary SET total_limit = GREATEST(COALESCE(total_limit,0)-%s,0) "
                       "WHERE schedule_date=%s AND doctor_code=%s"
                       + (" AND opd_qs_schedule_tmpl_type_id=%s" if t is not None else ""),
                       tuple([k, date, doc] + ([t] if t is not None else [])))
        bumped.append("total_limit")
        for sc, k in by_s.items():
            hc.execute("UPDATE opd_queue_schedule SET slot_count = GREATEST(COALESCE(slot_count,0)-%s,0),"
                       " available_count = GREATEST(COALESCE(available_count,0)-%s,0)"
                       " WHERE opd_queue_schedule_id=%s", (k, k, sc))
        if by_s:
            bumped.append("slot_count")
    except Exception as e:
        hc.rollback()
        raise ValueError("อัปเดตโควตาไม่ได้ — ยกเลิกการลบทั้งหมด: %s" % e)

    gone.sort(key=lambda r: int(r.get("queue_slot_number_int") or 0))
    nums = [str(r.get("queue_slot_number") or r["opd_qs_slot_id"]).strip() for r in gone]
    msg = "ลบคิวว่าง %s  (โควตา -%d: %s)" % (", ".join(nums), n, "/".join(bumped) or "-")
    if skip:
        msg += " · ข้าม %d: %s" % (len(skip), ", ".join(skip[:6]))
    return n, msg


def slot_create_request_from(json_row):
    """คำขอ "สร้างตารางคิวเป็นช่วง" จากหน้า slot-admin — {"slot_create": {...}}

       คนละชนิดกับ {"slot_add": ...} (ที่แค่เพิ่มคิวในตารางที่เปิดไว้แล้ว)
       ตัวนี้เปิดตารางออกตรวจใหม่ทั้งใบ จึงต้องสร้างครบทั้ง 3 ชั้น"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_create")
    return a if isinstance(a, dict) else None


THAI_DOW = ("อาทิตย์", "จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์")


def _sec_of(t):
    """time -> วินาทีนับจากเที่ยงคืน (คำนวณช่วงคิวง่ายกว่าเล่นกับ datetime)"""
    return t.hour * 3600 + t.minute * 60 + t.second


def _time_at(sec):
    sec = max(0, min(86399, int(sec)))
    return datetime.time(sec // 3600, (sec % 3600) // 60, sec % 60)


def queue_prefix_for(hc, clinic, doctor_plain, doctor_code, fallback=""):
    """เลขคิวของแพทย์คนนี้ขึ้นต้นด้วยอะไร (E1 / SA / J ...)

       ลำดับที่หา: ตารางที่ HOSxP ตั้งไว้ -> ถอดจากคิวเดิมของแพทย์คนนี้ -> ค่าที่หน้าเว็บกรอก
       ไม่ตั้งเองมั่ว ๆ เพราะเลขคิวคือสิ่งที่คนไข้ถือบัตรมายืนรอ ซ้ำกับคลินิกอื่นแล้วเรียกผิดคน"""
    try:
        rows = hc.query("SELECT queue_prefix FROM opd_qs_clinic_doctor_prefix "
                        "WHERE COALESCE(clinic,'')=%s AND COALESCE(doctor_code,'')=%s",
                        (str(clinic or ""), str(doctor_plain or "")))
        if rows and str(rows[0].get("queue_prefix") or "").strip():
            return str(rows[0]["queue_prefix"]).strip()
    except Exception:
        hc.rollback()
    # ทั้งคลินิกใช้อักษรเดียวกัน (ตั้งที่ตาราง clinic) — ลำดับเดียวกับที่หน้า slot-admin โชว์ให้ดู
    if clinic:
        try:
            v = hc.scalar("SELECT queue_prefix FROM clinic WHERE clinic=%s", (str(clinic),))
            if v and str(v).strip():
                return str(v).strip()
        except Exception:
            hc.rollback()
    try:
        rows = hc.query("SELECT queue_slot_number, queue_slot_number_int FROM opd_qs_slot "
                        "WHERE doctor_code=%s AND queue_slot_number IS NOT NULL "
                        "ORDER BY schedule_date DESC, queue_slot_number_int LIMIT 1", (doctor_code,))
        if rows:
            p, _ = _num_parts(rows[0].get("queue_slot_number"), rows[0].get("queue_slot_number_int"))
            if p:
                return p
    except Exception:
        hc.rollback()
    p = re.sub(r"[^0-9A-Za-z]", "", str(fallback or "")).upper()
    if p:
        return p
    raise ValueError("ไม่รู้ว่าเลขคิวของ %s ขึ้นต้นด้วยอะไร — ตั้งใน opd_qs_clinic_doctor_prefix "
                     "หรือกรอกช่อง 'อักษรนำเลขคิว' มาด้วย" % doctor_code)


def ensure_limit_type(hc, qty, name_hint=""):
    """หา "ชุดโควตา" (opd_qs_limit_type) ที่ตัวเลขตรงกับที่ขอ ไม่มีก็สร้างใหม่

       qty = {opd_queue_slot_type_id: จำนวน} เช่น {1:5, 2:3, 3:2} = Walkin 5 / นัด 3 / Online 2
       ใช้ชุดเดิมซ้ำถ้าตรงกันทุกประเภท ตารางจะได้ไม่มีชุดโควตาความหมายเดียวกันเต็มไปหมด
       คืน opd_qs_limit_type_id"""
    want = {}
    for k, v in (qty or {}).items():
        try:
            k, v = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            want[k] = v
    if not want:
        raise ValueError("ต้องกำหนดจำนวนคิวอย่างน้อย 1 ประเภท")
    have = {}
    for r in hc.query("SELECT opd_qs_limit_type_id t, opd_queue_slot_type_id s, limit_qty q FROM opd_qs_limit", ()):
        try:
            have.setdefault(int(r["t"]), {})[int(r["s"])] = int(r["q"] or 0)
        except (TypeError, ValueError):
            pass
    for t, m in sorted(have.items()):
        if dict((k, v) for k, v in m.items() if v > 0) == want:
            return t
    names = {}
    for r in hc.query("SELECT opd_queue_slot_type_id s, opd_queue_slot_type_name n FROM opd_queue_slot_type", ()):
        names[int(r["s"])] = str(r["n"] or "").strip()
    label = str(name_hint or "").strip() or (
        " / ".join("%s %d" % (names.get(k, "ประเภท %d" % k), want[k]) for k in sorted(want))
        + " = %d" % sum(want.values()))
    tid = next_id(hc, "opd_qs_limit_type_id", "opd_qs_limit_type")
    cols = table_columns(hc, "opd_qs_limit_type")
    d = {"opd_qs_limit_type_id": tid, "opd_qs_limit_type_name": label[:120]}
    keys = [k for k in d if k.lower() in cols]
    hc.execute("INSERT INTO opd_qs_limit_type (%s) VALUES (%s)"
               % (",".join(keys), ",".join(["%s"] * len(keys))), tuple(d[k] for k in keys))
    for k in sorted(want):
        hc.execute("INSERT INTO opd_qs_limit (opd_qs_limit_id, opd_qs_limit_type_id,"
                   " opd_queue_slot_type_id, limit_qty) VALUES (%s,%s,%s,%s)",
                   (next_id(hc, "opd_qs_limit_id", "opd_qs_limit"), tid, k, want[k]))
    return tid


def _slot_windows(hc, ids):
    """opd_queue_slot_id -> ช่วงเวลาที่กำหนดไว้ (08:00:00-08:59:59)"""
    out = {}
    if not ids:
        return out
    q = ",".join(["%s"] * len(ids))
    for r in hc.query("SELECT opd_queue_slot_id i, opd_queue_slot_name n, opd_queue_slot_start_time a,"
                      " opd_queue_slot_finish_time b FROM opd_queue_slot WHERE opd_queue_slot_id IN (%s)" % q,
                      tuple(ids)):
        out[int(r["i"])] = {"name": str(r["n"] or "").strip(), "a": r["a"], "b": r["b"]}
    return out


def _iv_start(txt):
    """'08:30 - 11:59' -> 510 (นาทีของเวลาเริ่ม) · อ่านไม่ออกให้ไปท้ายสุด"""
    m = re.match(r"\s*(\d{1,2}):(\d{2})", str(txt or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 99999


def _iv_merge(old_text, add_text):
    """รวมช่วงเวลาของวันเดียวกันเป็นข้อความเดียว เรียงตามเวลาเริ่ม ไม่ซ้ำ

       '13:00 - 16:30' + '08:30 - 11:59' -> '08:30 - 11:59, 13:00 - 16:30'
       (รูปแบบเดียวกับที่ HOSxP เขียนเอง หน้าจอ 'ตารางการจำกัดการนัดหมายแบบ Slot' อ่านตรงนี้)"""
    parts = [p.strip() for p in str(old_text or "").split(",") if p.strip()]
    for t in ([add_text] if isinstance(add_text, str) else list(add_text or [])):
        t = str(t or "").strip()
        if t and t not in parts:
            parts.append(t)
    parts.sort(key=_iv_start)
    return ", ".join(parts)


def summary_upsert(hc, ds, doc, tmpl_type, txt, n, ucols=None):
    """เขียนบรรทัดสรุปโควตาของ "วันนั้น" ให้เหลือแถวเดียว

       HOSxP เก็บ 1 วัน = 1 แถว แล้วเอาช่วงเวลามาต่อกันด้วย ',' ส่วนโควตารวมกัน
       ถ้าเราแยกแถวตามช่วงเวลา หน้าจอ HOSxP จะโชว์วันเดียวกันซ้ำหลายบรรทัด
       ฟังก์ชันนี้ยุบแถวที่ซ้ำอยู่แล้วให้เหลือแถวแรกด้วย (ซ่อมของเก่าไปในตัว)

       txt = ช่วงเวลาที่เพิ่งสร้าง (None = แค่ยุบแถวซ้ำ ไม่บวกโควตา) · n = โควตาที่เพิ่ง +
       คืนจำนวนแถวที่ลบทิ้งเพราะซ้ำ"""
    if ucols is None:
        ucols = table_columns(hc, "opd_qs_slot_summary")
    w, ar = "schedule_date=%s AND doctor_code=%s", [ds, doc]
    if tmpl_type is not None and "opd_qs_schedule_tmpl_type_id" in ucols:
        w += " AND opd_qs_schedule_tmpl_type_id=%s"
        ar.append(tmpl_type)
    rows = hc.query("SELECT opd_qs_slot_summary_id i, interval_summary_text t, total_limit l"
                    " FROM opd_qs_slot_summary WHERE " + w + " ORDER BY opd_qs_slot_summary_id", tuple(ar))
    if not rows:
        if txt is None:
            return 0
        u = {"opd_qs_slot_summary_id": next_id(hc, "opd_qs_slot_summary_id", "opd_qs_slot_summary"),
             "schedule_date": ds, "doctor_code": doc, "interval_summary_text": txt,
             "total_limit": n, "day_name": THAI_DOW[_dow_of(ds)],
             "opd_qs_schedule_tmpl_type_id": tmpl_type}
        keys = [k for k in u if k.lower() in ucols and u[k] is not None]
        hc.execute("INSERT INTO opd_qs_slot_summary (%s) VALUES (%s)"
                   % (",".join(keys), ",".join(["%s"] * len(keys))), tuple(u[k] for k in keys))
        return 0
    keep = rows[0]
    text = keep.get("t")
    total = int(keep.get("l") or 0)
    for extra in rows[1:]:                       # ยุบแถวที่ซ้ำของวันเดียวกันเข้ามารวม
        text = _iv_merge(text, [p.strip() for p in str(extra.get("t") or "").split(",")])
        total += int(extra.get("l") or 0)
        hc.execute("DELETE FROM opd_qs_slot_summary WHERE opd_qs_slot_summary_id=%s", (extra["i"],))
    if txt is not None:
        text = _iv_merge(text, txt)
        total += int(n or 0)
    hc.execute("UPDATE opd_qs_slot_summary SET interval_summary_text=%s, total_limit=%s"
               " WHERE opd_qs_slot_summary_id=%s", (text, total, keep["i"]))
    return len(rows) - 1


def _dow_of(ds):
    d = datetime.date(int(ds[0:4]), int(ds[5:7]), int(ds[8:10]))
    return d.isoweekday() % 7


def fix_qs_summary(hc, d1, d2, doc=None):
    """ซ่อมบรรทัดสรุปที่แตกเป็นหลายแถวต่อวัน (ของที่สร้างไว้ก่อนแก้บั๊กนี้)

       ไม่แตะโควตารวม — แค่ยุบแถวและต่อข้อความช่วงเวลา ยอดรวมเท่าเดิม"""
    where = "schedule_date BETWEEN %s AND %s"
    args = [d1, d2]
    if doc:
        where += " AND doctor_code=%s"
        args.append(doc)
    keys = hc.query("SELECT schedule_date d, doctor_code c, opd_qs_schedule_tmpl_type_id t,"
                    " COUNT(*) n FROM opd_qs_slot_summary WHERE " + where +
                    " GROUP BY schedule_date, doctor_code, opd_qs_schedule_tmpl_type_id"
                    " HAVING COUNT(*) > 1 ORDER BY 1", tuple(args))
    ucols = table_columns(hc, "opd_qs_slot_summary")
    fixed = 0
    for k in keys:
        ds = str(k["d"])[:10]
        removed = summary_upsert(hc, ds, str(k["c"]).strip(), k["t"], None, 0, ucols)
        fixed += removed
        print("  ยุบ %s %s : %d แถว -> 1" % (ds, str(k["c"]).strip(), int(k["n"])))
    return len(keys), fixed


def slot_flag_request_from(json_row):
    """คำขอ "เปิด/ปิดใช้ระบบคิว" ของคลินิก/แพทย์ — {'slot_flag': {...}}

       เป็นการแก้ตารางตั้งค่าหลักของ HIS (clinic.use_app_qs_slot / doctor.use_app_slot)
       จึงให้เดินทางเดียวกับคำขออื่น: หน้าเว็บสั่ง -> ฐานกลาง -> gateway เขียน
       จะได้มี log ว่าใครสั่งเปลี่ยนอะไรเมื่อไหร่ ย้อนดูได้"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_flag")
    return a if isinstance(a, dict) else None


def _yn(v):
    s = str(v or "").strip().upper()
    return s if s in ("Y", "N") else None


def apply_slot_flags(hc, req):
    """เปิด/ปิดธงใช้ระบบคิวให้คลินิกและแพทย์ตามที่หน้าเว็บสั่ง

       อัปเดตเฉพาะแถวที่ค่าต่างจากเดิมจริง ๆ — ถ้าค่าเท่าเดิมก็ข้าม จะได้ไม่ไปแตะ
       แถวที่ไม่ได้ตั้งใจแก้ และรายงานได้ตรงว่าเปลี่ยนอะไรไปกี่รายการ
       คืน (จำนวนที่เปลี่ยนจริง, ข้อความสรุป)"""
    jobs = [
        ("clinic", "clinic", "clinic", "use_app_qs_slot", req.get("clinic")),
        ("แพทย์", "doctor", "code", "use_app_slot", req.get("doctor")),
    ]
    changed, skipped, detail = 0, 0, []
    # ชนิดตารางคิวของคลินิก (clinic.opd_qs_schedule_tmpl_type_id) — 1 แพทย์ / 2 คลินิก / 3 คลินิก:แพทย์
    # ตัวนี้บอกว่า doctor_code ในตารางคิวจะเก็บรูปแบบไหน ตั้งผิดแล้วหาคิวไม่เจอทั้งคลินิก
    want_tt = req.get("clinic_tmpl")
    if isinstance(want_tt, dict) and want_tt:
        ok_ids = set()
        for r in hc.query("SELECT opd_qs_schedule_tmpl_type_id i FROM opd_qs_schedule_tmpl_type", ()):
            ok_ids.add(int(r["i"]))
        for code, v in want_tt.items():
            code = str(code or "").strip()
            if not code:
                continue
            val = None
            if str(v or "").strip():
                try:
                    val = int(v)
                except (TypeError, ValueError):
                    raise ValueError("ชนิดตารางคิวของคลินิก %s ไม่ใช่ตัวเลข (%s)" % (code, v))
                if val not in ok_ids:
                    raise ValueError("ไม่รู้จักชนิดตารางคิว %s (มีให้เลือก: %s)"
                                     % (val, ",".join(str(x) for x in sorted(ok_ids))))
            cur = hc.query("SELECT opd_qs_schedule_tmpl_type_id t FROM clinic WHERE clinic=%s", (code,))
            if not cur:
                raise ValueError("ไม่พบคลินิกรหัส %s ในฐาน HIS" % code)
            old_v = cur[0].get("t")
            old_v = int(old_v) if old_v is not None else None
            if old_v == val:
                skipped += 1
                continue
            hc.execute("UPDATE clinic SET opd_qs_schedule_tmpl_type_id=%s WHERE clinic=%s", (val, code))
            changed += 1
            detail.append("ชนิดคิว %s -> %s" % (code, val if val is not None else "ไม่กำหนด"))
    for label, table, keycol, flagcol, want in jobs:
        if not isinstance(want, dict) or not want:
            continue
        cols = table_columns(hc, table)
        if flagcol not in cols:
            raise ValueError("ตาราง %s ไม่มีคอลัมน์ %s" % (table, flagcol))
        on, off = [], []
        for code, v in want.items():
            code = str(code or "").strip()
            val = _yn(v)
            if not code or not val:
                continue
            cur = hc.query("SELECT %s AS f FROM %s WHERE %s=%%s" % (flagcol, table, keycol), (code,))
            if not cur:
                raise ValueError("ไม่พบ %s รหัส %s ในฐาน HIS" % (label, code))
            if _yn(cur[0].get("f")) == val or (not _yn(cur[0].get("f")) and val == "N"):
                skipped += 1                      # ค่าเท่าเดิม (ค่าว่างถือเป็น N)
                continue
            hc.execute("UPDATE %s SET %s=%%s WHERE %s=%%s" % (table, flagcol, keycol), (val, code))
            changed += 1
            (on if val == "Y" else off).append(code)
        if on:
            detail.append("%s เปิด %d (%s)" % (label, len(on), ",".join(on[:6]) + ("…" if len(on) > 6 else "")))
        if off:
            detail.append("%s ปิด %d (%s)" % (label, len(off), ",".join(off[:6]) + ("…" if len(off) > 6 else "")))
    if not changed:
        raise ValueError("ทุกรายการมีค่าตรงกับที่ขออยู่แล้ว (%d รายการ) — ไม่ได้แก้อะไร" % skipped)
    note = " · ".join(detail) + ((" · เท่าเดิม %d" % skipped) if skipped else "")
    return changed, note[:255]


def slot_prefix_request_from(json_row):
    """คำขอแก้ "อักษรนำเลขคิว" ของคู่คลินิก+แพทย์ — {'slot_prefix': {'ops': [...]}}"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_prefix")
    return a if isinstance(a, dict) else None


def apply_qs_prefix(hc, req):
    """เพิ่ม/แก้/ลบแถวในตาราง opd_qs_clinic_doctor_prefix

       ตารางนี้มี unique index ที่ (clinic, doctor_code) — คู่เดิมจึงเป็น "แก้" ไม่ใช่ "เพิ่ม"
       ถ้าดันไป insert ซ้ำจะเป็น duplicate key แล้วทั้งทรานแซกชันตาย
       ตรวจว่ารหัสคลินิก/แพทย์มีจริงก่อนเขียน เพราะเลขคิวที่ผูกกับรหัสผิดจะไปโผล่ผิดห้อง

       ops = [{'op':'set'|'del', 'clinic', 'doctor_code', 'queue_prefix'}]
       คืน (จำนวนที่เปลี่ยน, ข้อความสรุป)"""
    ops = req.get("ops") if isinstance(req.get("ops"), list) else []
    if not ops:
        raise ValueError("คำขอไม่มีรายการที่จะแก้")
    if len(ops) > 200:
        raise ValueError("แก้ได้ครั้งละไม่เกิน 200 รายการ (ขอมา %d)" % len(ops))
    added = updated = removed = 0
    detail = []
    for o in ops:
        op = str(o.get("op") or "set").strip().lower()
        cl = str(o.get("clinic") or "").strip()[:3]
        dc = str(o.get("doctor_code") or "").strip()[:15]
        px = re.sub(r"[^0-9A-Za-z]", "", str(o.get("queue_prefix") or "")).upper()[:5]
        if not cl or not dc:
            raise ValueError("รายการหนึ่งไม่มีรหัสคลินิกหรือรหัสแพทย์")
        old = hc.query("SELECT opd_qs_clinic_doctor_prefix_id i, queue_prefix p"
                       " FROM opd_qs_clinic_doctor_prefix WHERE clinic=%s AND doctor_code=%s", (cl, dc))
        if op == "del":
            if old:
                hc.execute("DELETE FROM opd_qs_clinic_doctor_prefix WHERE opd_qs_clinic_doctor_prefix_id=%s",
                           (old[0]["i"],))
                removed += 1
                detail.append("ลบ %s/%s" % (cl, dc))
            continue
        if not px:
            raise ValueError("คู่ %s/%s ไม่มีอักษรนำเลขคิว" % (cl, dc))
        if not hc.query("SELECT clinic FROM clinic WHERE clinic=%s", (cl,)):
            raise ValueError("ไม่พบคลินิกรหัส %s ในฐาน HIS" % cl)
        if not hc.query("SELECT code FROM doctor WHERE code=%s", (dc,)):
            raise ValueError("ไม่พบแพทย์รหัส %s ในฐาน HIS" % dc)
        if old:
            if str(old[0].get("p") or "").strip().upper() == px:
                continue                       # เท่าเดิม ไม่ต้องเขียน
            hc.execute("UPDATE opd_qs_clinic_doctor_prefix SET queue_prefix=%s"
                       " WHERE opd_qs_clinic_doctor_prefix_id=%s", (px, old[0]["i"]))
            updated += 1
            detail.append("แก้ %s/%s -> %s" % (cl, dc, px))
        else:
            hc.execute("INSERT INTO opd_qs_clinic_doctor_prefix"
                       " (opd_qs_clinic_doctor_prefix_id, clinic, doctor_code, queue_prefix)"
                       " VALUES (%s,%s,%s,%s)",
                       (next_id(hc, "opd_qs_clinic_doctor_prefix_id", "opd_qs_clinic_doctor_prefix"),
                        cl, dc, px))
            added += 1
            detail.append("เพิ่ม %s/%s = %s" % (cl, dc, px))
    n = added + updated + removed
    if not n:
        raise ValueError("ทุกรายการมีค่าตรงกับที่ขออยู่แล้ว — ไม่ได้แก้อะไร")
    head = "เพิ่ม %d · แก้ %d · ลบ %d" % (added, updated, removed)
    return n, (head + " · " + ", ".join(detail[:8]) + ("…" if len(detail) > 8 else ""))[:255]


def slot_renumber_request_from(json_row):
    """คำขอ "เปลี่ยนอักษรนำของคิวที่สร้างไปแล้ว" — {'slot_renumber': {...}}"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_renumber")
    return a if isinstance(a, dict) else None


def renumber_qs_prefix(hc, req):
    """เขียนเลขคิวที่สร้างไว้แล้วให้ใช้อักษรนำตัวใหม่ (E1001 -> E4001)

       ใช้หลังแก้ opd_qs_clinic_doctor_prefix — คิวเก่าที่เจนไปแล้วยังถืออักษรเดิมอยู่
       ทำเฉพาะ "ตั้งแต่วันที่กำหนด (ปกติวันนี้) เป็นต้นไป" — ของที่ผ่านไปแล้วเป็นประวัติ
       ไม่ควรแก้ย้อนหลัง เพราะเลขคิวคือสิ่งที่พิมพ์ในบัตรคิวที่คนไข้ถือไปแล้ว

       คิวที่ถูกจองแล้ว (slot_register='Y' / slot_busy='Y' / มี vn) จะข้ามโดยปริยาย
       ด้วยเหตุผลเดียวกัน — ถ้าจงใจจะเปลี่ยนจริง ๆ ให้ส่ง include_used=true มา
       เลขลำดับ (queue_slot_number_int) ไม่แตะ เปลี่ยนแค่ส่วนอักษรนำ
       คืน (จำนวนที่แก้, ข้อความสรุป)"""
    clinic = str(req.get("clinic") or "").strip()
    doc = str(req.get("doctor_code") or "").strip()
    if not doc:
        raise ValueError("คำขอไม่มีรหัสแพทย์")
    px = re.sub(r"[^0-9A-Za-z]", "", str(req.get("queue_prefix") or "")).upper()
    if not px:
        # ไม่ได้ส่งมาก็ไปอ่านจากตารางที่เพิ่งแก้
        px = queue_prefix_for(hc, clinic, doc, (clinic + ":" + doc) if clinic else doc)
    d1 = str(req.get("date_from") or "").strip()[:10] or datetime.date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d1):
        raise ValueError("วันที่เริ่มไม่ถูกต้อง (%s)" % d1)
    include_used = bool(req.get("include_used"))

    codes = [c for c in [(clinic + ":" + doc) if clinic else "", doc] if c]
    ph = ",".join(["%s"] * len(codes))
    rows = hc.query("SELECT opd_qs_slot_id i, queue_slot_number n, queue_slot_number_int k,"
                    " slot_register r, slot_busy b, vn FROM opd_qs_slot"
                    " WHERE schedule_date >= %%s AND doctor_code IN (%s)"
                    " ORDER BY schedule_date, queue_slot_number_int" % ph, tuple([d1] + codes))
    if not rows:
        raise ValueError("ไม่พบคิวของ %s ตั้งแต่ %s เป็นต้นไป — ไม่มีอะไรให้แก้" % ("/".join(codes), d1))
    changed = skipped_used = same = 0
    sample = []
    for r in rows:
        old = str(r.get("n") or "").strip()
        num = r.get("k")
        if not old or num is None:
            continue
        stem, pad = _num_parts(old, num)
        new = px + (str(int(num)).zfill(pad) if pad else str(int(num)))
        if new == old:
            same += 1
            continue
        used = (str(r.get("r") or "").upper() == "Y" or str(r.get("b") or "").upper() == "Y"
                or str(r.get("vn") or "").strip() != "")
        if used and not include_used:
            skipped_used += 1
            continue
        hc.execute("UPDATE opd_qs_slot SET queue_slot_number=%s WHERE opd_qs_slot_id=%s", (new, r["i"]))
        changed += 1
        if len(sample) < 2:
            sample.append("%s->%s" % (old, new))
    if not changed:
        raise ValueError("ไม่มีคิวที่ต้องแก้ (ตรงอยู่แล้ว %d · ถูกจองแล้วข้าม %d)" % (same, skipped_used))
    note = "เปลี่ยนอักษรนำเป็น %s: %d คิว (%s) ตั้งแต่ %s" % (px, changed, ", ".join(sample), d1)
    if same:
        note += " · ตรงอยู่แล้ว %d" % same
    if skipped_used:
        note += " · ข้ามที่จองแล้ว %d" % skipped_used
    return changed, note[:255]


def slot_master_request_from(json_row):
    """คำขอแก้ตารางพื้นฐานของระบบคิว — {'slot_master': {'ops': [...]}}
       ครอบคลุม opd_queue_slot (ช่วงเวลา) และ opd_queue_slot_type (ประเภทคิว)"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_master")
    return a if isinstance(a, dict) else None


def _hhmmss(v, label):
    """'08:30' / '08:30:00' -> datetime.time · รูปแบบอื่นถือว่าผิด"""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$", str(v or ""))
    if not m:
        raise ValueError("%s ต้องเป็นเวลา HH:MM (ได้มา %r)" % (label, v))
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or se > 59:
        raise ValueError("%s ไม่ใช่เวลาที่ถูกต้อง (%s)" % (label, v))
    return datetime.time(h, mi, se)


# ตารางพื้นฐาน 2 ตัวที่หน้าเว็บแก้ได้ · เก็บชื่อคอลัมน์ไว้ที่เดียว จะได้ไม่พิมพ์ผิดกระจาย
_MASTER = {
    "slot": {"table": "opd_queue_slot", "id": "opd_queue_slot_id", "name": "opd_queue_slot_name",
             "label": "ช่วงเวลา",
             "refs": [("opd_qs_slot s JOIN opd_queue_schedule q ON q.opd_queue_schedule_id=s.opd_queue_schedule_id",
                       "q.opd_queue_slot_id", "คิวที่สร้างแล้ว"),
                      ("opd_queue_schedule", "opd_queue_slot_id", "ใบเปิดตรวจ"),
                      ("opd_qs_schedule_tmpl_list", "opd_queue_slot_id", "บรรทัดในเทมเพลต")]},
    "limit": {"table": "opd_qs_limit_type", "id": "opd_qs_limit_type_id",
              "name": "opd_qs_limit_type_name", "label": "ชุดโควตา",
              "refs": [("opd_queue_schedule", "opd_qs_limit_type_id", "ใบเปิดตรวจ"),
                       ("opd_qs_schedule_tmpl_list", "opd_qs_limit_type_id", "บรรทัดในเทมเพลต")]},
}


def _sync_limit_acl(hc, tid, table, idcol, valcol, values, maxlen, label):
    """ให้รายชื่อ "ใครใช้ชุดโควตานี้ได้" ตรงกับที่หน้าจอส่งมาเป๊ะ

       opd_qs_limit_depcode = อนุญาตตามห้องตรวจ (depcode) · opd_qs_limit_user = อนุญาตตาม user (loginname)
       ทั้งสองตารางมีผลเมื่อชุดโควตานั้นตั้ง check_access='Y' เท่านั้น
       แถวที่มีอยู่แล้วคงไว้ (ไม่ลบแล้วเพิ่มใหม่) จะได้ไม่กิน id ไปเรื่อย ๆ โดยเปล่าประโยชน์"""
    want = []
    for v in (values or []):
        v = str(v or "").strip()
        if not v:
            continue
        if len(v) > maxlen:
            raise ValueError("ชุดโควตา %s: %r ยาวเกิน %d ตัวอักษร" % (label, v, maxlen))
        if v not in want:
            want.append(v)
    if len(want) > 200:
        raise ValueError("ชุดโควตา %s ใส่รายชื่อสิทธิเกิน 200 รายการ" % label)
    have = {}
    for r in hc.query("SELECT %s AS i, %s AS v FROM %s WHERE opd_qs_limit_type_id=%%s"
                      % (idcol, valcol, table), (tid,)):
        have[str(r["v"] or "").strip()] = r["i"]
    for v in want:
        if v not in have:
            hc.execute("INSERT INTO %s (%s, opd_qs_limit_type_id, %s) VALUES (%%s,%%s,%%s)"
                       % (table, idcol, valcol), (next_id(hc, idcol, table), tid, v))
    for v, rid in have.items():
        if v not in want:
            hc.execute("DELETE FROM %s WHERE %s=%%s" % (table, idcol), (rid,))
    return len(want)


def apply_qs_master(hc, req):
    """เพิ่ม/แก้/ลบ ช่วงเวลา (opd_queue_slot) และประเภทคิว (opd_queue_slot_type)

       สองตารางนี้เป็น "แม่แบบ" ที่ตารางคิวจริงอ้างถึงด้วย id — ลบทิ้งทั้งที่ยังมีคนอ้างอยู่
       จะได้คิวที่ชี้ไปยัง id ที่ไม่มีตัวตน หน้าจอ HOSxP จะโชว์ช่องว่างโดยไม่บอกสาเหตุ
       จึงนับผู้อ้างอิงก่อนเสมอ มีคนใช้อยู่ = ไม่ลบให้ (แก้ชื่อ/เวลาได้ตามปกติ)

       ops = [{'kind':'slot'|'type', 'op':'set'|'del', 'id'?, ...ค่าของตารางนั้น}]
       คืน (จำนวนที่เปลี่ยน, ข้อความสรุป)"""
    ops = req.get("ops") if isinstance(req.get("ops"), list) else []
    if not ops:
        raise ValueError("คำขอไม่มีรายการที่จะแก้")
    if len(ops) > 100:
        raise ValueError("แก้ได้ครั้งละไม่เกิน 100 รายการ")
    added = updated = removed = 0
    detail = []
    for o in ops:
        kind = str(o.get("kind") or "").strip().lower()
        M = _MASTER.get(kind)
        if not M:
            raise ValueError("ไม่รู้จักตาราง %r" % o.get("kind"))
        op = "del" if str(o.get("op") or "set").strip().lower() == "del" else "set"
        rid = o.get("id")
        rid = int(rid) if str(rid or "").strip().isdigit() else None

        if op == "del":
            if rid is None:
                raise ValueError("ลบ%sต้องมี id" % M["label"])
            for tbl, col, what in M["refs"]:
                n = int(hc.scalar("SELECT COUNT(*) FROM %s WHERE %s=%%s" % (tbl, col), (rid,)) or 0)
                if n:
                    raise ValueError("ลบ%s id %s ไม่ได้ — มี%sอ้างถึงอยู่ %d รายการ"
                                     % (M["label"], rid, what, n))
            if kind == "limit":
                # ลูกของชุดโควตาไม่มีใครอ้างถึงโดยตรง ลบไปพร้อมหัวได้ ไม่งั้นค้างเป็นขยะ
                # (รวมรายชื่อสิทธิห้องตรวจ/user ด้วย — เป็นของชุดโควตาใบนี้ ไม่ใช่ผู้อ้างอิงจากภายนอก)
                for ch in ("opd_qs_limit", "opd_qs_limit_depcode", "opd_qs_limit_user"):
                    hc.execute("DELETE FROM %s WHERE opd_qs_limit_type_id=%%s" % ch, (rid,))
            hc.execute("DELETE FROM %s WHERE %s=%%s" % (M["table"], M["id"]), (rid,))
            removed += 1
            detail.append("ลบ%s %s" % (M["label"], rid))
            continue

        name = str(o.get("name") or "").strip()[:200]
        if not name:
            raise ValueError("%s ต้องมีชื่อ" % M["label"])
        d = {M["name"]: name}
        if kind == "slot":
            a = _hhmmss(o.get("start_time"), "เวลาเริ่ม")
            b = _hhmmss(o.get("finish_time"), "เวลาสิ้นสุด")
            if b <= a:
                raise ValueError("ช่วง %s: เวลาสิ้นสุดต้องมากกว่าเวลาเริ่ม (%s - %s)" % (name, a, b))
            d["opd_queue_slot_start_time"] = a
            d["opd_queue_slot_finish_time"] = b
        else:
            d["check_access"] = "Y" if str(o.get("check_access") or "").strip().upper() == "Y" else "N"
            # จำนวนคิวแยกตามประเภท (Walkin/นัด/Online) — เก็บเป็นลูกในตาราง opd_qs_limit
            qty = {}
            for k, v in (o.get("qty") or {}).items():
                try:
                    k, v = int(k), int(v)
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    qty[k] = v
            if not qty:
                raise ValueError("ชุดโควตา %s ต้องมีจำนวนอย่างน้อย 1 ประเภท" % name)
            if sum(qty.values()) > 500:
                raise ValueError("ชุดโควตา %s รวมเกิน 500 คิว" % name)

        if rid is not None:
            if not hc.query("SELECT %s FROM %s WHERE %s=%%s" % (M["id"], M["table"], M["id"]), (rid,)):
                raise ValueError("ไม่พบ%s id %s" % (M["label"], rid))
            sets = ", ".join("%s=%%s" % k for k in d)
            hc.execute("UPDATE %s SET %s WHERE %s=%%s" % (M["table"], sets, M["id"]),
                       tuple(list(d.values()) + [rid]))
            updated += 1
            detail.append("แก้%s %s" % (M["label"], name))
        else:
            d[M["id"]] = next_id(hc, M["id"], M["table"])
            keys = list(d.keys())
            hc.execute("INSERT INTO %s (%s) VALUES (%s)"
                       % (M["table"], ",".join(keys), ",".join(["%s"] * len(keys))),
                       tuple(d[k] for k in keys))
            added += 1
            detail.append("เพิ่ม%s %s" % (M["label"], name))
        if kind == "limit":
            # ให้ลูกตรงกับที่ขอเป๊ะ: มีอยู่แล้วก็แก้จำนวน ไม่มีก็เพิ่ม ที่ไม่ได้ขอมาก็ลบ
            tid = rid if rid is not None else d[M["id"]]
            have = {}
            for r2 in hc.query("SELECT opd_qs_limit_id i, opd_queue_slot_type_id s FROM opd_qs_limit"
                               " WHERE opd_qs_limit_type_id=%s", (tid,)):
                have[int(r2["s"])] = r2["i"]
            for st, v in qty.items():
                if st in have:
                    hc.execute("UPDATE opd_qs_limit SET limit_qty=%s WHERE opd_qs_limit_id=%s", (v, have[st]))
                else:
                    hc.execute("INSERT INTO opd_qs_limit (opd_qs_limit_id, opd_qs_limit_type_id,"
                               " opd_queue_slot_type_id, limit_qty) VALUES (%s,%s,%s,%s)",
                               (next_id(hc, "opd_qs_limit_id", "opd_qs_limit"), tid, st, v))
            for st, lid in have.items():
                if st not in qty:
                    hc.execute("DELETE FROM opd_qs_limit WHERE opd_qs_limit_id=%s", (lid,))
            # รายชื่อสิทธิ — ส่งมาเฉพาะตอนที่หน้าจอแก้จริง ไม่มีคีย์ = ไม่แตะของเดิม
            # (คำขอเก่าที่ค้างคิวอยู่ก่อนอัปเดตจะไม่ล้างสิทธิที่ตั้งไว้ทิ้ง)
            nacl = []
            if "depcodes" in o:
                nacl.append("ห้องตรวจ %d" % _sync_limit_acl(
                    hc, tid, "opd_qs_limit_depcode", "opd_qs_limit_depcode_id", "depcode",
                    o.get("depcodes"), 3, name))
            if "users" in o:
                nacl.append("user %d" % _sync_limit_acl(
                    hc, tid, "opd_qs_limit_user", "opd_qs_limit_user_id", "staff",
                    o.get("users"), 50, name))
            if nacl:
                detail.append("สิทธิ %s (%s)" % (name, " · ".join(nacl)))

    n = added + updated + removed
    if not n:
        raise ValueError("ไม่มีรายการที่เปลี่ยน")
    head = "เพิ่ม %d · แก้ %d · ลบ %d" % (added, updated, removed)
    return n, (head + " · " + ", ".join(detail[:6]) + ("…" if len(detail) > 6 else ""))[:255]


def slot_tmpl_request_from(json_row):
    """คำขอสร้าง/แก้เทมเพลตตารางคิว — {'slot_tmpl': {'ops': [...]}}"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    a = j.get("slot_tmpl")
    return a if isinstance(a, dict) else None


def apply_qs_tmpl(hc, req):
    """เพิ่ม/แก้/ลบเทมเพลตตารางคิว (opd_qs_schedule_tmpl + opd_qs_schedule_tmpl_list)

       หัวเก็บ ชื่อ/รหัสที่ใช้/โควตารวม/ชนิด · ลูกเก็บว่าใช้ช่วงเวลาไหนกับชุดโควตาใด
       รูปแบบของ doctor_code ต้องคู่กับชนิดเทมเพลต (1 แพทย์ / 2 คลินิก / 3 คลินิก:แพทย์)
       ตั้งผิดคู่แล้วตอนสร้างตารางคิวจริงจะหาไม่เจอทั้งใบ จึงตรวจให้ตั้งแต่ตอนบันทึกเทมเพลต

       ops = [{'op':'set'|'del', 'id'?, 'name', 'tmpl_type', 'doctor_code', 'total_limit', 'lines':[...]}]
       คืน (จำนวนที่เปลี่ยน, ข้อความสรุป)"""
    ops = req.get("ops") if isinstance(req.get("ops"), list) else []
    if not ops:
        raise ValueError("คำขอไม่มีรายการที่จะแก้")
    if len(ops) > 50:
        raise ValueError("แก้ได้ครั้งละไม่เกิน 50 เทมเพลต")
    ok_types = set()
    for r in hc.query("SELECT opd_qs_schedule_tmpl_type_id i FROM opd_qs_schedule_tmpl_type", ()):
        ok_types.add(int(r["i"]))
    added = updated = removed = 0
    detail = []
    for o in ops:
        op = "del" if str(o.get("op") or "set").strip().lower() == "del" else "set"
        rid = o.get("id")
        rid = int(rid) if str(rid or "").strip().isdigit() else None

        if op == "del":
            if rid is None:
                raise ValueError("ลบเทมเพลตต้องมี id")
            hc.execute("DELETE FROM opd_qs_schedule_tmpl_list WHERE opd_qs_schedule_tmpl_id=%s", (rid,))
            hc.execute("DELETE FROM opd_qs_schedule_tmpl WHERE opd_qs_schedule_tmpl_id=%s", (rid,))
            removed += 1
            detail.append("ลบเทมเพลต %s" % rid)
            continue

        name = str(o.get("name") or "").strip()[:200]
        if not name:
            raise ValueError("เทมเพลตต้องมีชื่อ")
        try:
            tt = int(o.get("tmpl_type") or 0)
        except (TypeError, ValueError):
            raise ValueError("ชนิดเทมเพลตของ %s ต้องเป็นตัวเลข" % name)
        if tt not in ok_types:
            raise ValueError("ไม่รู้จักชนิดเทมเพลต %s (มีให้เลือก: %s)"
                             % (tt, ",".join(str(x) for x in sorted(ok_types))))
        doc = str(o.get("doctor_code") or "").strip()[:15]
        if not doc:
            raise ValueError("เทมเพลต %s ยังไม่มีรหัสแพทย์/คลินิก" % name)
        # รูปแบบรหัสต้องเข้ากับชนิด — 3 ต้องเป็น "คลินิก:แพทย์" ส่วน 1 กับ 2 ต้องไม่มี :
        if tt == 3 and ":" not in doc:
            raise ValueError("เทมเพลต %s ชนิด 'คลินิก : แพทย์' รหัสต้องเป็นรูปแบบ คลินิก:แพทย์ (ได้ %s)"
                             % (name, doc))
        if tt in (1, 2) and ":" in doc:
            raise ValueError("เทมเพลต %s ชนิดนี้ต้องใช้รหัสเดี่ยว ไม่ใช่ คลินิก:แพทย์ (ได้ %s)" % (name, doc))

        lines = o.get("lines") if isinstance(o.get("lines"), list) else []
        clean = []
        for l in lines:
            try:
                gid = int(l.get("opd_queue_slot_id"))
            except (TypeError, ValueError):
                raise ValueError("เทมเพลต %s มีบรรทัดที่ไม่ได้เลือกช่วงเวลา" % name)
            lt = l.get("opd_qs_limit_type_id")
            lt = int(lt) if str(lt or "").strip().isdigit() else None
            # คอลัมน์นี้เป็น NOT NULL ในฐาน — ปล่อยว่างแล้ว insert จะพังทั้งทรานแซกชัน
            if lt is None:
                raise ValueError("เทมเพลต %s: ทุกช่วงเวลาต้องเลือกชุดโควตา" % name)
            if not hc.query("SELECT opd_queue_slot_id FROM opd_queue_slot WHERE opd_queue_slot_id=%s", (gid,)):
                raise ValueError("ไม่พบช่วงเวลา id %s" % gid)
            if lt is not None and not hc.query(
                    "SELECT opd_qs_limit_type_id FROM opd_qs_limit_type WHERE opd_qs_limit_type_id=%s", (lt,)):
                raise ValueError("ไม่พบชุดโควตา id %s" % lt)
            clean.append((gid, lt))
        if not clean:
            raise ValueError("เทมเพลต %s ต้องมีอย่างน้อย 1 ช่วงเวลา" % name)

        # โควตารวม: ไม่ได้ส่งมาก็บวกจากชุดโควตาของทุกบรรทัดให้เอง
        tot = o.get("total_limit")
        if str(tot or "").strip().isdigit():
            tot = int(tot)
        else:
            tot = 0
            for _gid, lt in clean:
                if lt is None:
                    continue
                tot += int(hc.scalar("SELECT COALESCE(SUM(limit_qty),0) FROM opd_qs_limit"
                                     " WHERE opd_qs_limit_type_id=%s", (lt,)) or 0)

        head = {"opd_qs_schedule_tmpl_name": name, "doctor_code": doc,
                "total_limit": tot, "opd_qs_schedule_tmpl_type_id": tt}
        if rid is not None:
            if not hc.query("SELECT opd_qs_schedule_tmpl_id FROM opd_qs_schedule_tmpl"
                            " WHERE opd_qs_schedule_tmpl_id=%s", (rid,)):
                raise ValueError("ไม่พบเทมเพลต id %s" % rid)
            hc.execute("UPDATE opd_qs_schedule_tmpl SET %s WHERE opd_qs_schedule_tmpl_id=%%s"
                       % ", ".join("%s=%%s" % k for k in head),
                       tuple(list(head.values()) + [rid]))
            updated += 1
            detail.append("แก้เทมเพลต %s" % name)
        else:
            rid = next_id(hc, "opd_qs_schedule_tmpl_id", "opd_qs_schedule_tmpl")
            head["opd_qs_schedule_tmpl_id"] = rid
            keys = list(head.keys())
            hc.execute("INSERT INTO opd_qs_schedule_tmpl (%s) VALUES (%s)"
                       % (",".join(keys), ",".join(["%s"] * len(keys))), tuple(head[k] for k in keys))
            added += 1
            detail.append("เพิ่มเทมเพลต %s" % name)

        # ลูก: เขียนใหม่ทั้งชุด — จำนวนบรรทัดไม่เยอะ และไม่มีใครอ้าง id ของบรรทัดนี้ต่อ
        hc.execute("DELETE FROM opd_qs_schedule_tmpl_list WHERE opd_qs_schedule_tmpl_id=%s", (rid,))
        for gid, lt in clean:
            hc.execute("INSERT INTO opd_qs_schedule_tmpl_list (opd_qs_schedule_tmpl_list_id,"
                       " opd_qs_schedule_tmpl_id, opd_queue_slot_id, opd_qs_limit_type_id)"
                       " VALUES (%s,%s,%s,%s)",
                       (next_id(hc, "opd_qs_schedule_tmpl_list_id", "opd_qs_schedule_tmpl_list"),
                        rid, gid, lt))
    n = added + updated + removed
    if not n:
        raise ValueError("ไม่มีรายการที่เปลี่ยน")
    return n, ("เพิ่ม %d · แก้ %d · ลบ %d · %s" % (added, updated, removed, ", ".join(detail[:5])))[:255]


def create_qs_schedule(hc, req):
    """เปิดตารางออกตรวจ + สร้างคิวว่าง ให้ทีเดียวหลายวัน (คำขอจากหน้า slot-admin)

       สร้างครบ 3 ชั้นเหมือนที่ HOSxP สร้างเอง ขาดชั้นไหนหน้าจอฝั่ง HOSxP จะเห็นไม่ครบ:
         opd_queue_schedule   ใบเปิดตรวจ 1 แถว = วัน + แพทย์ + ช่วงเวลา
         opd_qs_slot          คิวว่างรายใบ แยกตามประเภทคิว (Walkin / นัด / Online)
         opd_qs_slot_summary  บรรทัดสรุปโควตาต่อช่วง — หน้าทำนัดใช้ตัวนี้เป็นตัวหาร

       สูตรเวลาถอดจากข้อมูลจริงที่ HOSxP สร้างไว้:
         ช่วง 08:00:00-08:59:59 โควตารวม 10 ใบ -> ก้าวละ 3599 // 10 = 359 วินาที
         ใบแรกเริ่มต้นช่วง + 1 วินาที และ "แต่ละประเภทคิวเริ่มนับเวลาใหม่จากต้นช่วง"
         (คิวคู่ขนาน ไม่ใช่ต่อคิวกัน) ส่วนเลขคิวเดินต่อกันไปทั้งวัน

       วันไหนมีใบเปิดตรวจของช่วงนั้นอยู่แล้ว = ข้าม ไม่เขียนทับของเดิม
       คืน (จำนวนคิวที่สร้าง, ข้อความสรุป)"""
    dfrom = str(req.get("date_from") or "")[:10]
    dto = str(req.get("date_to") or "")[:10]
    doc = str(req.get("doctor_code") or "").strip()
    doc_plain = str(req.get("doctor_plain") or "").strip()
    clinic = str(req.get("clinic") or "").strip()
    tmpl_type = int(req.get("tmpl_type") or 0) or None
    lines = req.get("lines") if isinstance(req.get("lines"), list) else []
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", dfrom) or not re.match(r"^\d{4}-\d{2}-\d{2}$", dto):
        raise ValueError("ช่วงวันที่ไม่ถูกต้อง (ต้องเป็น YYYY-MM-DD)")
    d1 = datetime.date(*[int(x) for x in dfrom.split("-")])
    d2 = datetime.date(*[int(x) for x in dto.split("-")])
    if d2 < d1:
        raise ValueError("วันสิ้นสุดอยู่ก่อนวันเริ่ม")
    # กันพิมพ์ปีผิดเท่านั้น — ปริมาณงานจริงวัดที่ "จำนวนวันหลังหักตัวกรอง" ซึ่งเช็คหลังสร้าง days
    if (d2 - d1).days > 3653:
        raise ValueError("ช่วงวันยาวเกิน 10 ปี — ตรวจปีที่กรอกอีกครั้ง")
    if not doc:
        raise ValueError("คำขอไม่มีรหัสแพทย์/คลินิก")
    if not lines:
        raise ValueError("คำขอไม่ได้เลือกช่วงเวลา")
    dows = set()
    for x in (req.get("dows") or []):
        try:
            dows.add(int(x))
        except (TypeError, ValueError):
            pass
    # สัปดาห์ที่เท่าไหร่ของเดือน — นับแบบ "ครั้งที่ N ของวันนั้นในเดือน" (จันทร์ที่ 1 = จันทร์ตัวแรก)
    # ตรงกับที่หน้าเว็บคำนวณไว้ ตัวเลขวันที่ต้องออกมาชุดเดียวกันเป๊ะ ไม่งั้นพรีวิวโกหก
    weeks = set()
    for x in (req.get("weeks") or []):
        try:
            weeks.add(int(x))
        except (TypeError, ValueError):
            pass

    # ตรวจให้ครบก่อนลงมือ — สร้างไปได้ครึ่งทางแล้วค่อยพัง คือสิ่งที่ตามเก็บยากที่สุด
    wins = _slot_windows(hc, [int(l.get("opd_queue_slot_id") or 0) for l in lines if l.get("opd_queue_slot_id")])
    plan = []
    for l in lines:
        gid = int(l.get("opd_queue_slot_id") or 0)
        if gid not in wins:
            raise ValueError("ไม่รู้จักช่วงเวลา opd_queue_slot_id=%s" % l.get("opd_queue_slot_id"))
        qty = {}
        for k, v in (l.get("limits") or {}).items():
            try:
                if int(v) > 0:
                    qty[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        if not qty:
            raise ValueError("ช่วง %s ยังไม่ได้กำหนดจำนวนคิว" % wins[gid]["name"])
        if sum(qty.values()) > 300:
            raise ValueError("ช่วง %s ขอคิวเกิน 300 ใบต่อวัน" % wins[gid]["name"])
        plan.append({"gid": gid, "qty": qty, "win": wins[gid],
                     "limit_name": str(l.get("limit_name") or "").strip()})

    # วันที่หน้าเว็บสั่งข้าม (วันหยุดแพทย์ / วันหยุดตามปฏิทิน) — ส่งมาเป็นรายวันเลย
    # ไม่ให้ gateway ไปคำนวณเอง เพราะพรีวิวที่คนกดเห็นต้องตรงกับของที่สร้างจริงเป๊ะ
    skip = set()
    for x in (req.get("skip_dates") or []):
        x = str(x or "").strip()[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", x):
            skip.add(x)
    days = []
    d = d1
    while d <= d2:
        if ((not dows or (d.isoweekday() % 7) in dows)    # 0=อาทิตย์ ให้ตรงกับ JS getDay()
                and (not weeks or ((d.day - 1) // 7 + 1) in weeks)
                and d.isoformat() not in skip):
            days.append(d)
        d += datetime.timedelta(days=1)
    if not days:
        raise ValueError("ช่วงวันที่เลือกไม่เหลือวันไหนเลย (หลังหักวันในสัปดาห์/สัปดาห์ในเดือน"
                         "%s)" % (" และวันหยุด %d วัน" % len(skip) if skip else ""))
    # วนสร้างทีละวัน วันละหลายคิวรี่ — คุมที่จำนวนวันจริง ไม่ใช่ระยะบนปฏิทิน
    if len(days) > 500:
        raise ValueError("คำขอนี้จะเปิดตรวจ %d วัน (เกิน 500) — แบ่งเป็นหลายคำขอ" % len(days))
    # เพดาน 3000 ต้องนับ "ใบที่จะสร้างจริง" ไม่ใช่ทุกวันที่ผ่านตัวกรอง
    # วัน+ช่วงเวลาไหนมีใบออกตรวจอยู่แล้วจะถูกข้าม (ไม่เขียนทับ) จึงไม่ควรถูกนับรวมในเพดาน
    # ไม่งั้นคนที่ต่อตารางเดือนถัดไปทับช่วงเดิมนิดหน่อย จะโดนปัดตกทั้งที่ของใหม่มีไม่กี่ใบ
    gids = sorted(set(p["gid"] for p in plan))
    exist = set()
    for r in hc.query("SELECT schedule_date d, opd_queue_slot_id g FROM opd_queue_schedule"
                      " WHERE schedule_date BETWEEN %s AND %s AND doctor_code=%s"
                      " AND opd_queue_slot_id IN (" + ",".join(["%s"] * len(gids)) + ")",
                      tuple([days[0].isoformat(), days[-1].isoformat(), doc] + gids)):
        exist.add((str(r["d"])[:10], int(r["g"])))
    total_plan = 0
    for day in days:
        ds = day.isoformat()
        for p in plan:
            if (ds, p["gid"]) not in exist:
                total_plan += sum(p["qty"].values())
    if not total_plan:
        raise ValueError("ทุกวันในช่วงนี้มีตารางของช่วงเวลาที่เลือกอยู่แล้ว — ไม่มีอะไรให้สร้าง")
    if total_plan > 5000:
        raise ValueError("คำขอนี้จะสร้างคิว %d ใบ (เกิน 5000 ใบที่สร้างจริง) — ย่อยช่วงวันให้สั้นลง"
                         % total_plan)

    prefix = queue_prefix_for(hc, clinic, doc_plain or doc, doc, req.get("prefix"))
    prio = {}
    for r in hc.query("SELECT opd_queue_slot_type_id s, COALESCE(queue_priority_id,99) p FROM opd_queue_slot_type", ()):
        prio[int(r["s"])] = int(r["p"])
    desc = str(req.get("description_name") or "").strip()
    refcl = str(req.get("ref_clinic_name") or "").strip()
    scols = table_columns(hc, "opd_qs_slot")
    hcols = table_columns(hc, "opd_queue_schedule")
    ucols = table_columns(hc, "opd_qs_slot_summary")
    now = datetime.datetime.now().replace(microsecond=0)

    made = skipped = sched_made = 0
    skip_days = []
    for day in days:
        ds = day.isoformat()
        # เลขคิวเดินต่อจากที่มีอยู่ของวันนั้น (เผื่อมาเปิดตารางเพิ่มทีหลังคนละช่วงเวลา)
        cur_no = int(hc.scalar("SELECT COALESCE(MAX(queue_slot_number_int),0) FROM opd_qs_slot"
                               " WHERE schedule_date=%s AND doctor_code=%s", (ds, doc)) or 0)
        for p in plan:
            ex = hc.query("SELECT opd_queue_schedule_id FROM opd_queue_schedule WHERE schedule_date=%s"
                          " AND doctor_code=%s AND opd_queue_slot_id=%s LIMIT 1", (ds, doc, p["gid"]))
            if ex:
                skipped += 1
                skip_days.append(ds)
                continue
            lt = ensure_limit_type(hc, p["qty"], p["limit_name"])
            sid = next_id(hc, "opd_queue_schedule_id", "opd_queue_schedule")
            h = {"opd_queue_schedule_id": sid, "schedule_date": ds, "doctor_code": doc,
                 "opd_queue_slot_id": p["gid"], "opd_qs_limit_type_id": lt,
                 "opd_qs_schedule_tmpl_type_id": tmpl_type, "slot_count": 0, "confirm_count": 0,
                 "available_count": 0, "description_name": desc[:120] or None,
                 "clinic_code": clinic or None, "ref_clinic_name": refcl[:120] or None,
                 "update_datetime": now}
            keys = [k for k in h if k.lower() in hcols and h[k] is not None]
            hc.execute("INSERT INTO opd_queue_schedule (%s) VALUES (%s)"
                       % (",".join(keys), ",".join(["%s"] * len(keys))), tuple(h[k] for k in keys))
            sched_made += 1

            a, b = p["win"]["a"], p["win"]["b"]
            span = max(1, _sec_of(b) - _sec_of(a))
            total = sum(p["qty"].values())
            step = max(1, span // total)
            n_here = 0
            for st_id in sorted(p["qty"], key=lambda k: (prio.get(k, 99), k)):
                for i in range(p["qty"][st_id]):
                    cur_no += 1
                    n_here += 1
                    t1 = _time_at(_sec_of(a) + 1 + i * step)
                    t2 = _time_at(_sec_of(t1) + step - 1)
                    s = {"opd_qs_slot_id": next_id(hc, "opd_qs_slot_id", "opd_qs_slot"),
                         "schedule_date": ds, "opd_queue_schedule_id": sid, "doctor_code": doc,
                         "queue_slot_number": prefix + str(cur_no).zfill(3),
                         "queue_slot_number_int": cur_no, "start_time": t1, "finish_time": t2,
                         "time_second": step, "slot_busy": "N", "slot_register": "N", "call_status": "N",
                         "opd_queue_slot_type_id": st_id, "opd_qs_schedule_tmpl_type_id": tmpl_type,
                         "slot_key": "%s%s-%d" % (t1.strftime("%H%M%S"), t2.strftime("%H%M%S"), cur_no),
                         "update_datetime": now}
                    keys = [k for k in s if k.lower() in scols and s[k] is not None]
                    hc.execute("INSERT INTO opd_qs_slot (%s) VALUES (%s)"
                               % (",".join(keys), ",".join(["%s"] * len(keys))), tuple(s[k] for k in keys))
            made += n_here
            hc.execute("UPDATE opd_queue_schedule SET slot_count=%s, available_count=%s"
                       " WHERE opd_queue_schedule_id=%s", (n_here, n_here, sid))

            # บรรทัดสรุป — HOSxP เก็บ "1 วัน = 1 แถว" แล้วต่อช่วงเวลาด้วย ',' โควตารวมกัน
            # แยกแถวตามช่วงเวลาจะทำให้หน้าจอ HOSxP โชว์วันเดียวกันซ้ำหลายบรรทัด
            txt = "%s - %s" % (a.strftime("%H:%M"), b.strftime("%H:%M"))
            summary_upsert(hc, ds, doc, tmpl_type, txt, n_here, ucols)

    if not made:
        raise ValueError("ทุกวันที่ขอมีตารางออกตรวจอยู่แล้ว — ไม่ได้สร้างอะไรเพิ่ม (%d ใบเดิม)" % skipped)
    note = "เปิดตรวจ %d ใบ · คิว %d ใบ · ขึ้นต้น %s" % (sched_made, made, prefix)
    if skipped:
        note += " · ข้าม %d ใบ (มีตารางแล้ว: %s)" % (skipped, ", ".join(sorted(set(skip_days))[:3]))
    return made, note[:255]


def qs_slot_freed_by_cancel(hc, r, oapp_id):
    """คิวที่ติด slot_register='Y' แต่ใบนัดเจ้าของถูกยกเลิกใน HOSxP แล้ว -> คืนให้จองใหม่ได้

       HOSxP ยกเลิกนัด (oapp_status_id=4) โดยไม่ล้าง slot_register/ref_key ของคิว
       กติกาเดียวกับ qsUsedSql ในหน้าทำนัด:
         - ยังไม่มี vn (คนไข้ยังไม่ได้มาใช้คิวนี้)
         - ไม่มีใบนัดที่ยังไม่ยกเลิก (นอกจากใบที่กำลังจอง) ชี้มาที่คิวนี้
         - มีใบนัดที่ถูกยกเลิกชี้มาจริง (ธงติดแต่ไม่มีใบนัดเลย = จองจากทางอื่น ไม่แตะ)"""
    if str(r.get("vn") or "").strip():
        return False
    sid = int(r["opd_qs_slot_id"])
    ref = str(r.get("ref_key") or "").strip()
    ref_id = ref[5:] if ref.upper().startswith("OAPP:") and ref[5:].isdigit() else None
    T = hc.cfg["table"]                      # ตารางนัดของ HIS (ปกติ oapp)
    has_col = "opd_qs_slot_id" in table_columns(hc, T)
    conds, args = [], []
    if has_col:
        conds.append("opd_qs_slot_id=%s")
        args.append(sid)
    if ref_id:
        conds.append("oapp_id=%s")
        args.append(int(ref_id))
    if not conds:
        return False
    rows = hc.query("SELECT oapp_id, oapp_status_id FROM %s WHERE (%s) AND oapp_id<>%%s"
                    % (T, " OR ".join(conds)), tuple(args) + (int(oapp_id),))
    if not rows:
        return False
    return all(str(x.get("oapp_status_id") or "").strip() == str(OAPP_STATUS_CANCELLED) for x in rows)


def claim_qs_slot(hc, slot_id, oapp_id):
    """จองคิวย่อยใน opd_qs_slot ให้ใบนัดที่เพิ่งสร้าง — ทำในทรานแซกชันเดียวกับ oapp

       รูปแบบนี้ลอกจากแถวที่ HOSxP จองมาจากใบนัดจริง:
         slot_register='Y' · ref_key='OAPP:<oapp_id>' · queue_datetime/update_datetime = เวลาที่จอง
       สิ่งที่ "ไม่" แตะโดยตั้งใจ:
         - slot_busy : ของจริงยังเป็น 'N' แม้จองแล้ว (คนละความหมายกับ "จอง")
         - vn        : คนไข้ยังไม่มา ยังไม่มี visit — HOSxP เติมให้เองตอนมาถึง
         - slot_key / opd_qs_room_id / register_staff : ตัวจัดคิวของ HOSxP เป็นคนใส่ เดาเองแล้วเสี่ยงผิด

       ล็อกแถวด้วย FOR UPDATE ก่อนเช็ค กัน gateway สองตัว (หรือเจ้าหน้าที่ที่หน้าจอ HOSxP)
       จองคิวเดียวกันพร้อมกัน · คืน (สำเร็จไหม, ข้อความ)"""
    try:
        sid = int(slot_id)
    except (TypeError, ValueError):
        return False, "opd_qs_slot_id ไม่ใช่ตัวเลข (%s)" % slot_id
    if sid <= 0:
        return False, ""
    rows = hc.query("SELECT opd_qs_slot_id, slot_register, ref_key, schedule_date, queue_slot_number, vn "
                    "FROM opd_qs_slot WHERE opd_qs_slot_id=%s FOR UPDATE", (sid,))
    if not rows:
        return False, "ไม่พบคิว opd_qs_slot_id=%s ในตาราง opd_qs_slot" % sid
    r = rows[0]
    num = str(r.get("queue_slot_number") or "").strip()
    mine = "OAPP:%s" % oapp_id
    if (str(r.get("slot_register") or "").strip().upper() == "Y" and str(r.get("ref_key") or "").strip() != mine
            and not qs_slot_freed_by_cancel(hc, r, oapp_id)):
        return False, "คิว %s (%s) ถูกจองไปแล้ว" % (num or sid, r.get("schedule_date"))
    now = datetime.datetime.now().replace(microsecond=0)
    hc.execute("UPDATE opd_qs_slot SET slot_register='Y', ref_key=%s, queue_datetime=%s, update_datetime=%s "
               "WHERE opd_qs_slot_id=%s", (mine, now, now, sid))
    return True, num or str(sid)


def mark_queue_done(cc, table, cid, oapp_id=None):
    """ปิดแถวคิวว่าเขียนเข้า HIS แล้ว + เก็บ oapp_id ของนัดจริงไว้ที่ ref_id

       ref_id = กุญแจอ้างกลับไปหาแถวใน HIS (คอลัมน์นี้มีอยู่แล้ว หน้าทะเบียนนัดใช้ส่ง oapp_id
       ของนัดเดิมมาให้) หน้า insight เอาไปเทียบว่าคิวกับ oapp ตรงกันจริงไหมแบบชนกันตรงตัว
       ไม่ต้องเดาจาก HN+วันนัด

       เขียน ref_id ไม่สำเร็จ (ตารางเก่าไม่มีคอลัมน์นี้) ต้องไม่ทำให้ทั้งแถวล้ม —
       นัดเขียนลง HIS ไปแล้ว ถ้าปล่อยให้ error แถวจะถูกส่งซ้ำรอบหน้า"""
    if oapp_id is not None:
        try:
            cc.execute("UPDATE %s SET status='done', ref_id=%%s WHERE id=%%s" % table,
                       (str(oapp_id)[:20], cid))
            cc.commit()
            return
        except Exception as e:
            cc.rollback()
            print("      %s[เตือน]%s เก็บ oapp_id ลง ref_id ไม่ได้: %s" % (C_WARN, C_END, e))
    cc.execute("UPDATE %s SET status='done' WHERE id=%%s" % table, (cid,))
    cc.commit()


def has_column(hc, table, column):
    """ตารางนี้มีคอลัมน์นี้ไหม — cache ไว้ ไม่ต้องถาม information_schema ซ้ำทุกแถว"""
    key = (str(table).lower(), str(column).lower())
    if key not in _HAS_COL:
        try:
            _HAS_COL[key] = str(column).lower() in table_columns(hc, table)
        except Exception:
            hc.rollback()
            _HAS_COL[key] = False
    return _HAS_COL[key]


_SERIAL_FN = {}       # id(conn) -> ฐานนี้มีฟังก์ชัน get_serialnumber ไหม
_SERIAL_SYNC = {}     # ชื่อคอลัมน์ -> ตรวจเทียบกับ MAX แล้ว ('ok' / 'max')


def _has_serial_fn(hc):
    """ฐานปลายทางมี get_serialnumber ไหม — ถามผ่าน catalog (ไม่เรียกฟังก์ชันตรง ๆ)
       เพราะถ้าเรียกฟังก์ชันที่ไม่มีอยู่ จะเป็น error กลางทรานแซกชัน แล้วพา insert ทั้งใบพังไปด้วย"""
    key = id(hc.conn)
    if key in _SERIAL_FN:
        return _SERIAL_FN[key]
    try:
        if hc.driver == "pgsql":
            n = hc.scalar("SELECT COUNT(*) FROM pg_proc WHERE proname='get_serialnumber'")
        else:
            n = hc.scalar("SELECT COUNT(*) FROM information_schema.ROUTINES"
                          " WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME='get_serialnumber'")
        ok = bool(n and int(n) > 0)
    except Exception:
        hc.rollback()
        ok = False
    _SERIAL_FN[key] = ok
    if not ok:
        print("  %s[เตือน]%s ฐานนี้ไม่มีฟังก์ชัน get_serialnumber -> ใช้ MAX+1 แทน "
              "(เสี่ยงชนกับเลขที่ HOSxP จ่ายเอง)" % (C_WARN, C_END))
    return ok


def next_id(hc, col, table):
    """ขอเลข primary key ตัวถัดไปแบบเดียวกับที่ HOSxP ใช้

       ตาราง HOSxP หลายตัวไม่ auto-increment แต่ใช้ get_serialnumber('<ชื่อคอลัมน์>')
       เป็นตัวจ่ายเลข (เก็บตัวนับไว้ในฐาน) ถ้าเราไปใช้ MAX+1 เอง ตัวนับจะไม่ขยับ
       รอบหน้าที่เจ้าหน้าที่สร้างเรคอร์ดจาก HOSxP จริง จะได้เลขที่เราใช้ไปแล้ว
       -> ชนกันเป็น duplicate key ที่ฝั่งผู้ใช้ HOSxP (ไม่ใช่ที่เครื่องมือเรา หาสาเหตุยาก)

       ครั้งแรกของแต่ละคอลัมน์ในการรันหนึ่ง ๆ จะเทียบกับ MAX ก่อน ถ้าตัวนับตามหลังอยู่
       (เพราะเคยถูกเขียนด้วย MAX+1 มาก่อน) จะเรียกซ้ำเพื่อดันตัวนับให้พ้น MAX
       — ดันขึ้นอย่างเดียว ปลอดภัยเสมอ อย่างมากก็แค่ข้ามเลขไปบ้าง"""
    if _has_serial_fn(hc):
        try:
            v = int(hc.scalar("SELECT get_serialnumber(%s)", (col,)) or 0)
        except Exception:
            hc.rollback()
            v = 0
        if v > 0:
            state = _SERIAL_SYNC.get(col)
            if state is None:
                cur = int(hc.scalar("SELECT COALESCE(MAX(%s),0) FROM %s" % (col, table)) or 0)
                if v <= cur:
                    for _ in range(5000):          # ดันตัวนับให้พ้นของเดิม (ทำครั้งเดียวต่อคอลัมน์)
                        v = int(hc.scalar("SELECT get_serialnumber(%s)", (col,)) or 0)
                        if v > cur:
                            break
                    if v > cur:
                        print("  %s[แก้ตัวนับ]%s %s ตามหลังอยู่ -> ดันขึ้นเป็น %s แล้ว"
                              % (C_WARN, C_END, col, v))
                        _SERIAL_SYNC[col] = "ok"
                    else:
                        print("  %s[เตือน]%s ตัวนับ %s ตามหลัง MAX (%s) มากเกินไป -> ใช้ MAX+1 แทน"
                              % (C_WARN, C_END, col, cur))
                        _SERIAL_SYNC[col] = "max"
                        return cur + 1
                else:
                    _SERIAL_SYNC[col] = "ok"
            if _SERIAL_SYNC.get(col) == "ok":
                return v
    return int(hc.scalar("SELECT COALESCE(MAX(%s),0)+1 FROM %s" % (col, table)) or 1)


def _first_col(cols, names):
    for n in names:
        if n.lower() in cols:
            return cols[n.lower()]
    return None


def insert_doctor_note_if_missing(hc, req):
    """Insert patient_visit_doctor_note for web-entered notes when the patient has no existing note."""
    if not req or not req.get("hn") or not req.get("text"):
        return 0
    table = "patient_visit_doctor_note"
    cols = table_columns(hc, table)
    if not cols:
        raise ValueError("ไม่พบตาราง patient_visit_doctor_note")
    text_col = _first_col(cols, ["doctor_note_text", "doctor_note", "doctornote", "note_text", "note", "note1", "detail", "cc"])
    if not text_col:
        raise ValueError("patient_visit_doctor_note ไม่มีคอลัมน์ข้อความ note ที่รู้จัก")

    hn_col = _first_col(cols, ["hn"])
    vn_col = _first_col(cols, ["vn"])
    if not hn_col:
        raise ValueError("patient_visit_doctor_note ไม่มีคอลัมน์ hn")

    # กันซ้ำอีกชั้นที่ปลายทาง: ถ้ามี note ที่มีข้อความของ HN นี้แล้ว ไม่ต้อง insert
    n = hc.scalar("SELECT COUNT(*) FROM %s WHERE %s=%%s AND COALESCE(%s,'')<>''" % (table, hn_col, text_col),
                  (req["hn"],))
    if n and int(n) > 0:
        return 0

    now = datetime.datetime.now()
    row = {hn_col: req["hn"], text_col: req["text"]}
    if vn_col and req.get("vn"):
        row[vn_col] = req["vn"]

    id_col = _first_col(cols, ["patient_visit_doctor_note_id", "doctor_note_id", "note_id", "id"])
    if id_col:
        row[id_col] = next_id(hc, id_col, table)

    guid_col = _first_col(cols, ["hos_guid", "guid", "uuid"])
    if guid_col:
        row[guid_col] = "{" + str(uuid.uuid4()).upper() + "}"

    note_dt_col = _first_col(cols, ["note_datetime", "doctor_note_datetime", "entry_datetime", "create_datetime", "created_at", "update_datetime"])
    if note_dt_col:
        row[note_dt_col] = now
    vstdate_col = _first_col(cols, ["vstdate", "note_date", "doctor_note_date", "entry_date", "create_date"])
    if vstdate_col:
        row[vstdate_col] = req.get("vstdate") or now.date()
    vsttime_col = _first_col(cols, ["vsttime", "note_time", "doctor_note_time", "entry_time", "create_time"])
    if vsttime_col:
        row[vsttime_col] = req.get("vsttime") or now.time().replace(microsecond=0)

    doctor_col = _first_col(cols, ["doctor", "doctor_code", "provider_code"])
    if doctor_col and req.get("doctor_code"):
        row[doctor_col] = req["doctor_code"]
    staff_col = _first_col(cols, ["staff", "entry_staff", "note_staff", "update_staff", "recorder"])
    if staff_col and req.get("staff"):
        row[staff_col] = req["staff"][:50]

    _ins(hc, table, row)
    return 1


def insert_xray(hc, oapp_id, orders):
    """เขียนรายการรังสีลง oapp_xray — oapp_xray_id ไม่ auto-increment จึงใช้ max+1 เหมือน oapp_id"""
    if not orders:
        return 0
    for o in orders:
        # กันซ้ำ: นัดใบเดียวกันสั่งรายการเดิมซ้ำ
        n = hc.scalar("SELECT COUNT(*) FROM oapp_xray WHERE oapp_id=%s AND xray_items_code=%s",
                      (oapp_id, o["code"]))
        if n and int(n) > 0:
            continue
        nid = next_id(hc, "oapp_xray_id", "oapp_xray")
        hc.execute("INSERT INTO oapp_xray (oapp_xray_id, oapp_id, xray_items_code) VALUES (%s,%s,%s)",
                   (nid, oapp_id, o["code"]))
    return len(orders)


def lab_orders_from(json_row):
    """ดึง lab จาก json_row เอาเฉพาะที่ map กับ lab master ได้ (code เป็นตัวเลข + type ITEM/PROFILE)"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return []
    out, seen = [], set()
    for o in (j.get("orders") or []):
        code = _num(o.get("code"))
        t = str(o.get("type") or "").upper()
        if code is None or t not in ("ITEM", "PROFILE"):
            continue                                  # ข้ามตัวที่ยังไม่ได้ map (code เป็น LAB-xxx)
        if (t, code) in seen:
            continue
        seen.add((t, code))
        out.append({"code": code, "type": t, "name": str(o.get("name") or o.get("raw") or "")[:250]})
    return out


LAB_LIST_MAX = 250          # ใช้เมื่ออ่านความยาวคอลัมน์จริงไม่ได้ (เดาแบบปลอดภัยไว้ก่อน)
_COLMAX = {}                # แคชความยาวคอลัมน์ต่อ (ตาราง, คอลัมน์) จะได้ไม่ถามสคีมาซ้ำทุกแถว


def col_max_len(hc, table, column):
    """ความยาวสูงสุดที่คอลัมน์รับได้ — text/clob คืน None (ไม่ต้องตัด), varchar(n) คืน n
       HOSxP แต่ละเวอร์ชันไม่เหมือนกัน (hosxp4 ใช้ text) จึงอ่านจากสคีมาแทนการเดา"""
    key = (table, column)
    if key in _COLMAX:
        return _COLMAX[key]
    val = LAB_LIST_MAX
    try:
        rows = hc.query("""SELECT data_type, character_maximum_length
                           FROM information_schema.columns
                           WHERE table_name=%s AND column_name=%s""", (table, column))
        if rows:
            n = rows[0].get("character_maximum_length")
            val = int(n) if n else None          # text -> None = ไม่ต้องตัด
    except Exception:
        hc.rollback()
    _COLMAX[key] = val
    return val


def lab_list_from_services(hc, oapp_id, table="oapp"):
    """อ่านรายการที่สั่งจริงจาก lab_app_order_service (ผูกกับนัดผ่าน lab_app_head.oapp_id)
       คืนข้อความคั่นด้วย , ตามลำดับที่สั่ง (ตัดชื่อซ้ำออก) — ตัดความยาวเฉพาะตอนคอลัมน์เป็น varchar"""
    rows = hc.query("""SELECT s.lab_name
                       FROM lab_app_order_service s
                       JOIN lab_app_head h ON h.lab_app_order_number = s.lab_app_order_number
                       WHERE h.oapp_id = %s
                       ORDER BY s.lab_app_order_service_id""", (oapp_id,))
    names = []
    for r in rows:
        nm = str(r.get("lab_name") or "").strip()
        if nm and nm not in names:
            names.append(nm)
    txt = ", ".join(names)
    cap = col_max_len(hc, table, "lab_list_text")
    return (txt[:cap] if cap else txt), len(names)


def sync_lab_list_text(hc, table, oapp_id, only_if_empty=False):
    """เขียน oapp.lab_list_text ให้ตรงกับใบสั่ง lab จริงของนัดใบนั้น
       only_if_empty=True -> เติมให้เฉพาะตอนที่ยังว่าง (กรณีนัดมีใบ lab อยู่ก่อนแล้ว ไม่ไปทับของเดิม)
       คืน (ข้อความที่เขียน, จำนวนรายการ) หรือ (None, 0) ถ้าไม่ได้เขียน"""
    txt, n = lab_list_from_services(hc, oapp_id, table)
    if not n:
        return None, 0
    if only_if_empty:
        cur = hc.scalar("SELECT lab_list_text FROM %s WHERE oapp_id=%%s" % table, (oapp_id,))
        if str(cur or "").strip():
            return None, 0
    hc.execute("UPDATE %s SET lab_list_text=%%s WHERE oapp_id=%%s" % table, (txt, oapp_id))
    return txt, n


def _ins(hc, table, row):
    row = {k: v for k, v in row.items() if v is not None}
    cols = list(row.keys())
    hc.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join(["%s"] * len(cols))),
               tuple(row.values()))
    return row


def resolve_lab_orders(hc, orders):
    """อ่านข้อมูลจริงจาก HIS + จัดกลุ่มตามกลุ่ม lab
       คืน (groups {group_code: {'prof':[...], 'item':[...]}}, gname, skipped)"""
    gname = {r["lab_items_group_code"]: r["lab_items_group_name"]
             for r in hc.query("SELECT lab_items_group_code, lab_items_group_name FROM lab_items_group")}

    prof, covered = [], {}
    for o in [x for x in orders if x["type"] == "PROFILE"]:
        g = hc.query("""SELECT lab_items_sub_group_name, lab_items_group_code, group_icode,
                               group_price, specimen_code
                        FROM lab_items_sub_group WHERE lab_items_sub_group_code=%s""", (o["code"],))
        if not g:
            raise ValueError("ไม่พบ PROFILE (sub group) %s (%s) ใน HIS" % (o["code"], o["name"]))
        g = g[0]
        its = hc.query("""SELECT lab_items_code, lab_items_name, lab_items_normal_value, lab_items_group
                          FROM lab_items WHERE lab_items_sub_group_code=%s AND active_status='Y'
                          ORDER BY lab_items_code""", (o["code"],))
        if not its:
            raise ValueError("PROFILE %s (%s) ไม่มี lab_items ที่ active อยู่ข้างใน" % (o["code"], o["name"]))
        prof.append((o, g, its))
        for it in its:
            covered[it["lab_items_code"]] = o["name"]

    items, skipped = [], []
    for o in [x for x in orders if x["type"] == "ITEM"]:
        if o["code"] in covered:                      # PROFILE ครอบอยู่แล้ว -> ไม่สั่งซ้ำ/ไม่คิดเงินซ้ำ
            skipped.append("%s (%s) อยู่ใน PROFILE '%s' แล้ว" % (o["name"], o["code"], covered[o["code"]]))
            continue
        it = hc.query("""SELECT lab_items_name, lab_items_group, lab_items_sub_group_code, icode,
                                service_price, specimen_code, lab_items_normal_value
                         FROM lab_items WHERE lab_items_code=%s""", (o["code"],))
        if not it:
            raise ValueError("ไม่พบ lab_items_code %s (%s) ใน HIS" % (o["code"], o["name"]))
        items.append((o, it[0]))

    def prof_group(g, its):
        # กลุ่มของ PROFILE: ใช้กลุ่มของ lab_items ข้างในเป็นหลัก (specimen/กลุ่มจริงอยู่ที่ item)
        # เพราะ lab_items_sub_group.lab_items_group_code หลาย รพ. ตั้งไม่ตรง เช่น CBC (Hematology)
        # ถูกตั้งเป็น Chemistry ทำให้ใบสั่งไม่แยกกลุ่ม -> fallback ไป group_code ของ sub_group ถ้า item ว่าง
        for it in its:
            if it.get("lab_items_group") is not None:
                return it["lab_items_group"]
        return g.get("lab_items_group_code")

    groups = {}
    for o, g, its in prof:                            # กลุ่มของ PROFILE (จาก item ข้างใน)
        groups.setdefault(prof_group(g, its), {"prof": [], "item": []})["prof"].append((o, g, its))
    for o, it in items:                               # กลุ่มของ ITEM
        groups.setdefault(it.get("lab_items_group"), {"prof": [], "item": []})["item"].append((o, it))
    return groups, gname, skipped


def insert_labs(hc, oapp_id, d, orders, staff, doctor_code=None):
    """สร้างใบสั่ง lab แยกตามกลุ่ม lab — 1 กลุ่ม = 1 lab_app_head (1 lab_app_order_number)
       - form_name = ชื่อกลุ่ม lab (เช่น 2-Chemistry)
       - แต่ละใบเป็นอิสระ ไม่ใส่ link_lab_order_number (ไม่ผูกใบเข้าด้วยกัน)
       - PROFILE แตกเป็น lab_items ย่อยลง lab_app_order
       - doctor_code = แพทย์ที่ลงในใบสั่ง (ทะเบียนนัด=รหัสคงที่ 9757 · หน้าโน้ตแพทย์=แพทย์ที่ login)
         ไม่ส่งมา -> ใช้ค่าคงที่ LAB_DOCTOR_CODE
       คืน (heads[], n_order รวม, n_service รวม, skipped[])"""
    doctor_code = doctor_code or LAB_DOCTOR_CODE
    if not orders:
        return [], 0, 0, []
    groups, gname, skipped = resolve_lab_orders(hc, orders)
    if not groups:
        return [], 0, 0, skipped

    now = datetime.datetime.now()
    heads, tot_ord, tot_svc = [], 0, 0

    for gc in sorted(groups.keys(), key=lambda x: (x is None, x)):
        num = next_id(hc, "lab_app_order_number", "lab_app_head")   # 1 กลุ่ม = 1 ใบ = 1 เลข
        bag = groups[gc]
        form = gname.get(gc) or ("กลุ่ม %s" % gc if gc is not None else "ไม่ระบุกลุ่ม")
        sub_list = ",".join(str(o["code"]) for o, _, _ in bag["prof"])

        _ins(hc, "lab_app_head", {
            "lab_app_order_number": num,
            "hn": d.get("hn"), "vn": d.get("vn"),
            "doctor_code": doctor_code,               # ทะเบียนนัด=9757 · หน้าโน้ตแพทย์=แพทย์ที่ login
            "order_date": now.date(), "order_time": now.time().replace(microsecond=0),
            "nextdate": d.get("nextdate"),
            "oapp_id": oapp_id,                       # ผูกกับนัดที่เพิ่งสร้าง
            "lab_items_group_code": gc,
            "form_name": str(form)[:200],             # ชื่อกลุ่ม lab
            "department": "OPD",
            "hospital_department_id": LAB_HOSPITAL_DEPARTMENT_ID,
            "order_department": d.get("depcode"),
            "sub_group_list": sub_list or None,
            # link_lab_order_number: ไม่ใส่เลยทุกใบ (แต่ละใบเป็นอิสระ ไม่ผูกกันตามที่ รพ. ต้องการ)
            # confirm_order: ไม่ใส่ -> เป็น NULL (ตามที่ รพ. ต้องการ)
            "confirm_report": "N",
            "lab_perform_status_id": 1, "lab_priority_id": 0,
            "finance_lab_clear": "N", "lab_request_type_id": 1,
            "entry_datetime": now, "update_datetime": now,
            "order_staff": str(staff)[:25],
        })

        seen = set()          # PK lab_app_order = (lab_app_order_number, lab_items_code) -> ห้ามซ้ำในใบเดียวกัน
        n_o = n_s = 0

        def add_service(typ, code, name, icode, price, spec):
            nonlocal n_s
            sid = next_id(hc, "lab_app_order_service_id", "lab_app_order_service")
            _ins(hc, "lab_app_order_service", {
                "lab_app_order_service_id": sid, "lab_app_order_number": num,
                "lab_order_type": typ, "lab_code": code, "lab_name": str(name)[:250],
                "icode": icode, "price": price, "vn": d.get("vn"),
                "department": "OPD", "specimen_code": spec,
            })
            n_s += 1

        def add_order(it, sub_code):
            nonlocal n_o
            code_i = it["lab_items_code"]
            if code_i in seen:
                return
            seen.add(code_i)
            _ins(hc, "lab_app_order", {
                "lab_app_order_number": num,
                "lab_items_code": code_i,
                "lab_items_sub_group_code": sub_code,
                "order_type": "A", "confirm": "N", "staff": str(staff)[:50],
                "lab_items_name_ref": (str(it.get("lab_items_name") or "")[:150] or None),
                "lab_items_normal_value_ref": (str(it["lab_items_normal_value"])[:200]
                                               if it.get("lab_items_normal_value") else None),
                "check_key_a": str(sub_code) if sub_code is not None else None,
            })
            n_o += 1

        for o, g, its in bag["prof"]:                 # PROFILE -> 1 service + หลาย order
            add_service("PROFILE", o["code"], o["name"], g.get("group_icode"), g.get("group_price"), g.get("specimen_code"))
            for it in its:
                add_order(it, o["code"])
        for o, it in bag["item"]:                     # ITEM -> 1 service + 1 order
            add_service("ITEM", o["code"], o["name"], it.get("icode"), it.get("service_price"), it.get("specimen_code"))
            # สั่งเป็น ITEM (lab_order_type='ITEM') -> lab_app_order ไม่ผูก sub group
            # lab_items_sub_group_code = NULL และ check_key_a = NULL (ส่ง sub_code=None)
            add_order(dict(it, lab_items_code=o["code"]), None)

        heads.append({"num": num, "group": form, "orders": n_o, "services": n_s})
        tot_ord += n_o; tot_svc += n_s

    return heads, tot_ord, tot_svc, skipped


def gen_hos_guid():
    """UUID v4 ตัวพิมพ์ใหญ่ ครอบปีกกา — ยืนยันจาก ovst จริง 9,471/9,471 แถวเป็นรูปแบบนี้"""
    return "{%s}" % str(uuid.uuid4()).upper()


def gen_ovst_key():
    """hex 32 ตัวพิมพ์ใหญ่ ไม่มีขีด (ไม่ใช่ UUID — ของจริงไม่มีหลักบอกเวอร์ชัน)"""
    return uuid.uuid4().hex.upper()


def be_yymmdd(d):
    """ปี พ.ศ. 2 หลัก + MMDD — ใช้ทั้งขึ้นต้น vn และตั้งชื่อ serial ของเลขคิว"""
    return "%02d%02d%02d" % ((d.year + 543) % 100, d.month, d.day)


def gen_vn(hc, now):
    """vn = ปี พ.ศ. 2 หลัก + MMDD + HHMMSS ของเวลาที่เขียน
       ถ้าชนกับที่มีอยู่ (คนละคนเขียนวินาทีเดียวกัน) เลื่อนไปวินาทีถัดไป"""
    for i in range(10):
        t = now + datetime.timedelta(seconds=i)
        vn = be_yymmdd(t) + t.strftime("%H%M%S")
        if not hc.scalar("SELECT 1 FROM ovst WHERE vn=%s LIMIT 1", (vn,)):
            return vn, t, i
    raise RuntimeError("หา vn ที่ไม่ซ้ำไม่ได้ (ลองแล้ว 10 วินาทีติดกัน)")


def next_oqueue(hc, d):
    """เลขคิวของวัน ขอจากตัวนับของ HOSxP เอง: get_serialnumber('ovst-q-<YYMMDD พ.ศ.>')

       ห้ามใช้ MAX(oqueue)+1 เพราะตัวนับในตาราง serial จะไม่ขยับ รอบหน้าที่เจ้าหน้าที่
       เปิด visit จาก HOSxP จริงจะได้เลขที่เราใช้ไปแล้ว -> คิวซ้ำที่ฝั่งผู้ใช้ หาสาเหตุยาก
       (เหตุผลเดียวกับ next_id ด้านบน)"""
    name = "ovst-q-" + be_yymmdd(d)
    if _has_serial_fn(hc):
        try:
            v = int(hc.scalar("SELECT get_serialnumber(%s)", (name,)) or 0)
            if v > 0:
                return v, name
        except Exception:
            hc.rollback()
    v = int(hc.scalar("SELECT COALESCE(MAX(oqueue),0)+1 FROM ovst WHERE vstdate=%s", (d,)) or 1)
    print("  %s[เตือน]%s ไม่มี get_serialnumber -> ใช้ MAX+1 (%s) เสี่ยงชนกับเลขที่ HOSxP จ่ายเอง"
          % (C_WARN, C_END, v))
    return v, name


def refer_from(json_row):
    """รหัส รพ. ต้นทางที่ส่งคนไข้มา — ใช้ทำเครื่องหมาย 'รับต่อจากสถานพยาบาลอื่น' ใน ovst"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return ""
    h = j.get("from_hospital")
    return str((h or {}).get("code") or "").strip() if isinstance(h, dict) else ""


def patient_exists(hc, hn):
    """HN นี้มีบัตรในฐาน HIS ปลายทางไหม

       ถ้าไม่มีแล้วยัง INSERT ovst จะได้ VN ที่ไม่มีคนไข้ผูกอยู่ — HOSxP เปิดดูไม่เจอ
       ต้องไปตามลบทิ้งทีหลัง จึงต้องกันไว้ก่อน
       คืน None เมื่อตรวจไม่ได้ (ไม่มีตาราง patient) -> ไม่เอามาตัดสิน"""
    try:
        return bool(hc.scalar("SELECT 1 FROM patient WHERE hn=%s LIMIT 1", (hn,)))
    except Exception:
        hc.rollback()
        return None


# รหัสที่หน้าเว็บฝังมาคงที่ ถ้าตั้งไว้สำหรับอีกฐานหนึ่ง visit จะเปิดได้แต่ข้อมูลอ้างอิงว่าง
# ตรวจให้เห็นตั้งแต่ preview ดีกว่าไปงงทีหลังว่าทำไมหน้าจอ HOSxP ไม่ขึ้นแผนก/แพทย์
_MASTER_CHECKS = [("doctor", "doctor", "code"),
                  ("cur_dep", "kskdepartment", "depcode"),
                  ("main_dep", "kskdepartment", "depcode"),
                  ("pttype", "pttype", "pttype"),
                  ("spclty", "spclty", "spclty"),
                  ("hospmain", "hospcode", "hospcode"),
                  ("hospsub", "hospcode", "hospcode")]


def check_masters(hc, d):
    """คืนรายชื่อรหัสที่หาไม่เจอในตารางแม่ของฐานปลายทาง (เตือนอย่างเดียว ไม่บล็อก)"""
    bad = []
    for field, table, col in _MASTER_CHECKS:
        v = d.get(field)
        if not v:
            continue
        try:
            if not hc.scalar("SELECT 1 FROM %s WHERE %s=%%s LIMIT 1" % (table, col), (v,)):
                bad.append("%s=%s (ไม่มีใน %s)" % (field, v, table))
        except Exception:
            hc.rollback()          # ไม่มีตารางแม่ตัวนั้นในรุ่นนี้ -> ข้ามไป ไม่ถือเป็นความผิด
    return bad


def ovst_from(json_row):
    """ดึงบล็อก ovst ที่หน้าเว็บเตรียมมา (ค่าที่ผูกกับเวลาเป็น null รอเราสร้าง)"""
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return {}
    o = j.get("ovst")
    return dict(o) if isinstance(o, dict) else {}


def ovst_fixed(row):
    """ค่าคงที่ที่หน้าเว็บส่งมา + HN สำรองจากคอลัมน์ของแถวคิว"""
    o = ovst_from(row.get("json_row"))
    d = {k: o.get(k) for k in OVST_FIXED_FIELDS if o.get(k) not in (None, "")}
    if not d.get("hn"):
        d["hn"] = row.get("hn")
    return d


def build_ovst(hc, row, now=None):
    """ประกอบแถว ovst ที่จะเขียนจริง — สร้างค่าที่ขาด ณ วินาทีนี้"""
    d = ovst_fixed(row)
    now = now or datetime.datetime.now()
    vn, t, shifted = gen_vn(hc, now)
    d["vn"] = vn
    d["vstdate"] = t.date()
    d["vsttime"] = t.strftime("%H:%M:%S")
    d["hos_guid"] = gen_hos_guid()
    d["ovst_key"] = gen_ovst_key()
    added = fill_ovst_extras(hc, d, row)

    # ขอเลขคิวเป็นขั้นสุดท้ายเสมอ — get_serialnumber ของฐานนี้สร้างจาก sequence แล้วเรียก
    # nextval() ซึ่ง PostgreSQL ตั้งใจไม่ให้ rollback (กันเซสชันอื่นรอคิว) เลขที่ขอไปแล้ว
    # จึงหายถาวรแม้ทรานแซกชันจะถูกยกเลิก -> คิวกระโดด (เคยเกิดจริง: 1 แล้วข้ามไป 3)
    # ตรวจค่าที่จำเป็นให้ครบก่อนขอ จะได้ไม่กินเลขทิ้งเพราะแถวที่เขียนไม่ได้อยู่ดี
    miss = [k for k in OVST_REQUIRED if k != "oqueue" and not d.get(k)]
    if miss:
        raise ValueError("ขาดค่าที่จำเป็น: " + ", ".join(miss))
    q, qname = next_oqueue(hc, t.date())
    cols = table_columns(hc, "ovst")
    if (not cols) or ("oqueue" in cols):
        d["oqueue"] = q
    return d, {"vn_shifted": shifted, "queue_serial": qname, "extras": added}


def fill_ovst_extras(hc, d, row):
    """เติมคอลัมน์ที่ HOSxP เติมเองทุกแถว + ทำเครื่องหมายว่าเป็นผู้ป่วยรับส่งต่อ

       เขียนเฉพาะคอลัมน์ที่มีจริงในฐานปลายทาง และเฉพาะช่องที่หน้าเว็บไม่ได้ส่งมา
       (หน้าเว็บชนะเสมอ — ที่นี่แค่เติมช่องว่าง ไม่ทับของที่ส่งมาแล้ว)
       ท้ายสุดตัดคอลัมน์ที่ HOSxP รุ่นนี้ไม่มีทิ้ง กัน INSERT ล้มทั้งแถว"""
    cols = table_columns(hc, "ovst")
    has = lambda k: (not cols) or (k in cols)
    added = {}
    for k, v in OVST_DEFAULTS.items():
        if has(k) and d.get(k) in (None, ""):
            d[k] = v
            added[k] = v
    if has("ovstist") and not d.get("ovstist"):
        d["ovstist"] = OVST_REFER_IST
        added["ovstist"] = d["ovstist"]
    src = refer_from(row.get("json_row"))
    if src and has("rfrilct") and not d.get("rfrilct"):
        d["rfrilct"] = src                       # สถานพยาบาลที่ส่งมา (ใช้ในรายงานรับส่งต่อ)
        added["rfrilct"] = src
    if cols:
        for k in [k for k in d if k not in cols]:
            d.pop(k)
    return added


def build_visit_pttype(hc, ovst_row):
    """แถวสิทธิที่คู่กับ ovst ที่เพิ่งสร้าง — คืน (แถวที่จะเขียน, เหตุผลที่เขียนไม่ได้)

       ค่าสิทธิลอกจากแถว ovst ทั้งหมด ไม่ไปหาเองจากที่อื่น เพราะถ้าสองตารางไม่ตรงกัน
       HOSxP จะโชว์สิทธิคนละอันกับที่บันทึกไว้ใน visit"""
    cols = table_columns(hc, VISIT_PTTYPE_TABLE)
    if not cols:
        return None, "ฐานปลายทางไม่มีตาราง %s (หรืออ่านโครงสร้างไม่ได้)" % VISIT_PTTYPE_TABLE
    if not ovst_row.get("pttype"):
        return None, "คำขอไม่มี pttype (%s.pttype เป็น NOT NULL)" % VISIT_PTTYPE_TABLE
    d = {"vn": ovst_row["vn"], "pttype": ovst_row["pttype"]}
    for k in VISIT_PTTYPE_FROM_OVST:
        if k in ovst_row:
            d[k] = ovst_row[k]
    d.update(VISIT_PTTYPE_DEFAULTS)
    for k in [k for k in d if k not in cols]:      # HOSxP รุ่นที่คอลัมน์ไม่ครบ
        d.pop(k)
    return d, None


def referin_number_from(row):
    """เลขที่ส่งต่อของต้นทาง — json_row ส่ง referout_no มาให้ ถ้าไม่มีใช้ ref_id ของแถวคิว
       (เป็นเลขเดียวกัน ref_id ถูกตั้งจาก referout_no ตอนหน้าเว็บส่งคำขอเข้าฐานกลาง)"""
    try:
        j = json.loads(row.get("json_row") or "{}")
    except Exception:
        j = {}
    return str(j.get("referout_no") or row.get("ref_id") or "").strip()


def build_referin(hc, ovst_row, queue_row):
    """ทะเบียนรับส่งต่อของ visit ที่เพิ่งเปิด — คืน (แถวที่จะเขียน, เหตุผลที่เขียนไม่ได้)

       วัน/เวลาใช้ของ visit ที่เปิด ไม่ใช่วันที่ต้นทางออกใบส่งตัว เพราะ feed ไม่ได้ส่งวันนั้นมา
       (แถวที่เจ้าหน้าที่คีย์มือก็ลงวันที่รับตัวเหมือนกัน)"""
    cols = table_columns(hc, REFERIN_TABLE)
    if not cols:
        return None, "ฐานปลายทางไม่มีตาราง %s (หรืออ่านโครงสร้างไม่ได้)" % REFERIN_TABLE
    src = refer_from(queue_row.get("json_row"))
    num = referin_number_from(queue_row)
    if not (src or num):
        return None, "คำขอไม่มีทั้งรหัส รพ. ต้นทางและเลขที่ส่งต่อ"
    d = {"vn": ovst_row["vn"], "hn": ovst_row.get("hn"),
         "refer_date": ovst_row.get("vstdate"), "refer_time": ovst_row.get("vsttime"),
         "refer_hospcode": src or None, "referin_number": num or None}
    d.update(REFERIN_DEFAULTS)
    return {k: v for k, v in d.items() if v is not None and k in cols}, None


def referin_exists(hc, vn):
    try:
        return bool(hc.scalar("SELECT 1 FROM %s WHERE vn=%%s LIMIT 1" % REFERIN_TABLE, (vn,)))
    except Exception:
        hc.rollback()
        return None                  # ตรวจไม่ได้ -> ไม่เอามาตัดสิน


def visit_table():
    return re.sub(r"[^A-Za-z0-9_]", "", VISIT_TABLE)


def fetch_visit_pending(central, limit, day=None):
    """คำขอเปิด visit ของวันนี้ — visit_date คือวันที่คนไข้จะมา (ตรงกับ ovst.vstdate ที่จะเขียน)

       กรองวันเหมือนคิวนัดที่กรอง datetime วันนี้ ไม่งั้นคำขอที่ค้างจากวันก่อน
       จะถูกเปิด visit ให้ในวันที่คนไข้ไม่ได้มา (vstdate เป็นวันที่เขียน ไม่ใช่วันที่ขอ)"""
    return central.query(
        "SELECT id, hn, vn, ref_id, status, json_row, requested_at, visit_date FROM %s "
        "WHERE status='pending' AND visit_date=%%s ORDER BY id LIMIT %d" % (visit_table(), limit),
        (day or datetime.date.today(),))


def count_visit_other_days(central, day=None):
    """คำขอที่ยัง pending แต่ไม่ใช่ของวันนี้ (รวมพวกที่ไม่ได้ระบุวัน) — เอาไว้บอกผู้ใช้
       ว่ามีของค้างอยู่นะ แค่ไม่ถูกหยิบมาทำรอบนี้ จะได้ไม่นึกว่าคิวว่าง"""
    try:
        return int(central.scalar(
            "SELECT COUNT(*) FROM %s WHERE status='pending' AND (visit_date IS NULL OR visit_date<>%%s)"
            % visit_table(), (day or datetime.date.today(),)) or 0)
    except Exception:
        central.rollback()
        return 0


def today_bounds(days_back=None):
    """ช่วงเวลาของคิวที่จะหยิบมาทำ: 00:00 ของ N วันก่อน ถึง 00:00 ของพรุ่งนี้

       N มาจาก SYNC_BACK_DAYS (env BMS_SYNC_BACK_DAYS หรือ --days)
       0 = เฉพาะคิวที่สร้างวันนี้ · อ่านค่าตอนเรียกทุกครั้ง เพราะ --days แก้ค่านี้ตอนรัน"""
    n = SYNC_BACK_DAYS if days_back is None else max(0, int(days_back))
    today = datetime.date.today()
    d0 = today - datetime.timedelta(days=n)
    d1 = today + datetime.timedelta(days=1)
    return d0.strftime("%Y-%m-%d 00:00:00"), d1.strftime("%Y-%m-%d 00:00:00")


def window_label():
    """ข้อความบอกช่วงคิวที่กำลังใช้ — เอาไว้พิมพ์ให้คนดูรู้ว่าหยิบย้อนหลังอยู่กี่วัน"""
    a, b = today_bounds()
    return ("เฉพาะวันนี้ (%s)" % a[:10]) if SYNC_BACK_DAYS == 0 else            ("%s ถึง %s (ย้อนหลัง %d วัน)" % (a[:10], datetime.date.today().strftime("%Y-%m-%d"), SYNC_BACK_DAYS))


def fetch_pending(central, limit):
    t = central.cfg["table"]
    return central.query(
        "SELECT id, hn, vn, datetime, status, json_row FROM %s WHERE status='pending' AND datetime >= %%s AND datetime < %%s ORDER BY id LIMIT %d" % (t, limit),
        today_bounds())


def slot_table():
    return re.sub(r"[^A-Za-z0-9_]", "", SLOT_TABLE)


def fetch_slot_pending(central, limit):
    """คำขอ "เพิ่มคิว" ที่พยาบาลกดไว้ (ตาราง slot_add_queue) — ยังไม่ถูกทำ = status 'pending'

       ไม่กรองด้วยวันที่สร้างเหมือนคิวใบนัด เพราะคำขอเพิ่มคิวมักทำล่วงหน้าข้ามวัน
       ถ้าค้างไว้ก็ต้องได้ทำอยู่ดี (ตราบใดที่วันนัดยังมาไม่ถึง)"""
    return central.query(
        "SELECT id, schedule_date, doctor_code, clinic_code, queue_schedule_id, queue_slot_id,"
        " slot_count, reason, requested_by, hn, json_row FROM %s "
        "WHERE status='pending' ORDER BY id LIMIT %d" % (slot_table(), limit))


def slot_request_row(r):
    """แถวในตารางคำขอ -> dict ที่ add_qs_slots ใช้ (อ่านจากคอลัมน์จริงก่อน ไม่ต้องแกะ json)"""
    req = slot_add_request_from(r.get("json_row")) or {}
    out = {
        "schedule_date": str(r.get("schedule_date") or req.get("schedule_date") or "")[:10],
        "doctor_code": str(r.get("doctor_code") or req.get("doctor_code") or "").strip(),
        "count": int(r.get("slot_count") or req.get("count") or 0),
        "opd_queue_schedule_id": r.get("queue_schedule_id") or req.get("opd_queue_schedule_id"),
        "opd_queue_slot_id": r.get("queue_slot_id") or req.get("opd_queue_slot_id"),
        "reason": str(r.get("reason") or req.get("reason") or ""),
        "by": str(r.get("requested_by") or req.get("by") or ""),
    }
    return out


def slot_mark(cc, cid, status, note=""):
    """ปิดงานคำขอเพิ่มคิว — เก็บผลไว้ในแถวเดียวกัน (สร้างคิวอะไรไปบ้าง / พังเพราะอะไร)"""
    try:
        cc.execute("UPDATE %s SET status=%%s, result_note=%%s, done_at=%%s WHERE id=%%s" % slot_table(),
                   (status, str(note or "")[:255], datetime.datetime.now().replace(microsecond=0), cid))
        cc.commit()
    except Exception as e:
        cc.rollback()
        print("      %s[เตือน]%s อัปเดตสถานะคำขอเพิ่มคิวไม่ได้: %s" % (C_WARN, C_END, e))


def fetch_lab_pending(central, limit):
    """แถวจากหน้า 'ทะเบียนนัด' — นัดมีอยู่แล้วใน HIS ต้องการแค่ใบสั่ง lab เพิ่ม"""
    t = central.cfg["table"]
    return central.query(
        "SELECT id, hn, vn, datetime, status, json_row FROM %s WHERE status='lab_pending' AND datetime >= %%s AND datetime < %%s ORDER BY id LIMIT %d" % (t, limit),
        today_bounds())


def oapp_id_from(json_row):
    try:
        j = json.loads(json_row or "{}")
    except Exception:
        return None
    return _num(j.get("oapp_id"))


# ---- ตรวจก่อนเขียน: รหัสในคิวอยู่ในฐาน HIS ปลายทางจริงไหม ----
# เครื่องมือฝั่งเว็บอ่าน lab master จากฐาน รพ. ผ่าน tunnel ส่วน gateway เขียนลงฐานที่ตั้งใน
# BMS_HIS_* ถ้าสองอันนี้คนละฐาน รหัสจะไม่ตรงเลย แล้วทุกแถวจะกลายเป็น error ทีละร้อยแถว
# จึงสุ่มตรวจก่อน ถ้าตรงน้อยผิดปกติให้หยุดแล้วบอกสาเหตุ แทนที่จะไล่ทำจนพัง
PREFLIGHT_MIN_CODES = 3       # รหัสน้อยกว่านี้ไม่ตัดสิน (ข้อมูลน้อยเกินจะสรุป)
PREFLIGHT_MIN_RATE = 0.5      # ต่ำกว่านี้ถือว่าน่าจะตั้งฐานผิด


def _codes_in(hc, table, col, codes):
    """คืนเซ็ตของรหัสที่มีอยู่จริงในตารางนั้น (แบ่งยิงทีละ 500 กัน SQL ยาวเกิน)"""
    out = set()
    codes = [c for c in codes if c is not None]
    for i in range(0, len(codes), 500):
        part = codes[i:i + 500]
        ph = ",".join(["%s"] * len(part))
        try:
            for x in hc.query("SELECT %s AS c FROM %s WHERE %s IN (%s)" % (col, table, col, ph), tuple(part)):
                out.add(x["c"])
        except Exception:
            hc.rollback()          # ไม่มีตารางนั้น (HOSxP บางรุ่น) -> ถือว่าตรวจไม่ได้
            return None
    return out


def preflight_codes(hc, rows):
    """สรุปว่ารหัส lab/x-ray ในคิวมีในฐานปลายทางกี่ % + ตัวอย่างที่ไม่เจอ"""
    want = {"PROFILE": {}, "ITEM": {}, "XRAY": {}}      # code -> ชื่อ (ไว้โชว์ตอนแจ้งเตือน)
    for r in rows:
        for o in lab_orders_from(r["json_row"]):
            want[o["type"]][o["code"]] = o.get("name") or ""
        for o in xray_orders_from(r["json_row"]):
            want["XRAY"][o["code"]] = o.get("name") or ""
    checks = [("ITEM", "lab_items", "lab_items_code"),
              ("PROFILE", "lab_items_sub_group", "lab_items_sub_group_code"),
              ("XRAY", "xray_items", "xray_items_code")]
    total = found = 0
    missing = []
    for kind, table, col in checks:
        codes = list(want[kind].keys())
        if not codes:
            continue
        hit = _codes_in(hc, table, col, codes)
        if hit is None:                                  # ตรวจตารางนี้ไม่ได้ -> ไม่นับรวม
            continue
        total += len(codes)
        found += len(hit)
        for c in codes:
            if c not in hit:
                missing.append((kind, c, want[kind][c]))
    return {"total": total, "found": found, "missing": missing,
            "rate": (found / total) if total else 1.0}


def preflight_guard(hc, HIS, rows, force=False):
    """คืน True ถ้าควรทำต่อ · False ถ้าควรหยุด (พร้อมพิมพ์สาเหตุให้แล้ว)"""
    if not rows:
        return True
    pf = preflight_codes(hc, rows)
    if pf["total"] == 0:
        return True
    pct = pf["rate"] * 100
    if pf["total"] < PREFLIGHT_MIN_CODES or pf["rate"] >= PREFLIGHT_MIN_RATE:
        if pf["missing"]:
            print("  %s[เตือน]%s รหัสที่ไม่มีในฐาน HIS %d จาก %d (%.0f%% ใช้ได้) — แถวที่ใช้รหัสเหล่านี้จะขึ้น error"
                  % (C_WARN, C_END, len(pf["missing"]), pf["total"], pct))
            for kind, c, nm in pf["missing"][:5]:
                print("      %s%s %s (%s)%s" % (C_DIM, kind, c, nm, C_END))
            print()
        return True
    print("=" * 62)
    print("  %sหยุดไว้ก่อน — รหัสในคิวแทบไม่มีในฐาน HIS ปลายทาง%s" % (C_BAD, C_END))
    print("=" * 62)
    print("  ฐานที่ gateway จะเขียน : %s %s@%s:%s/%s"
          % (HIS["driver"], HIS["user"] or "-", HIS["host"], HIS["port"], HIS["name"]))
    print("  รหัสที่ใช้ได้          : %d จาก %d (%.0f%%)" % (pf["found"], pf["total"], pct))
    print("  ตัวอย่างที่ไม่พบ       :")
    for kind, c, nm in pf["missing"][:8]:
        print("      %s %s (%s)" % (kind, c, nm))
    print()
    print("  สาเหตุที่พบบ่อย: หน้าเว็บอ่าน lab master จากฐาน รพ. ผ่าน tunnel")
    print("  แต่ BMS_HIS_* ใน config.bat ชี้ไปคนละฐาน -> รหัสคนละชุดกัน")
    print()
    print("  ทางแก้ (เลือกอย่างใดอย่างหนึ่ง):")
    print("    1) แก้ BMS_HIS_* ใน config.bat ให้ชี้ฐาน HOSxP ตัวเดียวกับที่หน้าเว็บใช้")
    print("    2) เปิดหน้าทะเบียนนัดด้วย session ที่ชี้ฐานเดียวกับ BMS_HIS_*")
    print("    3) ถ้ายืนยันว่าถูกแล้ว สั่ง sync --force เพื่อข้ามการตรวจนี้")
    print()
    if force:
        print("  %s--force: ข้ามการตรวจ ทำต่อตามคำสั่ง%s\n" % (C_WARN, C_END))
        return True
    return False


def do_sync_labs(cc, hc, CENTRAL, HIS, rows):
    """lab อย่างเดียว: ไม่สร้าง oapp ใหม่ แค่เขียน lab_app_head/order/service แนบ oapp_id ที่มีอยู่แล้ว
       (หน้าทะเบียนนัดส่ง status='lab_pending' + oapp_id ของนัดเดิมมาให้)
       คืน (done, skipped, failed)"""
    ct, T = CENTRAL["table"], HIS["table"]
    done = failed = skipped = 0
    for r in rows:
        cid = r["id"]
        try:
            oapp_id = oapp_id_from(r["json_row"])
            if oapp_id is None:
                raise ValueError("ไม่มี oapp_id ใน json_row — ไม่รู้ว่าจะแนบ lab กับนัดใบไหน")
            o = hc.query("SELECT hn, vn, nextdate, depcode, doctor FROM %s WHERE oapp_id=%%s" % T, (oapp_id,))
            if not o:
                raise ValueError("ไม่พบ oapp_id %s ใน HIS (นัดถูกลบไปแล้ว?)" % oapp_id)
            d = dict(o[0])

            # กันซ้ำ: นัดใบนี้มีใบสั่ง lab ผูกอยู่แล้ว -> ไม่สั่งซ้ำ/ไม่คิดเงินซ้ำ
            # แต่ถ้า lab_list_text ยังว่างอยู่ ก็เติมให้จากใบสั่งเดิม (ไม่ทับข้อความที่มีอยู่แล้ว)
            n = hc.scalar("SELECT COUNT(*) FROM lab_app_head WHERE oapp_id=%s", (oapp_id,))
            if n and int(n) > 0:
                fill, n_fill = sync_lab_list_text(hc, T, oapp_id, only_if_empty=True)
                if fill is None:
                    hc.rollback()
                else:
                    hc.commit()
                cc.execute("UPDATE %s SET status='done' WHERE id=%%s" % ct, (cid,)); cc.commit()
                skipped += 1; done += 1
                print("  %s[ข้าม]%s   #%s  oapp_id %s มีใบสั่ง lab อยู่แล้ว%s"
                      % (C_DIM, C_END, cid, oapp_id,
                         ("  (เติม lab_list_text %s รายการ)" % n_fill) if fill is not None else ""))
                continue

            labs = lab_orders_from(r["json_row"])
            if not labs:
                raise ValueError("ไม่มีรายการ lab ที่ map กับ lab master ได้")
            staff = os.getenv("BMS_HIS_STAFF") or "gateway"
            # มาจากหน้าทะเบียนนัด -> ใช้รหัสแพทย์คงที่ LAB_DOCTOR_CODE (9757)
            heads, n_ord, n_svc, skipped_labs = insert_labs(hc, oapp_id, d, labs, staff, LAB_DOCTOR_CODE)
            insert_xray(hc, oapp_id, xray_orders_from(r["json_row"]))
            # ให้ oapp.lab_list_text ตรงกับใบสั่ง lab ที่เพิ่งเขียน
            _, n_lst = sync_lab_list_text(hc, T, oapp_id)
            hc.commit()

            cc.execute("UPDATE %s SET status='done' WHERE id=%%s" % ct, (cid,)); cc.commit()
            done += 1
            print("  %s[lab เพิ่มแล้ว]%s #%s -> oapp_id %s  HN %s  lab %s ใบ (%s รายการ / %s คำสั่ง)%s"
                  % (C_OK, C_END, cid, oapp_id, d.get("hn"), len(heads), n_ord, n_svc,
                     ("  + lab_list_text %s รายการ" % n_lst) if n_lst else ""))
            for h in heads:
                print("      lab #%s  %s  (%s รายการ / %s คำสั่ง)" % (h["num"], h["group"], h["orders"], h["services"]))
            for s in skipped_labs:
                print("      %sข้าม lab:%s %s" % (C_DIM, C_END, s))
        except Exception as e:
            hc.rollback()
            try:
                cc.execute("UPDATE %s SET status='error' WHERE id=%%s" % ct, (cid,)); cc.commit()
            except Exception:
                cc.rollback()
            failed += 1
            print("  %s[ผิดพลาด]%s #%s  %s" % (C_BAD, C_END, cid, e))
    return done, skipped, failed


# ---- แจ้งนัดเข้าหมอพร้อม (MOPH Alert API) ----
# หลัง gateway เขียนนัดลง oapp สำเร็จ ส่งการ์ดแจ้งนัด (Flex Message) ไปหาคนไข้ทาง LINE OA / แอปหมอพร้อม
# ด้วยเลขบัตรประชาชน · เป็นแค่ "ข้อความแจ้ง" ไม่ได้สร้างนัดในระบบนัดของหมอพร้อม
#
# ปิดไว้เป็นค่าเริ่มต้น — เปิดด้วย BMS_MOPH_ALERT=1 ใน config.bat หรือหน้าตั้งค่า gateway_ui
# ยืนยันตัวตนได้ 2 แบบ (BMS_MOPH_ALERT_AUTH):
#   key   client-key + secret-key ของหน่วยบริการ (ขอที่ moph-alert.moph.go.th) -> POST /alert/v3.1/messages
#         ไม่มีเอกสารทางการ — รูปแบบจากโค้ดที่ใช้งานจริง (github tehnplk/test_moph_alert) · ค่าเริ่มต้น
#   user  user/password MOPH Account Center ที่มี Role MOPH_Alert_API -> ขอ JWT จาก cvp1
#         แล้ว POST /api/v2/send-message/send-now (คู่มือ 30 ส.ค. 67)
# อ่านจาก HIS อย่างเดียว (oapp / patient / clinic / kskdepartment / doctor) ไม่เขียนอะไรลง HIS
# กันส่งซ้ำด้วยตาราง moph_alert_log ในฐานกลาง: 1 oapp_id = 1 แถว
#   sent     ส่งแล้ว
#   skipped  ไม่ส่ง (ไม่มีเลขบัตร / เลขบัตรผิด / นัดยกเลิก / วันนัดผ่านไปแล้ว)
#   retry    ส่งไม่ถึงชั่วคราว (เน็ต / ปลายทางล่ม) — รอบหลังลองใหม่ตาม next_try
#   no_account  ส่งแล้วแต่คนไข้ไม่มีบัญชีหมอพร้อม / ไม่ได้ผูก MOPH Connect ("CID Not found") — ไม่ลองอีก
#               ที่ รพ.หาดใหญ่ ระบบแจ้งคิวของ HOSxP เจอแบบนี้ราวครึ่งหนึ่ง จึงแยกออกจาก failed
#   failed   ปลายทางปฏิเสธ หรือลองครบ ALERT_MAX_TRIES แล้ว — ไม่ลองอีก
ALERT_SOURCE = "appointment-autofill"      # source_type ของคิวจากหน้าทำนัดจากโน้ตแพทย์ (v3)
ALERT_LOG_TABLE = "moph_alert_log"
ALERT_BASE = {"prod": "https://morpromt2c.moph.go.th",
              "uat": "https://pentestmohprompt2c.moph.go.th"}
ALERT_TOKEN_URL = "https://cvp1.moph.go.th/token"
ALERT_SEND_PATH = "/api/v2/send-message/send-now"      # auth=user
ALERT_V31_PATH = "/alert/v3.1/messages"                # auth=key
ALERT_AUTHS = ("key", "user")
ALERT_TIMEOUT = 60
ALERT_MAX_TRIES = 5
THAI_MONTH = ("มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
              "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม")


def _alert_raw():
    """ค่าตั้ง BMS_MOPH_ALERT* — env มาก่อน ไม่มีค่อยอ่าน config.bat ที่วางข้าง ๆ
       gateway_ui import gateway แล้วเรียก do_sync ตรง ๆ โดยโหลดเข้า env แค่ค่าในฟอร์มฐานข้อมูล"""
    vals = {}
    if bmscrypt is not None:
        try:
            vals = {k: v for k, v in bmscrypt.read_config().items() if k.startswith("BMS_MOPH_ALERT")}
        except Exception:
            vals = {}
    for k, v in os.environ.items():
        if k.startswith("BMS_MOPH_ALERT"):
            vals[k] = v
    # gateway_ui ถอดรหัสแล้วตั้ง plaintext ใน env (และลบ _ENC ออกจาก env) -> ต้องชนะ _ENC เก่าที่อ่านจากไฟล์
    for name in ("BMS_MOPH_ALERT_USER", "BMS_MOPH_ALERT_PASS", "BMS_MOPH_ALERT_CLIENT_KEY", "BMS_MOPH_ALERT_SECRET_KEY"):
        if name in os.environ and name + "_ENC" not in os.environ:
            vals.pop(name + "_ENC", None)
    return vals


def _alert_secret(vals, name):
    enc = (vals.get(name + "_ENC") or "").strip()
    if enc and bmscrypt is not None:
        return bmscrypt.decrypt(enc)
    return vals.get(name) or ""


def alert_password_hash(password):
    """password_hash ที่ cvp1.moph.go.th ต้องการ = HMAC-SHA256(รหัสผ่าน, คีย์ '$jwt@moph#') เป็นฐานสิบหกตัวใหญ่"""
    return hmac.new(b"$jwt@moph#", str(password).encode("utf-8"), hashlib.sha256).hexdigest().upper()


def alert_cfg(v=None):
    """v = ค่าตั้งที่ส่งมาเอง (เช่นค่าที่กรอกในหน้าตั้งค่าของ gateway_ui แต่ยังไม่บันทึก) · None = อ่านจาก env/config.bat"""
    v = _alert_raw() if v is None else v
    env = (v.get("BMS_MOPH_ALERT_ENV") or "uat").strip().lower()
    if env not in ALERT_BASE:
        env = "uat"
    auth = (v.get("BMS_MOPH_ALERT_AUTH") or "key").strip().lower()
    if auth not in ALERT_AUTHS:
        auth = "key"
    c = {"on": str(v.get("BMS_MOPH_ALERT") or "").strip().lower() in ("1", "true", "yes", "on"),
         "env": env, "auth": auth, "client_key": "", "secret_key": "",
         "base": (v.get("BMS_MOPH_ALERT_BASE") or ALERT_BASE[env]).strip().rstrip("/"),
         "token_url": (v.get("BMS_MOPH_ALERT_TOKEN_URL") or ALERT_TOKEN_URL).strip(),
         "hospcode": (v.get("BMS_MOPH_ALERT_HOSPCODE") or "").strip(),
         "phone": (v.get("BMS_MOPH_ALERT_PHONE") or "").strip(),
         "user": "", "pass_hash": "", "error": ""}
    try:
        if auth == "key":
            c["client_key"] = _alert_secret(v, "BMS_MOPH_ALERT_CLIENT_KEY").strip()
            c["secret_key"] = _alert_secret(v, "BMS_MOPH_ALERT_SECRET_KEY").strip()
        else:
            c["user"] = _alert_secret(v, "BMS_MOPH_ALERT_USER").strip()
            pw = _alert_secret(v, "BMS_MOPH_ALERT_PASS")
            c["pass_hash"] = alert_password_hash(pw) if pw else ""
    except Exception as e:
        c["error"] = "ถอดรหัสค่าลับของ MOPH Alert ไม่ได้: %s" % e
        return c
    need = ((("BMS_MOPH_ALERT_CLIENT_KEY", "client_key"), ("BMS_MOPH_ALERT_SECRET_KEY", "secret_key"))
            if auth == "key" else
            (("BMS_MOPH_ALERT_USER", "user"), ("BMS_MOPH_ALERT_PASS", "pass_hash"), ("BMS_MOPH_ALERT_HOSPCODE", "hospcode")))
    miss = [n for n, k in need if not c[k]]
    if miss:
        c["error"] = "ยังไม่ได้ตั้ง %s ใน config.bat" % ", ".join(miss)
    return c


def alert_cfg_summary(c):
    """บรรทัดสรุปค่าตั้งสำหรับ status / preview — ไม่แสดงคีย์หรือรหัส"""
    if c["error"]:
        return c["error"]
    if c["auth"] == "key":
        return "ครบ (client-key / secret-key · MOPH Alert v3.1)"
    return "ครบ (user %s · hospcode %s · MOPH Alert v2)" % (c["user"], c["hospcode"])


def _alert_http(url, method="GET", body=None, token=None, headers=None):
    """คืน (http_code หรือ None ถ้าต่อไม่ได้, ข้อความตอบกลับ, ข้อผิดพลาด) · headers = header เพิ่ม (เช่น client-key)"""
    # เซิร์ฟเวอร์ฝั่ง moph.go.th ส่ง security header ซ้ำเกิน 100 บรรทัด -> http.client ปฏิเสธทั้งที่ curl ผ่าน
    http.client._MAXHEADERS = max(getattr(http.client, "_MAXHEADERS", 100), 1000)
    extra, headers = headers, {"Accept": "application/json", "User-Agent": "BMS-Gateway"}
    headers.update(extra or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=ALERT_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return e.code, raw, ""
    except Exception as e:
        return None, "", str(e)


_ALERT_TOKEN = {"key": None, "token": "", "exp": 0.0}


def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return float(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0.0


def alert_token(cfg, force=False):
    """JWT จาก MOPH Account Center — เก็บไว้ในหน่วยความจำ ขอใหม่ก่อนหมดอายุ 10 นาที
       ห้ามพิมพ์ URL ของคำขอนี้ลง log เพราะมี password_hash อยู่ใน query string"""
    key = (cfg["token_url"], cfg["user"], cfg["pass_hash"], cfg["hospcode"])
    t = _ALERT_TOKEN
    if not force and t["key"] == key and t["token"] and time.time() < t["exp"] - 600:
        return t["token"], ""
    cred = {"user": cfg["user"], "password_hash": cfg["pass_hash"], "hospital_code": cfg["hospcode"]}
    qs = urllib.parse.urlencode(dict({"Action": "get_moph_access_token"}, **cred))
    # คู่มือ MOPH Alert (30 ส.ค. 67) เขียนเป็น GET แต่คู่มือ MOPH IC/PHR และตัวที่ใช้งานจริงยิง POST JSON
    # จึงลอง GET ก่อน ได้ไม่ใช่ JWT ค่อยลอง POST · สำเร็จ = ตอบเป็นสตริง JWT ล้วน
    # ผิดรหัส = {"code":"404","message":"User/Password Invalid"}
    code, raw, err = _alert_http(cfg["token_url"] + "?" + qs)
    tok = _token_from(raw) if code == 200 else ""
    if not tok:
        code2, raw2, err2 = _alert_http(cfg["token_url"] + "?Action=get_moph_access_token", "POST", cred)
        if code2 == 200 and _token_from(raw2):
            tok = _token_from(raw2)
        elif code is None and code2 is None:
            return "", "ขอ token จาก MOPH Account Center ไม่ได้: %s" % (err2 or err)
        else:
            code, raw = (code2, raw2) if code2 is not None else (code, raw)
    if not tok:
        return "", "ขอ token ไม่สำเร็จ (HTTP %s) — %s" % (code, (raw or "").strip()[:200] or "ไม่มีข้อความตอบกลับ")
    # อายุจริงอ่านจาก exp ใน token (คู่มือ Alert บอก 24 ชม. แต่คู่มือ PHR ตัวอย่างจริงเป็น 3 ชม.)
    t.update(key=key, token=tok, exp=_jwt_exp(tok) or (time.time() + 2 * 3600))
    return tok, ""


def _token_from(raw):
    tok = (raw or "").strip().strip('"')
    if tok.startswith("{"):
        try:
            j = json.loads(tok)
            tok = str(j.get("token") or j.get("access_token") or j.get("jwt") or "")
        except Exception:
            tok = ""
    return tok if tok.count(".") == 2 and " " not in tok else ""


def _alert_outcome(code, raw, err):
    """ผลการส่ง -> (state, note)
       auth = คีย์/token/สิทธิ์ใช้ไม่ได้ -> หยุดทั้งรอบ ไม่นับเป็นครั้งที่ลอง
       v3.1 ตอบคีย์ผิดเป็น HTTP 200 + {"message":"client-key or secret-key incorrect","message_code":401}
       (ลองจริงแล้ว) จึงต้องตัดสินจาก message_code ด้วย ไม่ใช่ HTTP code อย่างเดียว"""
    if code is None:
        return "retry", "ต่อ MOPH Alert ไม่ได้: %s" % err
    j = None
    try:
        j = json.loads(raw) if raw else None
    except Exception:
        j = None
    msg, mcode, status, errs = "", None, None, []
    if isinstance(j, dict):
        msg = str(j.get("message") or j.get("Message") or "")
        mcode = j.get("message_code", j.get("MessageCode"))
        status = j.get("status")
        errs = [v for k, v in j.items() if "error" in str(k).lower() and v]
    try:
        mc = int(mcode) if mcode is not None else None
    except (TypeError, ValueError):
        mc = None
    if code in (401, 403) or mc in (401, 403):
        return "auth", "MOPH Alert ไม่รับการยืนยันตัวตน (%s) — ตรวจคีย์ / user / Role MOPH_Alert_API" % (msg or "HTTP %s" % code)
    if "cid not found" in (msg + " " + (raw or "")).lower():
        return "no_account", "คนไข้ไม่มีบัญชีหมอพร้อม หรือยังไม่ผูก MOPH Connect"
    if code == 429 or code >= 500 or mc == 429 or (mc or 0) >= 500:
        return "retry", "HTTP %s %s" % (code, msg or (raw or "")[:150])
    if 200 <= code < 300 and not isinstance(j, dict):
        return "retry", "HTTP %s ตอบไม่ใช่ JSON: %s" % (code, (raw or "")[:150])
    if 200 <= code < 300 and not errs and (mc == 200 or str(status).lower() == "success"
                                           or (mc is None and status is None)):
        return "sent", msg or "ok"
    return "failed", "HTTP %s message_code %s %s" % (code, mcode, msg or (raw or "")[:150])


def alert_send(cfg, cid, message):
    """คืน (state, http_code, note, raw)"""
    if cfg.get("auth") == "key":
        # v3.1 ต้องมีข้อความสำรอง (title/text/html) สำหรับช่องทางที่แสดง Flex ไม่ได้ · message_type HPT ตามตัวอย่างที่ใช้งานจริง
        alt = message.get("altText") or "แจ้งนัดหมาย"
        body = {"cid": [cid], "messages": [message], "message_title": "แจ้งนัดหมาย",
                "message_text": alt, "message_html": "<p>%s</p>" % html_escape(alt), "message_type": "HPT"}
        code, raw, err = _alert_http(cfg["base"] + ALERT_V31_PATH, "POST", body,
                                     headers={"client-key": cfg["client_key"], "secret-key": cfg["secret_key"]})
        state, note = _alert_outcome(code, raw, err)
        return state, code, note, raw
    tok, err = alert_token(cfg)
    if not tok:
        return "auth", None, err, ""
    body = {"datas": [cid], "messages": [message]}
    url = cfg["base"] + ALERT_SEND_PATH
    code, raw, err = _alert_http(url, "POST", body, tok)
    if code == 401:                       # token ถูกเพิกถอนก่อนหมดอายุ -> ขอใหม่หนึ่งครั้ง
        tok, err = alert_token(cfg, force=True)
        if not tok:
            return "auth", None, err, ""
        code, raw, err = _alert_http(url, "POST", body, tok)
    state, note = _alert_outcome(code, raw, err)
    return state, code, note, raw


def valid_cid(v):
    """เลขบัตรประชาชน 13 หลักที่ผ่าน checksum -> สตริง 13 หลัก · ไม่ผ่าน -> None"""
    s = re.sub(r"\D", "", str(v or ""))
    if len(s) != 13 or len(set(s)) == 1:
        return None
    total = sum(int(s[i]) * (13 - i) for i in range(12))
    return s if (11 - total % 11) % 10 == int(s[12]) else None


def _mask_cid(s):
    return ("*********" + s[-4:]) if s else "-"


def _as_date(v):
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _hhmm(v):
    """time (psycopg2) / timedelta (pymysql) / สตริง -> 'HH:MM'"""
    if v is None:
        return ""
    if isinstance(v, datetime.timedelta):
        s = int(v.total_seconds())
        return "%02d:%02d" % ((s // 3600) % 24, (s % 3600) // 60)
    if isinstance(v, (datetime.time, datetime.datetime)):
        return v.strftime("%H:%M")
    m = re.match(r"\s*(\d{1,2}):(\d{2})", str(v))
    return ("%02d:%s" % (int(m.group(1)), m.group(2))) if m else ""


def _thai_date(d):
    return "วัน%sที่ %d %s %d" % (THAI_DOW[d.isoweekday() % 7], d.day, THAI_MONTH[d.month - 1], d.year + 543)


def alert_hospital_name(hc):
    try:
        v = hc.scalar("SELECT hospitalname FROM opdconfig LIMIT 1")
        hc.rollback()
        return str(v or "").strip()
    except Exception:
        hc.rollback()
        return ""


def alert_appointment(hc, oapp_id):
    """อ่านนัดจาก HIS พร้อมชื่อคลินิก/ห้อง/แพทย์และเลขบัตร — ต่อฐานตรงจึงไม่เจอปัญหาเลขบัตรเพี้ยนแบบ /api/sql"""
    T = hc.cfg["table"]
    death = ", p.death" if has_column(hc, "patient", "death") else ""
    rows = hc.query(
        "SELECT o.oapp_id, o.hn, o.nextdate, o.nexttime, o.nexttime_end, o.note, o.contact_point,"
        " o.oapp_status_id, c.name AS clinic_name, k.department AS room_name, d.name AS doctor_name,"
        " p.pname, p.fname, p.lname, p.cid%s"
        " FROM %s o LEFT JOIN patient p ON p.hn = o.hn"
        " LEFT JOIN clinic c ON c.clinic = o.clinic"
        " LEFT JOIN kskdepartment k ON k.depcode = o.depcode"
        " LEFT JOIN doctor d ON d.code = o.doctor"
        " WHERE o.oapp_id = %%s" % (death, T), (int(oapp_id),))
    hc.rollback()          # จบทรานแซกชันอ่านทันที ไม่ค้าง idle in transaction ระหว่างรอหมอพร้อมตอบ
    return rows[0] if rows else None


def alert_decide(a):
    """นัดนี้ควรแจ้งไหม -> '' = ส่ง · ข้อความ = เหตุผลที่ไม่ส่ง"""
    if not a:
        return "ไม่พบ oapp_id นี้ใน HIS"
    try:
        if a.get("oapp_status_id") is not None and int(a["oapp_status_id"]) == OAPP_STATUS_CANCELLED:
            return "นัดถูกยกเลิกแล้ว"
    except (TypeError, ValueError):
        pass
    nd = _as_date(a.get("nextdate"))
    if not nd or nd < datetime.date.today():
        return "วันนัดผ่านไปแล้ว"
    if str(a.get("death") or "").strip().upper() == "Y":
        return "คนไข้เสียชีวิตแล้ว"
    if not valid_cid(a.get("cid")):
        return "ไม่มีเลขบัตรประชาชน 13 หลักที่ถูกต้อง"
    return ""


def alert_flex(a, hosp, cfg):
    """การ์ดแจ้งนัดตามมาตรฐาน LINE Flex Message (bubble เดียว ข้อความล้วน ไม่มีรูป)"""
    def txt(v, n):
        v = re.sub(r"[ \t]+", " ", str(v or "")).strip()
        return (v[:n - 1] + "…") if len(v) > n else v

    nd = _as_date(a.get("nextdate"))
    day = _thai_date(nd) if nd else ""
    t1, t2 = _hhmm(a.get("nexttime")), _hhmm(a.get("nexttime_end"))
    if t1 == "00:00" and t2 in ("", "00:00"):
        t1 = t2 = ""
    tm = ("%s - %s น." % (t1, t2)) if (t1 and t2 and t2 != t1) else (("%s น." % t1) if t1 else "")
    name = ("%s%s %s" % (a.get("pname") or "", a.get("fname") or "", a.get("lname") or "")).strip()

    body = [{"type": "text", "text": "แจ้งนัดหมาย", "weight": "bold", "size": "xl", "color": "#0F6E56"}]
    if hosp:
        body.append({"type": "text", "text": txt(hosp, 120), "size": "sm", "color": "#4A5C64", "wrap": True})
    if name:
        body.append({"type": "text", "text": "เรียน " + txt(name, 120), "size": "md", "margin": "lg", "wrap": True})
    detail = []
    for k, v in (("HN", a.get("hn")), ("วันนัด", day), ("เวลา", tm), ("คลินิก", a.get("clinic_name")),
                 ("ห้องตรวจ", a.get("room_name")), ("แพทย์", a.get("doctor_name")),
                 ("เตรียมตัว", a.get("note")), ("ติดต่อ", a.get("contact_point"))):
        v = txt(v, 400)
        if v:
            detail.append({"type": "box", "layout": "baseline", "spacing": "sm", "contents": [
                {"type": "text", "text": k, "size": "sm", "color": "#8496A0", "flex": 2},
                {"type": "text", "text": v, "size": "sm", "color": "#12242E", "flex": 5, "wrap": True}]})
    if detail:
        body.append({"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": detail})
    foot = "หากไม่สะดวกมาตามนัด กรุณาติดต่อโรงพยาบาลเพื่อเลื่อนนัด"
    if cfg.get("phone"):
        foot += " โทร " + cfg["phone"]
    return {"type": "flex",
            "altText": txt(" ".join(x for x in ("แจ้งนัดหมาย", hosp, day, tm) if x), 400),
            "contents": {"type": "bubble",
                         "body": {"type": "box", "layout": "vertical", "contents": body},
                         "footer": {"type": "box", "layout": "vertical", "contents": [
                             {"type": "text", "text": foot, "size": "xs", "color": "#8496A0", "wrap": True}]}}}


def ensure_alert_log(cc):
    if cc.driver == "pgsql":
        ddl = ("CREATE TABLE IF NOT EXISTS %s (id BIGSERIAL PRIMARY KEY, oapp_id VARCHAR(20) NOT NULL UNIQUE,"
               " queue_id BIGINT, hn VARCHAR(20), nextdate DATE, status VARCHAR(20), attempts INT DEFAULT 0,"
               " http_code INT, note VARCHAR(255), response TEXT,"
               " created_at TIMESTAMP, updated_at TIMESTAMP, next_try TIMESTAMP)")
    else:
        ddl = ("CREATE TABLE IF NOT EXISTS %s (id BIGINT AUTO_INCREMENT PRIMARY KEY, oapp_id VARCHAR(20) NOT NULL,"
               " queue_id BIGINT, hn VARCHAR(20), nextdate DATE, status VARCHAR(20), attempts INT DEFAULT 0,"
               " http_code INT, note VARCHAR(255), response TEXT,"
               " created_at DATETIME, updated_at DATETIME, next_try DATETIME,"
               " UNIQUE KEY uq_oapp (oapp_id), INDEX(status), INDEX(created_at)"
               ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    cc.execute(ddl % ALERT_LOG_TABLE)
    cc.commit()


def alert_log_exists(cc):
    try:
        cc.scalar("SELECT COUNT(*) FROM %s WHERE 1=0" % ALERT_LOG_TABLE)
        return True
    except Exception:
        cc.rollback()
        return False


def fetch_alert_candidates(cc, limit, with_log=True):
    """คิวจากหน้าทำนัดที่ gateway เขียนเข้า HIS แล้ว (done + ref_id = oapp_id) และยังไม่ได้แจ้ง
       ช่วงคิวเดียวกับที่ sync หยิบ (today_bounds) · แถว retry จะกลับมาเมื่อถึง next_try"""
    t = cc.cfg["table"]
    a, b = today_bounds()
    if not with_log:
        return cc.query(
            "SELECT q.id, q.hn, q.ref_id, q.datetime, NULL AS attempts FROM %s q"
            " WHERE q.status='done' AND q.source_type=%%s AND q.ref_id IS NOT NULL AND q.ref_id <> ''"
            " AND q.datetime >= %%s AND q.datetime < %%s ORDER BY q.id LIMIT %d" % (t, limit),
            (ALERT_SOURCE, a, b))
    return cc.query(
        "SELECT q.id, q.hn, q.ref_id, q.datetime, l.attempts FROM %s q"
        " LEFT JOIN %s l ON l.oapp_id = q.ref_id"
        " WHERE q.status='done' AND q.source_type=%%s AND q.ref_id IS NOT NULL AND q.ref_id <> ''"
        " AND q.datetime >= %%s AND q.datetime < %%s"
        " AND (l.id IS NULL OR (l.status='retry' AND l.next_try <= %%s))"
        " ORDER BY q.id LIMIT %d" % (t, ALERT_LOG_TABLE, limit),
        (ALERT_SOURCE, a, b, datetime.datetime.now().replace(microsecond=0)))


def alert_log_write(cc, row):
    cols = ["oapp_id", "queue_id", "hn", "nextdate", "status", "attempts", "http_code",
            "note", "response", "created_at", "updated_at", "next_try"]
    upd = [k for k in cols if k not in ("oapp_id", "created_at")]
    if cc.driver == "pgsql":
        tail = "ON CONFLICT (oapp_id) DO UPDATE SET " + ",".join("%s=EXCLUDED.%s" % (k, k) for k in upd)
    else:
        tail = "ON DUPLICATE KEY UPDATE " + ",".join("%s=VALUES(%s)" % (k, k) for k in upd)
    row = dict(row, note=str(row.get("note") or "")[:255], response=str(row.get("response") or "")[:2000])
    try:
        cc.execute("INSERT INTO %s (%s) VALUES (%s) %s"
                   % (ALERT_LOG_TABLE, ",".join(cols), ",".join(["%s"] * len(cols)), tail),
                   tuple(row.get(k) for k in cols))
        cc.commit()
    except Exception:
        cc.rollback()
        raise


def do_alert_sync(CENTRAL, HIS, limit, explicit=False):
    """แจ้งนัดที่เพิ่งเขียนเข้า HIS ไปหาคนไข้ทางหมอพร้อม — เรียกต่อท้าย do_sync ทุกรอบ
       ปิดอยู่ (BMS_MOPH_ALERT ไม่ใช่ 1) -> ไม่ต่อฐาน ไม่ทำอะไรเลย"""
    cfg = alert_cfg()
    if not cfg["on"]:
        if explicit:
            print("MOPH Alert ปิดอยู่ — ตั้ง BMS_MOPH_ALERT=1 ใน config.bat ก่อน "
                  "(ดูรายการที่จะแจ้งได้ด้วย gateway alert-preview)")
        return 0
    cc, cerr = connect(CENTRAL, "BMS_DB_")
    if not cc:
        print("%s[MOPH Alert]%s ต่อฐานกลางไม่ได้: %s" % (C_BAD, C_END, cerr)); return 1
    try:
        ensure_alert_log(cc)
        cands = fetch_alert_candidates(cc, limit)
    except Exception as e:
        cc.rollback(); cc.close()
        print("%s[MOPH Alert]%s อ่านคิวไม่ได้: %s" % (C_BAD, C_END, e)); return 1
    if not cands:
        if explicit:
            print("MOPH Alert: ไม่มีนัดที่รอแจ้งในช่วง %s" % window_label())
        cc.close(); return 0
    n_appt = len({str(r.get("ref_id") or "").strip() for r in cands})     # คิวซ้ำใบเดียวกันนับครั้งเดียว
    print("\nแจ้งนัดเข้าหมอพร้อม (MOPH Alert · %s · %s) %d รายการ"
          % (cfg["env"], "v3.1 คีย์" if cfg["auth"] == "key" else "v2 user", n_appt))
    if cfg["error"]:
        print("  %s[ยังส่งไม่ได้]%s %s" % (C_BAD, C_END, cfg["error"])); cc.close(); return 1
    hc, herr = connect(HIS, "BMS_HIS_")
    if not hc:
        print("  %s[ยังส่งไม่ได้]%s ต่อฐาน HIS ไม่ได้: %s" % (C_BAD, C_END, herr)); cc.close(); return 1

    n = {"sent": 0, "skipped": 0, "no_account": 0, "retry": 0, "failed": 0}
    seen = set()
    try:
        hosp = alert_hospital_name(hc)
        for r in cands:
            oid = str(r.get("ref_id") or "").strip()
            if not oid.isdigit() or oid in seen:      # ใบนัดเดียวกันถูกยืนยันซ้ำ -> ref_id เดียวกันหลายแถว
                continue
            seen.add(oid)
            now = datetime.datetime.now().replace(microsecond=0)
            try:
                a = alert_appointment(hc, oid)
            except Exception as e:
                hc.rollback()            # อ่าน HIS ไม่ได้ชั่วคราว -> ไม่ลงบันทึก รอบหน้าหยิบใหม่เอง
                print("  %s[ข้ามรอบนี้]%s #%s oapp_id %s อ่านนัดจาก HIS ไม่ได้: %s" % (C_WARN, C_END, r["id"], oid, e))
                continue
            tries = int(r.get("attempts") or 0)
            row = {"oapp_id": oid, "queue_id": r["id"], "hn": (a or {}).get("hn") or r.get("hn"),
                   "nextdate": _as_date((a or {}).get("nextdate")), "attempts": tries,
                   "http_code": None, "response": None, "created_at": now, "updated_at": now, "next_try": None}
            why = alert_decide(a)
            if why:
                alert_log_write(cc, dict(row, status="skipped", note=why))
                n["skipped"] += 1
                print("  %s[ไม่แจ้ง]%s  #%s oapp_id %s HN %s — %s" % (C_DIM, C_END, r["id"], oid, row["hn"], why))
                continue
            cid = valid_cid(a.get("cid"))
            state, code, note, raw = alert_send(cfg, cid, alert_flex(a, hosp, cfg))
            if state == "auth":
                print("  %s[หยุด]%s %s — ยังไม่บันทึกผล รอบหน้าลองใหม่" % (C_BAD, C_END, note))
                break
            tries += 1
            if state == "retry" and tries >= ALERT_MAX_TRIES:
                state, note = "failed", "ลองครบ %d ครั้ง: %s" % (tries, note)
            nxt = now + datetime.timedelta(minutes=5 * (2 ** (tries - 1))) if state == "retry" else None
            alert_log_write(cc, dict(row, status=state, attempts=tries, http_code=code,
                                     note=note, response=raw, next_try=nxt))
            n[state] += 1
            tag = {"sent": (C_OK, "แจ้งแล้ว"), "no_account": (C_DIM, "ไม่มีบัญชีหมอพร้อม"),
                   "retry": (C_WARN, "ลองใหม่"), "failed": (C_BAD, "แจ้งไม่ได้")}[state]
            print("  %s[%s]%s #%s oapp_id %s HN %s CID %s นัด %s%s"
                  % (tag[0], tag[1], C_END, r["id"], oid, row["hn"], _mask_cid(cid), row["nextdate"],
                     "" if state in ("sent", "no_account") else "  — %s" % note))
    finally:
        hc.close(); cc.close()
    print("  สรุป MOPH Alert: แจ้งแล้ว %d · ไม่แจ้ง %d · ไม่มีบัญชีหมอพร้อม %d · รอลองใหม่ %d · ล้มเหลว %d"
          % (n["sent"], n["skipped"], n["no_account"], n["retry"], n["failed"]))
    return 0 if not n["failed"] else 2


def do_alert_preview(CENTRAL, HIS, limit, cfg=None):
    """ดูว่ารอบหน้าจะแจ้งนัดใครบ้าง — ไม่ขอ token ไม่ส่ง ไม่สร้างตาราง ไม่เขียนอะไรเลย"""
    cfg = cfg or alert_cfg()
    print("MOPH Alert : %s · %s %s" % ("เปิด" if cfg["on"] else "ปิด (BMS_MOPH_ALERT=0)", cfg["env"], cfg["base"]))
    print("ค่าตั้ง     : %s" % alert_cfg_summary(cfg))
    print("ช่วงคิว     : %s\n" % window_label())
    cc, cerr = connect(CENTRAL, "BMS_DB_")
    if not cc:
        print("%sต่อฐานกลางไม่ได้%s: %s" % (C_BAD, C_END, cerr)); return 1
    has_log = alert_log_exists(cc)
    try:
        cands = fetch_alert_candidates(cc, limit, has_log)
    except Exception as e:
        cc.rollback(); cc.close()
        print("%sอ่านคิวไม่ได้%s: %s" % (C_BAD, C_END, e)); return 1
    cc.close()
    if not has_log:
        print("%s(ยังไม่มีตาราง %s — จะสร้างให้เองตอนส่งครั้งแรก)%s" % (C_DIM, ALERT_LOG_TABLE, C_END))
    if not cands:
        print("ไม่มีนัดที่รอแจ้ง"); return 0
    hc, herr = connect(HIS, "BMS_HIS_")
    if not hc:
        print("%sต่อฐาน HIS ไม่ได้%s: %s" % (C_BAD, C_END, herr)); return 1
    ready, sample, seen = 0, None, set()
    try:
        hosp = alert_hospital_name(hc)
        for r in cands:
            oid = str(r.get("ref_id") or "").strip()
            if not oid.isdigit() or oid in seen:
                continue
            seen.add(oid)
            try:
                a = alert_appointment(hc, oid)
            except Exception as e:
                hc.rollback()
                print("  %s[อ่านไม่ได้]%s #%s oapp_id %s: %s" % (C_WARN, C_END, r["id"], oid, e)); continue
            why = alert_decide(a)
            if why:
                print("  %s[ไม่แจ้ง]%s  #%s oapp_id %s HN %s — %s" % (C_DIM, C_END, r["id"], oid, (a or {}).get("hn") or r.get("hn"), why))
                continue
            ready += 1
            msg = alert_flex(a, hosp, cfg)
            sample = sample or msg
            print("  %s[จะแจ้ง]%s  #%s oapp_id %s HN %s CID %s — %s"
                  % (C_OK, C_END, r["id"], oid, a.get("hn"), _mask_cid(valid_cid(a.get("cid"))), msg["altText"]))
    finally:
        hc.close()
    print("\nสรุป: จะแจ้ง %d / %d รายการ" % (ready, len(seen)))
    if sample:
        print("\nตัวอย่างข้อความ (รายการแรก):")
        print(json.dumps(sample, ensure_ascii=False, indent=2))
    return 0


def do_alert_test(HIS, cid, oapp_id=None, cfg=None):
    """ส่งการ์ดทดสอบไปที่เลขบัตรที่ระบุ (ของเจ้าหน้าที่เอง) — ไม่ลงบันทึก ไม่ต้องเปิด BMS_MOPH_ALERT
       --oapp = ใช้เนื้อหาของนัดจริงใบนั้น แต่ส่งไปที่ --cid ไม่ใช่คนไข้
       คืน 0 = ส่งสำเร็จ · 3 = คีย์ใช้ได้แต่เลขบัตรนี้ไม่มีบัญชีหมอพร้อม · 4 = ยืนยันตัวตนไม่ผ่าน · 1 = อื่น ๆ"""
    cfg = cfg or alert_cfg()
    print("MOPH Alert : %s %s · %s" % (cfg["env"], cfg["base"],
                                       "client-key (v3.1)" if cfg["auth"] == "key" else "user/password (v2)"))
    if cfg["error"]:
        print("%s%s%s" % (C_BAD, cfg["error"], C_END)); return 1
    to = valid_cid(cid)
    if not to:
        print("%s--cid ต้องเป็นเลขบัตรประชาชน 13 หลักที่ถูกต้อง%s" % (C_BAD, C_END)); return 1
    hosp = ""
    hc, herr = connect(HIS, "BMS_HIS_")
    if hc:
        hosp = alert_hospital_name(hc)
    if oapp_id:
        if not hc:
            print("%sต่อฐาน HIS ไม่ได้%s: %s" % (C_BAD, C_END, herr)); return 1
        try:
            a = alert_appointment(hc, oapp_id)
        finally:
            hc.close()
        if not a:
            print("%sไม่พบ oapp_id %s ใน HIS%s" % (C_BAD, oapp_id, C_END)); return 1
    else:
        if hc:
            hc.close()
        a = {"hn": "000000000", "pname": "", "fname": "ทดสอบ", "lname": "ระบบแจ้งนัด",
             "nextdate": datetime.date.today() + datetime.timedelta(days=1),
             "nexttime": "09:00", "nexttime_end": "12:00", "clinic_name": "ข้อความทดสอบ ไม่ใช่นัดจริง"}
    if cfg["auth"] == "user":
        tok, err = alert_token(cfg)
        if not tok:
            print("%s%s%s" % (C_BAD, err, C_END)); return 4
        print("token      : ok (หมดอายุ %s)" % time.strftime("%Y-%m-%d %H:%M", time.localtime(_ALERT_TOKEN["exp"])))
    msg = alert_flex(a, hosp, cfg)
    state, code, note, raw = alert_send(cfg, to, msg)
    print("ส่งไปที่    : %s" % _mask_cid(to))
    print("ผล         : %s (HTTP %s) %s" % (state, code, note))
    print("ตอบกลับ    : %s" % (raw or "")[:1000])
    return {"sent": 0, "no_account": 3, "auth": 4}.get(state, 1)


def show_cfg(c, title, err=None, extra=None):
    print("  %s" % title)
    print("    ชนิด/เซิร์ฟเวอร์ : %s  %s@%s:%s/%s" % (c["driver"], c["user"] or "-", c["host"], c["port"], c["name"] or "(ยังไม่ตั้ง)"))
    print("    ตาราง          : %s" % c["table"])
    if err:
        print("    เชื่อมต่อ       : %sFAIL%s  %s" % (C_BAD, C_END, err))
    else:
        print("    เชื่อมต่อ       : %sok%s" % (C_OK, C_END))
    for k, v in (extra or []):
        print("    %-14s : %s" % (k, v))


def do_status(CENTRAL, HIS):
    print("=" * 62)
    print("  BMS Gateway — ฐานกลาง -> HIS")
    print("=" * 62)
    cc, cerr = connect(CENTRAL, "BMS_DB_")
    extra = []
    if cc:
        try:
            t = CENTRAL["table"]
            tb = today_bounds()
            extra.append(("ช่วงคิวที่หยิบ", window_label()))
            lb = "pending ในช่วง" if SYNC_BACK_DAYS else "pending วันนี้"
            extra.append((lb, cc.scalar("SELECT COUNT(*) FROM %s WHERE status='pending' AND datetime >= %%s AND datetime < %%s" % t, tb)))
            extra.append((lb.replace("pending", "lab_pending"), cc.scalar("SELECT COUNT(*) FROM %s WHERE status='lab_pending' AND datetime >= %%s AND datetime < %%s" % t, tb)))
            # ค้างนอกช่วง = ไม่มีใครหยิบไปทำ ถ้าไม่ขยาย --days / BMS_SYNC_BACK_DAYS
            extra.append(("pending นอกช่วง", cc.scalar("SELECT COUNT(*) FROM %s WHERE status='pending' AND NOT (datetime >= %%s AND datetime < %%s)" % t, tb)))
            extra.append(("ทั้งหมด", cc.scalar("SELECT COUNT(*) FROM %s" % t)))
        except Exception as e:
            extra.append(("ตาราง", "%sERROR%s %s" % (C_BAD, C_END, e)))
        # คิวเปิด visit อยู่คนละตาราง (ไม่มีคอลัมน์ datetime จึงนับทั้งตาราง ไม่กรองวันนี้)
        try:
            vt = visit_table()
            extra.append(("ตารางเปิด visit", vt))
            extra.append(("รอเปิด visit วันนี้",
                          cc.scalar("SELECT COUNT(*) FROM %s WHERE status='pending' AND visit_date=%%s" % vt,
                                    (datetime.date.today(),))))
            extra.append(("รอเปิด visit วันอื่น", count_visit_other_days(cc)))
            extra.append(("เปิด visit แล้ว", cc.scalar("SELECT COUNT(*) FROM %s WHERE status='done'" % vt)))
        except Exception as e:
            cc.rollback()
            extra.append(("ตารางเปิด visit", "%sยังไม่มี/อ่านไม่ได้%s %s" % (C_WARN, C_END, e)))
    show_cfg(CENTRAL, "[1] ฐานกลาง (ต้นทาง)", cerr, extra)
    print()
    hc, herr = connect(HIS, "BMS_HIS_")
    extra = []
    if hc:
        try:
            _n, _approx = approx_rows(hc, HIS["table"])
            _txt = "อ่านไม่ได้" if _n is None else (("~%s (ประมาณ)" % format(_n, ",")) if _approx else format(_n, ","))
            extra.append(("แถวใน %s" % HIS["table"], _txt))
            _next = count_today(hc, HIS["table"], "nextdate")
            _vst = count_today(hc, HIS["table"], "vstdate")
            if _next is not None:
                extra.append(("นัดวันนี้ (nextdate)", format(_next, ",")))
            if _vst is not None:
                extra.append(("บันทึกวันนี้ (vstdate)", format(_vst, ",")))
        except Exception as e:
            extra.append(("ตาราง", "%sERROR%s %s" % (C_BAD, C_END, e)))
    show_cfg(HIS, "[2] ฐาน HIS (ปลายทาง)", herr, extra)
    print()
    ac = alert_cfg()
    print("  [3] แจ้งนัดเข้าหมอพร้อม (MOPH Alert)")
    extra = [("เปิดใช้", "เปิด" if ac["on"] else "ปิด (BMS_MOPH_ALERT=0)"),
             ("ปลายทาง", "%s  %s" % (ac["env"], ac["base"])),
             ("ค่าตั้ง", alert_cfg_summary(ac))]
    if cc and alert_log_exists(cc):
        try:
            got = cc.query("SELECT status, COUNT(*) AS n FROM %s WHERE created_at >= %%s GROUP BY status"
                           % ALERT_LOG_TABLE, (datetime.date.today(),))
            extra.append(("ผลวันนี้", " · ".join("%s %s" % (g["status"], g["n"]) for g in got) or "ยังไม่มี"))
        except Exception:
            cc.rollback()
    for k, v in extra:
        print("    %-14s : %s" % (k, v))
    print()
    ok = bool(cc) and bool(hc)
    print("  สรุป: %s" % (("%sพร้อมใช้งาน%s" % (C_OK, C_END)) if ok else ("%sยังเชื่อมต่อไม่ครบ%s" % (C_BAD, C_END))))
    if cc: cc.close()
    if hc: hc.close()
    return 0 if ok else 1


def do_preview(CENTRAL, HIS, limit):
    cc, cerr = connect(CENTRAL, "BMS_DB_")
    if not cc:
        print("%sต่อฐานกลางไม่ได้%s: %s" % (C_BAD, C_END, cerr)); return 1
    rows = fetch_pending(cc, limit)
    try:
        lab_rows = fetch_lab_pending(cc, limit)
    except Exception:
        cc.rollback(); lab_rows = []
    if not rows and not lab_rows:
        print("ไม่มีรายการ pending / lab_pending ในช่วง %s" % window_label()); cc.close(); return 0
    print("พบ pending วันนี้ (%s) %d รายการ (ยังไม่เขียนเข้า HIS)\n" % (datetime.date.today().strftime("%Y-%m-%d"), len(rows)))
    ready = 0
    for r in rows:
        d = map_oapp(r["json_row"], r["hn"], r["vn"])
        d["oapp_status_id"] = OAPP_STATUS_NEW
        miss = [k for k in REQUIRED if not d.get(k)]
        if miss:
            print("  %s[ขาดค่า]%s #%s  ขาด: %s" % (C_WARN, C_END, r["id"], ", ".join(miss)))
        else:
            ready += 1
            print("  %s[พร้อม]%s  #%s  HN %s -> นัด %s คลินิก %s" % (C_OK, C_END, r["id"], d["hn"], d["nextdate"], d["clinic"]))
        print("    %s%s%s" % (C_DIM, json.dumps(d, ensure_ascii=False), C_END))
        xr = xray_orders_from(r["json_row"])
        if xr:
            print("    xray -> oapp_xray : %s" % ", ".join("%s %s" % (o["code"], o["name"]) for o in xr))
        labs = lab_orders_from(r["json_row"])
        if labs:
            print("    lab -> lab_app_head/order/service : %s"
                  % ", ".join("%s %s (%s)" % (o["type"], o["code"], o["name"]) for o in labs))
        else:
            print("    %s(ไม่มี lab ที่ map กับ lab master ได้ — จะไม่สร้าง lab_app_*)%s" % (C_DIM, C_END))
        note_req = doctor_note_request_from(r["json_row"], r["hn"], r["vn"])
        if note_req:
            print("    doctor note -> patient_visit_doctor_note : insert ถ้ายังไม่มี note")
    print("\nสรุป: พร้อมเขียน %d / %d รายการ" % (ready, len(rows)))

    if lab_rows:
        print("\nlab_pending %d รายการ (นัดมีอยู่แล้วใน HIS — จะเพิ่มเฉพาะใบสั่ง lab)\n" % len(lab_rows))
        for r in lab_rows:
            oid = oapp_id_from(r["json_row"])
            labs = lab_orders_from(r["json_row"])
            tag = ("%s[พร้อม]%s " % (C_OK, C_END)) if (oid and labs) else ("%s[ขาดค่า]%s" % (C_WARN, C_END))
            print("  %s #%s  HN %s -> oapp_id %s  lab %d รายการ" % (tag, r["id"], r["hn"], oid, len(labs)))
            if labs:
                print("    %s%s%s" % (C_DIM, ", ".join("%s %s (%s)" % (o["type"], o["code"], o["name"]) for o in labs), C_END))

    # เตือนเรื่องรหัสไม่ตรงฐานตั้งแต่ตอน preview จะได้รู้ก่อนสั่ง sync จริง
    hc, _ = connect(HIS, "BMS_HIS_")
    if hc:
        print()
        preflight_guard(hc, HIS, rows + lab_rows, force=False)
        hc.close()
    cc.close()
    return 0


def do_sync(CENTRAL, HIS, limit, force=False):
    """เขียนคิวเข้า HIS แล้วแจ้งนัดเข้าหมอพร้อมต่อ (เฉพาะเมื่อ BMS_MOPH_ALERT=1)
       gateway_ui เรียกฟังก์ชันนี้ตรง ๆ จึงต้องต่อท้ายที่นี่ · แจ้งไม่สำเร็จไม่ทำให้ผล sync เสีย"""
    rc = _sync_queue(CENTRAL, HIS, limit, force)
    try:
        do_alert_sync(CENTRAL, HIS, limit)
    except Exception as e:
        print("%s[MOPH Alert]%s ล้มเหลว: %s" % (C_BAD, C_END, e))
    return rc


def _sync_queue(CENTRAL, HIS, limit, force=False):
    cc, cerr = connect(CENTRAL, "BMS_DB_")
    if not cc:
        print("%sต่อฐานกลางไม่ได้%s: %s" % (C_BAD, C_END, cerr)); return 1
    print("ช่วงคิวที่หยิบ: %s" % window_label())    # ดู log แล้วรู้ทันทีว่ารอบนี้กวาดย้อนหลังแค่ไหน
    rows = fetch_pending(cc, limit)
    try:
        lab_rows = fetch_lab_pending(cc, limit)        # นัดที่มีอยู่แล้ว ต้องการแค่ lab (หน้าทะเบียนนัด)
    except Exception:
        cc.rollback(); lab_rows = []
    try:
        slot_rows = fetch_slot_pending(cc, limit)      # คำขอ "เพิ่มคิว" จากพยาบาล
    except Exception:
        cc.rollback(); slot_rows = []
    if not rows and not lab_rows and not slot_rows:
        print("ไม่มีรายการ pending / lab_pending ในช่วง %s" % window_label()); cc.close(); return 0
    hc, herr = connect(HIS, "BMS_HIS_")
    if not hc:
        print("%sต่อฐาน HIS ไม่ได้%s: %s" % (C_BAD, C_END, herr)); cc.close(); return 1

    # กันเคสตั้งฐาน HIS ผิด แล้วไล่ mark error ทีละร้อยแถว
    if not preflight_guard(hc, HIS, rows + lab_rows, force):
        hc.close(); cc.close()
        return 3

    ct, T = CENTRAL["table"], HIS["table"]
    done = failed = skipped = 0
    for r in rows:
        cid = r["id"]
        try:
            d = map_oapp(r["json_row"], r["hn"], r["vn"])
            miss = [k for k in REQUIRED if not d.get(k)]
            if miss:
                raise ValueError("ขาดค่าที่จำเป็น: " + ", ".join(miss))
            _, au_warn = fix_app_user(hc, d, r["json_row"])     # ผู้บันทึก -> ชื่อล็อกอิน HOSxP
            if au_warn:
                print("    %s[app_user]%s #%s %s" % (C_WARN, C_END, cid, au_warn))

            # กันซ้ำ: มี "ใบเดียวกัน" ใน HIS แล้วหรือยัง — ข้ามนัดที่ถูกยกเลิก (oapp_status_id=4)
            # ไม่งั้นยกเลิกใน HOSxP แล้วนัดใหม่วันเดิมจะถูกมองว่า "มีแล้ว" แล้วไม่ยอม insert
            #
            # ใบเดียวกัน = HN + วันนัด + แพทย์ + ห้องตรวจ ตรงกันครบทั้งชุด
            # คนไข้คนเดียวนัดวันเดียวกันได้หลายใบ ถ้าคนละแพทย์หรือคนละห้องตรวจ
            # (เช่นเช้าพบอายุรกรรม บ่ายพบศัลยกรรม) เดิมกันซ้ำด้วย HN+วันนัดอย่างเดียว
            # ใบที่สองจึงถูกข้ามทิ้งเงียบ ๆ
            # COALESCE ทั้งสองฝั่ง: แพทย์/ห้องเป็น NULL ได้ ถ้าเทียบ NULL ตรง ๆ จะไม่มีวันตรงกัน
            # ดึง oapp_id ของใบที่มีอยู่มาด้วย (ไม่ใช่แค่ COUNT) — แถวที่ถูกข้ามจะได้ชี้ไปหานัดจริงใน HIS
            # หน้า insight จึงแยกออกว่า "ข้ามเพราะมีอยู่แล้ว" ไม่ใช่ "หายไปเฉย ๆ"
            dup_id = hc.scalar("SELECT oapp_id FROM %s WHERE hn=%%s AND nextdate=%%s "
                               "AND COALESCE(doctor,'')=%%s AND COALESCE(depcode,'')=%%s "
                               "AND (oapp_status_id IS NULL OR oapp_status_id <> %%s) "
                               "ORDER BY oapp_id DESC LIMIT 1" % T,
                               (d["hn"], d["nextdate"], str(d.get("doctor") or ""),
                                str(d.get("depcode") or ""), OAPP_STATUS_CANCELLED))
            if dup_id is not None:
                n_note = insert_doctor_note_if_missing(hc, doctor_note_request_from(r["json_row"], r["hn"], r["vn"]))
                if n_note:
                    hc.commit()
                else:
                    hc.rollback()
                mark_queue_done(cc, ct, cid, dup_id)
                skipped += 1; done += 1
                print("  %s[ข้าม]%s   #%s  มีนัด HN %s วันที่ %s (แพทย์ %s ห้อง %s) ใน HIS แล้ว -> oapp_id %s%s"
                      % (C_DIM, C_END, cid, d["hn"], d["nextdate"], d.get("doctor") or "-",
                         d.get("depcode") or "-", dup_id, ("  + doctor_note" if n_note else "")))
                continue

            # oapp_id: HOSxP ไม่ auto-increment -> ใช้ max+1
            d["oapp_id"] = next_id(hc, "oapp_id", T)
            d["oapp_status_id"] = OAPP_STATUS_NEW      # นัดใหม่ = รอให้ถึงวันนัด
            # โยงนัดกลับไปหาใบส่งต่อของ visit ที่ลงนัด (HOSxP ใช้ช่องนี้หา referin ของนัด)
            # เติมเฉพาะเมื่อมีแถว referin ของ vn นั้นจริง จะได้ไม่ชี้ไปทะเบียนที่ไม่มีอยู่
            if not d.get("referin_vn") and d.get("vn") and has_column(hc, T, "referin_vn"):
                if referin_exists(hc, d["vn"]):
                    d["referin_vn"] = str(d["vn"])[:12]
            # ประทับเวลาที่เขียนนัดเข้า HIS — หน้าทะเบียนนัด (appointment-center.html)
            # ใช้ update_datetime กรอง "นัดที่ถูกบันทึกในวันที่ ..." ถ้าไม่ใส่ นัดที่ gateway
            # สร้างจะไม่โผล่ในหน้านั้นเลย  (เช็คก่อนว่าสคีมามีคอลัมน์นี้จริง ไม่งั้น INSERT พังทั้งใบ)
            if has_column(hc, T, "update_datetime"):
                d["update_datetime"] = datetime.datetime.now().replace(microsecond=0)

            cols = list(d.keys())
            sql = "INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols)))
            hc.execute(sql, tuple(d.values()))

            # lab: เขียน lab_app_head/order/service ในทรานแซกชันเดียวกับ oapp
            labs = lab_orders_from(r["json_row"])
            staff = os.getenv("BMS_HIS_STAFF") or str(d.get("app_user") or "gateway")   # login name ใน HOSxP
            # มาจากหน้าโน้ตแพทย์ -> ใช้รหัสแพทย์ที่ login (oapp.doctor) · ว่าง -> fallback 9757
            heads, n_ord, n_svc, skipped_labs = insert_labs(hc, d["oapp_id"], d, labs, staff, d.get("doctor"))
            # จองคิวย่อยที่เลือกมาจากหน้าทำนัด — จองไม่ได้ก็ยังบันทึกนัด แต่ต้องไม่ให้ใบนัด
            # ชี้ไปคิวของคนอื่น จึงล้าง opd_qs_slot_id ทิ้งแล้วเตือนไว้ในบรรทัดสรุป
            slot_note = ""
            if d.get("opd_qs_slot_id"):
                ok_slot, slot_msg = claim_qs_slot(hc, d["opd_qs_slot_id"], d["oapp_id"])
                if ok_slot:
                    slot_note = "  + จองคิว %s" % slot_msg
                else:
                    hc.execute("UPDATE %s SET opd_qs_slot_id=NULL WHERE oapp_id=%%s" % T, (d["oapp_id"],))
                    slot_note = "  %s[จองคิวไม่ได้]%s %s" % (C_WARN, C_END, slot_msg)
            xrays = xray_orders_from(r["json_row"])
            n_xray = insert_xray(hc, d["oapp_id"], xrays)
            n_note = insert_doctor_note_if_missing(hc, doctor_note_request_from(r["json_row"], r["hn"], r["vn"]))
            # ให้ oapp.lab_list_text ตรงกับใบสั่ง lab ที่เขียนจริง (ไม่ใช่ข้อความที่เครื่องมือส่งมา)
            _, n_lst = sync_lab_list_text(hc, T, d["oapp_id"])

            hc.commit()   # oapp + lab + xray สำเร็จพร้อมกัน (ถ้าอันใดพัง -> rollback ทั้งหมด)

            mark_queue_done(cc, ct, cid, d["oapp_id"])
            done += 1
            extra = ("  + lab %s ใบ (%s รายการ / %s คำสั่ง)" % (len(heads), n_ord, n_svc)) if heads else "  (ไม่มี lab)"
            if n_xray:
                extra += "  + xray %s รายการ" % n_xray
            if n_note:
                extra += "  + doctor_note"
            if n_lst:
                extra += "  + lab_list_text %s รายการ" % n_lst
            if slot_note:
                extra += slot_note
            print("  %s[เขียนแล้ว]%s #%s -> oapp_id %s  HN %s  นัด %s%s"
                  % (C_OK, C_END, cid, d["oapp_id"], d["hn"], d["nextdate"], extra))
            for h in heads:
                print("      lab #%s  %s  (%s รายการ / %s คำสั่ง)" % (h["num"], h["group"], h["orders"], h["services"]))
            for s in skipped_labs:
                print("      %sข้าม lab:%s %s" % (C_DIM, C_END, s))
        except Exception as e:
            hc.rollback()
            try:
                cc.execute("UPDATE %s SET status='error' WHERE id=%%s" % ct, (cid,)); cc.commit()
            except Exception:
                cc.rollback()
            failed += 1
            print("  %s[ผิดพลาด]%s #%s  %s" % (C_BAD, C_END, cid, e))

    # ทำคำขอเพิ่มคิวก่อนใบนัด — คิวที่เพิ่งเพิ่มจะได้ถูกใบนัดในรอบเดียวกันจองได้เลย
    if slot_rows:
        print("\nคำขอเพิ่มคิว (slot_pending) %d รายการ" % len(slot_rows))
        for r in slot_rows:
            cid = r["id"]
            try:
                # คำขอมี 2 ชนิด — เพิ่มคิวในตารางเดิม (slot_add) กับ เปิดตารางใหม่ทั้งช่วง (slot_create)
                cre = slot_create_request_from(r.get("json_row"))
                flg = slot_flag_request_from(r.get("json_row"))
                pfx = slot_prefix_request_from(r.get("json_row"))
                rnm = slot_renumber_request_from(r.get("json_row"))
                mst = slot_master_request_from(r.get("json_row"))
                tpl = slot_tmpl_request_from(r.get("json_row"))
                rmv = slot_remove_request_from(r.get("json_row"))
                if rmv:
                    req = rmv
                    n_made, detail = remove_qs_slots(hc, rmv, cc)
                    span = str(rmv.get("schedule_date"))
                elif tpl:
                    req = tpl
                    n_made, detail = apply_qs_tmpl(hc, tpl)
                    span = "เทมเพลตตารางคิว"
                elif mst:
                    req = mst
                    n_made, detail = apply_qs_master(hc, mst)
                    span = "ตารางพื้นฐานคิว"
                elif rnm:
                    req = rnm
                    n_made, detail = renumber_qs_prefix(hc, rnm)
                    span = "เลขคิวที่สร้างไว้แล้ว"
                elif pfx:
                    req = pfx
                    n_made, detail = apply_qs_prefix(hc, pfx)
                    span = "อักษรนำเลขคิว"
                elif flg:
                    req = flg
                    n_made, detail = apply_slot_flags(hc, flg)
                    span = "เปิด/ปิดใช้ระบบคิว"
                elif cre:
                    req = cre
                    n_made, detail = create_qs_schedule(hc, cre)
                    span = "%s..%s" % (cre.get("date_from"), cre.get("date_to"))
                else:
                    req = slot_request_row(r)
                    n_made, detail = add_qs_slots(hc, req)
                    span = str(req.get("schedule_date"))
                hc.commit()
                slot_mark(cc, cid, "done", detail)
                done += 1
                print("  %s[%s]%s #%s  %s %s %s%d คิว  %s%s"
                      % (C_OK, "ลดคิว" if rmv else "เพิ่มคิว", C_END, cid, span, req.get("doctor_code"),
                         "-" if rmv else "+", n_made, detail,
                         ("  (%s)" % req.get("reason")) if req.get("reason") else ""))
            except Exception as e:
                hc.rollback()
                slot_mark(cc, cid, "error", str(e))
                failed += 1
                print("  %s[ผิดพลาด]%s #%s  เพิ่มคิวไม่สำเร็จ: %s" % (C_BAD, C_END, cid, e))

    if lab_rows:
        print("\nlab เพิ่มให้นัดที่มีอยู่แล้ว (lab_pending) %d รายการ" % len(lab_rows))
        d2, s2, f2 = do_sync_labs(cc, hc, CENTRAL, HIS, lab_rows)
        done += d2; skipped += s2; failed += f2

    print("\nสรุป: สำเร็จ %d (ข้าม %d) · ล้มเหลว %d · จากทั้งหมด %d"
          % (done, skipped, failed, len(rows) + len(lab_rows) + len(slot_rows)))
    hc.close(); cc.close()
    return 0 if failed == 0 else 2


def do_visit_preview(CENTRAL, HIS, limit):
    """ดูว่าจะเปิด visit ให้ใครบ้าง — ไม่เขียนอะไรลงฐานทั้งสองฝั่ง

       ไม่เรียก get_serialnumber ตรงนี้ เพราะมันกินเลขคิวไปจริงแม้จะแค่ดู"""
    cc, cerr = connect(CENTRAL, "BMS_DB_")
    if not cc:
        print("%sต่อฐานกลางไม่ได้%s: %s" % (C_BAD, C_END, cerr)); return 1
    try:
        rows = fetch_visit_pending(cc, limit)
    except Exception as e:
        print("%sอ่านตาราง %s ไม่ได้%s: %s" % (C_BAD, VISIT_TABLE, C_END, e)); cc.close(); return 1
    other = count_visit_other_days(cc)
    if not rows:
        print("ไม่มีคำขอเปิด visit ของวันนี้ (%s) ในตาราง %s%s"
              % (datetime.date.today().strftime("%Y-%m-%d"), VISIT_TABLE,
                 "" if not other else ("  —  มีค้างของวันอื่น/ไม่ระบุวัน %d รายการ" % other)))
        cc.close(); return 0

    hc, herr = connect(HIS, "BMS_HIS_")
    if not hc:
        print("%sต่อฐาน HIS ไม่ได้%s: %s" % (C_BAD, C_END, herr)); cc.close(); return 1

    print("พบคำขอเปิด visit ของวันนี้ (%s) %d รายการ — ยังไม่เขียนอะไรลงฐาน%s\n"
          % (datetime.date.today().strftime("%Y-%m-%d"), len(rows),
             "" if not other else ("  ·  ค้างของวันอื่น/ไม่ระบุวันอีก %d รายการ" % other)))
    print("ปลายทาง: %s %s@%s:%s/%s\n"
          % (HIS["driver"], HIS["user"] or "-", HIS["host"], HIS["port"], HIS["name"]))
    now = datetime.datetime.now()
    ready = blocked = 0
    for r in rows:
        d = ovst_fixed(r)
        if not d.get("hn"):
            print("  %s[ขาดค่า]%s #%s  ไม่มี HN" % (C_WARN, C_END, r["id"]))
            continue
        miss = [k for k in OVST_FIXED_FIELDS if not d.get(k)]

        # ไม่มีบัตร -> เปิด visit ไม่ได้ ต้องเห็นตั้งแต่ preview ไม่ใช่ไปเจอตอน sync
        if CHECK_PATIENT and patient_exists(hc, d["hn"]) is False:
            blocked += 1
            print("  %s[ติดบัตร]%s #%s  HN %s ไม่มีในตาราง patient ของ %s"
                  % (C_BAD, C_END, r["id"], d["hn"], HIS["name"]))
            print("    %sต้องทำบัตรใน HOSxP ก่อน แล้วคำขอนี้จะถูกหยิบไปทำเองในรอบถัดไป%s"
                  % (C_DIM, C_END))
            continue

        n = hc.scalar("SELECT COUNT(*) FROM ovst WHERE hn=%s AND vstdate=%s", (d["hn"], now.date()))
        if n and int(n) > 0:
            print("  %s[ข้าม]%s   #%s  HN %s มี visit ของวันนี้อยู่แล้ว %s ใบ"
                  % (C_DIM, C_END, r["id"], d["hn"], n))
            continue

        vn_now = be_yymmdd(now) + now.strftime("%H%M%S")
        taken = hc.scalar("SELECT 1 FROM ovst WHERE vn=%s LIMIT 1", (vn_now,))
        qname = "ovst-q-" + be_yymmdd(now.date())
        qnow = hc.scalar("SELECT serial_no FROM serial WHERE name=%s", (qname,))
        ready += 1
        print("  %s[พร้อม]%s  #%s  HN %s  ref %s" % (C_OK, C_END, r["id"], d["hn"], r.get("ref_id") or "-"))
        print("    จากหน้าเว็บ : %s%s%s" % (C_DIM, json.dumps(d, ensure_ascii=False), C_END))
        print("    จะสร้างเอง  : vn ~ %s%s · vstdate %s · vsttime %s"
              % (vn_now, "  (ชนแล้ว จะเลื่อนวินาที)" if taken else "", now.date(), now.strftime("%H:%M:%S")))
        print("                  oqueue <- get_serialnumber('%s')  ตอนนี้ตัวนับอยู่ที่ %s"
              % (qname, qnow if qnow is not None else "ยังไม่มีแถวนี้ (จะเริ่มที่ 1)"))
        print("                  hos_guid + ovst_key สุ่มใหม่ตอนเขียน")
        extra = dict(d)
        fill_ovst_extras(hc, extra, r)
        add = {k: extra[k] for k in extra if k not in d}
        if add:
            print("    gateway เติมให้ : %s%s%s"
                  % (C_DIM, json.dumps(add, ensure_ascii=False), C_END))
        if miss:
            print("    %s[เตือน]%s หน้าเว็บไม่ได้ส่งค่าเหล่านี้มา: %s" % (C_WARN, C_END, ", ".join(miss)))
        vpt, why = build_visit_pttype(hc, dict(extra, vn="<vn ที่จะสร้าง>"))
        if vpt:
            print("    สิทธิ -> %s : %s%s%s"
                  % (VISIT_PTTYPE_TABLE, C_DIM,
                     json.dumps({k: v for k, v in vpt.items() if k != "vn"}, ensure_ascii=False), C_END))
        else:
            print("    %s[เตือน]%s เขียน %s ไม่ได้: %s" % (C_WARN, C_END, VISIT_PTTYPE_TABLE, why))
        rin, rwhy = build_referin(hc, dict(extra, vn="<vn ที่จะสร้าง>",
                                           vstdate=now.date(), vsttime=now.strftime("%H:%M:%S")), r)
        if rin:
            print("    ส่งต่อ -> %s : %s%s%s"
                  % (REFERIN_TABLE, C_DIM,
                     json.dumps({k: v for k, v in rin.items() if k != "vn"},
                                ensure_ascii=False, default=str), C_END))
        else:
            print("    %s[เตือน]%s เขียน %s ไม่ได้: %s" % (C_WARN, C_END, REFERIN_TABLE, rwhy))
        bad = check_masters(hc, d)
        if bad:
            print("    %s[เตือน]%s รหัสที่หาไม่เจอในฐานปลายทาง: %s" % (C_WARN, C_END, " · ".join(bad)))

    print("\nสรุป: พร้อมเปิด visit %d / %d รายการ%s"
          % (ready, len(rows), ("  ·  ติดบัตร %d" % blocked) if blocked else ""))
    print("%s(preview ไม่เขียนอะไรเลย — สั่ง visit-sync เมื่อพร้อม)%s" % (C_DIM, C_END))
    hc.close(); cc.close()
    return 0


def do_visit_sync(CENTRAL, HIS, limit):
    """เปิด visit จริง: INSERT ovst แล้วเขียน vn / status='done' / done_at กลับเข้าคิว"""
    cc, cerr = connect(CENTRAL, "BMS_DB_")
    if not cc:
        print("%sต่อฐานกลางไม่ได้%s: %s" % (C_BAD, C_END, cerr)); return 1
    try:
        rows = fetch_visit_pending(cc, limit)
    except Exception as e:
        print("%sอ่านตาราง %s ไม่ได้%s: %s" % (C_BAD, VISIT_TABLE, C_END, e)); cc.close(); return 1
    other = count_visit_other_days(cc)
    if not rows:
        print("ไม่มีคำขอเปิด visit ของวันนี้ (%s)%s"
              % (datetime.date.today().strftime("%Y-%m-%d"),
                 "" if not other else ("  —  มีค้างของวันอื่น/ไม่ระบุวัน %d รายการ" % other)))
        cc.close(); return 0

    hc, herr = connect(HIS, "BMS_HIS_")
    if not hc:
        print("%sต่อฐาน HIS ไม่ได้%s: %s" % (C_BAD, C_END, herr)); cc.close(); return 1

    print("เปิด visit ของวันนี้ (%s) %d รายการ -> %s@%s:%s/%s%s\n"
          % (datetime.date.today().strftime("%Y-%m-%d"), len(rows),
             HIS["user"] or "-", HIS["host"], HIS["port"], HIS["name"],
             "" if not other else ("\n%sค้างของวันอื่น/ไม่ระบุวันอีก %d รายการ (ไม่หยิบมาทำ)%s"
                                   % (C_DIM, other, C_END))))
    t = visit_table()
    done = failed = skipped = blocked = no_pttype = no_referin = 0
    for r in rows:
        cid = r["id"]
        try:
            # กันซ้ำก่อนขอเลขคิว ไม่งั้นเลขคิวถูกกินทิ้งฟรีทุกครั้งที่เจอแถวซ้ำ
            hn0 = ovst_fixed(r).get("hn")
            if not hn0:
                raise ValueError("ไม่มี HN")

            # ยังไม่มีบัตรในฐานปลายทาง -> ไม่เปิด visit ให้ ไม่งั้นได้ VN ที่ HOSxP เปิดดูไม่เจอ
            # ปล่อยคำขอค้างเป็น pending ไว้ พอห้องบัตรทำบัตรแล้วรอบถัดไปจะเปิดให้เอง
            # (ตรวจก่อนขอเลขคิว เลขคิวจะได้ไม่ถูกกินทิ้ง)
            if CHECK_PATIENT and patient_exists(hc, hn0) is False:
                blocked += 1
                print("  %s[ติดบัตร]%s #%s  HN %s ไม่มีในตาราง patient — ยังไม่เปิด visit"
                      % (C_BAD, C_END, cid, hn0))
                continue

            today = datetime.date.today()
            n = hc.scalar("SELECT COUNT(*) FROM ovst WHERE hn=%s AND vstdate=%s", (hn0, today))
            if n and int(n) > 0:
                ex = hc.scalar("SELECT vn FROM ovst WHERE hn=%s AND vstdate=%s "
                               "ORDER BY vsttime DESC LIMIT 1", (hn0, today))
                cc.execute("UPDATE %s SET status='done', vn=%%s, done_at=NOW() WHERE id=%%s" % t, (ex, cid))
                cc.commit()
                skipped += 1; done += 1
                print("  %s[ข้าม]%s   #%s  HN %s มี visit วันนี้แล้ว (vn %s)" % (C_DIM, C_END, cid, hn0, ex))
                continue

            d, meta = build_ovst(hc, r)
            miss = [k for k in OVST_REQUIRED if not d.get(k)]
            if miss:
                raise ValueError("ขาดค่าที่จำเป็น: " + ", ".join(miss))

            _ins(hc, "ovst", d)
            # สิทธิต้องอยู่ในทรานแซกชันเดียวกัน — visit ที่ไม่มีสิทธิแนบมาแย่กว่าไม่เปิดเลย
            vpt, why = build_visit_pttype(hc, d)
            if vpt:
                _ins(hc, VISIT_PTTYPE_TABLE, vpt)
            # ทะเบียนรับส่งต่อ — "ถ้ายังไม่มี" ตาม PK (vn) vn เพิ่งสร้างจึงไม่ควรมี
            # แต่ตรวจไว้เผื่อกรณีเขียนซ้ำ จะได้ไม่ล้มทั้งใบเพราะ duplicate key
            rin, rwhy = build_referin(hc, d, r)
            if rin and referin_exists(hc, rin["vn"]):
                rin, rwhy = None, "มีทะเบียน %s ของ vn นี้อยู่แล้ว" % REFERIN_TABLE
            if rin:
                _ins(hc, REFERIN_TABLE, rin)
            hc.commit()

            cc.execute("UPDATE %s SET status='done', vn=%%s, done_at=NOW() WHERE id=%%s" % t, (d["vn"], cid))
            cc.commit()
            done += 1
            print("  %s[เปิดแล้ว]%s #%s  HN %s -> vn %s  คิว %s  (%s)"
                  % (C_OK, C_END, cid, d["hn"], d["vn"], d["oqueue"], meta["queue_serial"]))
            if meta.get("extras"):
                print("      %sเติมให้: %s%s"
                      % (C_DIM, json.dumps(meta["extras"], ensure_ascii=False), C_END))
            if vpt:
                print("      %sสิทธิ %s -> %s (pttype %s)%s"
                      % (C_DIM, vpt["pttype"], VISIT_PTTYPE_TABLE, vpt["pttype"], C_END))
            else:
                no_pttype += 1
                print("      %s[เตือน]%s ไม่ได้เขียน %s: %s" % (C_WARN, C_END, VISIT_PTTYPE_TABLE, why))
            if rin:
                print("      %sส่งต่อ %s เลขที่ %s -> %s%s"
                      % (C_DIM, rin.get("refer_hospcode") or "-",
                         rin.get("referin_number") or "-", REFERIN_TABLE, C_END))
            else:
                no_referin += 1
                print("      %s[เตือน]%s ไม่ได้เขียน %s: %s" % (C_WARN, C_END, REFERIN_TABLE, rwhy))
            if meta["vn_shifted"]:
                print("      %svn ชนกัน เลื่อนไป %d วินาที%s" % (C_DIM, meta["vn_shifted"], C_END))
        except Exception as e:
            hc.rollback()
            try:
                cc.execute("UPDATE %s SET status='error' WHERE id=%%s" % t, (cid,)); cc.commit()
            except Exception:
                cc.rollback()
            failed += 1
            print("  %s[ผิดพลาด]%s #%s  %s" % (C_BAD, C_END, cid, e))

    print("\nสรุป: สำเร็จ %d (ข้าม %d) · ล้มเหลว %d%s · จากทั้งหมด %d"
          % (done, skipped, failed,
             ("" if not blocked else (" · %sติดบัตร %d%s" % (C_BAD, blocked, C_END))), len(rows)))
    if no_pttype:
        print("%s%d รายการเปิด visit แล้วแต่ไม่มีแถวสิทธิใน %s — ต้องไปใส่สิทธิใน HOSxP เอง%s"
              % (C_WARN, no_pttype, VISIT_PTTYPE_TABLE, C_END))
    if no_referin:
        print("%s%d รายการเปิด visit แล้วแต่ไม่มีทะเบียนใน %s — ต้องไปคีย์ใบส่งต่อใน HOSxP เอง%s"
              % (C_WARN, no_referin, REFERIN_TABLE, C_END))
    if blocked:
        print("%sรายการที่ติดบัตรยังค้างเป็น pending — ทำบัตรใน HOSxP แล้วสั่ง visit-sync ซ้ำได้เลย%s"
              % (C_DIM, C_END))
    hc.close(); cc.close()
    return 0 if failed == 0 else 2


def do_fix_summary(his, dfrom, dto, doc):
    """ยุบบรรทัดสรุปที่แตกเป็นหลายแถวต่อวัน ให้เหลือวันละแถวแบบที่ HOSxP เขียนเอง

       ใช้ซ่อมข้อมูลที่ gateway รุ่นก่อนสร้างไว้ · ไม่แตะจำนวนโควตารวม"""
    hc, err = connect(his, "BMS_HIS_")
    if not hc:
        print("%sต่อฐาน HIS ไม่ได้%s: %s" % (C_BAD, C_END, err)); return 1
    today = datetime.date.today()
    d1 = dfrom or today.isoformat()
    d2 = dto or (today + datetime.timedelta(days=365)).isoformat()
    print("ยุบบรรทัดสรุป opd_qs_slot_summary ช่วง %s .. %s%s" % (d1, d2, ("  แพทย์ " + doc) if doc else ""))
    try:
        days, removed = fix_qs_summary(hc, d1, d2, doc)
        hc.commit()
        print(("\nเสร็จ: แก้ %d วัน · ลบแถวซ้ำ %d แถว" % (days, removed)) if days
              else ("\nไม่มีวันไหนที่มีบรรทัดสรุปซ้ำ"))
        return 0
    except Exception as e:
        hc.rollback()
        print("%sล้มเหลว%s: %s" % (C_BAD, C_END, e)); return 1
    finally:
        hc.close()


def main():
    ap = argparse.ArgumentParser(description="BMS Gateway: ฐานกลาง -> HIS (oapp)")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "preview", "sync",
                             "visit-preview", "visit-sync", "sync-all", "fix-summary",
                             "alert-preview", "alert-sync", "alert-test"])
    ap.add_argument("--limit", type=int, default=50, help="จำนวนสูงสุดต่อรอบ (ค่าเริ่มต้น 50)")
    ap.add_argument("--watch", type=int, default=0, metavar="SEC", help="ทำงานวนทุก SEC วินาที (โหมด service)")
    ap.add_argument("--force", action="store_true",
                    help="ข้ามการตรวจว่ารหัส lab/x-ray ในคิวมีอยู่ในฐาน HIS ปลายทางจริงไหม")
    ap.add_argument("--from", dest="dfrom", default=None, metavar="YYYY-MM-DD",
                    help="fix-summary: วันเริ่ม (ไม่ใส่ = วันนี้)")
    ap.add_argument("--to", dest="dto", default=None, metavar="YYYY-MM-DD",
                    help="fix-summary: วันสิ้นสุด (ไม่ใส่ = +1 ปี)")
    ap.add_argument("--doctor", default=None, metavar="CODE",
                    help="fix-summary: เฉพาะรหัสนี้ เช่น 065:1380 (ไม่ใส่ = ทุกคน)")
    ap.add_argument("--days", type=int, default=None, metavar="N",
                    help="หยิบคิวย้อนหลัง N วัน (0 = เฉพาะวันนี้) · ไม่ใส่ = ใช้ค่า BMS_SYNC_BACK_DAYS ใน config.bat")
    ap.add_argument("--cid", default=None, metavar="เลขบัตร13หลัก",
                    help="alert-test: เลขบัตรประชาชนที่จะรับการ์ดทดสอบ (ใช้ของเจ้าหน้าที่เอง)")
    ap.add_argument("--oapp", default=None, metavar="OAPP_ID",
                    help="alert-test: ใช้เนื้อหาของนัดใบนี้ (ส่งไปที่ --cid ไม่ใช่คนไข้)")
    a = ap.parse_args()

    load_config_if_needed()      # รองรับการรัน exe ตรง ๆ (อ่าน config.bat ที่วางข้าง ๆ)
    global SYNC_BACK_DAYS
    if a.days is not None:                       # --days ทับค่าใน config เฉพาะการรันครั้งนี้
        SYNC_BACK_DAYS = max(0, min(365, a.days))
    else:                                        # config.bat เพิ่งถูกอ่านเข้ามาหลัง import -> อ่านซ้ำ
        SYNC_BACK_DAYS = _int_env("BMS_SYNC_BACK_DAYS", 0, 0, 365)
    CENTRAL = cfg_from("BMS_DB_", "mysql", "appointment_queue")
    HIS = cfg_from("BMS_HIS_", "pgsql", "oapp")
    limit = max(1, min(a.limit, 500))

    if a.action == "status":
        return do_status(CENTRAL, HIS)
    if a.action == "preview":
        return do_preview(CENTRAL, HIS, limit)
    if a.action == "visit-preview":
        return do_visit_preview(CENTRAL, HIS, limit)
    if a.action == "fix-summary":
        return do_fix_summary(HIS, a.dfrom, a.dto, a.doctor)
    if a.action == "alert-preview":
        return do_alert_preview(CENTRAL, HIS, limit)
    if a.action == "alert-test":
        if not a.cid:
            print("ใส่ --cid เลขบัตรประชาชน 13 หลักของผู้รับการ์ดทดสอบ"); return 1
        return do_alert_test(HIS, a.cid, a.oapp)

    def one_round():
        """หนึ่งรอบของ service — คิวไหนล้มก็ไม่ลากอีกคิวลงไปด้วย"""
        rc = 0
        if a.action == "alert-sync":
            return do_alert_sync(CENTRAL, HIS, limit, explicit=True)
        if a.action in ("sync", "sync-all"):
            rc = do_sync(CENTRAL, HIS, limit, a.force) or rc
        if a.action in ("visit-sync", "sync-all"):
            if a.action == "sync-all":
                print()
            rc = do_visit_sync(CENTRAL, HIS, limit) or rc
        return rc

    if a.watch > 0:
        print("โหมด service: %s ทุก %d วินาที (กด Ctrl+C เพื่อหยุด)\n" % (a.action, a.watch))
        try:
            while True:
                print("--- %s ---" % time.strftime("%Y-%m-%d %H:%M:%S"))
                try:
                    one_round()
                except Exception as e:
                    print("%sรอบนี้ล้มเหลว%s: %s" % (C_BAD, C_END, e))
                time.sleep(a.watch)
        except KeyboardInterrupt:
            print("\nหยุดแล้ว")
            return 0
    return one_round()


if __name__ == "__main__":
    sys.exit(main())
