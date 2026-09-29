#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI ตั้งค่า + Monitor สำหรับ BMS Gateway  (tkinter — ไม่ต้องใช้เบราว์เซอร์)

  แท็บ "ตั้งค่า"  : แก้ค่าฐานกลาง + ฐาน HIS (mysql/pgsql) + แจ้งนัดเข้าหมอพร้อม -> บันทึกลง config.bat
  แท็บ "Monitor" : สถานะ 2 ฐาน, จำนวน pending/done/error, รายการล่าสุด,
                   ปุ่ม Preview / Sync และโหมด auto-sync

  config เก็บที่ config.bat ที่เดียว (ไฟล์เดียวกับที่ start.bat / gateway.bat ใช้)
  รัน: python gateway_ui.py     หรือดับเบิลคลิก gateway_ui.bat
"""

import os
import re
import sys
import queue
import socket
import datetime
import threading
import subprocess
import webbrowser
import contextlib
import tkinter as tk
from tkinter import ttk, messagebox


def _app_dir():
    """รันเป็น .exe -> โฟลเดอร์ของ exe · รันเป็น .py -> โฟลเดอร์สคริปต์"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _app_dir()
sys.path.insert(0, HERE)
import gateway   # reuse: DB, cfg_from, connect, do_preview, do_sync
import bmscrypt  # เข้ารหัส/ถอดรหัส user/pass ใน config.bat
CONFIG_BAT = bmscrypt.CONFIG_BAT      # ใช้ตัวเดียวกับ bmscrypt (frozen-aware แล้ว)

# ---- เว็บเซิร์ฟเวอร์ (รวมหน้าที่เดิมของ start.bat มาไว้ในนี้) ----
WEB_PORT = int(os.getenv("BMS_PORT") or 8137)
WEB_PAGE = "appointment-autofill-v3.html"
def _port_open(port):                 # มีเซิร์ฟเวอร์ฟังพอร์ตนี้อยู่ไหม (localhost)
    s = socket.socket(); s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", int(port))); return True
    except Exception:
        return False
    finally:
        s.close()
def _web_cmd():                       # คำสั่งเปิดเว็บเซิร์ฟเวอร์: server.exe ถ้ามี ไม่งั้น python server.py
    exe = os.path.join(HERE, "server.exe")
    if os.path.exists(exe):
        return [exe, str(WEB_PORT)]
    py = os.path.join(HERE, "server.py")
    if os.path.exists(py):
        return [sys.executable, py, str(WEB_PORT)]
    return None

ANSI = re.compile(r"\x1b\[[0-9;]*m")
FIELDS = [("DRIVER", "ชนิดฐาน"), ("HOST", "Host"), ("PORT", "Port"),
          ("NAME", "Database"), ("USER", "User"), ("PASS", "Password"), ("TABLE", "Table")]
AUTO_KEYS = ["BMS_AUTO_SYNC", "BMS_AUTO_INTERVAL", "BMS_SYNC_BACK_DAYS"]   # จำสถานะ auto-sync + ช่วงย้อนหลัง
# แจ้งนัดเข้าหมอพร้อม (MOPH Alert) — USER/PASS อยู่ใน bmscrypt.SECRET_KEYS จึงถูกเก็บเป็น _ENC
ALERT_KEYS = ["BMS_MOPH_ALERT", "BMS_MOPH_ALERT_ENV", "BMS_MOPH_ALERT_AUTH",
              "BMS_MOPH_ALERT_CLIENT_KEY", "BMS_MOPH_ALERT_SECRET_KEY", "BMS_MOPH_ALERT_HOSPCODE",
              "BMS_MOPH_ALERT_USER", "BMS_MOPH_ALERT_PASS", "BMS_MOPH_ALERT_PHONE"]
# write_config เติมบรรทัดใหม่ให้เฉพาะคีย์ในรายการนี้ — config.bat ชุดเก่า (เช่นใน dist) ยังไม่มีบรรทัด MOPH
ALL_KEYS = (["BMS_DB_" + k for k, _ in FIELDS] + ["BMS_HIS_" + k for k, _ in FIELDS]
            + ["BMS_API_KEY"] + AUTO_KEYS
            + ALERT_KEYS + ["BMS_MOPH_ALERT_USER_ENC", "BMS_MOPH_ALERT_PASS_ENC",
                            "BMS_MOPH_ALERT_CLIENT_KEY_ENC", "BMS_MOPH_ALERT_SECRET_KEY_ENC"])

BG, CARD, INK, INK2, RULE = "#EFF1EE", "#FFFFFF", "#12242E", "#4A5C64", "#D7DDDA"
OK, BAD, WARN, DIM = "#0F6E56", "#A32D2D", "#9A5B0B", "#8496A0"


# ---------------- config.bat read / write ----------------
def read_config():
    vals = {}
    if os.path.exists(CONFIG_BAT):
        with open(CONFIG_BAT, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*set\s+"([A-Za-z0-9_]+)=(.*)"\s*$', line)
                if m:
                    vals[m.group(1)] = m.group(2)
    return vals


def write_config(vals):
    """เขียนกลับ config.bat โดยรักษาคอมเมนต์เดิม (ต้องเป็น ASCII เท่านั้น — ไม่งั้น cmd.exe พัง)"""
    bad = [k for k, v in vals.items() if not str(v).isascii()]
    if bad:
        raise ValueError("ค่าต่อไปนี้มีอักขระที่ไม่ใช่ ASCII ซึ่ง .bat ใช้ไม่ได้: " + ", ".join(bad))
    lines = []
    if os.path.exists(CONFIG_BAT):
        with open(CONFIG_BAT, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    out, seen = [], set()
    for line in lines:
        m = re.match(r'(\s*)set\s+"([A-Za-z0-9_]+)=(.*)"\s*$', line)
        if m and m.group(2) in vals:
            k = m.group(2)
            seen.add(k)
            out.append('%sset "%s=%s"' % (m.group(1), k, vals[k]))
        else:
            out.append(line)
    missing = [k for k in ALL_KEYS if k in vals and k not in seen]
    if missing:
        out.append("")
        for k in missing:
            out.append('set "%s=%s"' % (k, vals[k]))
    with open(CONFIG_BAT, "w", encoding="ascii", errors="strict", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")


def load_display_config():
    """อ่าน config.bat แล้วถอดรหัส <NAME>_ENC ออกมาเป็น plaintext สำหรับแสดงในฟอร์ม"""
    raw = read_config()
    d = dict(raw)
    for k in bmscrypt.SECRET_KEYS:
        enc = raw.get(k + "_ENC", "")
        if enc:
            try:
                d[k] = bmscrypt.decrypt(enc)
            except Exception as e:
                d[k] = ""
                print("ถอดรหัส %s_ENC ไม่ได้: %s" % (k, e))
    return d


def save_display_config(form):
    """เขียนกลับ config.bat โดยเข้ารหัส user/pass เป็น <NAME>_ENC และล้าง plaintext ทิ้ง"""
    raw = read_config()
    for k, v in form.items():
        if k not in bmscrypt.SECRET_KEYS:
            raw[k] = v
    key = bmscrypt.load_key()          # สร้าง config.key อัตโนมัติถ้ายังไม่มี
    for k in bmscrypt.SECRET_KEYS:
        if k in form:
            raw[k + "_ENC"] = bmscrypt.encrypt(form[k], key) if form[k] else ""
            raw[k] = ""                # ไม่เก็บ plaintext ในไฟล์อีก
    write_config(raw)
    return raw


def read_auto():
    """อ่านค่า auto-sync ที่จำไว้ -> (เปิดไหม, กี่วินาที)"""
    c = read_config()
    on = str(c.get("BMS_AUTO_SYNC", "0")).strip().lower() in ("1", "true", "yes", "on")
    try:
        sec = max(10, int(str(c.get("BMS_AUTO_INTERVAL", "60")).strip() or 60))
    except ValueError:
        sec = 60
    return on, sec


def read_back_days():
    """คิวย้อนหลังกี่วันที่จำไว้ (0 = เฉพาะวันนี้)

       gateway หยิบคิวโดยเทียบคอลัมน์ datetime กับช่วงวัน ถ้าเป็น 0 คิวที่ค้างข้ามวัน
       (วันที่ไม่ได้เปิด gateway) จะไม่ถูกหยิบไปทำอีกเลย"""
    c = read_config()
    try:
        return max(0, min(365, int(str(c.get("BMS_SYNC_BACK_DAYS", "0")).strip() or 0)))
    except ValueError:
        return 0


def save_back_days(days):
    try:
        raw = read_config()
        raw["BMS_SYNC_BACK_DAYS"] = str(int(days))
        write_config(raw)
        return None
    except Exception as e:
        return str(e)


def save_auto(on, interval):
    """จำค่า auto-sync ลง config.bat — ปิดโปรแกรมแล้วเปิดใหม่จะกลับมาเหมือนเดิม"""
    try:
        raw = read_config()
        raw["BMS_AUTO_SYNC"] = "1" if on else "0"
        raw["BMS_AUTO_INTERVAL"] = str(interval)
        write_config(raw)
        return None
    except Exception as e:
        return str(e)


def apply_env(vals):
    """ตั้ง env ให้ค่าที่กรอกในฟอร์ม (plaintext) มีผลเหนือ _ENC เดิม"""
    for k, v in vals.items():
        os.environ[k] = str(v)
    for k in bmscrypt.SECRET_KEYS:
        if k in vals:
            os.environ.pop(k + "_ENC", None)


def env_with(form):
    """env สำหรับ subprocess: ฟอร์มเป็นตัวตัดสิน (ล้าง _ENC เก่าออก)"""
    e = os.environ.copy()
    e.update(form)
    for k in bmscrypt.SECRET_KEYS:
        if k in form:
            e.pop(k + "_ENC", None)
    return e


# ---------------- one database config panel ----------------
class DbPanel(ttk.LabelFrame):
    def __init__(self, master, prefix, title, def_driver, def_table, log):
        super().__init__(master, text=title, padding=10)
        self.prefix, self.def_driver, self.def_table, self.log = prefix, def_driver, def_table, log
        self.vars = {}
        for i, (key, label) in enumerate(FIELDS):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar()
            self.vars[key] = v
            if key == "DRIVER":
                w = ttk.Combobox(self, textvariable=v, values=["mysql", "pgsql"], state="readonly", width=24)
            elif key == "PASS":
                w = ttk.Entry(self, textvariable=v, width=26, show="*")
            else:
                w = ttk.Entry(self, textvariable=v, width=26)
            w.grid(row=i, column=1, sticky="we", pady=3, padx=(8, 0))
        self.columnconfigure(1, weight=1)
        bar = ttk.Frame(self)
        bar.grid(row=len(FIELDS), column=0, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Button(bar, text="ทดสอบเชื่อมต่อ", command=self.test).pack(side="left")
        self.status = ttk.Label(bar, text="", foreground=DIM)
        self.status.pack(side="left", padx=8)

    def load(self, vals):
        for key, _ in FIELDS:
            self.vars[key].set(vals.get(self.prefix + key, ""))
        if not self.vars["DRIVER"].get():
            self.vars["DRIVER"].set(self.def_driver)
        if not self.vars["TABLE"].get():
            self.vars["TABLE"].set(self.def_table)

    def collect(self):
        return {self.prefix + k: self.vars[k].get().strip() for k, _ in FIELDS}

    def cfg(self):
        apply_env(self.collect())
        return gateway.cfg_from(self.prefix, self.def_driver, self.def_table)

    def test(self):
        self.status.config(text="กำลังทดสอบ…", foreground=WARN)

        def work():
            cfg = self.cfg()
            db, err = gateway.connect(cfg, self.prefix)
            if db:
                try:
                    n, approx = gateway.approx_rows(db, cfg["table"])
                    _ntxt = "อ่านจำนวนแถวไม่ได้" if n is None else (("~%s แถว (ประมาณ)" % format(n, ",")) if approx else "%s แถว" % format(n, ","))
                    msg, col = "ok — ตาราง %s มี %s" % (cfg["table"], _ntxt), OK
                except Exception as e:
                    msg, col = "ต่อฐานได้ แต่ตารางมีปัญหา: %s" % e, WARN
                db.close()
            else:
                msg, col = "FAIL: %s" % err, BAD
            self.log("[ทดสอบ %s] %s" % (self.prefix, msg))
            self.after(0, lambda: self.status.config(text=msg[:70], foreground=col))

        threading.Thread(target=work, daemon=True).start()


# ---------------- แจ้งนัดเข้าหมอพร้อม (MOPH Alert) ----------------
class AlertPanel(ttk.LabelFrame):
    """ค่าตั้งส่งการ์ดแจ้งนัดหาคนไข้ทาง LINE OA / แอปหมอพร้อม — gateway ส่งต่อท้ายทุกรอบ sync

       มีผลเมื่อกด "บันทึกลง config.bat" เท่านั้น (ติ๊กเปิดไว้แต่ยังไม่บันทึก = ยังไม่ส่ง)
       ส่วนปุ่มทดสอบ/ดูรายการใช้ค่าที่กรอกอยู่ในฟอร์ม จะได้ลองคีย์ก่อนบันทึกได้"""
    KEYS = ("ENV", "CLIENT_KEY", "SECRET_KEY", "USER", "PASS", "HOSPCODE", "PHONE")
    AUTH_LABELS = (("key", "Client key / Secret key (MOPH Alert v3.1)"),
                   ("user", "User / Password (MOPH Account Center)"))

    def __init__(self, master, on_test, on_preview):
        super().__init__(master, text=" 3) แจ้งนัดเข้าหมอพร้อม (MOPH Alert — LINE OA / แอปหมอพร้อม) ", padding=10)
        self.on = tk.BooleanVar(value=False)
        self.vars = {k: tk.StringVar() for k in self.KEYS}
        self.auth = tk.StringVar()
        self.test_cid = tk.StringVar()
        self.groups = {"key": [], "user": []}
        ttk.Checkbutton(self, text="ส่งการ์ดแจ้งนัดให้คนไข้อัตโนมัติ หลังนัดจากหน้าทำนัดเขียนเข้า HIS สำเร็จ",
                        variable=self.on).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        def field(r, c, label, key, group=None):
            lb = ttk.Label(self, text=label)
            lb.grid(row=r, column=c, sticky="w", pady=3, padx=(0 if c == 0 else 16, 0))
            if key == "ENV":
                w = ttk.Combobox(self, textvariable=self.vars[key], values=["uat", "prod"], state="readonly", width=24)
            elif key == "AUTH":
                w = ttk.Combobox(self, textvariable=self.auth, values=[t for _, t in self.AUTH_LABELS],
                                 state="readonly", width=24)
                w.bind("<<ComboboxSelected>>", lambda e: self.show_auth())
            else:
                w = ttk.Entry(self, textvariable=self.vars[key], width=26,
                              show="*" if key in ("PASS", "SECRET_KEY") else "")
            w.grid(row=r, column=c + 1, sticky="we", pady=3, padx=(8, 0))
            if group:
                self.groups[group] += [lb, w]

        field(1, 0, "ปลายทาง", "ENV")
        field(1, 2, "วิธียืนยันตัวตน", "AUTH")
        field(2, 0, "Client key", "CLIENT_KEY", "key")
        field(2, 2, "Secret key", "SECRET_KEY", "key")
        field(3, 0, "User (MOPH Account Center)", "USER", "user")
        field(3, 2, "Password", "PASS", "user")
        field(4, 0, "รหัสหน่วยบริการ (5 หลัก)", "HOSPCODE", "user")
        field(4, 2, "เบอร์โทรบนการ์ด (ไม่บังคับ)", "PHONE")
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)
        bar = ttk.Frame(self)
        bar.grid(row=5, column=0, columnspan=4, sticky="we", pady=(10, 0))
        ttk.Label(bar, text="เลขบัตรที่รับการ์ดทดสอบ").pack(side="left")
        ttk.Entry(bar, textvariable=self.test_cid, width=16).pack(side="left", padx=6)
        self.btn_test = ttk.Button(bar, text="ส่งการ์ดทดสอบ", command=on_test)
        self.btn_test.pack(side="left")
        self.btn_preview = ttk.Button(bar, text="ดูรายการที่จะแจ้ง (ไม่ส่งจริง)", command=on_preview)
        self.btn_preview.pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="", foreground=DIM)
        self.status.pack(side="left", padx=8)
        self.hint = ttk.Label(self, text="", foreground=DIM)
        self.hint.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.set_auth("key")

    def auth_code(self):
        return next((code for code, text in self.AUTH_LABELS if text == self.auth.get()), "key")

    def set_auth(self, code):
        self.auth.set(dict(self.AUTH_LABELS).get(code, self.AUTH_LABELS[0][1]))
        self.show_auth()

    def show_auth(self):
        """โชว์เฉพาะช่องของวิธีที่เลือก — ค่าของอีกวิธียังเก็บไว้ สลับกลับมาไม่หาย"""
        code = self.auth_code()
        for group, widgets in self.groups.items():
            for w in widgets:
                if group == code:
                    w.grid()
                else:
                    w.grid_remove()
        self.hint.config(text=(
            "ขอ Client key / Secret key ได้ที่ moph-alert.moph.go.th (คีย์ของระบบนัด Open API ใช้แทนไม่ได้) · "
            if code == "key" else
            "บัญชีต้องมีสิทธิ์ Role MOPH_Alert_API ใน MOPH Account Center · ")
            + "ทดสอบด้วยเลขบัตรของเจ้าหน้าที่เอง (ไม่ลงบันทึก) · prod = ส่งถึง LINE ของคนไข้จริง")

    def load(self, vals):
        self.on.set(str(vals.get("BMS_MOPH_ALERT", "")).strip().lower() in ("1", "true", "yes", "on"))
        for k in self.KEYS:
            self.vars[k].set(vals.get("BMS_MOPH_ALERT_" + k, ""))
        if self.vars["ENV"].get() not in ("uat", "prod"):
            self.vars["ENV"].set("uat")
        self.set_auth(str(vals.get("BMS_MOPH_ALERT_AUTH", "") or "key").strip().lower())

    def collect(self):
        d = {"BMS_MOPH_ALERT_" + k: self.vars[k].get().strip() for k in self.KEYS}
        d["BMS_MOPH_ALERT_PASS"] = self.vars["PASS"].get()     # รหัสผ่านไม่ตัดช่องว่าง อาจเป็นส่วนหนึ่งของรหัสจริง
        d["BMS_MOPH_ALERT_AUTH"] = self.auth_code()
        d["BMS_MOPH_ALERT"] = "1" if self.on.get() else "0"
        return d

    def say(self, text, color=DIM):
        self.status.config(text=text[:90], foreground=color)


# ---------------- main app ----------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BMS Gateway — ตั้งค่า & Monitor")
        self.geometry("1000x700")
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.auto_job = None
        self.running = False
        self.web_proc = None            # เว็บเซิร์ฟเวอร์ที่โปรแกรมนี้เปิดเอง (ปิดตอนออก)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        # Monitor เป็นหน้าแรก (ใช้งานประจำ) ส่วนตั้งค่าเปิดเมื่อต้องแก้
        self.tab_mon = ttk.Frame(nb, padding=10)
        self.tab_cfg = ttk.Frame(nb, padding=10)
        nb.add(self.tab_mon, text="  Monitor  ")
        nb.add(self.tab_cfg, text="  ตั้งค่า  ")

        self._build_monitor()
        self._build_config()
        nb.select(self.tab_mon)

        vals = load_display_config()
        self.p_central.load(vals)
        self.p_his.load(vals)
        self.p_alert.load(vals)
        apply_env(vals)
        self.log("โหลดค่าจาก %s" % CONFIG_BAT)
        self.log("ความลับ: %s" % ("เข้ารหัสแล้ว (config.key)" if os.path.exists(bmscrypt.KEYFILE)
                                  else "ยังเป็น plaintext — กด 'บันทึกลง config.bat' เพื่อเข้ารหัส"))
        self.after(150, self._drain)
        self.refresh()

        # ครั้งก่อนเปิด auto-sync ค้างไว้ -> เปิดต่อให้เอง (หน่วงนิดให้หน้าจอ/สถานะฐานพร้อมก่อน)
        if self.auto.get():
            self.log("auto-sync ถูกจำไว้ว่าเปิดอยู่ (ทุก %s วินาที) — จะเริ่มทำงานเอง" % self.interval.get())
            self.after(2000, self._auto_tick)

        # รวม start.bat: เปิดเว็บเซิร์ฟเวอร์ให้อัตโนมัติ + ปิดตอนออกจากโปรแกรม
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(600, self._web_autostart)

    # ---------- tab: config ----------
    def _build_config(self):
        top = ttk.Frame(self.tab_cfg)
        top.pack(fill="x")
        ttk.Label(top, text="ตั้งค่าฐานข้อมูล 2 ฐาน + แจ้งนัดเข้าหมอพร้อม — บันทึกลงไฟล์ config.bat (ไฟล์เดียวกับที่ start.bat / gateway.bat ใช้)",
                  foreground=INK2).pack(anchor="w")
        wrap = ttk.Frame(self.tab_cfg)
        wrap.pack(fill="both", expand=True, pady=10)
        self.p_central = DbPanel(wrap, "BMS_DB_", " 1) ฐานกลาง (ต้นทาง — คิวนัดจากเครื่องมือ) ",
                                 "mysql", "appointment_queue", self.log)
        self.p_central.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.p_his = DbPanel(wrap, "BMS_HIS_", " 2) ฐาน HIS (ปลายทาง — ตาราง oapp) ",
                             "pgsql", "oapp", self.log)
        self.p_his.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.p_alert = AlertPanel(self.tab_cfg, self.alert_test, self.alert_preview)
        self.p_alert.pack(fill="x", pady=(0, 6))

        bar = ttk.Frame(self.tab_cfg)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="บันทึกลง config.bat", command=self.save_cfg).pack(side="left")
        ttk.Button(bar, text="โหลดใหม่จากไฟล์", command=self.reload_cfg).pack(side="left", padx=6)
        self.cfg_status = ttk.Label(bar, text="", foreground=DIM)
        self.cfg_status.pack(side="left", padx=10)
        ttk.Label(self.tab_cfg, text="หมายเหตุ: ค่าทั้งหมดต้องเป็นตัวอักษรอังกฤษ/ตัวเลข (ไฟล์ .bat ใช้ภาษาไทยไม่ได้)",
                  foreground=WARN).pack(anchor="w", pady=(8, 0))

    def save_cfg(self):
        form = {}
        form.update(self.p_central.collect())
        form.update(self.p_his.collect())
        alert = self.p_alert.collect()
        if not self.check_alert(alert):
            return
        form.update(alert)
        try:
            save_display_config(form)
            apply_env(form)
            self.cfg_status.config(text="บันทึกแล้ว — user/pass เข้ารหัสด้วย config.key", foreground=OK)
            self.log("บันทึก config.bat แล้ว (user/pass เข้ารหัส AES-256-GCM · คีย์: config.key)")
            self.refresh()
        except Exception as e:
            self.cfg_status.config(text=str(e), foreground=BAD)
            messagebox.showerror("บันทึกไม่สำเร็จ", str(e))

    def reload_cfg(self):
        vals = load_display_config()
        self.p_central.load(vals)
        self.p_his.load(vals)
        self.p_alert.load(vals)
        apply_env(vals)
        self.cfg_status.config(text="โหลดค่าจากไฟล์แล้ว", foreground=DIM)
        self.refresh()

    # ---------- แจ้งนัดเข้าหมอพร้อม ----------
    @staticmethod
    def _alert_err_text(err):
        for k, label in (("BMS_MOPH_ALERT_CLIENT_KEY", "Client key"), ("BMS_MOPH_ALERT_SECRET_KEY", "Secret key"),
                         ("BMS_MOPH_ALERT_USER", "User"), ("BMS_MOPH_ALERT_PASS", "Password"),
                         ("BMS_MOPH_ALERT_HOSPCODE", "รหัสหน่วยบริการ"), (" ใน config.bat", "")):
            err = err.replace(k, label)
        return err

    def check_alert(self, alert):
        """ก่อนบันทึก: กันเปิดส่งทั้งที่ค่าตั้งยังไม่ครบ + ถามยืนยันเมื่อเพิ่งเปิด/เพิ่งเปลี่ยนไป prod (ส่งถึงคนไข้จริง)"""
        if alert["BMS_MOPH_ALERT"] != "1":
            return True
        c = gateway.alert_cfg(alert)
        if c["error"]:
            messagebox.showerror("ยังเปิดส่งแจ้งนัดไม่ได้", self._alert_err_text(c["error"])
                                 + "\n\nกรอกให้ครบ หรือเอาติ๊ก 'ส่งการ์ดแจ้งนัด' ออกก่อนบันทึก")
            return False
        if c["auth"] == "user" and not re.fullmatch(r"\d{5}", alert["BMS_MOPH_ALERT_HOSPCODE"]):
            messagebox.showerror("ยังเปิดส่งแจ้งนัดไม่ได้", "รหัสหน่วยบริการต้องเป็นตัวเลข 5 หลัก")
            return False
        old = read_config()
        was_on = str(old.get("BMS_MOPH_ALERT", "")).strip().lower() in ("1", "true", "yes", "on")
        if was_on and str(old.get("BMS_MOPH_ALERT_ENV", "uat")).strip().lower() == c["env"] \
                and str(old.get("BMS_MOPH_ALERT_AUTH", "key")).strip().lower() == c["auth"]:
            return True
        return messagebox.askyesno(
            "ยืนยันเปิดส่งแจ้งนัดเข้าหมอพร้อม",
            "ปลายทาง: %s\n\n"
            "หลังบันทึก gateway จะส่งการ์ดแจ้งนัดทาง LINE / แอปหมอพร้อม ให้คนไข้ทุกนัดจากหน้าทำนัด"
            "ที่เขียนเข้า HIS แล้ว รวมนัดในช่วงอ่านย้อนหลัง %s วันที่ยังไม่เคยแจ้ง\n\n"
            "แนะนำให้กด 'ส่งการ์ดทดสอบ' และ 'ดูรายการที่จะแจ้ง' ก่อน\n\nเปิดใช้เลยหรือไม่?"
            % (c["env"], read_back_days()))

    def alert_test(self):
        c = gateway.alert_cfg(self.p_alert.collect())
        if c["error"]:
            self.p_alert.say(self._alert_err_text(c["error"]), BAD)
            return
        cid = gateway.valid_cid(self.p_alert.test_cid.get())
        if not cid:
            self.p_alert.say("เลขบัตรที่รับการ์ดทดสอบไม่ถูกต้อง (ต้องเป็นเลขบัตรประชาชน 13 หลัก)", BAD)
            return
        if c["env"] == "prod" and not messagebox.askyesno(
                "ยืนยัน", "ส่งการ์ดทดสอบผ่าน prod ไปที่เลขบัตร %s ?\n\n"
                          "การ์ดจะไปถึง LINE / หมอพร้อมของเจ้าของเลขบัตรนี้จริง" % gateway._mask_cid(cid)):
            return

        def done(rc):
            if rc == 0:
                self.p_alert.say("ส่งการ์ดทดสอบสำเร็จ — ดูที่ LINE / แอปหมอพร้อมของเลขบัตรนี้", OK)
            elif rc == 3:
                self.p_alert.say("ยืนยันตัวตนผ่าน แต่เลขบัตรนี้ไม่มีบัญชีหมอพร้อม — ลองเลขบัตรที่ใช้หมอพร้อมอยู่", WARN)
            elif rc == 4:
                self.p_alert.say("ยืนยันตัวตนไม่ผ่าน — ตรวจคีย์ / user / password (รายละเอียดที่ Monitor > Log)", BAD)
            else:
                self.p_alert.say("ส่งไม่สำเร็จ — ดูสาเหตุที่แท็บ Monitor > Log", BAD)

        if self.run_gw("alert-test", cid=cid, cfg=c, on_done=done):
            self.p_alert.say("กำลังขอ token และส่งการ์ดทดสอบ…", WARN)
        else:
            self.p_alert.say("gateway กำลังทำงานอยู่ — รอสักครู่แล้วกดใหม่", WARN)

    def alert_preview(self):
        def done(rc):
            self.p_alert.say("ดูรายการที่แท็บ Monitor > Log" if rc == 0 else "อ่านรายการไม่ได้ — ดูที่แท็บ Monitor > Log",
                             OK if rc == 0 else BAD)

        if self.run_gw("alert-preview", cfg=gateway.alert_cfg(self.p_alert.collect()), on_done=done):
            self.p_alert.say("กำลังอ่านรายการ…", WARN)
        else:
            self.p_alert.say("gateway กำลังทำงานอยู่ — รอสักครู่แล้วกดใหม่", WARN)

    # ---------- tab: monitor ----------
    def _build_monitor(self):
        st = ttk.LabelFrame(self.tab_mon, text=" สถานะ ", padding=10)
        st.pack(fill="x")
        self.lb_central = ttk.Label(st, text="● ฐานกลาง: —", foreground=DIM)
        self.lb_central.grid(row=0, column=0, sticky="w", padx=(0, 20))
        self.lb_his = ttk.Label(st, text="● ฐาน HIS: —", foreground=DIM)
        self.lb_his.grid(row=0, column=1, sticky="w", padx=(0, 20))
        self.lb_counts = ttk.Label(st, text="pending — · done — · error — · รวม —", foreground=INK2)
        self.lb_counts.grid(row=0, column=2, sticky="w")
        self.lb_alert = ttk.Label(st, text="○ แจ้งนัดหมอพร้อม: —", foreground=DIM)
        self.lb_alert.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        st.columnconfigure(2, weight=1)

        # ---- เว็บเซิร์ฟเวอร์ (หน้าทำนัด/ทะเบียนนัด) — รวมหน้าที่ start.bat ----
        web = ttk.LabelFrame(self.tab_mon, text=" เว็บเซิร์ฟเวอร์ (หน้าทำนัด / ทะเบียนนัด) ", padding=8)
        web.pack(fill="x", pady=(8, 0))
        self.web_status = ttk.Label(web, text="○ เว็บเซิร์ฟเวอร์: —", foreground=DIM)
        self.web_status.pack(side="left")
        self.web_toggle = ttk.Button(web, text="เริ่มเว็บเซิร์ฟเวอร์", command=self.toggle_web)
        self.web_toggle.pack(side="right")
        ttk.Button(web, text="เปิดหน้าเว็บ", command=self.open_web).pack(side="right", padx=6)

        bar = ttk.Frame(self.tab_mon)
        bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="รีเฟรช", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Preview (ไม่เขียนจริง)", command=lambda: self.run_gw("preview")).pack(side="left", padx=6)
        self.btn_sync = ttk.Button(bar, text="Sync เข้า HIS", command=self.confirm_sync)
        self.btn_sync.pack(side="left")
        # คิวเปิด visit (refer_open_visit -> ovst) เป็นคนละคิวกับคิวนัด จึงแยกปุ่ม
        ttk.Button(bar, text="Preview เปิด visit",
                   command=lambda: self.run_gw("visit-preview")).pack(side="left", padx=(14, 6))
        self.btn_visit = ttk.Button(bar, text="เปิด visit (ovst)", command=self.confirm_visit)
        self.btn_visit.pack(side="left")
        auto_on, auto_sec = read_auto()                 # ค่าที่จำไว้ครั้งก่อน
        self.auto = tk.BooleanVar(value=auto_on)
        ttk.Checkbutton(bar, text="auto-sync ทุก", variable=self.auto, command=self.toggle_auto).pack(side="left", padx=(16, 2))
        self.interval = tk.StringVar(value=str(auto_sec))
        ent = ttk.Entry(bar, textvariable=self.interval, width=5)
        ent.pack(side="left")
        ent.bind("<FocusOut>", lambda e: self.remember_auto())   # แก้ตัวเลขแล้วจำให้ด้วย
        ent.bind("<Return>", lambda e: self.remember_auto())
        # ช่วงคิวย้อนหลัง — 0 = เฉพาะวันนี้ (คิวที่ค้างข้ามวันจะไม่ถูกหยิบเลย)
        ttk.Label(bar, text="  อ่านย้อนหลัง").pack(side="left")
        self.back_days = tk.StringVar(value=str(read_back_days()))
        entb = ttk.Spinbox(bar, from_=0, to=365, width=4, textvariable=self.back_days,
                           command=self.remember_back_days)
        entb.pack(side="left", padx=2)
        entb.bind("<FocusOut>", lambda e: self.remember_back_days())
        entb.bind("<Return>", lambda e: self.remember_back_days())
        ttk.Label(bar, text="วัน").pack(side="left")
        ttk.Label(bar, text="วินาที").pack(side="left", padx=(2, 0))

        # สองคิวนี้คนละตารางและคนละปลายทาง (appointment_queue -> oapp, refer_open_visit -> ovst)
        # เอามารวมตารางเดียวแล้วดูไม่ออกว่าแถวไหนของคิวไหน จึงแยกเป็นแถบ
        rows = ttk.LabelFrame(self.tab_mon, text=" คิวล่าสุดในฐานกลาง ", padding=6)
        rows.pack(fill="both", expand=True)
        self.qtabs = ttk.Notebook(rows)
        self.qtabs.pack(fill="both", expand=True)
        self.tree = self._queue_tree(("id", "hn", "vn", "datetime", "status"),
                                     (60, 110, 130, 160, 90))
        self.tree_visit = self._queue_tree(("id", "hn", "vn", "ref_id", "requested_at", "status"),
                                           (60, 110, 130, 150, 150, 90))
        self.qtabs.add(self.tree.master, text=" คิวนัด -> oapp ")
        self.qtabs.add(self.tree_visit.master, text=" คิวเปิด visit -> ovst ")

        lg = ttk.LabelFrame(self.tab_mon, text=" Log ", padding=6)
        lg.pack(fill="both", expand=True, pady=(8, 0))
        self.txt = tk.Text(lg, height=10, bg="#FAFBFA", fg=INK2, relief="flat",
                           font=("Consolas", 9), wrap="word")
        self.txt.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(lg, orient="vertical", command=self.txt.yview)
        sb2.pack(side="right", fill="y")
        self.txt.configure(yscrollcommand=sb2.set)

    def _queue_tree(self, cols, widths):
        """ตารางคิวหนึ่งแถบ — คืน Treeview ที่มี Frame ของตัวเองเป็น master
           (เอา .master ไปใส่ notebook ได้เลย ไม่ต้องจำ frame แยกอีกตัว)"""
        f = ttk.Frame(self.qtabs, padding=4)
        tv = ttk.Treeview(f, columns=cols, show="headings", height=9)
        for c, w in zip(cols, widths):
            tv.heading(c, text=c)
            tv.column(c, width=w, anchor="w")
        tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(f, orient="vertical", command=tv.yview)
        sb.pack(side="right", fill="y")
        tv.configure(yscrollcommand=sb.set)
        for tag, color in (("pending", WARN), ("done", OK), ("error", BAD)):
            tv.tag_configure(tag, foreground=color)
        return tv

    @staticmethod
    def _fill_tree(tv, rows, cols):
        tv.delete(*tv.get_children())
        for r in rows:
            tv.insert("", "end", values=tuple(r.get(c) for c in cols),
                      tags=(str(r.get("status")),))

    # ---------- เว็บเซิร์ฟเวอร์ (รวม start.bat) ----------
    def _web_autostart(self):
        if _port_open(WEB_PORT):
            self.log("เว็บเซิร์ฟเวอร์ทำงานอยู่แล้วที่พอร์ต %d (ไม่เปิดซ้ำ)" % WEB_PORT)
        else:
            self.start_web()
        self._web_poll()

    def start_web(self):
        if _port_open(WEB_PORT):
            self.log("พอร์ต %d มีเซิร์ฟเวอร์อยู่แล้ว — ไม่เปิดซ้ำ" % WEB_PORT)
            self._web_refresh(); return
        cmd = _web_cmd()
        if not cmd:
            self.log("หา server.exe / server.py ไม่เจอในโฟลเดอร์นี้ — เปิดเว็บเซิร์ฟเวอร์ไม่ได้")
            return
        flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW: ไม่เด้ง console ของ server
        try:
            self.web_proc = subprocess.Popen(cmd, cwd=HERE, creationflags=flags)
            self.log("เปิดเว็บเซิร์ฟเวอร์: %s  พอร์ต %d" % (os.path.basename(cmd[0]), WEB_PORT))
        except Exception as e:
            self.web_proc = None
            self.log("เปิดเว็บเซิร์ฟเวอร์ไม่สำเร็จ: %s" % e)
        self.after(1200, self._web_refresh)

    def stop_web(self):
        p = self.web_proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                self.log("หยุดเว็บเซิร์ฟเวอร์แล้ว")
            except Exception as e:
                self.log("หยุดเว็บเซิร์ฟเวอร์ไม่สำเร็จ: %s" % e)
        self.web_proc = None
        self._web_refresh()

    def toggle_web(self):
        if self.web_proc is not None and self.web_proc.poll() is None:
            self.stop_web()
        else:
            self.start_web()

    def open_web(self):
        if not _port_open(WEB_PORT):
            self.log("เว็บเซิร์ฟเวอร์ยังไม่เปิด — กด 'เริ่มเว็บเซิร์ฟเวอร์' ก่อน")
            return
        webbrowser.open("http://localhost:%d/%s" % (WEB_PORT, WEB_PAGE))

    def _web_refresh(self):
        up = _port_open(WEB_PORT)
        mine = bool(self.web_proc is not None and self.web_proc.poll() is None)
        self.web_status.config(
            text=("● เว็บเซิร์ฟเวอร์: เปิดอยู่  http://localhost:%d" % WEB_PORT) if up
                 else "○ เว็บเซิร์ฟเวอร์: ปิด",
            foreground=OK if up else DIM)
        self.web_toggle.config(text=("หยุดเว็บเซิร์ฟเวอร์" if mine else "เริ่มเว็บเซิร์ฟเวอร์"))

    def _web_poll(self):
        self._web_refresh()
        self.after(3000, self._web_poll)

    def _on_close(self):
        with contextlib.suppress(Exception):
            self.stop_web()
        self.destroy()

    # ---------- logging (thread-safe) ----------
    def log(self, msg):
        self.q.put(msg)

    def _drain(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.txt.insert("end", ANSI.sub("", str(msg)).rstrip() + "\n")
                self.txt.see("end")
        except queue.Empty:
            pass
        self.after(150, self._drain)

    # ---------- monitor actions ----------
    def refresh(self):
        def work():
            c_cfg = self.p_central.cfg()
            h_cfg = self.p_his.cfg()
            # central
            db, err = gateway.connect(c_cfg, "BMS_DB_")
            counts, rows = None, []
            vcounts, vrows = None, []
            acfg, acounts = gateway.alert_cfg(), None
            if db:
                try:
                    t = c_cfg["table"]
                    tb = gateway.today_bounds()
                    counts = {s: db.scalar("SELECT COUNT(*) FROM %s WHERE status=%%s AND datetime >= %%s AND datetime < %%s" % t, (s, tb[0], tb[1])) for s in ("pending", "done", "error")}
                    counts["date"] = tb[0][:10]
                    counts["today_total"] = db.scalar("SELECT COUNT(*) FROM %s WHERE datetime >= %%s AND datetime < %%s" % t, tb)
                    counts["total"] = db.scalar("SELECT COUNT(*) FROM %s" % t)
                    rows = db.query("SELECT id,hn,vn,datetime,status FROM %s WHERE datetime >= %%s AND datetime < %%s ORDER BY id DESC LIMIT 100" % t, tb)
                except Exception as e:
                    self.log("อ่านตารางฐานกลางไม่ได้: %s" % e)
                # คิวเปิด visit อยู่คนละตาราง ถามแยก — ตารางนี้อาจยังไม่ถูกสร้าง
                # (สร้างโดย Refer-HDY/proxy.py ตอนมีคำขอแรก) จึงห้ามให้มันพาคิวนัดล้มไปด้วย
                try:
                    vt = gateway.visit_table()
                    vcounts = {st: db.scalar("SELECT COUNT(*) FROM %s WHERE status=%%s" % vt, (st,))
                               for st in ("pending", "done", "error")}
                    # gateway หยิบเฉพาะ visit_date วันนี้ เลขบนแถบจึงต้องเป็นเลขเดียวกัน
                    # ไม่งั้นแถบขึ้น "รอ 3" แต่กด sync แล้วไม่มีอะไรเกิดขึ้น
                    vcounts["today"] = db.scalar(
                        "SELECT COUNT(*) FROM %s WHERE status='pending' AND visit_date=%%s" % vt,
                        (datetime.date.today(),))
                    vcounts["other"] = vcounts["pending"] - (vcounts["today"] or 0)
                    vrows = db.query("SELECT id,hn,vn,ref_id,requested_at,status FROM %s "
                                     "ORDER BY id DESC LIMIT 100" % vt)
                except Exception:
                    db.rollback()
                # ผลแจ้งนัดหมอพร้อมวันนี้ — ตารางถูกสร้างตอนส่งครั้งแรก ยังไม่มีก็แค่ไม่แสดงตัวเลข
                try:
                    if gateway.alert_log_exists(db):
                        acounts = {r["status"]: r["n"] for r in db.query(
                            "SELECT status, COUNT(*) AS n FROM %s WHERE updated_at >= %%s GROUP BY status"
                            % gateway.ALERT_LOG_TABLE, (datetime.date.today(),))}
                except Exception:
                    db.rollback()
                db.close()
            # his
            hdb, herr = gateway.connect(h_cfg, "BMS_HIS_")
            hrows = None
            hrows_approx = False
            h_next = h_vst = None
            if hdb:
                try:
                    hrows, hrows_approx = gateway.approx_rows(hdb, h_cfg["table"])
                    h_next = gateway.count_today(hdb, h_cfg["table"], "nextdate")
                    h_vst = gateway.count_today(hdb, h_cfg["table"], "vstdate")
                except Exception as e:
                    self.log("อ่านตาราง HIS ไม่ได้: %s" % e)
                hdb.close()
            self.after(0, lambda: self._paint(c_cfg, err, counts, rows, h_cfg, herr, hrows,
                                              hrows_approx, h_next, h_vst, vcounts, vrows, acfg, acounts))

        threading.Thread(target=work, daemon=True).start()

    def _paint(self, c_cfg, cerr, counts, rows, h_cfg, herr, hrows, hrows_approx=False,
               h_next=None, h_vst=None, vcounts=None, vrows=(), acfg=None, acounts=None):
        self.lb_central.config(
            text="● ฐานกลาง: %s %s/%s" % (c_cfg["driver"], c_cfg["host"], c_cfg["name"] or "-") + ("" if not cerr else "  ต่อไม่ได้"),
            foreground=OK if not cerr else BAD)
        if hrows is None:
            htxt = ""
        else:
            parts = [("~%s แถว" % format(hrows, ",")) if hrows_approx else ("%s แถว" % format(hrows, ","))]
            if h_next is not None:
                parts.append("นัดวันนี้ %s" % format(h_next, ","))
            if h_vst is not None:
                parts.append("บันทึกวันนี้ %s" % format(h_vst, ","))
            htxt = "  (%s: %s)" % (h_cfg["table"], " · ".join(parts))
        self.lb_his.config(
            text="● ฐาน HIS: %s %s/%s%s" % (h_cfg["driver"], h_cfg["host"], h_cfg["name"] or "-",
                                            "" if not herr else "  ต่อไม่ได้") + htxt,
            foreground=OK if not herr else BAD)
        if cerr:
            self.log("ฐานกลางต่อไม่ได้: %s" % cerr)
        if herr:
            self.log("ฐาน HIS ต่อไม่ได้: %s" % herr)
        if counts:
            self.lb_counts.config(text="วันนี้ %s · pending %s · done %s · error %s · รวมวันนี้ %s · ทั้งหมด %s"
                                       % (counts["date"], counts["pending"], counts["done"], counts["error"], counts["today_total"], counts["total"]))
        else:
            self.lb_counts.config(text="pending — · done — · error — · รวม —")
        if acfg is not None:
            if not acfg["on"]:
                atxt, acol = "○ แจ้งนัดหมอพร้อม: ปิด", DIM
            elif acfg["error"]:
                atxt, acol = "● แจ้งนัดหมอพร้อม: เปิดแต่ยังส่งไม่ได้ — %s" % self._alert_err_text(acfg["error"]), BAD
            else:
                atxt, acol = "● แจ้งนัดหมอพร้อม: เปิด (%s · %s)" % (
                    acfg["env"], "คีย์ v3.1" if acfg["auth"] == "key" else "user v2"), OK
            if acounts is not None:
                atxt += ("  ·  วันนี้ แจ้งแล้ว %s · ไม่แจ้ง %s · ไม่มีบัญชีหมอพร้อม %s · รอลองใหม่ %s · ล้มเหลว %s"
                         % (acounts.get("sent", 0), acounts.get("skipped", 0), acounts.get("no_account", 0),
                            acounts.get("retry", 0), acounts.get("failed", 0)))
                if acounts.get("failed") and acol == OK:
                    acol = WARN
            self.lb_alert.config(text=atxt, foreground=acol)
        self._fill_tree(self.tree, rows, ("id", "hn", "vn", "datetime", "status"))
        self._fill_tree(self.tree_visit, vrows, ("id", "hn", "vn", "ref_id", "requested_at", "status"))
        # จำนวนที่ค้างอยู่ติดไว้ที่ชื่อแถบ จะได้เห็นคิวที่ยังไม่ได้ทำโดยไม่ต้องกดสลับแถบ
        self.qtabs.tab(0, text=" คิวนัด -> oapp · รอ %s "
                       % (counts["pending"] if counts else "—"))
        self.qtabs.tab(1, text=" คิวเปิด visit -> ovst · รอวันนี้ %s%s "
                       % ((vcounts["today"] if vcounts else "—"),
                          ("" if not (vcounts and vcounts["other"])
                           else (" · วันอื่น %s" % vcounts["other"]))))

    def confirm_sync(self):
        if messagebox.askyesno("ยืนยัน", "เขียนคิวที่ pending เข้า HIS (ตาราง oapp) จริงหรือไม่?\n\nแนะนำให้กด Preview ตรวจก่อน"):
            self.run_gw("sync")

    def confirm_visit(self):
        if messagebox.askyesno("ยืนยัน",
                               "เปิด visit จริงให้คิวใน refer_open_visit "
                               "(INSERT ลงตาราง ovst) หรือไม่?\n\n"
                               "คนไข้ที่ยังไม่มีบัตรในฐานปลายทางจะถูกข้ามไว้\n"
                               "แนะนำให้กด Preview เปิด visit ตรวจก่อน"):
            self.run_gw("visit-sync")

    def _busy_buttons(self):
        return (self.btn_sync, self.btn_visit, self.p_alert.btn_test, self.p_alert.btn_preview)

    def run_gw(self, action, **kw):
        """รันคำสั่ง gateway ในเธรด · คืน False ถ้ามีงานค้างอยู่ (ไม่ได้รัน)
           kw: cfg = ค่าตั้งหมอพร้อมจากฟอร์ม · cid = เลขบัตรการ์ดทดสอบ · on_done(rc) = เรียกเมื่อจบ"""
        if self.running:
            self.log("มีงานกำลังทำอยู่ รอสักครู่…")
            return False
        self.running = True
        for b in self._busy_buttons():
            b.config(state="disabled")
        form = {}
        form.update(self.p_central.collect())
        form.update(self.p_his.collect())
        apply_env(form)                       # ให้ gateway อ่าน config จากฟอร์มปัจจุบัน
        back = self.remember_back_days()      # ช่วงคิวย้อนหลังจากหน้าจอ
        os.environ["BMS_SYNC_BACK_DAYS"] = str(back)
        gateway.SYNC_BACK_DAYS = back         # gateway ถูก import ไปแล้ว env อย่างเดียวไม่พอ
        self.log("อ่านย้อนหลัง %d วัน" % back)
        self.log("\n$ gateway %s" % action)

        q = self.q

        class _Out:                           # จับ print() ของ gateway มาลง log ทีละบรรทัด
            def __init__(self):
                self.buf = ""

            def write(self, s):
                self.buf += s
                while "\n" in self.buf:
                    line, self.buf = self.buf.split("\n", 1)
                    q.put(line)

            def flush(self):
                if self.buf:
                    q.put(self.buf)
                    self.buf = ""

        def work():
            # เรียกฟังก์ชันตรง ๆ (ไม่ใช้ subprocess) -> ทำงานได้ทั้งตอนรันเป็น .py และ .exe
            out = _Out()
            rc = None
            try:
                CENTRAL = gateway.cfg_from("BMS_DB_", "mysql", "appointment_queue")
                HIS = gateway.cfg_from("BMS_HIS_", "pgsql", "oapp")
                with contextlib.redirect_stdout(out):
                    if action == "preview":
                        gateway.do_preview(CENTRAL, HIS, 50)
                    elif action == "visit-preview":
                        gateway.do_visit_preview(CENTRAL, HIS, 50)
                    elif action == "visit-sync":
                        gateway.do_visit_sync(CENTRAL, HIS, 50)
                    elif action == "alert-preview":
                        rc = gateway.do_alert_preview(CENTRAL, HIS, 50, cfg=kw.get("cfg"))
                    elif action == "alert-test":
                        rc = gateway.do_alert_test(HIS, kw["cid"], cfg=kw.get("cfg"))
                    else:                       # sync / sync-all
                        gateway.do_sync(CENTRAL, HIS, 50)
                        if action == "sync-all":
                            print()
                            gateway.do_visit_sync(CENTRAL, HIS, 50)
                out.flush()
                self.log("— จบ —")
            except Exception as e:
                out.flush()
                self.log("รันไม่สำเร็จ: %s" % e)
            finally:
                self.running = False
                self.after(0, lambda: [b.config(state="normal") for b in self._busy_buttons()])
                self.after(0, self.refresh)
                if kw.get("on_done"):
                    self.after(0, lambda: kw["on_done"](rc))

        threading.Thread(target=work, daemon=True).start()
        return True

    def remember_auto(self):
        """บันทึกสถานะ auto-sync ปัจจุบันลง config.bat"""
        try:
            sec = max(10, int(self.interval.get()))
        except ValueError:
            sec = 60
            self.interval.set("60")
        err = save_auto(self.auto.get(), sec)
        if err:
            self.log("จำค่า auto-sync ไม่ได้: %s" % err)
        return sec

    def remember_back_days(self):
        """บันทึกช่วงคิวย้อนหลังลง config.bat แล้วคืนค่าที่ใช้จริง (ปัดค่าที่กรอกผิดให้)"""
        try:
            n = max(0, min(365, int(self.back_days.get())))
        except ValueError:
            n = 0
        self.back_days.set(str(n))
        err = save_back_days(n)
        if err:
            self.log("จำค่าอ่านย้อนหลังไม่ได้: %s" % err)
        return n

    def toggle_auto(self):
        self.remember_auto()
        if self.auto_job:
            self.after_cancel(self.auto_job)
            self.auto_job = None
        if self.auto.get():
            self.log("เปิด auto-sync ทุก %s วินาที" % self.interval.get())
            self._auto_tick()
        else:
            self.log("ปิด auto-sync")

    def _auto_tick(self):
        if not self.auto.get():
            return
        self.run_gw("sync-all")          # ทั้งคิวนัดและคิวเปิด visit
        try:
            sec = max(10, int(self.interval.get()))
        except ValueError:
            sec = 60
        self.auto_job = self.after(sec * 1000, self._auto_tick)


if __name__ == "__main__":
    App().mainloop()
