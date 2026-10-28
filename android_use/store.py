import json
import os
import secrets
import threading

import app_paths

FILE = app_paths.data_dir() / "android_use.json"
SERVICE_ACCOUNT = app_paths.data_dir() / "android_fcm_service_account.json"

_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def token() -> str:
    env = os.environ.get("ANDROID_USE_TOKEN")
    if env:
        return env
    with _lock:
        data = _load()
        if not data.get("token"):
            data["token"] = secrets.token_urlsafe(24)
            _save(data)
        return data["token"]


def devices() -> dict:
    with _lock:
        return dict(_load().get("devices", {}))


def remember_device(device_id: str, **fields) -> None:
    with _lock:
        data = _load()
        known = data.setdefault("devices", {})
        entry = known.setdefault(device_id, {})
        entry.update({k: v for k, v in fields.items() if v not in (None, "")})
        _save(data)
