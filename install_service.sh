#!/usr/bin/env bash
# Install Lane_Check_Status as a systemd service on Ubuntu.
#
# Run on the TARGET host after copying the built binary here:
#   sudo ./install_service.sh
#
# It installs the binary + config to /opt/lane_check_status, installs the unit,
# enables it on boot, and starts it.
set -euo pipefail

INSTALL_DIR="/opt/lane_check_status"
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

echo "[install] Installing systemd unit ..."
install -m 0644 "$SRC_DIR/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"

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
