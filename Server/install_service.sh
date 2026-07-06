#!/usr/bin/env bash
# Install Lane_Check_Server as a systemd service on Ubuntu.
#
# Run on the TARGET host after building (./build.sh):
#   sudo ./install_service.sh
#
# Installs the binary + config to /opt/lane_check_server, installs the unit,
# enables it on boot, and starts it. MySQL must be reachable per the config.
set -euo pipefail

INSTALL_DIR="/home/lane_check_server"
SERVICE_NAME="lane_check_server.service"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi

if [ ! -f "$SRC_DIR/dist/Lane_Check_Server" ]; then
    echo "[install] ERROR: dist/Lane_Check_Server not found. Run ./build.sh first." >&2
    exit 1
fi

echo "[install] Creating $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"

echo "[install] Copying binary ..."
install -m 0755 "$SRC_DIR/dist/Lane_Check_Server" "$INSTALL_DIR/Lane_Check_Server"

# Copy an existing config if present; otherwise the program creates one on first run.
if [ -f "$SRC_DIR/Lane_Check_Server_config.json" ] && [ ! -f "$INSTALL_DIR/Lane_Check_Server_config.json" ]; then
    echo "[install] Copying existing config ..."
    install -m 0644 "$SRC_DIR/Lane_Check_Server_config.json" "$INSTALL_DIR/"
fi

# Create the manual-import folders up front so operators can drop files immediately.
echo "[install] Ensuring manual import folders ..."
mkdir -p "$INSTALL_DIR/manual/processed" "$INSTALL_DIR/manual/failed"

echo "[install] Installing systemd unit (pointing it at $INSTALL_DIR) ..."
# Generate the unit inline so the deploy bundle only needs the dist binary
# (no separate .service file to copy). Paths point at $INSTALL_DIR.
cat > "/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=Lane_Check_Server - RabbitMQ to MySQL ingestion for Lane_Check_Status
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/Lane_Check_Server

# Secret key for LogLibrary (decrypts RabbitMQ.Password / MySQL.Password).
# After the first run:
#   echo "LOGLIB_KEY=<key>" | sudo tee $INSTALL_DIR/lane_check_server.env
EnvironmentFile=-$INSTALL_DIR/lane_check_server.env

User=root
Group=root

Restart=always
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=lane_check_server

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
echo "  1) Ensure MySQL is set up:"
echo "       CREATE DATABASE lane_check CHARACTER SET utf8mb4;"
echo "       CREATE USER 'lane_check'@'%' IDENTIFIED BY '<password>';"
echo "       GRANT ALL PRIVILEGES ON lane_check.* TO 'lane_check'@'%'; FLUSH PRIVILEGES;"
echo "  2) Edit config:   sudo nano $INSTALL_DIR/Lane_Check_Server_config.json"
echo "       (set RabbitMQ.* and MySQL.* — Host/User/Password/Database)"
echo "  3) If secrets are used, capture the printed LOGLIB_KEY:"
echo "       sudo journalctl -u $SERVICE_NAME | grep LOGLIB_KEY"
echo "       echo 'LOGLIB_KEY=<key>' | sudo tee $INSTALL_DIR/lane_check_server.env"
echo "       sudo chmod 600 $INSTALL_DIR/lane_check_server.env"
echo "  4) Restart:       sudo systemctl restart $SERVICE_NAME"
echo "  5) Watch logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "  6) Manual import (if MQ is down): drop .json/.zip files into $INSTALL_DIR/manual/"
