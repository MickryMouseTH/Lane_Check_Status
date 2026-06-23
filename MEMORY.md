# Lane_Check_Status — Memory / Project Notes

> ไฟล์นี้ใช้เป็น "memory" ของโปรเจกต์ สรุปสถาปัตยกรรม, การตั้งค่า, และข้อตกลงสำคัญ
> เพื่อให้กลับมาทำงานต่อได้เร็วและไม่หลงทาง

## เป้าหมาย (Goal)
โปรแกรม agent บน **Ubuntu** ที่เก็บสถานะเครื่องแล้วส่งเข้า **RabbitMQ** เป็น JSON
ตามลำดับ: **อ่านค่า → สร้าง JSON → ส่ง RabbitMQ (มี spool รอส่งถ้า MQ ติดต่อไม่ได้)**

สิ่งที่เก็บทุกค่า:
- **CPU Use** (รวม + รายคอร์ + load average)
- **RAM Use** (RAM + swap)
- **Disk Use แต่ละ Path** (ตั้งได้หลาย path)
- **smartmontools ทุก Disk** (ผ่าน `smartctl -j`, auto-scan หรือระบุ device เอง)
- **Log ของโปรแกรม** ที่รันอยู่ (มากกว่า 1 โปรแกรมได้) อ่านแบบ tail + กรองด้วย regex

## หลักการสำคัญ (Design constraints — ห้ามลืม)
1. **กระทบโปรแกรมเดิมให้น้อยที่สุด** (ผู้ใช้ย้ำ):
   - อ่าน log แบบ **read-only ไม่ล็อกไฟล์**, เปิด `errors="replace"`, อ่านเฉพาะ byte ที่ต่อท้าย (offset) ไม่อ่านทั้งไฟล์
   - ลด priority ด้วย `Nice_Level` (os.nice) + `IO_Nice_Idle` (ionice class idle)
   - SMART รันทุก `Smart.Interval_Cycles` รอบ (ไม่ใช่ทุกรอบ) เพราะ smartctl ปลุกดิสก์/มี I/O
   - logging ใช้ `enqueue=True` ของ loguru = non-blocking
2. **ไม่ทำข้อมูลหาย**: ส่ง MQ ไม่ได้ → เขียนลง **spool dir** (1 ไฟล์ = 1 message), flush แบบ oldest-first เมื่อ broker กลับมา, ใช้ **publisher confirms** ก่อนลบไฟล์ (at-least-once)
3. **ใช้ LogLibrary.py ที่ให้มา** ทั้ง config (`Load_Config`) และ logging (`Loguru_Logging`)
   - คีย์ที่มีคำว่า `pass` (เช่น `RabbitMQ.Password`) จะถูก **เข้ารหัสบนดิสก์อัตโนมัติ** หลังรันครั้งแรก (ต้องตั้ง `LOGLIB_KEY` env เพื่อให้ decrypt รอบถัดไปได้)
4. **Log ละเอียดตาม log level + dump data**: ทุก collector log ระดับ DEBUG พร้อม dump ค่าที่ได้
5. **Build ด้วย PyInstaller `--onefile`** ได้

## โครงสร้างไฟล์
| ไฟล์ | หน้าที่ |
|------|---------|
| `LogLibrary.py` | (ของเดิม) config + loguru + เข้ารหัส secret |
| `main.py` | orchestrator: loop เก็บค่า → ประกอบ JSON → ส่ง, จัดการ low-impact, SMART caching |
| `system_metrics.py` | CPU / RAM / Disk usage (ใช้ `psutil`) |
| `smart_collector.py` | smartmontools ทุก disk (`smartctl -j`, scan + per-device summary) |
| `log_collector.py` | tail+filter log ของแต่ละโปรแกรม, offset state, **date-token ในชื่อ path** |
| `mq_publisher.py` | RabbitMQ publish + disk spool (store-and-forward) ใช้ `pika` |
| `json_archive.py` | เก็บสำเนา JSON เป็นไฟล์รายวัน + zip รวมรายวันตอน 00:01 + retention |
| `consumer.py` | **ฝั่งรับ** RabbitMQ (Lane_Check_Consumer): decode JSON → log สรุป + บันทึกไฟล์, reconnect อัตโนมัติ |
| `requirements.txt` | loguru, psutil, pika, cryptography, pyinstaller |
| `Lane_Check_Status.spec` / `build.sh` | build แบบ single-file (`--onefile`) |
| `lane_check_status.service` | systemd unit (root, low-impact, restart, hardening) |
| `install_service.sh` | สคริปต์ติดตั้ง service ลง /opt/lane_check_status |
| `sample_output.json` | ตัวอย่าง JSON ที่ส่งออก |

## Date token ในชื่อ log path (ผู้ใช้ขอ)
`Log_Path` รองรับ token วันที่ แทนค่าด้วยเวลาปัจจุบันทุกครอบ — รองรับไฟล์ที่ rotate รายวัน/รายเดือน
| token | ความหมาย | ตัวอย่าง (2026-06-23 14:05:09) |
|-------|----------|------|
| `yyyy` | ปี 4 หลัก | 2026 |
| `yy` | ปี 2 หลัก | 26 |
| `mm` | เดือน | 06 |
| `dd` | วัน | 23 |
| `HH` | ชั่วโมง | 14 |
| `MM` | นาที (ตัวพิมพ์ใหญ่!) | 05 |
| `SS` | วินาที | 09 |

ตัวอย่าง: `/tct/yyyy-mm/tct_app_ddmmyy.log` → `/tct/2026-06/tct_app_230626.log`
> ⚠️ `mm` = เดือน (พิมพ์เล็ก), `MM` = นาที (พิมพ์ใหญ่) — อย่าสับสน

## โครงสร้าง config (`Lane_Check_Status_config.json` สร้างอัตโนมัติรอบแรก)
- core ของ LogLibrary: `log_Level`, `Log_Console`, `log_Backup`, `Log_Size`
- `Interval_Seconds` — ระยะห่างแต่ละรอบ
- `Nice_Level`, `IO_Nice_Idle` — ปรับ low-impact
- `RabbitMQ.Enable` — **1 = ส่งไป RabbitMQ, 0 = ไม่ส่ง** (archive-only mode, ไม่เปิด connection เลย)
- `RabbitMQ.{Host,Port,VHost,Username,Password,Exchange,Routing_Key,Queue,Durable,Connection_Timeout}`
- `Output_Files.{Enable,Directory,Retention_Days,Daily_Zip,Daily_Zip_Time}` — **เก็บสำเนา JSON ที่ส่งออกลงไฟล์**
  - `Enable` 1/0 = สร้าง/ไม่สร้างไฟล์ ; `Retention_Days` = เก็บกี่วัน (0 = เก็บตลอด)
  - **โมเดลจัดเก็บ**: ระหว่างวันเขียนเป็น `.json` ธรรมดาใต้ `output/YYYY-MM-DD/` ; พอขึ้นวันใหม่ตอน `Daily_Zip_Time` (ดีฟอลต์ `00:01`) จะ **zip รวมโฟลเดอร์ของวันก่อนหน้า** เป็น `output/YYYY-MM-DD.zip` แล้วลบโฟลเดอร์ทิ้ง
  - `Daily_Zip` 1/0 = เปิด/ปิด การ zip รายวัน ; `Daily_Zip_Time` = `HH:MM` ที่จะ rollup
  - rollup ทำเฉพาะโฟลเดอร์ที่ **วันที่ < วันนี้** เท่านั้น (ไม่แตะวันที่กำลังเขียนอยู่) ; เรียกทุกรอบผ่าน `archive.maintain()` ใน `run_once`
  - prune ลบ `*.zip` ของวันเก่ากว่า retention + ไฟล์/โฟลเดอร์ที่ค้าง ; เก็บ**ทุกรอบไม่ว่าจะส่ง MQ สำเร็จหรือไม่** (แยกอิสระจาก spool) ; เขียน/zip แบบ atomic
- `Spool.{Enable,Directory,Max_Files}`
- `Disk_Paths` — list ของ path ที่จะวัด
- `Smart.{Enable,Smartctl_Path,Devices,Interval_Cycles}`
- `Programs[]` — แต่ละตัวมี `Name`, `Log_Path`, `Include_Patterns`, `Exclude_Patterns`, `Max_Lines`
  - `Include_Patterns` ว่าง = เก็บทุกบรรทัด; บรรทัดที่ match `Exclude_Patterns` จะถูกตัดทิ้ง

## รูปแบบ JSON ที่ส่ง
ดูตัวอย่างเต็มใน `sample_output.json`. คีย์หลัก:
`program, version, hostname, timestamp_utc, timestamp_epoch, os, cpu, memory, disk_usage[], smart[], smart_collected_at, program_logs[]`
- ค่าที่อ่านไม่ได้ (เช่น path หาย / smartctl fail) จะใส่ฟิลด์ `error` ราย item แทนที่จะล้มทั้งรอบ
- `program_logs[]` มี `log_path_pattern` (ดิบ) และ `log_path` (หลังแทนวันที่), `matched_count`, `lines[]`

## การ build
```bash
./build.sh           # สร้าง .buildenv, pip install, pyinstaller --onefile
# ผลลัพธ์: dist/Lane_Check_Status   (เอาไปวางบนเครื่อง Ubuntu เป้าหมาย)
```
ครั้งแรกที่รัน: สร้าง `Lane_Check_Status_config.json` ข้างไฟล์ binary → แก้ค่า → รันใหม่
ถ้ามี secret (Password) ให้ตั้ง `export LOGLIB_KEY=<key ที่ library พิมพ์ออกมา>`

## รันบน Ubuntu (systemd)
```bash
./build.sh                       # บนเครื่อง build → ได้ dist/Lane_Check_Status
# คัดลอกทั้งโฟลเดอร์ไปเครื่องเป้าหมาย แล้ว:
sudo ./install_service.sh        # ติดตั้งลง /opt/lane_check_status + enable + start
sudo journalctl -u lane_check_status -f   # ดู log realtime
```
- service รันเป็น **root** เพราะ `smartctl` ต้องใช้สิทธิ์ (ถ้าจะรัน unprivileged ให้ setcap/sudoers เฉพาะ smartctl แล้วตั้ง `User=`/`Group=` ในไฟล์ unit)
- `lane_check_status.service` มี low-impact ระดับ OS แล้ว: `Nice=10`, `CPUWeight=20`, `IOWeight=20`, `MemoryMax=256M` + hardening (`ProtectSystem=full`, log อ่าน read-only)
- secret key (`LOGLIB_KEY`) อ่านจาก `EnvironmentFile=/opt/lane_check_status/lane_check_status.env` (optional, ตั้ง 0600)
- ติดตั้ง smartmontools: `sudo apt install smartmontools` (install_service.sh ลองติดตั้งให้อัตโนมัติ)

## TODO / ส่วนที่ยังขยายได้
- [ ] ตัวอย่าง unit file ของ systemd
- [ ] รองรับ TLS ไป RabbitMQ (amqps)
- [ ] metric เพิ่ม: network I/O, อุณหภูมิ CPU (sensors)
- [ ] ทดสอบ end-to-end กับ broker จริง (ตอนนี้ทดสอบ syntax + date expansion + sample JSON แล้ว)
