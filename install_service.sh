#!/usr/bin/env bash
# Install Lane_Check_Status as a systemd service on Ubuntu.
#
# Run on the TARGET host after copying the built binary here:
#   sudo ./install_service.sh
#
# It installs the binary + config to /opt/lane_check_status, installs the unit,
# enables it on boot, and starts it.
set -euo pipefail

INSTALL_DIR="/home/lane_check_status"
SERVICE_NAME="lane_check_status.service"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi

echo "[install] Ensuring smartmontools is present ..."
if ! command -v smartctl >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y smartmontools || \
        echo "[install] WARNING: could not auto-install smartmontools; install it manually."
fi

echo "[install] Creating $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"

if [ ! -f "$SRC_DIR/dist/Lane_Check_Status" ]; then
    echo "[install] ERROR: dist/Lane_Check_Status not found. Run ./build.sh first." >&2
    exit 1
fi

echo "[install] Copying binary ..."
install -m 0755 "$SRC_DIR/dist/Lane_Check_Status" "$INSTALL_DIR/Lane_Check_Status"

# Copy an existing config if present; otherwise the program creates one on first run.
if [ -f "$SRC_DIR/Lane_Check_Status_config.json" ] && [ ! -f "$INSTALL_DIR/Lane_Check_Status_config.json" ]; then
    echo "[install] Copying existing config ..."
    install -m 0644 "$SRC_DIR/Lane_Check_Status_config.json" "$INSTALL_DIR/"
fi

echo "[install] Installing systemd unit (pointing it at $INSTALL_DIR) ..."
# Generate the unit inline so the deploy bundle only needs the dist binary
# (no separate .service file to copy). Paths point at $INSTALL_DIR.
cat > "/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=Lane_Check_Status - Ubuntu host status collector (CPU/RAM/Disk/SMART/logs -> RabbitMQ)
Documentation=file:$INSTALL_DIR/MEMORY.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple

WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/Lane_Check_Status

# Secret key for LogLibrary (decrypts RabbitMQ.Password). After the first run:
#   echo "LOGLIB_KEY=<key>" | sudo tee $INSTALL_DIR/lane_check_status.env
EnvironmentFile=-$INSTALL_DIR/lane_check_status.env

# smartctl needs root to query disks.
User=root
Group=root

Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=10

# Stay low-impact relative to the real workload on the host.
Nice=10
CPUWeight=20
IOWeight=20
MemoryMax=256M

# Hardening (loosen if it interferes with reading your app logs).
NoNewPrivileges=true
ProtectControlGroups=true
ProtectKernelModules=true
ProtectSystem=full
ReadWritePaths=$INSTALL_DIR

StandardOutput=journal
StandardError=journal
SyslogIdentifier=lane_check_status

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "/etc/systemd/system/$SERVICE_NAME"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "[install] Done."
echo
echo "Next steps:"
echo "  1) Edit config:   sudo nano $INSTALL_DIR/Lane_Check_Status_config.json"
echo "  2) If secrets are used, capture the printed LOGLIB_KEY:"
echo "       sudo journalctl -u $SERVICE_NAME | grep LOGLIB_KEY"
echo "       echo 'LOGLIB_KEY=<key>' | sudo tee $INSTALL_DIR/lane_check_status.env"
echo "       sudo chmod 600 $INSTALL_DIR/lane_check_status.env"
echo "  3) Restart:       sudo systemctl restart $SERVICE_NAME"
echo "  4) Watch logs:    sudo journalctl -u $SERVICE_NAME -f"
