<?php
// ============================================================
//  API เชื่อมต่อ MySQL กลาง (สะพานจากเบราว์เซอร์ -> MySQL)
//  รันมาพร้อม start.bat (php -S) อัตโนมัติ
//
//  ค่าเชื่อมต่อ MySQL อ่านจาก 2 ที่ ตามลำดับ:
//    1) ตัวแปรที่ตั้งใน start.bat (BMS_DB_*)  <- แนะนำ ปลอดภัยกว่า
//       เบราว์เซอร์ไม่ต้องส่ง user/password มาเลย
//    2) ค่าที่ส่งมาจากเบราว์เซอร์ (ถ้าไม่ได้ตั้งใน start.bat)
//
//  ตาราง: id · hn · vn · datetime · status · json_row  (สร้างอัตโนมัติ)
// ============================================================

require_once __DIR__ . '/bmscrypt.php';   // ถอดรหัส user/pass จาก config.bat (<NAME>_ENC)

// ---- CORS ----
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Headers: Content-Type, Authorization');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Content-Type: application/json; charset=utf-8');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

// ---- อ่านค่าเชื่อมต่อจาก start.bat (env) ----
$ENV_DB   = getenv('BMS_DB_NAME');
$USE_ENV  = ($ENV_DB !== false && $ENV_DB !== '');

// ---- GET = หน้าตรวจสถานะ (เปิด URL นี้ในเบราว์เซอร์ได้) ----
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
  echo json_encode([
    'ok'         => true,
    'service'    => 'BMS MySQL API (save_appointment)',
    'form_template' => true,   // รองรับ action tpl_get/tpl_save (client เช็คก่อนยิง จะได้ไม่ INSERT แถวเปล่าลงรุ่นเก่า)
    'php'        => PHP_VERSION,
    'pdo_mysql'  => extension_loaded('pdo_mysql'),
    'config_from'=> $USE_ENV ? 'config.bat (env)' : 'browser (ยังไม่ได้ตั้งใน config.bat)',
    'secrets'    => (getenv('BMS_DB_PASS_ENC') ? 'เข้ารหัส (config.key)' : 'plaintext'),
    'db'         => $USE_ENV ? (bms_env_secret('BMS_DB_USER') . '@' . (getenv('BMS_DB_HOST') ?: '127.0.0.1')
                                . ':' . (getenv('BMS_DB_PORT') ?: '3306') . '/' . $ENV_DB) : null,
    'table'      => $USE_ENV ? (getenv('BMS_DB_TABLE') ?: 'appointment_queue') : null,
    'hint'       => 'POST JSON {row:{hn,vn,ref_id,datetime,status,json_row}} เพื่อบันทึกนัด · '
                  . '{action:"list",ref_ids:[...]} เพื่อดูสถานะรายการในคิว'
  ], JSON_UNESCAPED_UNICODE);
  exit;
}

// ---- (ทางเลือก) ตรวจ API key : ตั้ง BMS_API_KEY ใน start.bat เพื่อเปิดใช้ ----
$EXPECT_KEY = bms_env_secret('BMS_API_KEY');
if ($EXPECT_KEY !== '' ) {
  $auth = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
  if ($auth !== 'Bearer ' . $EXPECT_KEY) {
    http_response_code(401);
    echo json_encode(['ok' => false, 'message' => 'unauthorized'], JSON_UNESCAPED_UNICODE); exit;
  }
}

// ---- อ่าน body ----
$raw = file_get_contents('php://input');
$in  = json_decode($raw, true);
if (!is_array($in)) {
  http_response_code(400);
  echo json_encode([
    'ok' => false,
    'message' => 'อ่าน JSON จาก body ไม่ได้ (ได้ ' . strlen($raw) . ' bytes, method=' . $_SERVER['REQUEST_METHOD']
      . ', content-type=' . ($_SERVER['CONTENT_TYPE'] ?? 'none') . ', json_error=' . json_last_error_msg() . ')'
  ], JSON_UNESCAPED_UNICODE);
  exit;
}
$row    = $in['row']    ?? [];
$cfg    = $in['mysql']  ?? [];
$action = $in['action'] ?? '';

// ---- เลือกค่าเชื่อมต่อ: start.bat (env) มาก่อน ----
if ($USE_ENV) {
  $HOST  = getenv('BMS_DB_HOST') ?: '127.0.0.1';
  $PORT  = (int)(getenv('BMS_DB_PORT') ?: 3306);
  $DB    = $ENV_DB;
  $USER  = bms_env_secret('BMS_DB_USER');   // รองรับทั้ง _ENC (เข้ารหัส) และ plaintext
  $PASS  = bms_env_secret('BMS_DB_PASS');
  $TABLE = getenv('BMS_DB_TABLE') ?: 'appointment_queue';
} else {
  $HOST  = $cfg['host']     ?? '127.0.0.1';
  $PORT  = (int)($cfg['port'] ?? 3306);
  $DB    = $cfg['database'] ?? '';
  $USER  = $cfg['user']     ?? '';
  $PASS  = $cfg['password'] ?? '';
  $TABLE = $cfg['table']    ?? 'appointment_queue';
}
$TABLE = preg_replace('/[^A-Za-z0-9_]/', '', (string)$TABLE);
if ($TABLE === '') { $TABLE = 'appointment_queue'; }

if ($DB === '') {
  http_response_code(400);
  echo json_encode(['ok'=>false,'message'=>'ยังไม่ได้ตั้งชื่อฐานข้อมูล — ตั้ง BMS_DB_NAME ใน start.bat หรือกรอกในหน้า ตั้งค่า'], JSON_UNESCAPED_UNICODE);
  exit;
}

try {
  $pdo = new PDO(
    "mysql:host=$HOST;port=$PORT;dbname=$DB;charset=utf8mb4",
    $USER, $PASS,
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
  );
  // บังคับ charset การเชื่อมต่อเป็น utf8mb4 — DSN charset ไม่ทำงานบน driver/MariaDB เก่า
  // (ถ้าไม่ตั้ง ไบต์ UTF-8 ไทยจะถูกตีความเป็น cp874/TIS-620 แล้วเพี้ยน)
  $pdo->exec("SET NAMES utf8mb4");

  // ตรวจว่ามีตารางอยู่แล้วไหม (ก่อนสร้าง)
  $existed = false;
  try { $pdo->query("SELECT 1 FROM `$TABLE` LIMIT 1"); $existed = true; }
  catch (Throwable $e) { $existed = false; }

  // สร้างตารางอัตโนมัติถ้ายังไม่มี
  // ref_id = กุญแจอ้างกลับไปของเดิมใน HIS (หน้าทะเบียนนัดใส่ oapp_id ของนัดที่มีอยู่แล้ว)
  $pdo->exec("CREATE TABLE IF NOT EXISTS `$TABLE` (
    id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    hn       VARCHAR(20),
    vn       VARCHAR(20),
    doctor_code VARCHAR(15),
    doctor_note TEXT,
    ref_id   VARCHAR(20),
    datetime DATETIME,
    status   VARCHAR(20),
    source_type VARCHAR(40),
    json_row LONGTEXT,
    INDEX(hn), INDEX(vn), INDEX(ref_id), INDEX(source_type)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4");

  // ตารางเก่าที่สร้างไว้ก่อนมีคอลัมน์นี้ -> เติมให้เอง ไม่งั้น INSERT พัง
  foreach ([['doctor_code','VARCHAR(15)'],['doctor_note','TEXT'],['ref_id','VARCHAR(20)'],['source_type','VARCHAR(40)']] as $c) {
    try { $pdo->query("SELECT `{$c[0]}` FROM `$TABLE` LIMIT 1"); }
    catch (Throwable $e) { try { $pdo->exec("ALTER TABLE `$TABLE` ADD COLUMN `{$c[0]}` {$c[1]}"); } catch (Throwable $e2) {} }
  }

  // ปุ่ม "สร้างตาราง"
  if ($action === 'create_table') {
    echo json_encode(['ok'=>true,'created'=>!$existed,'table'=>$TABLE,'db'=>$DB], JSON_UNESCAPED_UNICODE); exit;
  }

  // หน้าทะเบียนนัดถามว่า "รายการไหนส่งเข้าคิวไปแล้ว" — คืนคอลัมน์สั้น ๆ ไม่รวม json_row
  if ($action === 'list') {
    $refs = [];
    foreach ((array)($in['ref_ids'] ?? []) as $x) {
      $x = trim((string)$x);
      if ($x !== '') { $refs[] = substr($x, 0, 20); }
      if (count($refs) >= 500) break;
    }
    $hn    = trim((string)($in['hn'] ?? ''));
    $limit = (int)($in['limit'] ?? 500);
    if ($limit < 1 || $limit > 2000) { $limit = 500; }

    $where = []; $params = [];
    if ($refs) {
      $where[]  = 'ref_id IN (' . implode(',', array_fill(0, count($refs), '?')) . ')';
      $params   = array_merge($params, $refs);
    }
    if ($hn !== '') { $where[] = 'hn = ?'; $params[] = $hn; }
    $sql = "SELECT id, hn, vn, ref_id, status, datetime FROM `$TABLE`"
         . ($where ? ' WHERE ' . implode(' AND ', $where) : '')
         . " ORDER BY id DESC LIMIT $limit";
    $st = $pdo->prepare($sql);
    $st->execute($params);
    echo json_encode(['ok'=>true,'rows'=>$st->fetchAll(PDO::FETCH_ASSOC),'table'=>$TABLE], JSON_UNESCAPED_UNICODE);
    exit;
  }
  // ---- แม่แบบฟอร์ม (form template) — เก็บต่อ "login-user" บนเซิร์ฟเวอร์ ----
  //   pref_get {login, key}        -> คืน data (object/array) ของค่าตั้งนั้น
  //   pref_set {login, key, data}  -> เขียนทับ (1 แถวต่อ login+key)
  //   tpl_get/tpl_save = alias เดิมของ Form Template (key = form_tpl, data เป็น array เสมอ)
  //   ใช้เก็บ: form_tpl (แม่แบบฟอร์ม) · remember (ปุ่มจำค่าตัวเลือก) · draw_point (จุดเจาะเลือด)
  if ($action === 'pref_get' || $action === 'pref_set' || $action === 'tpl_get' || $action === 'tpl_save') {
    $pdo->exec("CREATE TABLE IF NOT EXISTS `appt_user_pref` (
      login      VARCHAR(64) NOT NULL,
      pref_key   VARCHAR(40) NOT NULL,
      data       LONGTEXT,
      updated_at DATETIME,
      PRIMARY KEY (login, pref_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4");
    $login = substr(trim((string)($in['login'] ?? '')), 0, 64);
    $isTpl = ($action === 'tpl_get' || $action === 'tpl_save');
    $isGet = ($action === 'pref_get' || $action === 'tpl_get');
    $key   = $isTpl ? 'form_tpl' : substr(trim((string)($in['key'] ?? '')), 0, 40);
    if ($login === '' || $key === '') {
      http_response_code(400);
      echo json_encode(['ok'=>false,'message'=>'ไม่มี login/key (จำค่าตามผู้ใช้ไม่ได้)'], JSON_UNESCAPED_UNICODE); exit;
    }
    if ($isGet) {
      $st = $pdo->prepare("SELECT data, updated_at FROM `appt_user_pref` WHERE login = ? AND pref_key = ?");
      $st->execute([$login, $key]);
      $r = $st->fetch(PDO::FETCH_ASSOC);
      $data = $isTpl ? [] : null;
      if ($r && $r['data'] !== null && $r['data'] !== '') {
        $tmp = json_decode($r['data'], true);
        if ($tmp !== null) { $data = $tmp; }
      }
      if ($isTpl && !is_array($data)) { $data = []; }        // form_tpl ต้องเป็น array เสมอ
      echo json_encode(['ok'=>true,'login'=>$login,'key'=>$key,'data'=>$data,'updated_at'=>($r['updated_at'] ?? null)], JSON_UNESCAPED_UNICODE);
      exit;
    }
    // set
    $data = array_key_exists('data', $in) ? $in['data'] : null;
    if ($isTpl && !is_array($data)) { $data = []; }
    $json = json_encode($isTpl ? array_values($data) : $data, JSON_UNESCAPED_UNICODE);
    if (strlen($json) > 200000) {
      http_response_code(400);
      echo json_encode(['ok'=>false,'message'=>'ข้อมูลค่าตั้งใหญ่เกินไป'], JSON_UNESCAPED_UNICODE); exit;
    }
    $st = $pdo->prepare("INSERT INTO `appt_user_pref` (login, pref_key, data, updated_at) VALUES (?, ?, ?, NOW())
                         ON DUPLICATE KEY UPDATE data = VALUES(data), updated_at = VALUES(updated_at)");
    $st->execute([$login, $key, $json]);
    echo json_encode(['ok'=>true,'login'=>$login,'key'=>$key], JSON_UNESCAPED_UNICODE);
    exit;
  }

  // ปุ่ม "ทดสอบ" (status=ping) -> ไม่ insert
  if (($row['status'] ?? '') === 'ping') {
    echo json_encode(['ok'=>true,'ping'=>true,'table'=>$TABLE,'db'=>$DB,'table_created'=>!$existed], JSON_UNESCAPED_UNICODE); exit;
  }

  // กันแถวว่าง: คำขอที่ไม่มี 'row' (เช่น action ที่ PHP รุ่นนี้ไม่รู้จัก หลุดมาถึง INSERT) จะกลายเป็นแถวเปล่า
  //            status default = 'pending' -> gateway หยิบไปแล้ว error ทิ้งไว้ จึงบล็อกตรงนี้ ไม่ให้เข้าคิว
  $HN_ins = trim((string)($row['hn'] ?? ''));
  $JR_ins = trim((string)($row['json_row'] ?? ''));
  if ($HN_ins === '' || $JR_ins === '' || $JR_ins === 'null') {
    http_response_code(400);
    echo json_encode(['ok'=>false,'message'=>'บล็อก: ไม่มี hn/json_row — ไม่บันทึกแถวว่างเข้าคิว'
      . ($action ? ' (action=' . $action . ' ไม่รองรับใน save_appointment.php รุ่นนี้ — อัปเดตไฟล์ที่เซิร์ฟเวอร์)' : '')],
      JSON_UNESCAPED_UNICODE);
    exit;
  }

  $st = $pdo->prepare("INSERT INTO `$TABLE` (hn, vn, doctor_code, doctor_note, ref_id, datetime, status, source_type, json_row) VALUES (?,?,?,?,?,?,?,?,?)");
  $st->execute([
    $row['hn']       ?? '',
    $row['vn']       ?? '',
    $row['doctor_code'] ?? '',
    $row['doctor_note'] ?? '',
    substr((string)($row['ref_id'] ?? ''), 0, 20),
    $row['datetime'] ?? date('Y-m-d H:i:s'),
    $row['status']   ?? 'pending',
    substr((string)($row['source_type'] ?? ''), 0, 40),
    $row['json_row'] ?? ''
  ]);

  echo json_encode(['ok' => true, 'id' => (int)$pdo->lastInsertId()], JSON_UNESCAPED_UNICODE);
} catch (Throwable $e) {
  http_response_code(500);
  echo json_encode(['ok' => false, 'message' => $e->getMessage()], JSON_UNESCAPED_UNICODE);
}
