#!/usr/bin/env bash
# Uninstall Lane_Check_Status from a systemd host (reverses install_service.sh).
#
# Run on the TARGET host:
#   sudo ./uninstall_service.sh
#
# Stops + disables the service, removes the systemd unit, and PURGES the install
# directory (binary, config JSON, LOGLIB_KEY env file, logs/ and spool/ data).
set -euo pipefail

INSTALL_DIR="/home/lane_check_status"
SERVICE_NAME="lane_check_status.service"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root: sudo $0" >&2
    exit 1
fi

echo "[uninstall] Stopping $SERVICE_NAME ..."
systemctl stop "$SERVICE_NAME" 2>/dev/null || true

echo "[uninstall] Disabling $SERVICE_NAME ..."
systemctl disable "$SERVICE_NAME" 2>/dev/null || true

UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
if [ -f "$UNIT_PATH" ]; then
    echo "[uninstall] Removing systemd unit $UNIT_PATH ..."
    rm -f "$UNIT_PATH"
fi

echo "[uninstall] Reloading systemd ..."
systemctl daemon-reload
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

if [ -d "$INSTALL_DIR" ]; then
    echo "[uninstall] Purging $INSTALL_DIR (binary, config, env, logs, spool) ..."
    rm -rf "$INSTALL_DIR"
fi

echo "[uninstall] Done."
echo
echo "Note: journald logs persist. To drop them too:"
echo "  sudo journalctl --rotate && sudo journalctl --vacuum-time=1s"
