-- Lane_Check_Server MySQL schema (reference).
-- The server creates these automatically on startup (db_mysql.ensure_schema).
-- This file is provided for manual setup / review. Adjust `lane_check` DB name
-- and any Table_Prefix to match your config.
--
--   CREATE DATABASE lane_check CHARACTER SET utf8mb4;
--   CREATE USER 'lane_check'@'%' IDENTIFIED BY 'change_me';
--   GRANT ALL PRIVILEGES ON lane_check.* TO 'lane_check'@'%';
--   FLUSH PRIVILEGES;

-- One row per host per collection cycle.
CREATE TABLE IF NOT EXISTS `host` (
    timestamp_utc   DATETIME(6)  NOT NULL,
    hostname        VARCHAR(150) NOT NULL,
    program         VARCHAR(100),
    version         VARCHAR(40),
    timestamp_epoch BIGINT,
    os_system       VARCHAR(60),
    os_release      VARCHAR(120),
    os_platform     VARCHAR(255),
    received_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (timestamp_utc, hostname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `cpu` (
    timestamp_utc    DATETIME(6)  NOT NULL,
    hostname         VARCHAR(150) NOT NULL,
    percent          DOUBLE,
    core_count       INT,
    load_avg_1m      DOUBLE,
    load_avg_5m      DOUBLE,
    load_avg_15m     DOUBLE,
    per_core_percent JSON,
    PRIMARY KEY (timestamp_utc, hostname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `memory` (
    timestamp_utc    DATETIME(6)  NOT NULL,
    hostname         VARCHAR(150) NOT NULL,
    ram_total_kb     BIGINT,
    ram_available_kb BIGINT,
    ram_used_kb      BIGINT,
    ram_percent      DOUBLE,
    swap_total_kb    BIGINT,
    swap_used_kb     BIGINT,
    swap_percent     DOUBLE,
    PRIMARY KEY (timestamp_utc, hostname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Multiple paths per host -> path is part of the key.
CREATE TABLE IF NOT EXISTS `disk_usage` (
    timestamp_utc DATETIME(6)  NOT NULL,
    hostname      VARCHAR(150) NOT NULL,
    path          VARCHAR(400) NOT NULL,
    total_kb      BIGINT,
    used_kb       BIGINT,
    free_kb       BIGINT,
    percent       DOUBLE,
    error         VARCHAR(255),
    PRIMARY KEY (timestamp_utc, hostname, path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Multiple disks per host -> device is part of the key.
CREATE TABLE IF NOT EXISTS `smart` (
    timestamp_utc     DATETIME(6)  NOT NULL,
    hostname          VARCHAR(150) NOT NULL,
    device            VARCHAR(120) NOT NULL,
    model_name        VARCHAR(150),
    serial_number     VARCHAR(120),
    firmware_version  VARCHAR(60),
    smart_passed      TINYINT,
    temperature_c     INT,
    power_on_hours    BIGINT,
    power_cycle_count BIGINT,
    error             VARCHAR(255),
    PRIMARY KEY (timestamp_utc, hostname, device)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Many attributes per disk -> attr_id is part of the key.
CREATE TABLE IF NOT EXISTS `smart_attributes` (
    timestamp_utc DATETIME(6)  NOT NULL,
    hostname      VARCHAR(150) NOT NULL,
    device        VARCHAR(120) NOT NULL,
    attr_id       INT          NOT NULL,
    name          VARCHAR(120),
    value         INT,
    worst         INT,
    thresh        INT,
    raw           BIGINT,
    raw_string    VARCHAR(120),
    type          VARCHAR(40),
    when_failed   VARCHAR(40),
    PRIMARY KEY (timestamp_utc, hostname, device, attr_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- RAID metadata from `dmraid -n` -> one row per host per cycle.
CREATE TABLE IF NOT EXISTS `raid` (
    timestamp_utc DATETIME(6)  NOT NULL,
    hostname      VARCHAR(150) NOT NULL,
    available     TINYINT,
    raid_detected TINYINT,
    command       VARCHAR(255),
    returncode    INT,
    output        JSON,
    stderr        VARCHAR(2000),
    error         VARCHAR(255),
    collected_at  VARCHAR(40),
    PRIMARY KEY (timestamp_utc, hostname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- systemd unit health -> one row per unit per cycle.
CREATE TABLE IF NOT EXISTS `services_systemd` (
    timestamp_utc DATETIME(6)  NOT NULL,
    hostname      VARCHAR(150) NOT NULL,
    unit          VARCHAR(200) NOT NULL,
    load_state    VARCHAR(40),
    active_state  VARCHAR(40),
    sub_state     VARCHAR(40),
    enabled       VARCHAR(40),
    main_pid      BIGINT,
    ok            TINYINT,
    error         VARCHAR(255),
    PRIMARY KEY (timestamp_utc, hostname, unit)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Named (non-systemd) process health -> one row per spec name per cycle.
CREATE TABLE IF NOT EXISTS `services_process` (
    timestamp_utc  DATETIME(6)  NOT NULL,
    hostname       VARCHAR(150) NOT NULL,
    name           VARCHAR(150) NOT NULL,
    pattern        VARCHAR(255),
    running        TINYINT,
    count          INT,
    pids           JSON,
    rss_kb         BIGINT,
    vms_kb         BIGINT,
    num_threads    INT,
    uptime_seconds BIGINT,
    ok             TINYINT,
    error          VARCHAR(255),
    PRIMARY KEY (timestamp_utc, hostname, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Ping reachability + latency -> one row per pinged address per cycle.
CREATE TABLE IF NOT EXISTS `ping` (
    timestamp_utc       DATETIME(6)  NOT NULL,
    hostname            VARCHAR(150) NOT NULL,
    address             VARCHAR(255) NOT NULL,
    name                VARCHAR(150),
    reachable           TINYINT,
    rtt_ms              DOUBLE,
    packet_loss_percent DOUBLE,
    ok                  TINYINT,
    error               VARCHAR(255),
    collected_at        VARCHAR(40),
    PRIMARY KEY (timestamp_utc, hostname, address)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- USB device presence -> one row per expected device (by name) per cycle.
CREATE TABLE IF NOT EXISTS `usb` (
    timestamp_utc DATETIME(6)  NOT NULL,
    hostname      VARCHAR(150) NOT NULL,
    name          VARCHAR(150) NOT NULL,
    vendor_id     VARCHAR(8),
    product_id    VARCHAR(8),
    present       TINYINT,
    count         INT,
    manufacturer  VARCHAR(255),
    product       VARCHAR(255),
    serial        VARCHAR(255),
    ok            TINYINT,
    error         VARCHAR(255),
    collected_at  VARCHAR(40),
    PRIMARY KEY (timestamp_utc, hostname, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- HTTP probe (curl a device status page + extract fields) -> one row per endpoint (by name) per cycle.
CREATE TABLE IF NOT EXISTS `http_probe` (
    timestamp_utc  DATETIME(6)  NOT NULL,
    hostname       VARCHAR(150) NOT NULL,
    name           VARCHAR(150) NOT NULL,
    url            VARCHAR(512),
    ok             TINYINT,
    status_code    INT,
    response_ms    DOUBLE,
    fields         JSON,
    fields_missing JSON,
    error          VARCHAR(255),
    collected_at   VARCHAR(40),
    PRIMARY KEY (timestamp_utc, hostname, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One row per monitored program per cycle.
CREATE TABLE IF NOT EXISTS `program_logs` (
    timestamp_utc    DATETIME(6)  NOT NULL,
    hostname         VARCHAR(150) NOT NULL,
    name             VARCHAR(150) NOT NULL,
    log_path_pattern VARCHAR(512),
    log_path         VARCHAR(512),
    new_lines_total  INT,
    matched_count    INT,
    output_truncated TINYINT,
    error            VARCHAR(255),
    PRIMARY KEY (timestamp_utc, hostname, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Matched log lines -> line_no is part of the key.
CREATE TABLE IF NOT EXISTS `program_log_lines` (
    timestamp_utc DATETIME(6)  NOT NULL,
    hostname      VARCHAR(150) NOT NULL,
    program_name  VARCHAR(150) NOT NULL,
    line_no       INT          NOT NULL,
    line          TEXT,
    PRIMARY KEY (timestamp_utc, hostname, program_name, line_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
