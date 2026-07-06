# Lane_Check_Status

ระบบเฝ้าระวังสถานะเครื่อง **Ubuntu** แบบครบวงจร: เก็บ CPU / RAM / Disk / S.M.A.R.T. และกรอง log ของโปรแกรมที่รันอยู่ → ส่งเป็น JSON เข้า **RabbitMQ** → ฝั่ง **Server** รับแล้วเก็บลง **MySQL** (แยกตารางตามฟังก์ชัน)

ออกแบบให้ **ไม่ทำข้อมูลหาย** (spool + sweeper ฝั่งส่ง, requeue + manual import ฝั่งรับ) และ **กระทบโปรแกรมเดิมบนเครื่องน้อยที่สุด** (อ่าน log แบบ read-only, nice/ionice, อ่าน SMART ห่างๆ)

---

## สถาปัตยกรรม

```
  [Ubuntu hosts]                         [Message bus]            [Server]
  ┌───────────────────┐                                          ┌────────────────────────┐
  │ Lane_Check_Status │  JSON  ─────►   RabbitMQ  ─────►  Lane_Check_Server             │
  │ (collector/agent) │                (system_status)          │  - เก็บลง MySQL (แยกตาราง) │
  │  - CPU/RAM/Disk    │                                         │  - เก็บไฟล์ JSON ที่รับ    │
  │  - SMART ทุก disk  │   MQ ล่ม → spool/ + sweeper ส่งซ้ำ        │  - Manual import (กันMQพัง)│
  │  - filter log      │                                         └────────────┬───────────┘
  └───────────────────┘                                                       ▼
                                                                          [ MySQL ]
       (ตัวเลือก) Lane_Check_Consumer = client ไว้ debug/ดู summary + เซฟไฟล์
```

---

## องค์ประกอบ

| โปรแกรม | ไฟล์ | บทบาท |
|---------|------|-------|
| **Collector** (agent) | `main.py` | รันบนทุกเครื่อง Ubuntu — เก็บ metric + log → ส่ง RabbitMQ |
| **Server** | `Server/server_consumer.py` | รับจาก RabbitMQ → เก็บลง MySQL + เก็บไฟล์ + manual import |
| **Consumer** (client/debug) | `consumer.py` | รับจาก RabbitMQ → log summary + เซฟไฟล์ (ไว้ debug) |

### โมดูลย่อย (collector)
`system_metrics.py` (CPU/RAM/Disk) · `smart_collector.py` (smartmontools) · `raid_collector.py` (dmraid -n) · `service_collector.py` (systemd units + เช็ค process ตามชื่อ) · `ping_collector.py` (ping + rtt/latency) · `usb_collector.py` (เช็คอุปกรณ์ USB ตาม VID:PID) · `http_collector.py` (curl หน้า status + ดึง field ตาม regex) · `log_collector.py` (tail+filter+date-token) · `mq_publisher.py` (publish + spool + sweeper) · `json_archive.py` (เก็บไฟล์ JSON รายวัน+zip) · `LogLibrary.py` (config + logging + เข้ารหัส secret)

### โมดูลย่อย (server)
`db_mysql.py` (schema + แตก payload ลงตาราง + purge ข้อมูลเก่า) · `db_cleanup.py` (thread ลบข้อมูลเกิน retention เป็นระยะ) · `json_archive.py` (เก็บไฟล์ที่รับ) · `manual_import.py` (กวาดโฟลเดอร์ → MySQL) · `LogLibrary.py`

---

## สิ่งที่เก็บ (payload JSON)

ดูตัวอย่างเต็มใน [`sample_output.json`](sample_output.json) — คีย์หลัก:
`program, version, hostname, timestamp_utc, timestamp_epoch, os, cpu, memory, disk_usage[], smart[], smart_collected_at, raid, raid_collected_at, services, services_collected_at, ping[], ping_collected_at, usb[], usb_collected_at, http_probe[], http_probe_collected_at, program_logs[]`

- **CPU**: percent รวม + รายคอร์ + load average
- **memory**: RAM/swap หน่วย **กิโลไบต์** (`*_kb`) + percent
- **disk_usage[]**: ต่อ path (ตั้งได้หลาย path)
- **smart[]**: ทุก disk — model/serial/health/temp/power-on-hours + **attributes ทุกตัว**
- **raid**: ผล `dmraid -n` (ATARAID/fakeRAID/BIOS RAID) — `available`, `raid_detected`, `command`, `returncode`, `output[]` (เก็บห่างๆ แบบ SMART; ต้องติดตั้ง `dmraid`) — `dmraid -n` ดัมป์ native metadata ดิบที่อาจมี byte ที่ไม่ใช่ UTF-8 (เช่น `0xb0`) → decode แบบ binary-safe (`errors="replace"`) byte ที่ถอดไม่ได้จะกลายเป็น `�` ไม่ทำให้รอบเก็บข้อมูลล้ม
- **services**: สุขภาพของ service/process — `systemd[]` (เช็คด้วย `systemctl show`: `active_state`/`sub_state`/`main_pid`/`ok`) และ `processes[]` (เช็คจากตาราง process ตามชื่อ/cmdline: `running`/`count`/`pids`/`rss_kb`/`vms_kb`/`num_threads`/`uptime_seconds`/`ok`) — ทุก entry มีฟิลด์ `ok` ไว้แจ้งเตือนเร็ว; `vms_kb` + `num_threads` ที่โตเรื่อยๆ ใช้จับ memory/thread leak ได้
- **ping[]**: ผล ping แต่ละ host ตาม config — `hostname` (ชื่อเรียก), `address`, `reachable`, **`rtt_ms`** (round-trip time / latency หน่วย ms), `packet_loss_percent`, `ok` (= ตอบกลับได้อย่างน้อย 1 แพ็กเก็ต) — ใช้จับ network ช้า/ขาดไปยัง upstream
- **usb[]**: เช็คว่าอุปกรณ์ USB ที่ควรมีถูกต่ออยู่จริงไหม — จับคู่ด้วย **Vendor:Product ID** (`vendor_id`/`product_id`), รายงาน `present`/`count`/`manufacturer`/`product`/`serial`/`ok` (enumerate ด้วย `usb-devices` → fallback `lsusb`)
- **http_probe[]**: ยิง HTTP GET (แบบ curl) ไปยัง endpoint ที่กำหนด แล้ว **ดึง field ตาม regex** (เช่น `serial_number`, `mac` จากหน้า status ของ Q-Free RSE) — รายงาน `url`/`status_code`/`response_ms`/`fields{}` (คีย์ตาม config)/`ok`; field ที่ regex ไม่ match อยู่ใน `fields_missing`
- **program_logs[]**: log ของแต่ละโปรแกรม (กรองด้วย include/exclude regex), รองรับ date-token ในชื่อ path

---

## ติดตั้ง & Build

ต้องมี Python 3.8+ (build บนเครื่องสถาปัตยกรรมเดียวกับเป้าหมาย เช่น x86_64 Linux)

### Collector
```bash
./build.sh                      # สร้าง venv, pip install, pyinstaller --onefile
# ได้ dist/Lane_Check_Status
sudo ./install_service.sh       # ติดตั้งเป็น systemd service (/opt/lane_check_status)
```

### Server
```bash
cd Server
./build.sh                      # ได้ dist/Lane_Check_Server (ใช้ PyMySQL)
sudo ./install_service.sh       # ติดตั้ง (/opt/lane_check_server)
```

ต้องติดตั้ง smartmontools บนเครื่อง collector: `sudo apt install smartmontools` (อ่าน SMART ต้องสิทธิ์ root → service รันเป็น root) — และติดตั้ง `dmraid` หากต้องการเก็บ RAID metadata: `sudo apt install dmraid` (ถ้าไม่มี `dmraid` ฟิลด์ `raid` จะรายงาน `available=false` เฉยๆ ไม่ error)

---

## การตั้งค่า (Config)

ไฟล์ config ตัวจริง (`*_config.json`) **ไม่ถูก commit** (มี secret) — ให้ก๊อปจาก template:

```bash
cp Lane_Check_Status_config.template.json Lane_Check_Status_config.json
cp Server/Lane_Check_Server_config.template.json Server/Lane_Check_Server_config.json
# แก้ค่า Host / Password / Disk_Paths / Programs ฯลฯ
```

- รันครั้งแรก: ถ้าไม่มีไฟล์ config โปรแกรมจะสร้างให้เองข้าง binary
- คีย์ที่มีคำว่า **`pass`** (เช่น `Password`) ถูก **เข้ารหัสบนดิสก์อัตโนมัติ** หลังรันครั้งแรก (กลายเป็น `ENC:...`)
  - ต้องตั้ง `LOGLIB_KEY` (โปรแกรมพิมพ์ออกมา 1 ครั้ง) ไว้ใน env / `*.env` เพื่อให้ decrypt รอบถัดไปได้

### date-token ในชื่อ log path
`Log_Path` รองรับ token วันที่ แทนค่าตามวันปัจจุบันทุกรอบ:
`yyyy`(ปี4) `yy`(ปี2) `mm`(เดือน) `dd`(วัน) `HH`(ชม.) `MM`(นาที) `SS`(วินาที)
> เช่น `/tct/yyyy-mm/tct_app_ddmmyy.log` → `/tct/2026-06/tct_app_230626.log`
> ⚠️ `mm` = เดือน (พิมพ์เล็ก), `MM` = นาที (พิมพ์ใหญ่)

### กรอง log (สำคัญ)
- `Include_Patterns` = เก็บบรรทัดที่ match (ว่าง = เก็บทุกบรรทัด)
- `Exclude_Patterns` = ตัดบรรทัดที่ match (ใช้กรอง noise)
- **ห้ามใส่ค่าเดียวกันทั้งสองฝั่ง** (exclude ถูกเช็คก่อน → จะตัดทิ้งหมด) — โปรแกรมจะ warn ให้

### ตรวจสอบ Service / Process (`Services`)
ตรวจสุขภาพได้ 2 แบบ (เปิด/ปิดอิสระ ใส่เท่าที่ต้องการ):

```jsonc
"Services": {
    "Enable": 1,
    "Systemctl_Path": "systemctl",
    "Interval_Cycles": 1,          // เช็คทุกกี่รอบ (service flap → ควรเช็คถี่; default ทุกรอบ)
    "Timeout_Seconds": 10,
    "Systemd_Units": ["rabbitmq-server.service", "nginx.service"],
    "Processes": [
        { "Name": "tps",     "Pattern": "bangkoktps.linux" },
        { "Name": "tct_app", "Pattern": "TCT_App.exe" }
    ]
}
```

- **`Systemd_Units`** — เช็คด้วย `systemctl show <unit>` → healthy เมื่อ `ActiveState=active` (ใช้ได้ถ้า container รัน systemd ของตัวเอง; ถ้าไม่มี `systemctl` จะรายงาน `ok=false` ไม่ error)
- **`Processes`** — สแกนตาราง process แล้ว match `Pattern` กับ **ชื่อ process หรือ command line** (case-insensitive substring) → เหมาะกับงานที่ **ไม่ได้คุมด้วย systemd** (เช่น binary ที่ถูกรันจาก app/shell, mono/.NET) → healthy เมื่อมีอย่างน้อย 1 instance
  - ⚠️ ใช้ `Pattern` ที่เจาะจงพอ (เช่น `bangkoktps.linux` ไม่ใช่ `tps`) เพราะ match แบบ substring บน cmdline อาจชนกับ process อื่นที่มีสตริงนั้นใน arguments

### ตรวจสอบ Ping / latency (`Ping`)
ping แต่ละ host แล้วบันทึก **เวลาไป-กลับ (`rtt_ms`)** + packet loss:

```jsonc
"Ping": {
    "Enable": 1,
    "Interval_Cycles": 1,      // ping ทุกกี่รอบ (1 = ทุกรอบ; latency ควรเก็บถี่)
    "Count": 1,                // จำนวน echo ต่อ host (>1 → rtt_ms เป็นค่าเฉลี่ย)
    "Timeout_Seconds": 2,      // timeout ต่อแพ็กเก็ต
    "Hosts": [
        { "Hostname": "Gateway",    "Address": "192.168.1.1" },
        { "Hostname": "Google DNS", "Address": "8.8.8.8" }
    ]
}
```

- `Hostname` = ชื่อเรียก (label), `Address` = ปลายทางที่ ping จริง (IP/hostname)
- healthy (`ok`) เมื่อได้รับ reply กลับอย่างน้อย 1 แพ็กเก็ต; ถ้า `unreachable` → `rtt_ms=null`, `packet_loss_percent=100`
- ต้องมีคำสั่ง `ping` บนเครื่อง (มากับ `iputils-ping` โดย default บน Ubuntu)

### ตรวจสอบอุปกรณ์ USB (`USB`)
เช็คว่าอุปกรณ์ USB ที่ควรมี (เครื่องอ่าน NFC, เครื่องพิมพ์, จอสัมผัส ฯลฯ) ถูกต่ออยู่จริงไหม จับคู่ด้วย **Vendor:Product ID**:

```jsonc
"USB": {
    "Enable": 1,
    "Command": "usb-devices",  // เครื่องมือ enumerate (fallback เป็น lsusb)
    "Interval_Cycles": 5,      // topology แทบไม่เปลี่ยน → เช็คห่างๆ + cache ระหว่างรอบ
    "Timeout_Seconds": 10,
    "Devices": [
        { "Name": "NFC Reader (SL600)",   "VendorID": "0471", "ProductID": "a112" },
        { "Name": "Printer (TM-T88VI)",   "VendorID": "04b8", "ProductID": "0202" }
    ]
}
```

- หา `VendorID`/`ProductID` (เลขฐาน 16 4 หลัก) ได้จากคำสั่ง **`usb-devices`** (บรรทัด `P: Vendor=.. ProdID=..`) หรือ `lsusb` (`ID xxxx:yyyy`)
- ใส่ `SerialNumber` เพิ่มได้เพื่อเจาะจงตัวเครื่อง (กรณีมีอุปกรณ์ VID:PID ซ้ำกันหลายตัว)
- healthy (`ok`) เมื่อพบอุปกรณ์อย่างน้อย 1 ตัว; ต้องติดตั้ง `usbutils`: `sudo apt install usbutils`

### ดึงข้อมูลผ่าน HTTP / curl (`Http_Probe`)
ยิง HTTP GET ไปยังหน้า status ของอุปกรณ์ (เช่น Q-Free RSE `10.0.0.15:1337`) แล้ว **ดึง field ตาม regex** — เอา `serial_number`, `MAC` ฯลฯ ออกมาได้:

```jsonc
"Http_Probe": {
    "Enable": 1,
    "Interval_Cycles": 15,     // ข้อมูล identity นิ่ง → probe ห่างๆ + cache
    "Timeout_Seconds": 10,
    "Endpoints": [
        {
            "Name": "RSE651 mra242",
            "URL": "10.0.0.15:1337",     // ไม่ใส่ scheme ก็ได้ → เติม http:// ให้อัตโนมัติ
            "Extract": {
                "serial_number": "Serialnumber:\\s*([^\\s<]+)",
                "mac": "MAC:\\s*([0-9A-Fa-f:]+)",
                "host": "Host:\\s*([^\\s<]+)"
            }
        }
    ]
}
```

- **generic** — `Extract` = map `ชื่อ field → regex` (ใช้ **capture group แรก** เป็นค่า, case-insensitive) ใช้กับอุปกรณ์อื่นได้แค่เปลี่ยน regex
- ใช้ `urllib` (stdlib) ไม่ต้องมี `curl`/`requests` บนเครื่อง
- `ok` = ได้ status 2xx; field ที่ดึงไม่ได้จะอยู่ใน `fields_missing` (ไม่ทำให้ `ok=false` — เผื่อหน้าเว็บเปลี่ยน layout จะได้เห็น) ; non-2xx/timeout → `ok=false` + `error`
- ⚠️ regex ในไฟล์ JSON ต้อง escape backslash เป็น `\\` (เช่น `\\s`)

---

## กลไกกันข้อมูลหาย

| จุด | กลไก |
|-----|------|
| Collector ส่ง MQ ไม่ได้ | เขียน `spool/msg_*.json` + **sweeper thread** กวาดส่งใหม่ทุก `Flush_Interval` วินาที |
| Server เขียน DB ไม่ได้ | **nack + requeue** ข้อความกลับคิว (at-least-once) |
| MQ พังยาว | ก๊อปไฟล์ `spool/` หรือ `output/*.zip` ไปวางที่ `Server/manual/` → server ดูดเข้า MySQL เอง |
| ส่งซ้ำ | DB ใช้ `INSERT ... ON DUPLICATE KEY UPDATE` (idempotent ด้วย PK timestamp+hostname) |

---

## ฐานข้อมูล MySQL (Server)

แยกตารางตามฟังก์ชัน ทุกตาราง PK `(timestamp_utc, hostname)` (ตารางหลายแถวต่อ host เพิ่ม discriminator) — schema สร้างเองตอนรัน หรือดู [`Server/schema.sql`](Server/schema.sql)

```
host · cpu · memory · disk_usage(+path) · smart(+device)
smart_attributes(+device,attr_id) · raid · services_systemd(+unit) · services_process(+name)
ping(+address) · usb(+name) · http_probe(+name) · program_logs(+name) · program_log_lines(+program_name,line_no)
```

เตรียม DB:
```sql
CREATE DATABASE lane_check CHARACTER SET utf8mb4;
CREATE USER 'lane_check'@'%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON lane_check.* TO 'lane_check'@'%';
FLUSH PRIVILEGES;
```

### ลบข้อมูลเก่าอัตโนมัติ (DB retention)
Server มี thread เบื้องหลัง (`db_cleanup.py`) คอยลบแถวที่ `timestamp_utc` เก่าเกิน `Retention_Days` ออกจาก **ทุกตาราง** เพื่อไม่ให้ DB โตไม่จำกัด (ค่า default = 30 วัน)

```jsonc
"Retention": {
    "Enable": 1,
    "Retention_Days": 30,          // ลบแถวที่เก่ากว่านี้ (0 = เก็บถาวร ไม่ลบ)
    "Cleanup_Interval_Hours": 24,  // รอบการกวาดลบ (ทุกกี่ชั่วโมง)
    "Run_On_Startup": 1            // 1 = กวาดครั้งแรกทันทีตอนสตาร์ท แล้วค่อยวนตาม interval
}
```

- ทำงานด้วย connection MySQL แยกของตัวเอง (ไม่ชนกับ consumer/manual import) — ถ้า MySQL ล่มชั่วคราวจะข้ามรอบนั้นแล้วลองใหม่รอบถัดไป ไม่ทำให้ server ล้ม
- ยึด `timestamp_utc` (เวลาที่ collector เก็บข้อมูล เป็น UTC) เป็นเกณฑ์ — สอดคล้องกับ retention ของไฟล์ JSON (`Received_Files.Retention_Days`)

---

## RabbitMQ — หมายเหตุ routing

- ใช้ queue เดียวกัน `system_status` ทั้ง collector / server / consumer
- ถ้า `Exchange=""` (default exchange) RabbitMQ route ตาม **ชื่อ queue** → publisher ส่งด้วย routing_key = ชื่อ Queue (จัดการให้แล้วใน `mq_publisher`)
- รัน **server หลายตัว** บน queue เดียว = แชร์โหลดอัตโนมัติ
- อย่ารัน **consumer (debug) พร้อม server** บน queue เดียวกันถ้าต้องการให้ทั้งคู่ได้ครบ (RabbitMQ จะ round-robin) — ถ้าต้องการให้ใช้ fanout + queue แยก

---

## รัน / ตรวจสอบ

```bash
sudo systemctl status lane_check_status      # collector
sudo systemctl status lane_check_server      # server
sudo journalctl -u lane_check_status -f      # ดู log realtime
```

log ละเอียดอยู่ใน `logs/` (rotate ตามขนาด + เก็บตาม `log_Backup` วัน), dump ข้อมูลที่เก็บได้ที่ระดับ DEBUG

---

## โครงสร้างโปรเจกต์

```
Lane_Check_Status/
├── main.py, system_metrics.py, smart_collector.py, raid_collector.py
├── service_collector.py, ping_collector.py, usb_collector.py, http_collector.py, log_collector.py
├── mq_publisher.py, json_archive.py, consumer.py, LogLibrary.py
├── *_config.template.json            # คัดลอกเป็น *_config.json (ของจริงไม่ commit)
├── build.sh, install_service.sh, *.spec, *.service, requirements.txt
├── sample_output.json, MEMORY.md, README.md
└── Server/
    ├── server_consumer.py, db_mysql.py, db_cleanup.py, manual_import.py, json_archive.py
    ├── seed_demo_data.py            # dev util: จำลองข้อมูลลง MySQL สำหรับทำ Dashboard
    ├── schema.sql, build.sh, install_service.sh, *.service, requirements.txt
    └── Lane_Check_Server_config.template.json
```

รายละเอียดเชิงลึก/บันทึกการตัดสินใจ ดูที่ [`MEMORY.md`](MEMORY.md)

---

## License / ผู้พัฒนา
ภายในองค์กร — ปรับแก้ตามการใช้งานได้
