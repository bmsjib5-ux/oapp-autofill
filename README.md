# ตัวช่วยนัดหมายจากโน้ตแพทย์ (Appointment Autofill v3)

อ่านโน้ตแพทย์จาก HOSxP (เช่น `F/U 4 wk + CBC + Cr`) แล้วเติมฟอร์มนัดให้อัตโนมัติ ทั้งวันนัด แพทย์ คลินิก รายการ Lab/X-ray และช่องคิว (slot)
ยืนยันแล้วบันทึกลงคิวฐานกลาง จากนั้น `gateway` จะเขียนเข้าตาราง `oapp` ของ HOSxP

```
หน้าเว็บ ──► server.py (/save_appointment.php) ──► ฐานกลาง appointment_queue (MySQL)
   │                                                     │
   └─► server.py (/sql_proxy.php) ──► BMS Session API     └─► gateway.py ──► HOSxP oapp / lab_app_* / oapp_xray
```

## ไฟล์ในโปรเจกต์

| ไฟล์ | หน้าที่ |
|---|---|
| `appointment-autofill-v3.html` | หน้าหลัก: ทำนัดจากโน้ตแพทย์ |
| `appointment-center.html` | ทะเบียนนัด (ปุ่ม "ทะเบียนนัด" ลิงก์มาหน้านี้) |
| `slip.js` | สร้าง/พิมพ์ใบนัด (โลโก้และ QR ฝังในไฟล์แล้ว) |
| `server.py` | เสิร์ฟหน้าเว็บ และตอบ `/sql_proxy.php` + `/save_appointment.php` (ไม่ต้องมี PHP) |
| `gateway.py` | ส่งคิวจากฐานกลางเข้า HOSxP |
| `gateway_ui.py` | หน้าจอ monitor และตั้งค่า gateway |
| `bmscrypt.py` | เข้ารหัส/ถอดรหัส user/password ใน `config.bat` (AES-256-GCM) |
| `save_appointment.php`, `sql_proxy.php`, `bmscrypt.php` | ทางเลือก: ใช้แทน `server.py` เมื่อวางบนเว็บเซิร์ฟเวอร์ PHP |
| `start.bat` / `gateway.bat` / `gateway_ui.bat` | ตัวเปิดโปรแกรมบน Windows |
| `build.bat`, `dist_start.bat` | สร้างไฟล์ `.exe` ไว้ใช้บนเครื่องที่ไม่มี Python (ผลลัพธ์อยู่ในโฟลเดอร์ `dist/`) |
| `config.example.bat` | แม่แบบการตั้งค่า: คัดลอกเป็น `config.bat` แล้วใส่ค่าจริง |

## เริ่มใช้งาน

1. ติดตั้ง Python 3.10+ แล้วติดตั้งไลบรารี
   ```
   pip install -r requirements.txt
   ```
2. คัดลอก `config.example.bat` เป็น `config.bat` แล้วใส่ค่าฐานกลาง (MySQL) และฐาน HIS (HOSxP)
3. เข้ารหัส user/password เก็บลงช่อง `*_ENC` (ระบบจะสร้าง `config.key` ให้)
   ```
   python bmscrypt.py encrypt-config
   ```
   หรือตั้งผ่านหน้าจอ `gateway_ui.bat`
4. เปิดหน้าเว็บด้วย `start.bat` แล้วเข้า `http://localhost:8137/appointment-autofill-v3.html`
   ต้องเปิดผ่าน http เท่านั้น เปิดเป็นไฟล์ตรง ๆ (`file://`) จะเรียก API ไม่ได้
5. เชื่อมต่อ BMS Session จากหน้าเว็บ (ปุ่มตั้งค่า)
6. เปิด `gateway.bat` (หรือ `gateway_ui.bat`) เพื่อส่งคิวเข้า HOSxP
   ตรวจการเชื่อมต่อแบบอ่านอย่างเดียวได้ด้วย `python gateway.py status`

## ข้อควรระวัง

- **ห้าม commit `config.bat` และ `config.key`** (อยู่ใน `.gitignore` แล้ว) เพราะมีรหัสผ่านฐานข้อมูลและกุญแจถอดรหัส
- ข้อมูลที่ไหลผ่านระบบเป็นข้อมูลผู้ป่วย: อย่า commit log, ไฟล์ export หรือภาพหน้าจอที่มี HN/VN/ชื่อผู้ป่วย
- ก่อนเปิด `gateway` ให้ตรวจว่า `BMS_HIS_NAME` ชี้ไปฐานที่ตั้งใจ (ฐานทดสอบหรือฐานจริง)
