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
`system_metrics.py` (CPU/RAM/Disk) · `smart_collector.py` (smartmontools) · `log_collector.py` (tail+filter+date-token) · `mq_publisher.py` (publish + spool + sweeper) · `json_archive.py` (เก็บไฟล์ JSON รายวัน+zip) · `LogLibrary.py` (config + logging + เข้ารหัส secret)

### โมดูลย่อย (server)
`db_mysql.py` (schema + แตก payload ลงตาราง) · `json_archive.py` (เก็บไฟล์ที่รับ) · `manual_import.py` (กวาดโฟลเดอร์ → MySQL) · `LogLibrary.py`

---

## สิ่งที่เก็บ (payload JSON)

ดูตัวอย่างเต็มใน [`sample_output.json`](sample_output.json) — คีย์หลัก:
`program, version, hostname, timestamp_utc, timestamp_epoch, os, cpu, memory, disk_usage[], smart[], smart_collected_at, program_logs[]`

- **CPU**: percent รวม + รายคอร์ + load average
- **memory**: RAM/swap หน่วย **กิโลไบต์** (`*_kb`) + percent
- **disk_usage[]**: ต่อ path (ตั้งได้หลาย path)
- **smart[]**: ทุก disk — model/serial/health/temp/power-on-hours + **attributes ทุกตัว**
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

ต้องติดตั้ง smartmontools บนเครื่อง collector: `sudo apt install smartmontools` (อ่าน SMART ต้องสิทธิ์ root → service รันเป็น root)

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
smart_attributes(+device,attr_id) · program_logs(+name) · program_log_lines(+program_name,line_no)
```

เตรียม DB:
```sql
CREATE DATABASE lane_check CHARACTER SET utf8mb4;
CREATE USER 'lane_check'@'%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON lane_check.* TO 'lane_check'@'%';
FLUSH PRIVILEGES;
```

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
├── main.py, system_metrics.py, smart_collector.py, log_collector.py
├── mq_publisher.py, json_archive.py, consumer.py, LogLibrary.py
├── *_config.template.json            # คัดลอกเป็น *_config.json (ของจริงไม่ commit)
├── build.sh, install_service.sh, *.spec, *.service, requirements.txt
├── sample_output.json, MEMORY.md, README.md
└── Server/
    ├── server_consumer.py, db_mysql.py, manual_import.py, json_archive.py
    ├── schema.sql, build.sh, install_service.sh, *.service, requirements.txt
    └── Lane_Check_Server_config.template.json
```

รายละเอียดเชิงลึก/บันทึกการตัดสินใจ ดูที่ [`MEMORY.md`](MEMORY.md)

---

## License / ผู้พัฒนา
ภายในองค์กร — ปรับแก้ตามการใช้งานได้
