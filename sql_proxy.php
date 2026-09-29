<?php
// ============================================================
//  PROXY : เบราว์เซอร์ -> ไฟล์นี้ (origin เดียวกัน ไม่มี CORS)
//                     -> BMS tunnel /api/sql (server-to-server ไม่มี CORS)
//
//  ทำไมต้องมี: header Authorization ทำให้เบราว์เซอร์ยิง preflight (OPTIONS)
//  ก่อน แต่ tunnel บางตัวไม่ตอบ Access-Control-Allow-Origin -> ถูกบล็อก
//  ไฟล์นี้รันมาพร้อม start.bat (php -S) อยู่แล้ว
//
//  รับ POST JSON: { url, key, sql, app }
//  ส่งกลับ: JSON ดิบจาก tunnel (สถานะเดิม) หรือ {MessageCode, Message} ถ้า proxy พลาด
// ============================================================

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Headers: Content-Type');
header('Access-Control-Allow-Methods: POST, OPTIONS');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

if ($_SERVER['REQUEST_METHOD'] === 'GET') {
  echo json_encode(['ok' => true, 'service' => 'BMS sql proxy',
                    'curl' => extension_loaded('curl'),
                    'hint' => 'POST {url,key,sql,app}'], JSON_UNESCAPED_UNICODE);
  exit;
}

$in  = json_decode(file_get_contents('php://input'), true);
if (!is_array($in)) {
  http_response_code(400);
  echo json_encode(['MessageCode' => 0, 'Message' => 'proxy: body ไม่ใช่ JSON'], JSON_UNESCAPED_UNICODE); exit;
}
$base = rtrim((string)($in['url'] ?? ''), '/');
$key  = (string)($in['key'] ?? '');
$sql  = (string)($in['sql'] ?? '');
$app  = (string)($in['app'] ?? 'BMS.Appointment.Autofill');

if ($base === '' || $sql === '') {
  http_response_code(400);
  echo json_encode(['MessageCode' => 0, 'Message' => 'proxy: ต้องมี url และ sql'], JSON_UNESCAPED_UNICODE); exit;
}
// ยอมเฉพาะ tunnel ของ hosxp เท่านั้น กันเอาไปใช้ยิงเว็บอื่น
$host = parse_url($base, PHP_URL_HOST);
if (!$host || !preg_match('/(^|\.)hosxp\.net$/i', $host)) {
  http_response_code(403);
  echo json_encode(['MessageCode' => 0, 'Message' => 'proxy: อนุญาตเฉพาะ *.hosxp.net'], JSON_UNESCAPED_UNICODE); exit;
}

$full = $base . '/api/sql?sql=' . rawurlencode($sql) . '&app=' . rawurlencode($app);

$ch = curl_init($full);
curl_setopt_array($ch, [
  CURLOPT_RETURNTRANSFER => true,
  CURLOPT_HTTPHEADER     => ['Authorization: Bearer ' . $key, 'Accept: application/json'],
  // 40 วิ ไม่ใช่ 20 — คิวรี่ของแดชบอร์ดตรวจนำเข้า lab หานัดคู่ทับวันที่มีนัด 2000+ ใบ
  // ซึ่งกินเวลาเกิน 20 วิได้ ถ้าตัดตรงนี้ก่อน เบราว์เซอร์จะเห็นเป็น 502 ทั้งที่ HOSxP ยังทำงานอยู่
  // (ต้องน้อยกว่า TIMEOUT ฝั่งเบราว์เซอร์ 45 วิ เพื่อให้ได้ข้อความ error จริงจาก tunnel)
  CURLOPT_TIMEOUT        => 40,
  CURLOPT_CONNECTTIMEOUT => 10,
  CURLOPT_FOLLOWLOCATION => true,
]);
$body = curl_exec($ch);
$code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err  = curl_error($ch);
curl_close($ch);

if ($body === false) {
  http_response_code(502);
  echo json_encode(['MessageCode' => 0, 'Message' => 'proxy ต่อ tunnel ไม่ได้: ' . $err], JSON_UNESCAPED_UNICODE); exit;
}

// tunnel ตอบไม่ใช่ JSON (เช่น 502 เปล่า ๆ จาก nginx) -> แปลงเป็นข้อความที่อ่านรู้เรื่อง
$trim = trim($body);
if ($trim === '' || ($trim[0] !== '{' && $trim[0] !== '[')) {
  $hint = ($code === 502 || $code === 504)
    ? 'tunnel ติดต่อ HOSxP ปลายทางไม่ได้ (เครื่อง รพ. ปิด / tunnel หลุด / session หมดอายุ)'
    : ($trim === '' ? 'tunnel ตอบเปล่า' : mb_substr(strip_tags($trim), 0, 200));
  http_response_code($code > 0 ? $code : 502);
  echo json_encode(['MessageCode' => $code, 'Message' => 'HTTP ' . $code . ' — ' . $hint], JSON_UNESCAPED_UNICODE);
  exit;
}

http_response_code($code > 0 ? $code : 200);
echo $body;   // ส่ง JSON ดิบจาก tunnel กลับไปตรง ๆ
