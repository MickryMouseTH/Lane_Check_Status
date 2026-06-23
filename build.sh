#!/usr/bin/env bash
# Build Lane_Check_Status into a single-file Linux executable with PyInstaller.
#
# Usage:
#   ./build.sh
#
# The resulting binary is written to dist/Lane_Check_Status. Copy it to the
# target Ubuntu host together with (optionally) a pre-edited
# Lane_Check_Status_config.json. On first run the config file is created next to
# the binary if absent.
set -euo pipefail

cd "$(dirname "$0")"

# Use a virtualenv so the build is reproducible and isolated.
PYTHON="${PYTHON:-python3}"
VENV_DIR=".buildenv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[build] Creating virtualenv in $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[build] Installing dependencies ..."
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "[build] Cleaning previous build artifacts ..."
rm -rf build dist

echo "[build] Running PyInstaller (--onefile) ..."
# --paths . makes PyInstaller find the local sibling modules (system_metrics,
# smart_collector, log_collector, mq_publisher, json_archive, LogLibrary), and
# we also name them as hidden imports so they are always bundled.
pyinstaller --clean --noconfirm --onefile \
    --name Lane_Check_Status \
    --paths . \
    --hidden-import system_metrics \
    --hidden-import smart_collector \
    --hidden-import log_collector \
    --hidden-import mq_publisher \
    --hidden-import json_archive \
    --hidden-import LogLibrary \
    --hidden-import pika \
    --hidden-import pika.adapters.blocking_connection \
    --hidden-import loguru \
    --hidden-import psutil \
    --hidden-import cryptography.fernet \
    main.py

echo "[build] Done. Single-file binary at: dist/Lane_Check_Status"
