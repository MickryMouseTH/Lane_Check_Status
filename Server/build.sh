#!/usr/bin/env bash
# Build Lane_Check_Server into a single-file executable with PyInstaller.
#
# Usage:  ./build.sh
# Output: dist/Lane_Check_Server
set -euo pipefail

cd "$(dirname "$0")"

# LogLibrary.py is maintained in the repo ROOT and shared with the collector.
# Sync the canonical copy here before building so the server can never ship a
# stale LogLibrary (which previously caused an ImportError on startup).
ROOT_LOGLIB="../LogLibrary.py"
if [ -f "$ROOT_LOGLIB" ]; then
    echo "[build] Syncing LogLibrary.py from repo root ..."
    cp "$ROOT_LOGLIB" ./LogLibrary.py
else
    echo "[build] WARNING: $ROOT_LOGLIB not found; using existing Server/LogLibrary.py." >&2
fi

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
pyinstaller --clean --noconfirm --onefile \
    --name Lane_Check_Server \
    --paths . \
    --hidden-import db_mysql \
    --hidden-import db_cleanup \
    --hidden-import json_archive \
    --hidden-import manual_import \
    --hidden-import LogLibrary \
    --hidden-import pika \
    --hidden-import pika.adapters.blocking_connection \
    --hidden-import pymysql \
    --hidden-import loguru \
    --hidden-import cryptography.fernet \
    server_consumer.py

echo "[build] Done. Single-file binary at: dist/Lane_Check_Server"
