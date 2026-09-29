<?php
// ============================================================
//  ถอดรหัสค่าลับใน config.bat (คู่กับ bmscrypt.py)
//  AES-256-GCM · คีย์ 32 ไบต์ (base64) ในไฟล์ config.key
//  รูปแบบ: enc:v1:<base64(iv12 + ciphertext + tag16)>
// ============================================================

function bms_key() {
    static $k = null;
    if ($k !== null) return $k;
    $f = __DIR__ . '/config.key';
    if (!is_file($f)) throw new Exception('ไม่พบ config.key (รัน: python bmscrypt.py encrypt-config)');
    $raw = base64_decode(trim(file_get_contents($f)), true);
    if ($raw === false || strlen($raw) !== 32) throw new Exception('config.key ไม่ถูกต้อง');
    $k = $raw;
    return $k;
}

/** ถ้าไม่ได้ขึ้นต้นด้วย enc:v1: ให้ถือว่าเป็น plaintext (ใช้ค่าเดิมได้เลย) */
function bms_decrypt($v) {
    $p = 'enc:v1:';
    if ($v === null || $v === '' || $v === false) return '';
    $v = (string)$v;
    if (strncmp($v, $p, strlen($p)) !== 0) return $v;
    $blob = base64_decode(substr($v, strlen($p)), true);
    if ($blob === false || strlen($blob) < 12 + 16 + 1) throw new Exception('ค่าที่เข้ารหัสผิดรูปแบบ');
    $iv  = substr($blob, 0, 12);
    $tag = substr($blob, -16);
    $ct  = substr($blob, 12, strlen($blob) - 12 - 16);
    $out = openssl_decrypt($ct, 'aes-256-gcm', bms_key(), OPENSSL_RAW_DATA, $iv, $tag);
    if ($out === false) throw new Exception('ถอดรหัสไม่สำเร็จ (config.key ไม่ตรงกับค่าที่เข้ารหัสไว้?)');
    return $out;
}

/** อ่านค่าลับจาก env: ใช้ <name>_ENC ก่อน ถ้าไม่มีค่อยใช้ <name> (plaintext) */
function bms_env_secret($name) {
    $enc = getenv($name . '_ENC');
    if ($enc !== false && $enc !== '') return bms_decrypt($enc);
    $plain = getenv($name);
    return $plain === false ? '' : $plain;
}
