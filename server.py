#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
เว็บเซิร์ฟเวอร์ Python — เสิร์ฟหน้าเว็บ + API ในตัวเดียว (ใช้แทน PHP ได้ทั้งหมด)

ทำไมต้องมี: เครื่องที่ไม่มี PHP จะรัน .php ไม่ได้ -> POST ได้ 501 -> ดึงข้อมูล/บันทึกไม่ได้
ตัวนี้ทำงานเหมือน sql_proxy.php + save_appointment.php ทุกอย่าง แต่เป็น Python ล้วน

  GET  /<ไฟล์>                 เสิร์ฟไฟล์ในโฟลเดอร์นี้ (หน้าเว็บ)
  POST /sql_proxy.php          {url,key,sql,app} -> ยิงต่อไป tunnel HOSxP (เลี่ยง CORS)
  POST /save_appointment.php   {mysql,row}       -> INSERT ลงฐานกลาง
  GET  ทั้งสอง endpoint         -> สถานะ (ไว้ตรวจว่า API ทำงาน)

ค่าเชื่อมต่อฐานกลางอ่านจาก config.bat (รองรับ user/pass ที่เข้ารหัสไว้)
รัน: python server.py [พอร์ต]
"""

import os
import re
import sys
import json
import datetime
import urllib.parse
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = app_dir()
sys.path.insert(0, HERE)
try:
    import bmscrypt
except Exception:
    bmscrypt = None
try:
    import gateway          # ใช้ DB / cfg_from ร่วมกัน (รองรับ mysql + pgsql + ถอดรหัส)
except Exception:
    gateway = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else int(os.getenv("BMS_PORT") or 8137)
PAGE = "appointment-autofill-v3.html"
REGISTER_PAGE = "appointment-register.html"        # ทะเบียนนัด (นัด + โน้ต + lab)
API_PATHS = ("/sql_proxy.php", "/save_appointment.php")
# เสิร์ฟผ่านเว็บได้เฉพาะไฟล์เว็บที่ตั้งใจ (default-deny) — กันโหลดไฟล์ระบบ/ความลับ
# (start.bat, config.bat, config.key, *.py, *.php, *.csv, *.json ฯลฯ) ผ่าน HTTP
ALLOWED_EXT = (".html", ".htm", ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif",
               ".svg", ".ico", ".webp", ".bmp", ".pdf", ".woff", ".woff2", ".ttf", ".otf", ".map")


def central_cfg(from_browser):
    """ค่าเชื่อมต่อฐานกลาง

       ลำดับ: ค่าที่ผู้ใช้ "ตั้งเองในหน้าเว็บ" (ปุ่ม ⚙) มาก่อน -> ถ้าไม่ได้ตั้ง ค่อยใช้ config.bat

       ที่ต้องให้หน้าเว็บชนะ: เครื่องหน้างานได้ config.bat ติดมากับ dist ของเครื่อง dev
       (build.bat ก๊อปทับให้ทุกครั้ง) ซึ่งมักชี้ 127.0.0.1 ที่เครื่องนั้นไม่มี MySQL
       ถ้า env ชนะตลอด ผู้ใช้จะแก้ผ่านหน้าเว็บไม่ได้เลย ต้องเข้าไปแก้ไฟล์ที่เครื่องนั้นอย่างเดียว

       นับว่า "ตั้งเอง" เมื่อกรอก database มาจริง ๆ เท่านั้น — หน้าเว็บส่ง host/port ว่างมาเสมอ
       ถ้าเอาแค่ host มาตัดสิน เครื่องที่ไม่เคยแตะปุ่ม ⚙ จะถูกเปลี่ยน config ไปด้วย"""
    b = from_browser or {}
    picked_by_user = bool(str(b.get("database") or "").strip())
    if gateway is not None and not picked_by_user:
        gateway.load_config_if_needed()
        c = gateway.cfg_from("BMS_DB_", "mysql", "appointment_queue")
        if c.get("name"):
            return c, "config.bat"
    drv = (b.get("driver") or "mysql").lower()
    return {
        "driver": drv if drv in ("mysql", "pgsql") else "mysql",
        "host": b.get("host") or "127.0.0.1",
        "port": int(b.get("port") or (5432 if drv == "pgsql" else 3306)),
        "name": b.get("database") or "",
        "user": b.get("user") or "",
        "pass": b.get("password") or "",
        "table": re.sub(r"[^A-Za-z0-9_]", "", str(b.get("table") or "appointment_queue")) or "appointment_queue",
    }, "browser"


# คอลัมน์ที่เพิ่มมาทีหลัง — ตารางเก่าที่ลูกค้าสร้างไว้แล้วต้องเติมให้เอง ไม่งั้น INSERT พัง
# ref_id = กุญแจอ้างกลับไปยังของเดิมใน HIS (หน้าทะเบียนนัดใส่ oapp_id ของนัดที่มีอยู่แล้ว)
NEW_COLUMNS = [("doctor_code", "VARCHAR(15)"), ("doctor_note", "TEXT"), ("ref_id", "VARCHAR(20)"), ("source_type", "VARCHAR(40)")]


def add_missing_columns(db, table):
    for col, typ in NEW_COLUMNS:
        try:
            db.scalar("SELECT %s FROM %s LIMIT 1" % (col, table))
            continue                                  # มีอยู่แล้ว
        except Exception:
            db.rollback()
        try:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
            db.commit()
            print("  เพิ่มคอลัมน์ %s.%s แล้ว" % (table, col))
        except Exception as e:
            db.rollback()
            print("  เพิ่มคอลัมน์ %s.%s ไม่ได้: %s" % (table, col, e))


# เบราว์เซอร์ปิด connection กลางคัน (refresh / ปิดแท็บ / ยกเลิกโหลด) ไม่ใช่ error ของเรา
DROPPED = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):        # เงียบ ๆ ไม่ต้องรก console
        pass

    def version_string(self):                 # ไม่บอกใบ้ยี่ห้อ/เวอร์ชันเซิร์ฟเวอร์ในเฮดเดอร์ Server
        return "BMS"

    def list_directory(self, path):           # ปิด directory listing — ไม่ให้ไล่ดูรายชื่อไฟล์ในโฟลเดอร์
        self.send_error(404, "Not Found")
        return None

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except DROPPED:                       # ฝั่งโน้นวางสายไปเอง -> ปิดเงียบ ๆ
            self.close_connection = True

    def end_headers(self):
        # หน้าเว็บห้ามให้เบราว์เซอร์แคช — SimpleHTTPRequestHandler ส่งแต่ Last-Modified
        # ไม่มี Cache-Control เบราว์เซอร์เลยใช้ heuristic caching หยิบของเก่ามาใช้โดยไม่ถามเซิร์ฟเวอร์
        # ผลคือแก้ไฟล์แล้วผู้ใช้ต้องกด Ctrl+Shift+R ทุกครั้งถึงจะเห็นของใหม่
        path = self.path.split("?")[0].lower()
        if path.endswith((".html", ".htm", ".js", ".css")) or path in API_PATHS:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            return json.loads(raw.decode("utf-8")), raw
        except Exception:
            return None, raw

    # ---------- HTTP verbs ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/sql_proxy.php":
            return self._send(200, {"ok": True, "service": "BMS sql proxy (python)",
                                    "engine": "python",
                                    "hint": "POST {url,key,sql,app}"})
        if p == "/save_appointment.php":
            c, src = central_cfg(None)
            return self._send(200, {"ok": True, "service": "BMS MySQL API (python)",
                                    "engine": "python",
                                    "form_template": True,   # รองรับ pref_*/tpl_* (client เช็คก่อนยิง กัน INSERT แถวเปล่า)
                                    "drivers": (gateway is not None),
                                    "config_from": src,
                                    "db": ("%s@%s:%s/%s" % (c["user"], c["host"], c["port"], c["name"])) if c["name"] else None,
                                    "table": c["table"] if c["name"] else None,
                                    "secrets": "เข้ารหัส (config.key)" if os.getenv("BMS_DB_PASS_ENC") else "plaintext",
                                    "hint": "POST JSON {row:{hn,vn,ref_id,datetime,status,json_row}} · "
                                            "{action:'list',ref_ids:[...]} เพื่อดูสถานะในคิว"})
        if p == "/":
            self.path = "/" + PAGE
            p = "/" + PAGE
        # default-deny: เสิร์ฟเฉพาะไฟล์เว็บที่อนุญาต — ไฟล์ระบบ/ความลับ (bat/py/php/key/config/csv/json)
        # และไดเรกทอรี/dotfile เข้าไม่ได้ -> 404
        seg = p.rsplit("/", 1)[-1]
        dot = seg.rfind(".")
        ext = seg[dot:].lower() if dot >= 0 else ""
        if ext not in ALLOWED_EXT or seg.startswith("."):
            return self._send(404, {"ok": False, "message": "not found"})
        return super().do_GET()

    def do_POST(self):
        p = self.path.split("?")[0]
        if p not in API_PATHS:
            return self._send(404, {"ok": False, "message": "ไม่รู้จัก endpoint %s" % p})
        body, raw = self._body()
        if body is None:
            return self._send(400, {"MessageCode": 0, "ok": False,
                                    "message": "อ่าน JSON จาก body ไม่ได้ (ได้ %d bytes)" % len(raw)})
        if p == "/sql_proxy.php":
            return self.do_proxy(body)
        return self.do_save(body)

    # ---------- /sql_proxy.php ----------
    def do_proxy(self, b):
        base = str(b.get("url") or "").rstrip("/")
        key = str(b.get("key") or "")
        sql = str(b.get("sql") or "")
        app = str(b.get("app") or "BMS.Appointment.Autofill")
        if not base or not sql:
            return self._send(400, {"MessageCode": 0, "Message": "proxy: ต้องมี url และ sql"})
        host = urllib.parse.urlparse(base).hostname or ""
        if not re.search(r"(^|\.)hosxp\.net$", host, re.I):      # กันเอาไปยิงเว็บอื่น
            return self._send(403, {"MessageCode": 0, "Message": "proxy: อนุญาตเฉพาะ *.hosxp.net"})

        url = base + "/api/sql?sql=" + urllib.parse.quote(sql, safe="") + "&app=" + urllib.parse.quote(app, safe="")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key,
                                                   "Accept": "application/json"})
        try:
            # 40 วิ ไม่ใช่ 20 — คิวรี่ของแดชบอร์ดตรวจนำเข้า lab หานัดคู่ทับวันที่มีนัด 2000+ ใบ
            # กินเวลาเกิน 20 วิได้ ตัดตรงนี้ก่อนจะกลายเป็น 502 ทั้งที่ HOSxP ยังทำงานอยู่
            # (ต้องน้อยกว่า TIMEOUT ฝั่งเบราว์เซอร์ 45 วิ เพื่อให้ได้ error จริงจาก tunnel)
            with urllib.request.urlopen(req, timeout=40) as r:
                data, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            data, code = e.read(), e.code
        except Exception as e:
            return self._send(502, {"MessageCode": 0, "Message": "proxy ต่อ tunnel ไม่ได้: %s" % e})

        txt = data.decode("utf-8", "replace").strip()
        if not txt or txt[0] not in "{[":          # tunnel ตอบไม่ใช่ JSON (เช่น 502 เปล่า)
            hint = ("tunnel ติดต่อ HOSxP ปลายทางไม่ได้ (เครื่อง รพ. ปิด / tunnel หลุด / session หมดอายุ)"
                    if code in (502, 504) else (re.sub(r"<[^>]+>", "", txt)[:200] or "tunnel ตอบเปล่า"))
            return self._send(code or 502, {"MessageCode": code, "Message": "HTTP %s — %s" % (code, hint)})
        return self._send(code or 200, data)       # ส่ง JSON ดิบจาก tunnel กลับไปตรง ๆ

    # ---------- action=list : บอกหน้าเว็บว่ารายการไหนส่งเข้าคิวไปแล้ว ----------
    def do_list(self, db, T, b):
        """คืนเฉพาะคอลัมน์สั้น ๆ (ไม่ส่ง json_row กลับ — ใหญ่และหน้าเว็บไม่ได้ใช้)
           กรองด้วย ref_ids (oapp_id ที่กำลังแสดงอยู่) หรือ hn"""
        refs = [str(x)[:20] for x in (b.get("ref_ids") or []) if str(x).strip()][:500]
        hn = str(b.get("hn") or "").strip()
        try:
            limit = max(1, min(int(b.get("limit") or 500), 2000))
        except Exception:
            limit = 500
        where, params = [], []
        if refs:
            where.append("ref_id IN (%s)" % ",".join(["%s"] * len(refs)))
            params += refs
        if hn:
            where.append("hn = %s")
            params.append(hn)
        sql = "SELECT id, hn, vn, ref_id, status, datetime FROM %s" % T
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT %d" % limit
        rows = db.query(sql, tuple(params))
        out = [{k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, (datetime.datetime, datetime.date)) else v)
                for k, v in dict(r).items()} for r in rows]
        return self._send(200, {"ok": True, "rows": out, "table": T})

    # ---------- action=requeue : ดันแถวที่ตกหล่นกลับเข้าคิว ให้ gateway อ่านใหม่ ----------
    def do_requeue(self, db, T, b):
        """ตั้ง status กลับเป็น pending เฉพาะแถวที่ส่ง id มา (ปุ่ม "อัพเดทสถานะ" ในหน้า insight)

           กันพลาด 2 ชั้น:
             1) แตะได้เฉพาะแถวที่ตอนนี้เป็น done/error — pending อยู่แล้วไม่ต้องทำอะไร
                และห้ามใช้ปลุกแถวที่ถูกยกเลิกใน HOSxP กลับมา (หน้าเว็บไม่ส่ง id พวกนั้นมาให้)
             2) ล้าง ref_id ทิ้งด้วย เพราะ oapp_id เดิมชี้ไปนัดที่ไม่มีอยู่แล้ว
           ถ้าเผลอดันแถวที่ HIS มีจริง gateway จะเจอว่าซ้ำ (HN+วันนัด+แพทย์+ห้อง) แล้วข้ามให้เอง
           ไม่เกิดนัดซ้ำในฐาน"""
        ids = []
        for x in (b.get("ids") or [])[:500]:
            sx = str(x).strip()
            if sx.isdigit():
                ids.append(int(sx))
        if not ids:
            return self._send(400, {"ok": False, "message": "ไม่ได้ระบุรายการที่จะอัพเดท"})
        marks = ",".join(["%s"] * len(ids))
        where = "id IN (%s) AND status IN ('done','error')" % marks
        n = int(db.scalar("SELECT COUNT(*) FROM %s WHERE %s" % (T, where), tuple(ids)) or 0)
        if n:
            db.execute("UPDATE %s SET status='pending', ref_id=NULL WHERE %s" % (T, where), tuple(ids))
            db.commit()
        return self._send(200, {"ok": True, "updated": n, "asked": len(ids)})

    # ---------- action=slot_add / slot_list : คิวคำขอ "เพิ่มคิวนัด" (ตารางแยกของตัวเอง) ----------
    def slot_table(self, b):
        """ชื่อตารางคำขอเพิ่มคิว — ตั้งทับได้ที่ config.bat (BMS_DB_SLOT_TABLE)
           แยกจาก appointment_queue เพราะคนละชนิดข้อมูล: ใบนัด vs คำขอเปิดคิว
           แยกแล้วอ่านง่าย ตรวจย้อนง่าย และ gateway หยิบไปทำคนละทางไม่ปนกัน"""
        t = str(b.get("slot_table") or os.getenv("BMS_DB_SLOT_TABLE") or "slot_add_queue")
        return re.sub(r"[^A-Za-z0-9_]", "", t) or "slot_add_queue"

    def slot_ensure(self, db, T, pg):
        """สร้างตารางให้เองถ้ายังไม่มี — เก็บเป็นคอลัมน์จริง ไม่ยัดทุกอย่างลง json
           เจ้าหน้าที่เปิดดูด้วย SQL ธรรมดาแล้วรู้เรื่องทันทีว่าใครขอเพิ่มคิวอะไรไว้"""
        if pg:
            db.execute("""CREATE TABLE IF NOT EXISTS %s (
                id BIGSERIAL PRIMARY KEY, requested_at TIMESTAMP, schedule_date DATE,
                doctor_code VARCHAR(30), clinic_code VARCHAR(10),
                queue_schedule_id INT, queue_slot_id INT, slot_count INT,
                reason VARCHAR(255), requested_by VARCHAR(64), hn VARCHAR(20),
                status VARCHAR(20), result_note VARCHAR(255), done_at TIMESTAMP, json_row TEXT)""" % T)
        else:
            db.execute("""CREATE TABLE IF NOT EXISTS %s (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, requested_at DATETIME, schedule_date DATE,
                doctor_code VARCHAR(30), clinic_code VARCHAR(10),
                queue_schedule_id INT, queue_slot_id INT, slot_count INT,
                reason VARCHAR(255), requested_by VARCHAR(64), hn VARCHAR(20),
                status VARCHAR(20), result_note VARCHAR(255), done_at DATETIME, json_row LONGTEXT,
                INDEX(status), INDEX(schedule_date), INDEX(doctor_code)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""" % T)
        db.commit()

    def slot_denied(self, db, r, scoped=False):
        """ไม่มีสิทธิ์เพิ่ม/สร้างคิวหรือเปล่า — คืนข้อความถ้าไม่ผ่าน (None = ผ่าน)

           หน้าเว็บซ่อนปุ่มให้แล้ว แต่ค่าฝั่งเบราว์เซอร์แก้ได้ จึงต้องกันซ้ำที่นี่
           รายชื่อผู้มีสิทธิ์เป็นค่ากลางใน appt_user_pref (login '_global', key 'slot_cfg')"""
        cfg = {}
        try:
            pr = db.query("SELECT data FROM appt_user_pref WHERE login=%s AND pref_key=%s", ("_global", "slot_cfg"))
            if pr:
                cfg = json.loads(pr[0]["data"] or "{}") or {}
        except Exception:
            db.rollback()
        # กติกาเทียบชุดเดียวกับหน้า slot-admin (whoClean / isMe) และหน้าทำนัด (whoIsMe)
        # — ต่างกันเมื่อไหร่ คนที่เห็นปุ่มจะโดนตีกลับ
        # ค่าในรายชื่อเป็นตัวเลข 4 หลัก (เช่น 0004) = รหัสแพทย์ -> เทียบกับ by_id (doctor_code จาก BMS Session)
        # นอกนั้น = ชื่อล็อกอิน HOSxP -> เทียบกับ by_login · ไม่เทียบด้วยชื่อ-สกุลแล้ว (ชื่อซ้ำ/คำนำหน้า ให้สิทธิ์ผิดคนได้)
        def _who_key(v):
            # ค่าในรายชื่อ: ข้อความเต็มจากรายการเลือก ("mazza  เอกพงษ์ ...") ใช้แค่รหัสหน้าสุด
            t = str(v or "").strip()
            m = re.match(r"^([\x21-\x7e]+)\s{2,}\S", t)
            return (m.group(1) if m else re.sub(r"\s+", " ", t)).lower()

        my_doc = str(r.get("by_id") or "").strip().lower()
        my_login = str(r.get("by_login") or "").strip().lower()

        def _is_me(v):
            k = _who_key(v)
            if not k:
                return False
            if re.fullmatch(r"\d{4}", k):
                return bool(my_doc) and k == my_doc
            return bool(my_login) and k == my_login

        allow = [_who_key(x) for x in str(cfg.get("who") or "").split(",") if x.strip()]
        is_admin = any(_is_me(a) for a in allow)
        scope = cfg.get("scope") if isinstance(cfg.get("scope"), list) else []
        rows = [x for x in scope if isinstance(x, dict) and str(x.get("who") or "").strip()]
        mine = None
        for x in rows:
            if _is_me(x.get("who")):
                mine = x
                break
        nope = "บัญชีนี้ไม่มีสิทธิ์เพิ่มคิว (ผู้ดูแลกำหนดรายชื่อไว้ในเมนูตั้งค่า)"
        # ข้อ ② ผู้ดูแลระบบ "ไม่ถูกจำกัดด้วยขอบเขตรายคน" ตามที่เขียนไว้ในหน้าตั้งค่า
        # (เดิมผู้ดูแลที่มีแถวขอบเขตด้วย ถูกจำกัดที่นี่ แต่หน้าเว็บไม่จำกัด)
        if is_admin:
            return None
        # คำขอแก้ตารางแม่แบบทั้ง รพ. (ช่วงเวลา/โควตา/เทมเพลต/ธง/อักษรนำ) ไม่มีคลินิกให้เทียบ -> ผู้ดูแลเท่านั้น
        if not scoped:
            return nope if allow else None
        # คำขอที่ระบุคลินิก/แพทย์ (สร้างตาราง / เพิ่มคิว / ลดคิว) — ข้อ ④ คนที่มีแถวขอบเขตรายคนทำได้
        # แม้ไม่อยู่ในรายชื่อผู้ดูแล (เดิมโดนตีกลับตั้งแต่บรรทัดรายชื่อ ขอบเขตจึงไม่เคยถูกใช้)
        # ยังไม่ตั้งทั้งผู้ดูแลและขอบเขต = ทุกคน · ตั้งไว้แล้วแต่ไม่มีชื่อทั้งสองที่ = ไม่มีสิทธิ์
        if not mine:
            return nope if (allow or rows) else None

        # ขอบเขตรายคน — พยาบาลบางคนให้สร้างได้เฉพาะคลินิก/แพทย์ที่กำหนด
        # หน้าเว็บบล็อกให้แล้ว แต่ค่าฝั่งเบราว์เซอร์แก้ได้ ด่านจริงต้องอยู่ตรงนี้
        # ในแถวของตัวเอง ช่องคลินิก/แพทย์ที่เว้นว่าง = ไม่จำกัดเรื่องนั้น
        # ขอบเขตคลินิก/แพทย์ใช้ได้เฉพาะคำขอที่ระบุคู่คลินิก+แพทย์อยู่แล้ว (สร้างตาราง / เพิ่มคิวรายวัน)
        # คำขอที่แก้ตารางแม่แบบทั้งโรงพยาบาล (ช่วงเวลา/ชุดโควตา/เทมเพลต) ไม่มีคลินิกให้เทียบ
        # จึงยังคุมด้วยรายชื่อ "ผู้มีสิทธิ์เพิ่มคิว" อย่างเดียวเหมือนเดิม
        clinic = str(r.get("clinic") or "").strip()
        doc = str(r.get("doctor_plain") or "").strip()
        if not doc:
            # slot_add ส่งมาเป็น "คลินิก:แพทย์" ตามรูปแบบของตารางคิว
            code = str(r.get("doctor_code") or "").strip()
            doc = code.split(":")[-1] if ":" in code else code
        cl_ok = [str(v).strip() for v in (mine.get("clinics") or []) if str(v).strip()]
        dr_ok = [str(v).strip() for v in (mine.get("doctors") or []) if str(v).strip()]
        if cl_ok and clinic and clinic not in cl_ok:
            return "บัญชีนี้สร้างตารางคิวของคลินิก %s ไม่ได้ — ทำได้เฉพาะ %s" % (clinic, ", ".join(cl_ok))
        if dr_ok and doc and doc not in dr_ok:
            return "บัญชีนี้สร้างตารางคิวของแพทย์ %s ไม่ได้ — ทำได้เฉพาะ %s" % (doc, ", ".join(dr_ok))
        # กำหนดขอบเขตไว้แต่คำขอไม่บอกว่าคลินิก/แพทย์ไหน = ปล่อยผ่านไม่ได้
        if cl_ok and not clinic:
            return "คำขอไม่ได้ระบุคลินิก — บัญชีนี้ถูกจำกัดให้ทำได้เฉพาะ %s" % ", ".join(cl_ok)
        if dr_ok and not doc:
            return "คำขอไม่ได้ระบุแพทย์ — บัญชีนี้ถูกจำกัดให้ทำได้เฉพาะ %s" % ", ".join(dr_ok)
        return None

    def do_slot_flag(self, db, b, pg):
        """คำขอ "เปิด/ปิดใช้ระบบคิว" ของคลินิก/แพทย์ — เก็บลงคิวเดียวกับคำขออื่น

           เป็นการแก้ตารางตั้งค่าหลักของ HIS จึงไม่ให้หน้าเว็บเขียนเอง
           gateway เป็นคนอัปเดต แล้วเขียนผลกลับมาในแถวเดิมว่าเปลี่ยนอะไรไปบ้าง"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        cl = r.get("clinic") if isinstance(r.get("clinic"), dict) else {}
        dc = r.get("doctor") if isinstance(r.get("doctor"), dict) else {}
        tt = r.get("clinic_tmpl") if isinstance(r.get("clinic_tmpl"), dict) else {}
        clean = {}
        for name, src in (("clinic", cl), ("doctor", dc)):
            out = {}
            for k, v in src.items():
                k = re.sub(r"[^A-Za-z0-9_\-]", "", str(k or ""))[:20]
                v = str(v or "").strip().upper()
                if k and v in ("Y", "N"):
                    out[k] = v
            clean[name] = out
        tt_clean = {}
        for k, v in tt.items():                 # ชนิดตารางคิวของคลินิก ("" = ล้างค่า)
            k = re.sub(r"[^A-Za-z0-9_-]", "", str(k or ""))[:10]
            v = str(v or "").strip()
            if not k:
                continue
            if v and not v.isdigit():
                return self._send(400, {"ok": False, "message": "ชนิดตารางคิวต้องเป็นตัวเลข (%s)" % v})
            tt_clean[k] = v
        clean["clinic_tmpl"] = tt_clean
        n = len(clean["clinic"]) + len(clean["doctor"]) + len(tt_clean)
        if not n:
            return self._send(400, {"ok": False, "message": "ไม่มีรายการที่จะเปลี่ยน"})
        if n > 500:
            return self._send(400, {"ok": False, "message": "เปลี่ยนได้ครั้งละไม่เกิน 500 รายการ"})
        deny = self.slot_denied(db, r)
        if deny:
            return self._send(403, {"ok": False, "message": deny})
        payload = dict(clean)
        payload["by"] = str(r.get("by") or "")[:64]
        payload["reason"] = str(r.get("reason") or "")[:255]
        cols = ["requested_at", "slot_count", "reason", "requested_by", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), n,
                ("เปิด/ปิดใช้ระบบคิว คลินิก %d · แพทย์ %d · ชนิดคิว %d %s"
                 % (len(clean["clinic"]), len(clean["doctor"]), len(tt_clean),
                    r.get("reason") or ""))[:255],
                payload["by"], "pending", json.dumps({"slot_flag": payload}, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))),
                   tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "count": n,
                                "clinic": len(clean["clinic"]), "doctor": len(clean["doctor"]),
                                "clinic_tmpl": len(tt_clean)})

    def do_slot_tmpl(self, db, b, pg):
        """คำขอสร้าง/แก้เทมเพลตตารางคิว — เข้าคิวให้ gateway เขียนลง HOSxP"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        raw = r.get("ops") if isinstance(r.get("ops"), list) else []
        ops = []
        for o in raw:
            if not isinstance(o, dict):
                continue
            op = "del" if str(o.get("op") or "").strip().lower() == "del" else "set"
            item = {"op": op}
            rid = str(o.get("id") or "").strip()
            if rid.isdigit():
                item["id"] = int(rid)
            if op == "del":
                if "id" not in item:
                    return self._send(400, {"ok": False, "message": "รายการลบต้องมี id"})
                ops.append(item)
                continue
            item["name"] = str(o.get("name") or "").strip()[:200]
            item["doctor_code"] = re.sub(r"[^A-Za-z0-9_:-]", "", str(o.get("doctor_code") or ""))[:15]
            tt = str(o.get("tmpl_type") or "").strip()
            if not item["name"] or not item["doctor_code"] or not tt.isdigit():
                return self._send(400, {"ok": False,
                    "message": "ทุกเทมเพลตต้องมีชื่อ รหัสแพทย์/คลินิก และชนิด"})
            item["tmpl_type"] = int(tt)
            tot = str(o.get("total_limit") or "").strip()
            if tot.isdigit():
                item["total_limit"] = int(tot)
            lines = []
            for l in (o.get("lines") if isinstance(o.get("lines"), list) else []):
                if not isinstance(l, dict):
                    continue
                gid = str(l.get("opd_queue_slot_id") or "").strip()
                lt = str(l.get("opd_qs_limit_type_id") or "").strip()
                if not gid.isdigit():
                    continue
                if not lt.isdigit():        # NOT NULL ในฐาน — ต้องมีทุกบรรทัด
                    return self._send(400, {"ok": False,
                        "message": "ทุกช่วงเวลาในเทมเพลตต้องเลือกชุดโควตา"})
                lines.append({"opd_queue_slot_id": int(gid), "opd_qs_limit_type_id": int(lt)})
            if not lines:
                return self._send(400, {"ok": False,
                    "message": "เทมเพลต %s ต้องมีอย่างน้อย 1 ช่วงเวลา" % item["name"]})
            item["lines"] = lines
            ops.append(item)
        if not ops:
            return self._send(400, {"ok": False, "message": "ไม่มีรายการที่จะแก้"})
        deny = self.slot_denied(db, r)
        if deny:
            return self._send(403, {"ok": False, "message": deny})
        payload = {"ops": ops, "by": str(r.get("by") or "")[:64], "reason": str(r.get("reason") or "")[:255]}
        cols = ["requested_at", "slot_count", "reason", "requested_by", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), len(ops),
                ("เทมเพลตตารางคิว %d รายการ %s" % (len(ops), r.get("reason") or ""))[:255],
                payload["by"], "pending", json.dumps({"slot_tmpl": payload}, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))),
                   tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "count": len(ops)})

    def do_slot_master(self, db, b, pg):
        """คำขอแก้ตารางพื้นฐาน (ช่วงเวลา / ประเภทคิว) — เข้าคิวให้ gateway เขียน"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        raw = r.get("ops") if isinstance(r.get("ops"), list) else []
        ops = []
        for o in raw:
            if not isinstance(o, dict):
                continue
            kind = str(o.get("kind") or "").strip().lower()
            if kind not in ("slot", "limit"):
                continue
            op = "del" if str(o.get("op") or "").strip().lower() == "del" else "set"
            item = {"kind": kind, "op": op}
            rid = str(o.get("id") or "").strip()
            if rid.isdigit():
                item["id"] = int(rid)
            if op == "del":
                if "id" not in item:
                    return self._send(400, {"ok": False, "message": "รายการลบต้องมี id"})
                ops.append(item)
                continue
            item["name"] = str(o.get("name") or "").strip()[:200]
            if not item["name"]:
                return self._send(400, {"ok": False, "message": "ทุกรายการต้องมีชื่อ"})
            if kind == "slot":
                item["start_time"] = str(o.get("start_time") or "").strip()[:8]
                item["finish_time"] = str(o.get("finish_time") or "").strip()[:8]
            else:
                item["check_access"] = "Y" if str(o.get("check_access") or "").upper() == "Y" else "N"
                q = o.get("qty") if isinstance(o.get("qty"), dict) else {}
                qty = {}
                for k, v in q.items():
                    k, v = str(k).strip(), str(v).strip()
                    if k.isdigit() and v.isdigit():
                        qty[k] = int(v)
                if not qty:
                    return self._send(400, {"ok": False,
                        "message": "ชุดโควตาต้องมีจำนวนอย่างน้อย 1 ประเภท"})
                item["qty"] = qty
                # สิทธิการใช้ชุดโควตา — ห้องตรวจ (depcode, 3 ตัว) และ user (loginname)
                # ใส่คีย์ต่อเมื่อหน้าจอส่งมาจริง เพื่อให้ gateway แยกออกว่า "ไม่ได้แก้" กับ "แก้เป็นว่าง"
                for fld, src, mx in (("depcodes", "depcodes", 3), ("users", "users", 50)):
                    if not isinstance(o.get(src), list):
                        continue
                    seen, out = set(), []
                    for v in o.get(src):
                        v = str(v or "").strip()[:mx]
                        if v and v not in seen:
                            seen.add(v)
                            out.append(v)
                    if len(out) > 200:
                        return self._send(400, {"ok": False,
                            "message": "รายชื่อสิทธิของชุดโควตาเกิน 200 รายการ"})
                    item[fld] = out
            ops.append(item)
        if not ops:
            return self._send(400, {"ok": False, "message": "ไม่มีรายการที่จะแก้"})
        if len(ops) > 100:
            return self._send(400, {"ok": False, "message": "แก้ได้ครั้งละไม่เกิน 100 รายการ"})
        deny = self.slot_denied(db, r)
        if deny:
            return self._send(403, {"ok": False, "message": deny})
        payload = {"ops": ops, "by": str(r.get("by") or "")[:64], "reason": str(r.get("reason") or "")[:255]}
        cols = ["requested_at", "slot_count", "reason", "requested_by", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), len(ops),
                ("ตารางพื้นฐานคิว %d รายการ %s" % (len(ops), r.get("reason") or ""))[:255],
                payload["by"], "pending", json.dumps({"slot_master": payload}, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))),
                   tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "count": len(ops)})

    def do_slot_renumber(self, db, b, pg):
        """คำขอเขียนเลขคิวที่สร้างไว้แล้วให้ใช้อักษรนำตัวใหม่ — เข้าคิวให้ gateway ทำ"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        cl = re.sub(r"[^A-Za-z0-9_-]", "", str(r.get("clinic") or ""))[:3]
        dc = re.sub(r"[^A-Za-z0-9_-]", "", str(r.get("doctor_code") or ""))[:15]
        px = re.sub(r"[^0-9A-Za-z]", "", str(r.get("queue_prefix") or "")).upper()[:5]
        d1 = str(r.get("date_from") or "").strip()[:10]
        if not dc:
            return self._send(400, {"ok": False, "message": "ต้องระบุรหัสแพทย์"})
        if d1 and not re.match(r"^\d{4}-\d{2}-\d{2}$", d1):
            return self._send(400, {"ok": False, "message": "วันที่เริ่มต้องเป็น YYYY-MM-DD"})
        deny = self.slot_denied(db, r)
        if deny:
            return self._send(403, {"ok": False, "message": deny})
        payload = {"clinic": cl, "doctor_code": dc, "queue_prefix": px, "date_from": d1,
                   "include_used": bool(r.get("include_used")),
                   "by": str(r.get("by") or "")[:64]}
        cols = ["requested_at", "doctor_code", "clinic_code", "slot_count", "reason",
                "requested_by", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), dc[:30], cl or None, 0,
                ("แก้เลขคิวที่สร้างแล้วเป็น %s (%s/%s) %s"
                 % (px or "อัตโนมัติ", cl or "-", dc, r.get("reason") or ""))[:255],
                payload["by"], "pending", json.dumps({"slot_renumber": payload}, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))),
                   tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "doctor_code": dc, "queue_prefix": px})

    def do_slot_prefix(self, db, b, pg):
        """คำขอแก้ตารางอักษรนำเลขคิว (opd_qs_clinic_doctor_prefix) — เข้าคิวให้ gateway เขียน"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        raw = r.get("ops") if isinstance(r.get("ops"), list) else []
        ops = []
        for o in raw:
            if not isinstance(o, dict):
                continue
            cl = re.sub(r"[^A-Za-z0-9_\-]", "", str(o.get("clinic") or ""))[:3]
            dc = re.sub(r"[^A-Za-z0-9_\-]", "", str(o.get("doctor_code") or ""))[:15]
            px = re.sub(r"[^0-9A-Za-z]", "", str(o.get("queue_prefix") or "")).upper()[:5]
            op = "del" if str(o.get("op") or "").strip().lower() == "del" else "set"
            if not cl or not dc or (op == "set" and not px):
                continue
            ops.append({"op": op, "clinic": cl, "doctor_code": dc, "queue_prefix": px})
        if not ops:
            return self._send(400, {"ok": False, "message": "ไม่มีรายการที่จะแก้ (ต้องมีคลินิก แพทย์ และอักษรนำเลขคิว)"})
        if len(ops) > 200:
            return self._send(400, {"ok": False, "message": "แก้ได้ครั้งละไม่เกิน 200 รายการ"})
        deny = self.slot_denied(db, r)
        if deny:
            return self._send(403, {"ok": False, "message": deny})
        payload = {"ops": ops, "by": str(r.get("by") or "")[:64], "reason": str(r.get("reason") or "")[:255]}
        cols = ["requested_at", "slot_count", "reason", "requested_by", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), len(ops),
                ("อักษรนำเลขคิว %d รายการ %s" % (len(ops), r.get("reason") or ""))[:255],
                payload["by"], "pending", json.dumps({"slot_prefix": payload}, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))),
                   tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "count": len(ops)})

    def do_slot_create(self, db, b, pg):
        """คำขอ "เปิดตารางคิวใหม่เป็นช่วง" จากหน้า slot-admin — เก็บลงคิวเดียวกับคำขอเพิ่มคิว

           แยกชนิดกันที่ json_row (slot_create vs slot_add) แล้ว gateway เลือกทางเอง
           ที่นี่ไม่แตะ HIS เลย แค่ตรวจว่าคำขอครบและคนขอมีสิทธิ์"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        d1 = str(r.get("date_from") or "").strip()[:10]
        d2 = str(r.get("date_to") or "").strip()[:10]
        doc = str(r.get("doctor_code") or "").strip()
        lines = r.get("lines") if isinstance(r.get("lines"), list) else []
        dt = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if not dt.match(d1) or not dt.match(d2) or not doc:
            return self._send(400, {"ok": False, "message": "ต้องมีช่วงวันที่ (YYYY-MM-DD) และรหัสแพทย์/คลินิก"})
        if d2 < d1:
            return self._send(400, {"ok": False, "message": "วันสิ้นสุดอยู่ก่อนวันเริ่ม"})
        if not lines:
            return self._send(400, {"ok": False, "message": "ยังไม่ได้เลือกช่วงเวลา"})
        deny = self.slot_denied(db, r, scoped=True)
        if deny:
            return self._send(403, {"ok": False, "message": deny})
        # นับล่วงหน้าว่าจะได้กี่คิว เก็บไว้ในคอลัมน์ slot_count ให้ตารางคำขออ่านง่าย
        per_day = 0
        for l in lines:
            for v in (l.get("limits") or {}).values():
                try:
                    per_day += max(0, int(v))
                except (TypeError, ValueError):
                    pass
        dows = set()
        for x in (r.get("dows") or []):
            try:
                dows.add(int(x))
            except (TypeError, ValueError):
                pass
        skip = []                           # วันหยุดที่หน้าเว็บให้ข้าม (ส่งมาเป็นรายวัน)
        for x in (r.get("skip_dates") or []):
            x = str(x or "").strip()[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", x) and x not in skip:
                skip.append(x)
        if len(skip) > 500:
            return self._send(400, {"ok": False, "message": "รายการวันหยุดที่ข้ามเกิน 500 วัน"})
        r["skip_dates"] = skip
        weeks = set()                       # สัปดาห์ที่เท่าไหร่ของเดือน (ว่าง = ทุกสัปดาห์)
        for x in (r.get("weeks") or []):
            try:
                weeks.add(int(x))
            except (TypeError, ValueError):
                pass
        a = datetime.date(int(d1[0:4]), int(d1[5:7]), int(d1[8:10]))
        b2 = datetime.date(int(d2[0:4]), int(d2[5:7]), int(d2[8:10]))
        # กันพิมพ์ปีผิด (เช่น 2599) เท่านั้น — ระยะบนปฏิทินไม่ใช่ตัวบอกปริมาณงาน
        # คลินิกสัปดาห์ละวันยาว 3 ปี = 156 วัน ซึ่งน้อยกว่าคลินิกทุกวันแค่ 8 เดือนเสียอีก
        # หน้าเว็บเปลี่ยนเช็ค 3 ปีเป็น "คำเตือน" แล้ว ตรงนี้จึงเหลือไว้กันพิมพ์ปีเพี้ยนหนัก ๆ (เช่น 2599)
        if (b2 - a).days > 3653:
            return self._send(400, {"ok": False,
                "message": "ช่วงวันยาวเกิน 10 ปี — ตรวจปี พ.ศ. ที่กรอกอีกครั้ง"})
        days = 0
        d = a
        while d <= b2:
            if ((not dows or (d.isoweekday() % 7) in dows)
                    and (not weeks or ((d.day - 1) // 7 + 1) in weeks)
                    and d.isoformat() not in skip):
                days += 1
            d += datetime.timedelta(days=1)
        if not days:
            return self._send(400, {"ok": False,
                "message": "ช่วงวันที่เลือกไม่ตรงกับวันในสัปดาห์/สัปดาห์ในเดือนที่ติ๊กไว้"})
        # เพดานจริงอยู่ที่ "จำนวนวันที่จะเปิดตรวจ" — ตัวนี้ต่างหากที่เป็นภาระของ gateway
        if days > 500:
            return self._send(400, {"ok": False,
                "message": "คำขอนี้จะเปิดตรวจ %d วัน (เกิน 500) — แบ่งเป็นหลายคำขอ" % days})
        total = days * per_day
        if total < 1:
            return self._send(400, {"ok": False, "message": "ยังไม่ได้กำหนดจำนวนคิว"})
        # เพดาน 3000 ต้องนับ "ใบที่จะสร้างจริง" ไม่ใช่ทุกวันที่ผ่านตัวกรองวัน
        # ตัวนี้ต่ออยู่กับฐานกลาง มองไม่เห็น HOSxP จึงไม่รู้ว่าวันไหนมีตารางอยู่แล้ว
        # หน้าเว็บกดตรวจสอบกับ HOSxP มาแล้วก่อนส่งเสมอ -> ใช้ตัวเลขที่คนเห็นบนจอ (ไม่เกินยอดเต็ม)
        # ส่วนด่านจริงอยู่ที่ gateway ซึ่งนับเองอีกรอบตอนจะลงมือสร้าง
        plan = total
        try:
            exp = int(r.get("expect_slots") or 0)
        except (TypeError, ValueError):
            exp = 0
        if 0 < exp <= total:
            plan = exp
        if plan > 5000:
            return self._send(400, {"ok": False,
                "message": "คำขอนี้จะสร้างคิว %d ใบ (เกิน 5000) — ย่อยช่วงวันให้สั้นลง" % plan})
        cols = ["requested_at", "schedule_date", "doctor_code", "clinic_code", "queue_slot_id",
                "slot_count", "reason", "requested_by", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), d1, doc[:30],
                str(r.get("clinic") or "")[:10] or None,
                int(lines[0].get("opd_queue_slot_id") or 0) or None,
                plan, ("เปิดตาราง %s..%s %d วัน · %s" % (d1, d2, days, r.get("reason") or ""))[:255],
                str(r.get("by") or "")[:64], "pending",
                json.dumps({"slot_create": r}, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))),
                   tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "days": days, "slots": plan,
                                "date_from": d1, "date_to": d2, "doctor_code": doc})

    def do_slot_add(self, db, b, pg):
        """รับคำขอเพิ่มคิวจากหน้าทำนัด — แค่บันทึกไว้ ยังไม่แตะ HIS
           ตัว gateway จะมาอ่านแถว status='pending' ไปสร้างคิวจริงอีกที"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        date = str(r.get("schedule_date") or "").strip()[:10]
        doc = str(r.get("doctor_code") or "").strip()
        try:
            n = int(r.get("count") or 0)
        except (TypeError, ValueError):
            n = 0
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date) or not doc:
            return self._send(400, {"ok": False, "message": "ต้องมี schedule_date (YYYY-MM-DD) และ doctor_code"})
        if n < 1 or n > 50:
            return self._send(400, {"ok": False, "message": "จำนวนคิวต้องอยู่ระหว่าง 1-50"})
        deny = self.slot_denied(db, r, scoped=True)
        if deny:
            return self._send(403, {"ok": False, "message": deny})
        cols = ["requested_at", "schedule_date", "doctor_code", "clinic_code", "queue_schedule_id",
                "queue_slot_id", "slot_count", "reason", "requested_by", "hn", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), date, doc[:30],
                str(r.get("clinic") or "")[:10] or None,
                int(r.get("opd_queue_schedule_id") or 0) or None,
                int(r.get("opd_queue_slot_id") or 0) or None,
                n, str(r.get("reason") or "")[:255], str(r.get("by") or "")[:64],
                str(r.get("hn") or "")[:20] or None, "pending", json.dumps(r, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))), tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "count": n, "schedule_date": date, "doctor_code": doc})

    def do_slot_remove(self, db, b, pg):
        """รับคำขอ "ลดคิว" จากหน้า slot-admin — แค่บันทึกไว้ ยังไม่แตะ HIS

           ส่งมาเป็นรายการ opd_qs_slot_id ของคิวที่ยังว่าง (ไม่ใช่ "ลบ N คิวของวันนั้น")
           gateway เช็คซ้ำตอนลงมือว่ายังว่างอยู่ไหม คิวที่ถูกจองไประหว่างรอจะถูกข้าม ไม่ถูกลบ
           slot_count เก็บเป็นค่าติดลบ — เปิดตารางคำขอดูแล้วรู้ทันทีว่าเป็นคำขอลด"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        r = b.get("slot") if isinstance(b.get("slot"), dict) else {}
        date = str(r.get("schedule_date") or "").strip()[:10]
        doc = str(r.get("doctor_code") or "").strip()
        ids = []
        for v in (r.get("slot_ids") if isinstance(r.get("slot_ids"), list) else []):
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if i > 0 and i not in ids:
                ids.append(i)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date) or not doc:
            return self._send(400, {"ok": False, "message": "ต้องมี schedule_date (YYYY-MM-DD) และ doctor_code"})
        if not ids or len(ids) > 200:
            return self._send(400, {"ok": False, "message": "จำนวนคิวที่ลดต้องอยู่ระหว่าง 1-200"})
        deny = self.slot_denied(db, r, scoped=True)
        if deny:
            return self._send(403, {"ok": False, "message": deny})

        def _int(v):
            try:
                return int(v or 0) or None
            except (TypeError, ValueError):
                return None
        nums = r.get("numbers") if isinstance(r.get("numbers"), list) else []
        payload = {"schedule_date": date, "doctor_code": doc[:30], "clinic": str(r.get("clinic") or "")[:10],
                   "opd_queue_schedule_id": _int(r.get("opd_queue_schedule_id")),
                   "opd_queue_slot_id": _int(r.get("opd_queue_slot_id")),
                   "slot_ids": ids, "numbers": [str(x)[:20] for x in nums][:200],
                   "reason": str(r.get("reason") or "")[:255], "by": str(r.get("by") or "")[:64]}
        cols = ["requested_at", "schedule_date", "doctor_code", "clinic_code", "queue_schedule_id",
                "queue_slot_id", "slot_count", "reason", "requested_by", "status", "json_row"]
        vals = [datetime.datetime.now().replace(microsecond=0), date, doc[:30], payload["clinic"] or None,
                payload["opd_queue_schedule_id"], payload["opd_queue_slot_id"], -len(ids),
                ("ลดคิว " + payload["reason"]).strip()[:255], payload["by"], "pending",
                json.dumps({"slot_remove": payload}, ensure_ascii=False)]
        db.execute("INSERT INTO %s (%s) VALUES (%s)" % (T, ",".join(cols), ",".join(["%s"] * len(cols))), tuple(vals))
        db.commit()
        return self._send(200, {"ok": True, "table": T, "count": len(ids), "schedule_date": date, "doctor_code": doc})

    def do_slot_list(self, db, b, pg):
        """คำขอล่าสุด — หน้าเว็บเอาไปโชว์สถานะว่า gateway ทำให้แล้วหรือยัง"""
        T = self.slot_table(b)
        self.slot_ensure(db, T, pg)
        try:
            lim = max(1, min(int(b.get("limit") or 20), 200))
        except (TypeError, ValueError):
            lim = 20
        rows = db.query("SELECT id, requested_at, schedule_date, doctor_code, slot_count, reason,"
                        " requested_by, status, result_note, done_at FROM %s ORDER BY id DESC LIMIT %d" % (T, lim))
        for x in rows:
            for k in ("requested_at", "schedule_date", "done_at"):
                if x.get(k) is not None:
                    x[k] = str(x[k])
        return self._send(200, {"ok": True, "table": T, "rows": rows})

    # ---------- action=verify : กุญแจของแต่ละแถวในคิว ไว้เทียบกับ oapp ฝั่ง HOSxP ----------
    def do_verify(self, db, T, b):
        """คืน "กุญแจ" ของแถวในคิว (hn / วันนัด / แพทย์ / ห้องตรวจ / ref_id) ให้หน้า insight
           เอาไปเทียบกับ oapp เอง — เซิร์ฟเวอร์ตัวนี้ต่อ HIS โดยตรงไม่ได้ (สิทธิ์อยู่ที่ BMS session
           ในเบราว์เซอร์) จึงส่งแค่ฝั่งคิวไปให้ แล้วให้หน้าเว็บยิง /api/sql ไปเทียบเอง

           ref_id = oapp_id ที่ gateway เขียนกลับมาหลัง insert สำเร็จ (ของเก่าจะว่าง -> เทียบด้วยกุญแจแทน)
           ตัด json_row ดิบทิ้ง เหลือเฉพาะฟิลด์ที่ใช้เทียบ payload จะได้ไม่บวม"""
        def _vd(s):
            s = str(s or "")
            return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else ""
        to  = _vd(b.get("to"))  or datetime.date.today().strftime("%Y-%m-%d")
        frm = _vd(b.get("from")) or (datetime.datetime.strptime(to, "%Y-%m-%d").date()
                                     - datetime.timedelta(days=29)).strftime("%Y-%m-%d")
        CAP = 5000
        rows = db.query(
            "SELECT id, hn, ref_id, datetime, status, json_row FROM %s "
            "WHERE datetime >= %%s AND datetime <= %%s ORDER BY id LIMIT %d" % (T, CAP),
            (frm + " 00:00:00", to + " 23:59:59"))
        out = []
        for r in rows:
            try:
                j = json.loads(r.get("json_row") or "{}")
            except Exception:
                j = {}
            o = j.get("oapp") if isinstance(j.get("oapp"), dict) else {}
            out.append({
                "id": r.get("id"),
                "hn": str(r.get("hn") or o.get("hn") or "").strip(),
                "ref_id": str(r.get("ref_id") or "").strip(),
                "status": str(r.get("status") or "").strip(),
                "at": str(r.get("datetime") or ""),
                "nextdate": str(o.get("nextdate") or "")[:10],
                "doctor": str(o.get("doctor") or "").strip(),
                "depcode": str(o.get("depcode") or "").strip(),
                "lab_only": bool(j.get("lab_only")),
                "split_role": str(j.get("split_role") or ""),
            })
        return self._send(200, {"ok": True, "from": frm, "to": to, "capped": len(rows) >= CAP,
                                "rows": out})

    # ---------- action=insight : สรุปเชิงวิเคราะห์ให้แดชบอร์ด Insight (แยกจากหน้าเว็บหลัก) ----------
    def do_insight(self, db, T, b):
        """อ่านแถวในช่วงวันของคอลัมน์ datetime (วัน "ทำนัด/เข้าคิว" ไม่ใช่วันนัด) แล้วแตก json_row
           รวมยอดฝั่งเซิร์ฟเวอร์ คืนเฉพาะตัวเลขสรุป (payload เล็ก — ไม่ส่ง json_row ดิบกลับ)
           ช่วงวันไม่ส่งมา = ย้อนหลัง 30 วัน · เพดานสแกน 20000 แถว"""
        def _vd(s):
            s = str(s or "")
            return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else ""
        to  = _vd(b.get("to"))  or datetime.date.today().strftime("%Y-%m-%d")
        frm = _vd(b.get("from")) or (datetime.datetime.strptime(to, "%Y-%m-%d").date()
                                     - datetime.timedelta(days=29)).strftime("%Y-%m-%d")
        CAP = 20000
        rows = db.query(
            "SELECT hn, doctor_code, doctor_note, datetime, status, json_row FROM %s "
            "WHERE datetime >= %%s AND datetime <= %%s ORDER BY id DESC LIMIT %d" % (T, CAP),
            (frm + " 00:00:00", to + " 23:59:59"))
        today = datetime.date.today().strftime("%Y-%m-%d")

        def g(d, *path):                          # ดึงค่าซ้อนใน dict อย่างปลอดภัย
            for k in path:
                if not isinstance(d, dict):
                    return None
                d = d.get(k)
            return d

        total = 0
        status_c, hn_set, doc_set = {}, set(), set()
        daily, by_hour = {}, [0] * 24
        doctors, clinics, services, departments = {}, {}, {}, {}
        LEAD_KEYS = ["ผ่านมาแล้ว", "0-7", "8-14", "15-30", "31-60", "61-90", "90+"]
        lead = {k: 0 for k in LEAD_KEYS}
        future, weekday_next = {}, [0] * 7
        split = {"single": 0, "draw": 0, "visit": 0}
        source_c = {}           # ที่มา (source_type) — อ่านจาก json_row.source ของแต่ละแถว
        recorders = {}          # คนบันทึก — key = ชื่อล็อกอิน (มีทุกหน้า) กันคนเดียวกันแตกเป็นสองแถว
        errors = []

        for r in rows:
            total += 1
            status = str(r.get("status") or "").strip().lower()
            status_c[status] = status_c.get(status, 0) + 1
            hn = str(r.get("hn") or "").strip()
            if hn:
                hn_set.add(hn)
            dt = r.get("datetime")
            dts = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, "strftime") else str(dt or "")
            day = dts[:10]
            try:
                hour = int(dts[11:13])
            except Exception:
                hour = -1
            if 0 <= hour < 24:
                by_hour[hour] += 1
            dd = daily.setdefault(day, {"total": 0, "st": {}})
            dd["total"] += 1
            dd["st"][status] = dd["st"].get(status, 0) + 1

            try:
                j = json.loads(r.get("json_row") or "{}")
            except Exception:
                j = {}
            if not isinstance(j, dict):
                j = {}
            ap, oa, rs = (j.get("appoint") or {}), (j.get("oapp") or {}), (j.get("oapp_resolve") or {})

            doc_name = g(ap, "doctor") or g(rs, "doctor", "name") or ""
            doc_code = str(r.get("doctor_code") or g(rs, "doctor", "code") or "")
            dk = doc_code or doc_name or "—"
            dn = doctors.setdefault(dk, {"name": doc_name or doc_code or "—", "code": doc_code, "count": 0})
            dn["count"] += 1
            if dk != "—":
                doc_set.add(dk)
            clinic = g(rs, "clinic", "name") or g(ap, "clinic") or ""
            if clinic:
                clinics[clinic] = clinics.get(clinic, 0) + 1
            service = g(rs, "depcode", "name") or g(ap, "room") or ""
            if service:
                services[service] = services.get(service, 0) + 1
            dept = g(rs, "spclty", "name") or g(ap, "department") or ""
            if dept:
                departments[dept] = departments.get(dept, 0) + 1

            nextdate = str(oa.get("nextdate") or ap.get("date") or "")[:10]
            vstdate = str(oa.get("vstdate") or "")[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", nextdate):
                wd = datetime.datetime.strptime(nextdate, "%Y-%m-%d").weekday()   # Mon=0
                weekday_next[(wd + 1) % 7] += 1                                   # -> Sun=0
                if nextdate >= today:
                    future[nextdate] = future.get(nextdate, 0) + 1
                if re.match(r"^\d{4}-\d{2}-\d{2}$", vstdate):
                    dlt = (datetime.datetime.strptime(nextdate, "%Y-%m-%d")
                           - datetime.datetime.strptime(vstdate, "%Y-%m-%d")).days
                    lead[("ผ่านมาแล้ว" if dlt < 0 else "0-7" if dlt <= 7 else "8-14" if dlt <= 14
                          else "15-30" if dlt <= 30 else "31-60" if dlt <= 60
                          else "61-90" if dlt <= 90 else "90+")] += 1

            role = j.get("split_role") or ""
            split["draw" if role == "draw" else "visit" if role == "visit" else "single"] += 1

            # ที่มาของแถว (source_type) — json_row.source: appointment-autofill / appointment-register
            src = str(j.get("source") or r.get("source_type") or "").strip() or "(ไม่ระบุ)"
            sc = source_c.setdefault(src, {"count": 0, "st": {}})
            sc["count"] += 1
            sc["st"][status] = sc["st"].get(status, 0) + 1

            # คนบันทึก: หน้า autofill = oapp.app_user (ล็อกอิน) + appoint.recorder (ชื่ออ่านได้)
            #           หน้าทะเบียนนัด = sent_by.user · lab อย่างเดียว = lab.order_staff
            rec_key = str(g(oa, "app_user") or g(j, "sent_by", "user") or g(ap, "recorder")
                          or g(j, "doctor_note", "staff") or g(j, "lab", "order_staff") or "").strip() or "(ไม่ระบุ)"
            rec_name = str(g(ap, "recorder") or g(j, "doctor_note", "staff") or "").strip()
            rr = recorders.setdefault(rec_key, {"key": rec_key, "name": "", "count": 0,
                                                "st": {}, "hn": set(), "days": {}})
            if rec_name and not rr["name"]:
                rr["name"] = rec_name
            rr["count"] += 1
            rr["st"][status] = rr["st"].get(status, 0) + 1
            if hn:
                rr["hn"].add(hn)
            rr["days"][day] = rr["days"].get(day, 0) + 1
            if status == "error" and len(errors) < 50:
                errors.append({"hn": hn, "doctor": doc_name or doc_code, "clinic": clinic,
                               "datetime": dts, "note": str(r.get("doctor_note") or "")[:120]})

        def topn(d, n=12):
            return [{"name": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]

        # สรุปฝั่งคนบันทึก: อันดับรวม + อนุกรมรายวันของ 4 อันดับแรก (ที่เหลือรวมเป็น "อื่น ๆ")
        rec_sorted = sorted(recorders.values(), key=lambda x: -x["count"])
        rec_days = sorted(daily.keys())
        rec_series = [{"name": r["name"] or r["key"],
                       "counts": [r["days"].get(d, 0) for d in rec_days]} for r in rec_sorted[:4]]
        if len(rec_sorted) > 4:
            other = [0] * len(rec_days)
            for r in rec_sorted[4:]:
                for i, d in enumerate(rec_days):
                    other[i] += r["days"].get(d, 0)
            rec_series.append({"name": "อื่น ๆ (%d คน)" % (len(rec_sorted) - 4),
                               "counts": other, "other": True})

        err_c = status_c.get("error", 0)
        done_c, pend_c, lab_c = status_c.get("done", 0), status_c.get("pending", 0), status_c.get("lab_only", 0)
        return self._send(200, {
            "ok": True,
            "range": {"from": frm, "to": to},
            "scanned": total,
            "capped": total >= CAP,
            "summary": {
                "total": total,
                "status": {"done": done_c, "pending": pend_c, "error": err_c,
                           "lab_only": lab_c, "other": total - done_c - pend_c - err_c - lab_c},
                "unique_hn": len(hn_set),
                "unique_doctor": len(doc_set),
                "error_rate": (err_c / total) if total else 0,
            },
            "status_all": status_c,
            "daily": [{"date": k, "total": v["total"], "st": v["st"]} for k, v in sorted(daily.items())],
            "by_hour": by_hour,
            "doctors": sorted(doctors.values(), key=lambda x: -x["count"])[:12],
            "clinics": topn(clinics),
            "services": topn(services),
            "departments": topn(departments),
            "recorder_count": len([k for k in recorders if k != "(ไม่ระบุ)"]),
            "recorders": [{"name": (r["name"] or r["key"]), "key": r["key"], "count": r["count"],
                           "st": r["st"], "unique_hn": len(r["hn"])} for r in rec_sorted[:15]],
            "recorder_daily": {"dates": rec_days, "series": rec_series},
            "lead_time": [{"label": k, "count": lead[k]} for k in LEAD_KEYS],
            "future_load": [{"date": k, "count": future[k]} for k in sorted(future.keys())][:70],
            "weekday_next": weekday_next,
            "split": split,
            "source_types": [{"name": k, "count": v["count"], "st": v["st"]}
                             for k, v in sorted(source_c.items(), key=lambda kv: -kv[1]["count"])],
            "errors": errors,
        })

    # ---------- /save_appointment.php ----------
    def do_save(self, b):
        if gateway is None:
            return self._send(500, {"ok": False, "message": "ไม่มีไดรเวอร์ฐานข้อมูล (ต้องมี pymysql/psycopg2)"})
        row = b.get("row") or {}
        cfg, src = central_cfg(b.get("mysql"))
        if not cfg["name"]:
            return self._send(400, {"ok": False, "message": "ยังไม่ได้ตั้งชื่อฐานข้อมูล — ตั้ง BMS_DB_NAME ใน config.bat"})
        T = cfg["table"]
        try:
            db = gateway.DB(cfg, "BMS_DB_")
        except Exception as e:
            return self._send(500, {"ok": False, "message": str(e)})
        try:
            existed = True
            try:
                db.scalar("SELECT 1 FROM %s LIMIT 1" % T)
            except Exception:
                existed = False
                db.rollback()
            if cfg["driver"] == "pgsql":
                db.execute("""CREATE TABLE IF NOT EXISTS %s (
                    id BIGSERIAL PRIMARY KEY, hn VARCHAR(20), vn VARCHAR(20),
                    doctor_code VARCHAR(15), doctor_note TEXT, ref_id VARCHAR(20),
                    datetime TIMESTAMP, status VARCHAR(20), source_type VARCHAR(40), json_row TEXT)""" % T)
            else:
                db.execute("""CREATE TABLE IF NOT EXISTS %s (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY, hn VARCHAR(20), vn VARCHAR(20),
                    doctor_code VARCHAR(15), doctor_note TEXT, ref_id VARCHAR(20),
                    datetime DATETIME, status VARCHAR(20), source_type VARCHAR(40), json_row LONGTEXT,
                    INDEX(hn), INDEX(vn), INDEX(ref_id), INDEX(source_type)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""" % T)
            db.commit()
            add_missing_columns(db, T)   # ตารางที่สร้างไว้ก่อนหน้า -> เติมคอลัมน์ใหม่ให้เอง

            if b.get("action") == "create_table":
                db.close()
                return self._send(200, {"ok": True, "created": not existed, "table": T, "db": cfg["name"]})
            if b.get("action") == "list":
                out = self.do_list(db, T, b)
                db.close()
                return out
            if b.get("action") == "insight":
                out = self.do_insight(db, T, b)
                db.close()
                return out
            if b.get("action") in ("slot_add", "slot_list", "slot_create", "slot_flag",
                                   "slot_prefix", "slot_renumber", "slot_master", "slot_tmpl",
                                   "slot_remove"):
                pg = str(cfg.get("driver") or "").lower() == "pgsql"
                act = b.get("action")
                out = (self.do_slot_add(db, b, pg) if act == "slot_add"
                       else self.do_slot_create(db, b, pg) if act == "slot_create"
                       else self.do_slot_flag(db, b, pg) if act == "slot_flag"
                       else self.do_slot_prefix(db, b, pg) if act == "slot_prefix"
                       else self.do_slot_renumber(db, b, pg) if act == "slot_renumber"
                       else self.do_slot_master(db, b, pg) if act == "slot_master"
                       else self.do_slot_tmpl(db, b, pg) if act == "slot_tmpl"
                       else self.do_slot_remove(db, b, pg) if act == "slot_remove"
                       else self.do_slot_list(db, b, pg))
                db.close()
                return out
            if b.get("action") == "verify":
                out = self.do_verify(db, T, b)
                db.close()
                return out
            if b.get("action") == "requeue":
                out = self.do_requeue(db, T, b)
                db.close()
                return out
            if b.get("action") in ("pref_get", "pref_set", "tpl_get", "tpl_save"):
                out = self.do_pref(db, cfg, b)
                db.close()
                return out
            if str(row.get("status") or "") == "ping":
                db.close()
                return self._send(200, {"ok": True, "ping": True, "table": T, "db": cfg["name"],
                                        "table_created": not existed})

            # กันแถวว่าง: คำขอที่ไม่มี 'row' (เช่น action ที่ไม่รู้จัก หลุดมาถึง INSERT) จะกลายเป็นแถวเปล่า
            #            status ดีฟอลต์ 'pending' -> gateway หยิบไปแล้ว error ทิ้งไว้ จึงบล็อกไว้ตรงนี้
            hn_ins = str(row.get("hn") or "").strip()
            jr_ins = str(row.get("json_row") or "").strip()
            if not hn_ins or not jr_ins or jr_ins == "null":
                db.close()
                return self._send(400, {"ok": False, "message": "บล็อก: ไม่มี hn/json_row — ไม่บันทึกแถวว่างเข้าคิว"
                                        + ((" (action=%s ไม่รองรับ)" % b.get("action")) if b.get("action") else "")})

            db.execute("INSERT INTO %s (hn,vn,doctor_code,doctor_note,ref_id,datetime,status,source_type,json_row) VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % T,
                       (row.get("hn") or "", row.get("vn") or "", row.get("doctor_code") or "",
                        row.get("doctor_note") or "", str(row.get("ref_id") or "")[:20],
                        row.get("datetime") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        row.get("status") or "pending", str(row.get("source_type") or "")[:40], row.get("json_row") or ""))
            db.commit()
            try:
                new_id = db.scalar("SELECT MAX(id) FROM %s" % T)
            except Exception:
                new_id = None
            db.close()
            return self._send(200, {"ok": True, "id": new_id})
        except Exception as e:
            db.rollback(); db.close()
            return self._send(500, {"ok": False, "message": str(e)})

    # ---------- ค่าตั้งผู้ใช้ที่ฐานกลาง (จำตาม login) — ตาราง appt_user_pref ----------
    #   pref_get/pref_set {login, key[, data]}  · tpl_get/tpl_save = alias key=form_tpl (data เป็น list)
    #   ใช้เก็บ: form_tpl (แม่แบบฟอร์ม) · remember (ปุ่มจำค่าตัวเลือก) · draw_point (จุดเจาะเลือด)
    def do_pref(self, db, cfg, b):
        action = b.get("action")
        is_tpl = action in ("tpl_get", "tpl_save")
        is_get = action in ("pref_get", "tpl_get")
        login = str(b.get("login") or "").strip()[:64]
        key = "form_tpl" if is_tpl else str(b.get("key") or "").strip()[:40]
        if not login or not key:
            return self._send(400, {"ok": False, "message": "ไม่มี login/key (จำค่าตามผู้ใช้ไม่ได้)"})
        if cfg["driver"] == "pgsql":
            db.execute("""CREATE TABLE IF NOT EXISTS appt_user_pref (
                login VARCHAR(64) NOT NULL, pref_key VARCHAR(40) NOT NULL,
                data TEXT, updated_at TIMESTAMP, PRIMARY KEY (login, pref_key))""")
        else:
            db.execute("""CREATE TABLE IF NOT EXISTS appt_user_pref (
                login VARCHAR(64) NOT NULL, pref_key VARCHAR(40) NOT NULL,
                data LONGTEXT, updated_at DATETIME, PRIMARY KEY (login, pref_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        db.commit()
        if is_get:
            rows = db.query("SELECT data FROM appt_user_pref WHERE login=%s AND pref_key=%s", (login, key))
            data = [] if is_tpl else None
            if rows and rows[0].get("data"):
                try:
                    tmp = json.loads(rows[0]["data"])
                    if tmp is not None:
                        data = tmp
                except Exception:
                    pass
            if is_tpl and not isinstance(data, list):
                data = []
            return self._send(200, {"ok": True, "login": login, "key": key, "data": data})
        # set
        data = b.get("data")
        if is_tpl and not isinstance(data, list):
            data = []
        js = json.dumps(list(data) if is_tpl else data, ensure_ascii=False)
        if len(js) > 200000:
            return self._send(400, {"ok": False, "message": "ข้อมูลค่าตั้งใหญ่เกินไป"})
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if cfg["driver"] == "pgsql":
            db.execute("""INSERT INTO appt_user_pref (login, pref_key, data, updated_at) VALUES (%s,%s,%s,%s)
                          ON CONFLICT (login, pref_key) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at""",
                       (login, key, js, now))
        else:
            db.execute("""INSERT INTO appt_user_pref (login, pref_key, data, updated_at) VALUES (%s,%s,%s,%s)
                          ON DUPLICATE KEY UPDATE data=VALUES(data), updated_at=VALUES(updated_at)""",
                       (login, key, js, now))
        db.commit()
        return self._send(200, {"ok": True, "login": login, "key": key})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # ด่านสุดท้าย: กัน traceback ยาว ๆ ตอน connection หลุดระหว่าง finish/flush
        if isinstance(sys.exc_info()[1], DROPPED):
            return
        super().handle_error(request, client_address)


def main():
    os.chdir(HERE)
    try:
        srv = Server(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print("เปิดพอร์ต %d ไม่ได้: %s" % (PORT, e))
        print("มีเซิร์ฟเวอร์ตัวเก่าค้างอยู่หรือเปล่า -> ปิดก่อน หรือสั่ง  python server.py %d" % (PORT + 1))
        sys.exit(1)
    url = "http://localhost:%d/%s" % (PORT, PAGE)
    c, src = central_cfg(None)
    print("=" * 58)
    print("  BMS Appointment Autofill — Python server (ไม่ต้องใช้ PHP)")
    print("=" * 58)
    print("  หน้าทำนัด   : %s" % url)
    print("  ทะเบียนนัด  : http://localhost:%d/%s" % (PORT, REGISTER_PAGE))
    print("  API         : /sql_proxy.php · /save_appointment.php  (พร้อมใช้)")
    print("  ฐานกลาง     : %s" % (("%s %s@%s:%s/%s" % (c["driver"], c["user"], c["host"], c["port"], c["name"]))
                                  if c["name"] else "(ยังไม่ตั้งใน config.bat)"))
    print("  ไดรเวอร์ DB : %s" % ("พร้อม" if gateway is not None else "ไม่พบ (บันทึกฐานกลางไม่ได้)"))
    print("  หยุด        : กด Ctrl+C")
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nหยุดเซิร์ฟเวอร์แล้ว")


if __name__ == "__main__":
    main()
