from __future__ import annotations

import json
import os
import threading
import time

import app_paths

_lock = threading.Lock()


def _path():
    return app_paths.data_dir() / "agent_models_cache.json"


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(provider: str) -> list[str]:
    entry = _read().get(provider)
    ids = entry.get("ids") if isinstance(entry, dict) else None
    if not isinstance(ids, list):
        return []
    return [str(i) for i in ids if str(i).strip()]


def fetched_at(provider: str) -> float:
    entry = _read().get(provider)
    try:
        return float(entry.get("t", 0)) if isinstance(entry, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def save(provider: str, ids: list[str]) -> None:
    with _lock:
        data = _read()
        data[provider] = {"t": time.time(), "ids": sorted({str(i).strip() for i in ids if str(i).strip()})}
        target = _path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(temp, target)
