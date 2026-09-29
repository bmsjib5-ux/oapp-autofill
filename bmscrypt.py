#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
เข้ารหัส/ถอดรหัส ค่าลับใน config.bat (user / password / api key)

  วิธี : AES-256-GCM  คีย์สุ่ม 32 ไบต์ เก็บที่ไฟล์ config.key (ข้างๆ config.bat)
  รูปแบบที่เก็บใน config.bat :  enc:v1:<base64(iv12 + ciphertext + tag16)>
  ฝั่ง PHP ถอดรหัสชุดเดียวกันได้ที่ bmscrypt.php

  ขอบเขตการป้องกัน (พูดตามตรง):
    กันคนเปิดอ่าน config.bat เจอรหัสตรง ๆ / ก็อป config.bat ไปเครื่องอื่นแล้วใช้ไม่ได้ /
    เผลอแชร์จอหรือส่งไฟล์ให้คนอื่น
    ไม่ได้กันคนที่เข้าถึงโฟลเดอร์นี้ได้ทั้งหมด (เพราะ config.key อยู่เครื่องเดียวกัน)
    -> config.key ถูกจำกัดสิทธิ์ให้เฉพาะ Windows user ปัจจุบัน และห้าม commit ขึ้น git

CLI:
  python bmscrypt.py status           ดูว่าอะไรเข้ารหัสแล้วบ้าง
  python bmscrypt.py encrypt-config   เข้ารหัส user/pass ใน config.bat (ทำครั้งเดียว)
  python bmscrypt.py newkey           สร้างคีย์ใหม่ (ค่าที่เข้ารหัสไว้เดิมจะถอดไม่ได้!)
"""

import os
import re
import sys
import base64
import secrets
import subprocess

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def app_dir():
    """โฟลเดอร์ที่ config.bat / config.key อยู่
       - รันเป็น .py  -> โฟลเดอร์ของสคริปต์
       - รันเป็น .exe -> โฟลเดอร์ของ exe (ไม่ใช่ temp _MEIPASS ที่ PyInstaller แตกไฟล์ไว้)"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = app_dir()
KEYFILE = os.path.join(HERE, "config.key")
CONFIG_BAT = os.path.join(HERE, "config.bat")
PREFIX = "enc:v1:"

# ค่าที่ถือว่าเป็นความลับ -> เก็บเป็น <NAME>_ENC
SECRET_KEYS = ["BMS_DB_USER", "BMS_DB_PASS", "BMS_HIS_USER", "BMS_HIS_PASS", "BMS_API_KEY",
               "BMS_MOPH_ALERT_USER", "BMS_MOPH_ALERT_PASS",      # บัญชี MOPH Account Center (แจ้งนัดหมอพร้อม v2)
               "BMS_MOPH_ALERT_CLIENT_KEY", "BMS_MOPH_ALERT_SECRET_KEY"]   # คีย์ MOPH Alert v3.1


def _restrict(path):
    """จำกัดสิทธิ์ config.key ให้เฉพาะ user ปัจจุบัน (Windows)"""
    user = os.environ.get("USERNAME")
    if not user or os.name != "nt":
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % user],
                       capture_output=True, creationflags=0x08000000)
    except Exception:
        pass


def load_key(create=True):
    if os.path.exists(KEYFILE):
        with open(KEYFILE, "r", encoding="ascii") as f:
            k = base64.b64decode(f.read().strip())
        if len(k) != 32:
            raise ValueError("config.key ไม่ถูกต้อง (ต้องเป็นคีย์ 32 ไบต์)")
        return k
    if not create:
        raise FileNotFoundError("ไม่พบ config.key — ยังไม่ได้สร้างคีย์")
    k = secrets.token_bytes(32)
    with open(KEYFILE, "w", encoding="ascii", newline="\n") as f:
        f.write(base64.b64encode(k).decode("ascii"))
    _restrict(KEYFILE)
    return k


def is_enc(v):
    return str(v or "").startswith(PREFIX)


def encrypt(plain, key=None):
    if plain is None or plain == "":
        return ""
    key = key or load_key()
    iv = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, str(plain).encode("utf-8"), None)   # ct ต่อท้ายด้วย tag 16 ไบต์
    return PREFIX + base64.b64encode(iv + ct).decode("ascii")


def decrypt(value, key=None):
    """ถ้าไม่ได้ขึ้นต้นด้วย enc:v1: ให้ถือว่าเป็น plaintext (ใช้ค่าเดิมได้เลย)"""
    if value is None or value == "":
        return ""
    v = str(value)
    if not v.startswith(PREFIX):
        return v
    key = key or load_key(create=False)
    blob = base64.b64decode(v[len(PREFIX):])
    iv, ct = blob[:12], blob[12:]
    return AESGCM(key).decrypt(iv, ct, None).decode("utf-8")


def env_secret(name):
    """อ่านค่าลับจาก env: ใช้ <name>_ENC ก่อน ถ้าไม่มีค่อยใช้ <name> (plaintext)"""
    enc = os.getenv(name + "_ENC")
    if enc:
        try:
            return decrypt(enc)
        except Exception as e:
            raise RuntimeError("ถอดรหัส %s_ENC ไม่สำเร็จ: %s" % (name, e))
    return os.getenv(name) or ""


# ---------------- config.bat helpers ----------------
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
    bad = [k for k, v in vals.items() if not str(v).isascii()]
    if bad:
        raise ValueError("ค่าเหล่านี้มีอักขระที่ไม่ใช่ ASCII ซึ่ง .bat ใช้ไม่ได้: " + ", ".join(bad))
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
    missing = [k for k in vals if k not in seen]
    if missing:
        out.append("")
        for k in missing:
            out.append('set "%s=%s"' % (k, vals[k]))
    with open(CONFIG_BAT, "w", encoding="ascii", errors="strict", newline="\r\n") as f:
        f.write("\n".join(out) + "\n")


def encrypt_config():
    """ย้าย plaintext user/pass ใน config.bat -> <NAME>_ENC แล้วล้าง plaintext ทิ้ง"""
    vals = read_config()
    key = load_key()
    changed = []
    for k in SECRET_KEYS:
        plain = vals.get(k, "")
        already = vals.get(k + "_ENC", "")
        if already and not plain:
            continue                       # เข้ารหัสไว้แล้ว
        if plain:
            vals[k + "_ENC"] = encrypt(plain, key)
            vals[k] = ""                   # ล้าง plaintext
            changed.append(k)
        else:
            vals.setdefault(k + "_ENC", already or "")
    write_config(vals)
    return changed


def status():
    vals = read_config()
    print("config.bat : %s" % CONFIG_BAT)
    print("config.key : %s" % ("มีแล้ว" if os.path.exists(KEYFILE) else "ยังไม่มี (จะสร้างอัตโนมัติเมื่อเข้ารหัส)"))
    print()
    for k in SECRET_KEYS:
        plain, enc = vals.get(k, ""), vals.get(k + "_ENC", "")
        if enc:
            try:
                ok = "เข้ารหัสแล้ว (ถอดทดสอบผ่าน: %s)" % ("ว่าง" if decrypt(enc) == "" else "***")
            except Exception as e:
                ok = "เข้ารหัสแล้ว แต่ถอดไม่ได้! %s" % e
        elif plain:
            ok = "!! ยังเป็น plaintext -> รัน: python bmscrypt.py encrypt-config"
        else:
            ok = "(ว่าง)"
        print("  %-16s %s" % (k, ok))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "encrypt-config":
        changed = encrypt_config()
        if changed:
            print("เข้ารหัสแล้ว: %s" % ", ".join(changed))
            print("ล้าง plaintext ใน config.bat เรียบร้อย · คีย์อยู่ที่ config.key")
            print("อย่าลบ config.key และอย่า commit ขึ้น git")
        else:
            print("ไม่มีอะไรต้องเข้ารหัส (อาจเข้ารหัสไว้แล้ว)")
        print()
        status()
    elif cmd == "newkey":
        if os.path.exists(KEYFILE):
            print("มี config.key อยู่แล้ว — ลบทิ้งก่อนถ้าจะสร้างใหม่")
            print("เตือน: ค่าที่เข้ารหัสด้วยคีย์เดิมจะถอดไม่ได้อีก")
            return 1
        load_key(create=True)
        print("สร้าง config.key ใหม่แล้ว")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
