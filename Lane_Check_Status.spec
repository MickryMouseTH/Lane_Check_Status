# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Lane_Check_Status.
# Build with:  pyinstaller Lane_Check_Status.spec
#
# Produces a single-file executable in dist/. Hidden imports are declared
# explicitly because pika/loguru/psutil pull in submodules PyInstaller's static
# analysis can miss.

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'pika',
        'pika.adapters',
        'pika.adapters.blocking_connection',
        'loguru',
        'psutil',
        'cryptography',
        'cryptography.fernet',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Lane_Check_Status',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
