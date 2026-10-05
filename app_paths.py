import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)


def _registered_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\CodyMCPServer") as key:
            return winreg.QueryValueEx(key, "DataDir")[0]
    except (ImportError, OSError):
        return None


def packaged() -> bool:
    return FROZEN or bool(os.environ.get("CODY_DATA_DIR"))


def data_dir() -> Path:
    explicit = os.environ.get("CODY_DATA_DIR")
    if explicit:
        return Path(explicit)
    if FROZEN:
        registered = _registered_dir()
        return Path(registered) if registered else Path(sys.executable).resolve().parent / "data"
    return ROOT


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", ROOT))


def config_file() -> Path:
    return data_dir() / "config.json"


def logs_dir() -> Path:
    return data_dir() / ("logs" if packaged() else "tray_logs")


def history_dir() -> Path:
    return data_dir() / "history" if packaged() else logs_dir() / "history"
