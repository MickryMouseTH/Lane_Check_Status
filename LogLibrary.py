from loguru import logger
import json
import sys
import os

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:  # cryptography is optional; secrets stay plaintext without it.
    _HAS_CRYPTO = False

# --------------------------- Secret handling ---------------------------
# Any config key whose name contains "pass" (case-insensitive) is treated as a
# secret: stored encrypted on disk and decrypted in memory for the application.
_ENC_PREFIX = 'ENC:'          # marks an already-encrypted value in the JSON file.
_KEY_ENV = 'LOGLIB_KEY'       # environment variable holding the Fernet key.

'''
To use default config minimum, copy the following code snippet into your main program.
----------------------------------------------------------------------------------------------------------------------------------------

from LogLibrary import Load_Config, Loguru_Logging
# ----------------------- Configuration Values -----------------------
Program_Name = ""        # Program name for identification and logging.
Program_Version = ""            # Program version used for file naming and logging.
# ---------------------------------------------------------------------

default_config = {
            "log_Level": "DEBUG",
            "Log_Console": 1,  # 1/true to enable console logging, 0/false to disable.
            "log_Backup": 90,         # Log retention duration in days (older logs are removed).
            "Log_Size": "10 MB"       # Maximum log file size before rotation.
            # Any key containing "pass" (e.g. "DB_Password") is encrypted on disk
            # automatically after the first run. Put the plaintext value here the
            # first time only.
        }

config = Load_Config(default_config, Program_Name)
logger = Loguru_Logging(config, Program_Name, Program_Version)

# Secret/password handling:
# - Any config key whose name contains "pass" (case-insensitive) is treated as a
#   secret. On the FIRST run you set it as plaintext; the library then encrypts it
#   in the JSON file (value becomes "ENC:..."). At runtime `config[...]` always
#   holds the decrypted plaintext for your program to use.
# - Encryption uses the `cryptography` package (pip install cryptography) with a
#   key read from the LOGLIB_KEY environment variable. If LOGLIB_KEY is not set,
#   a key is generated and printed once — export it so secrets stay readable:
#       export LOGLIB_KEY=<printed key>
----------------------------------------------------------------------------------------------------------------------------------------
'''
global script_dir

if getattr(sys, 'frozen', False):
    # When packaged into a single executable (e.g., PyInstaller), place files
    # next to the executable to keep configuration and logs with the binary.
    script_dir = os.path.dirname(sys.executable)
else:
    # When running as a normal script, co-locate config/logs with this module.
    script_dir = os.path.dirname(os.path.abspath(__file__))

def _is_truthy(value):
    """Interpret common truthy representations from JSON/config.

    Accepts native bools/ints as well as strings like "1", "true", "yes",
    and "on" (case-insensitive) so the console toggle is forgiving of how
    operators edit the JSON file by hand.
    """
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _write_config(config_path, config):
    """Atomically write `config` to `config_path` as pretty-printed JSON.

    Writing to a temporary file and replacing the target avoids leaving a
    truncated/corrupt config behind if the process is interrupted mid-write.
    """
    tmp_path = f'{config_path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as tmp_file:
        json.dump(config, tmp_file, indent=4)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, config_path)


def _is_secret_key(key_name):
    """Return True if `key_name` looks like a secret (contains "pass")."""
    return 'pass' in str(key_name).lower()


def _config_has_secret(obj):
    """Recursively check whether any key in `obj` is a secret with a value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _is_secret_key(key) and isinstance(value, str) and value != '':
                return True
            if _config_has_secret(value):
                return True
    elif isinstance(obj, list):
        return any(_config_has_secret(item) for item in obj)
    return False


def _resolve_fernet():
    """Return (Fernet, generated_flag).

    Uses the key in the `LOGLIB_KEY` env var when present. Otherwise generates
    a fresh key and signals the caller (generated_flag=True) so it can tell the
    operator to persist it; without persistence, secrets cannot be decrypted on
    the next run.
    """
    key = os.environ.get(_KEY_ENV)
    if key:
        try:
            return Fernet(key.encode()), False
        except (ValueError, TypeError) as exc:
            sys.stderr.write(
                f'[LogLibrary] Invalid {_KEY_ENV} value ({exc}); '
                f'generating a temporary key.\n'
            )
    return Fernet(Fernet.generate_key()), True


def _process_secrets(obj, fernet):
    """Walk `obj`, returning (runtime_obj, disk_obj, changed).

    - Plaintext secrets: kept as-is for runtime, encrypted for disk.
    - Encrypted (ENC:) secrets: decrypted for runtime, left encrypted on disk.
    `changed` is True when at least one plaintext secret was encrypted.
    """
    if isinstance(obj, dict):
        runtime, disk, changed = {}, {}, False
        for key, value in obj.items():
            if _is_secret_key(key) and isinstance(value, str) and value != '':
                if value.startswith(_ENC_PREFIX):
                    token = value[len(_ENC_PREFIX):].encode()
                    try:
                        runtime[key] = fernet.decrypt(token).decode()
                    except InvalidToken:
                        sys.stderr.write(
                            f'[LogLibrary] Could not decrypt "{key}" — wrong or '
                            f'missing {_KEY_ENV}. Leaving value encrypted.\n'
                        )
                        runtime[key] = value
                    disk[key] = value
                else:
                    runtime[key] = value  # plaintext for the application to use
                    disk[key] = _ENC_PREFIX + fernet.encrypt(value.encode()).decode()
                    changed = True
            else:
                child_runtime, child_disk, child_changed = _process_secrets(value, fernet)
                runtime[key], disk[key] = child_runtime, child_disk
                changed = changed or child_changed
        return runtime, disk, changed

    if isinstance(obj, list):
        runtime, disk, changed = [], [], False
        for item in obj:
            child_runtime, child_disk, child_changed = _process_secrets(item, fernet)
            runtime.append(child_runtime)
            disk.append(child_disk)
            changed = changed or child_changed
        return runtime, disk, changed

    return obj, obj, False


def _apply_secret_encryption(config, config_path, force_write):
    """Encrypt plaintext secrets / decrypt stored secrets.

    Returns the runtime config (with plaintext secret values). Rewrites the
    config file with encrypted values when something changed or `force_write`
    is set and the file is missing.

    Args:
        config: The merged configuration dict.
        config_path: Path to the JSON config file.
        force_write: When True, (re)write the file even if no plaintext secret
            needed encrypting (used when creating a brand-new config file).
    """
    if not _config_has_secret(config):
        return config

    if not _HAS_CRYPTO:
        sys.stderr.write(
            '[LogLibrary] Secret keys found but "cryptography" is not installed; '
            'storing values as plaintext. Run: pip install cryptography\n'
        )
        return config

    fernet, generated = _resolve_fernet()
    runtime_config, disk_config, changed = _process_secrets(config, fernet)

    if changed or force_write:
        try:
            _write_config(config_path, disk_config)
        except OSError as exc:
            sys.stderr.write(
                f'[LogLibrary] Failed to persist encrypted config ({exc}).\n'
            )

    if changed and generated:
        # Print the generated key so the operator can persist it; otherwise the
        # next run generates a different key and decryption will fail.
        sys.stderr.write(
            '[LogLibrary] Secrets encrypted with a NEW generated key. '
            'To keep them readable on the next run, set this in your environment:\n'
            f'{_generated_key_hint(fernet)}'
        )

    return runtime_config


def _generated_key_hint(fernet):
    """Build a copy-paste export line for a freshly generated Fernet key."""
    # Reconstruct the original urlsafe-base64 key from Fernet's split halves.
    import base64
    raw = fernet._signing_key + fernet._encryption_key
    key_str = base64.urlsafe_b64encode(raw).decode()
    return f'    export {_KEY_ENV}={key_str}\n'


def Load_Config(default_config, Program_Name):
    """Load or create a JSON config for the application.

    Behavior:
    - If `<Program_Name>_config.json` does not exist, create it with
      `default_config` and write to disk (pretty-printed).
    - Read the config and merge it onto `default_config` so newly introduced
      default keys are always present even for older config files.
    - If the file is missing or corrupt, fall back to `default_config`
      instead of crashing the host application.

    Args:
        default_config: A dict with default settings to seed the file.
        Program_Name: The app name used to derive the config filename.

    Returns:
        dict: Parsed configuration content (defaults merged with file values).
    """
    # Use a stable name even when Program_Name is empty.
    safe_name = Program_Name if Program_Name else 'app'
    config_file_name = f'{safe_name}_config.json'
    config_path = os.path.join(script_dir, config_file_name)

    # Create config file with default values if it does not exist.
    if not os.path.exists(config_path):
        # Persist defaults so operators can edit them later. Encrypt any secret
        # values up front so the new file never lands plaintext on disk.
        _write_config(config_path, default_config)
        return _apply_secret_encryption(dict(default_config), config_path, force_write=True)

    # Load configuration, tolerating a missing/corrupt file.
    try:
        with open(config_path, 'r', encoding='utf-8') as config_file:
            file_config = json.load(config_file)
        if not isinstance(file_config, dict):
            raise ValueError('Config root must be a JSON object.')
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        # Don't take the host application down because of a bad config file;
        # back up the broken file and continue with defaults.
        sys.stderr.write(
            f'[LogLibrary] Failed to read config "{config_path}" ({exc}); '
            f'using default configuration.\n'
        )
        try:
            if os.path.exists(config_path):
                os.replace(config_path, f'{config_path}.bak')
        except OSError:
            pass
        return _apply_secret_encryption(dict(default_config), config_path, force_write=True)

    # Merge file values over the defaults so missing keys are backfilled.
    config = dict(default_config)
    config.update(file_config)

    # Encrypt any plaintext secrets (first run) / decrypt stored secrets so the
    # application always receives usable plaintext values.
    return _apply_secret_encryption(config, config_path, force_write=False)

# ----------------------- Loguru Logging Setup -----------------------
def Loguru_Logging(config, Program_Name, Program_Version):
    """Initialize Loguru sinks per configuration.

    Sinks:
    - Console (optional): enabled when `Log_Console` is truthy.
    - File: `<script_dir>/logs/<Program_Name>_<Program_Version>.log` with
      size-based rotation and day-based retention.

    Args:
        config: The configuration dict returned by `Load_Config`.
        Program_Name: Application name (used in file naming and banner).
        Program_Version: Application version (used in file naming and banner).

    Returns:
        loguru.Logger: Configured logger instance ready for use.
    """
    logger.remove()

    log_Backup = int(config.get('log_Backup', 90))
    Log_Size = str(config.get('Log_Size', '10 MB')).upper()
    log_Level = str(config.get('log_Level', 'DEBUG')).upper()

    log_dir = os.path.join(script_dir, 'logs')
    os.makedirs(log_dir, exist_ok=True)

    safe_name = Program_Name if Program_Name else 'app'
    log_file_name = f'{safe_name}_{Program_Version}.log'
    log_file = os.path.join(log_dir, log_file_name)

    # Accept 1, "1", true, "true", "yes" etc. as enabling the console sink.
    if _is_truthy(config.get('Log_Console', 0)):
        logger.add(
            sys.stdout,
            level=log_Level,
            format="<green>{time}</green> | <blue>{level}</blue> | <cyan>{thread.id}</cyan> | <magenta>{function}</magenta> | {message}",
            enqueue=True,
        )

    logger.add(
        log_file,
        format="{time} | {level} | {thread.id} | {function} | {message}",
        level=log_Level,
        rotation=Log_Size,
        retention=f"{log_Backup} days",
        compression="zip",
        enqueue=True,       # non-blocking, process-safe writes (better throughput)
        backtrace=False,    # avoid leaking full stack frames into log files
        diagnose=False,     # avoid logging local variable values (perf + security)
    )

    logger.info('-' * 117)
    logger.info(f"Start {Program_Name} Version {Program_Version}")
    logger.info('-' * 117)

    return logger