"""Task activity log and live state, read by the GUI.

agent_activity.jsonl   one line when a task starts, one when it ends (same "id")
agent_state.json       latest snapshot of what is loaded / rate-limited
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import app_paths

MAX_LOG_BYTES = 3_000_000
_lock = threading.Lock()


def log_path() -> Path:
    return app_paths.logs_dir() / "agent_activity.jsonl"


def state_path() -> Path:
    return app_paths.logs_dir() / "agent_state.json"


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def _append(entry: dict) -> None:
    target = log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if target.exists() and target.stat().st_size > MAX_LOG_BYTES:
                os.replace(target, target.with_suffix(".jsonl.1"))
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def begin(task_id: str, **fields) -> None:
    _append({"id": task_id, "phase": "start", "t": time.time(), **fields})


def update(task_id: str, **fields) -> None:
    _append({"id": task_id, "phase": "update", "t": time.time(), **fields})


def end(task_id: str, **fields) -> None:
    _append({"id": task_id, "phase": "end", "t": time.time(), **fields})


def read_since(offset: int) -> tuple[list[dict], int]:
    """New log entries after byte `offset`; returns (entries, new_offset). A shrunk
    file (rotation) restarts from the beginning."""
    target = log_path()
    try:
        size = target.stat().st_size
    except OSError:
        return [], 0
    if offset > size:
        offset = 0
    entries: list[dict] = []
    try:
        with target.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return [], offset
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return [], offset
    for line in chunk[: cut + 1].splitlines():
        try:
            entries.append(json.loads(line.decode("utf-8", "replace")))
        except ValueError:
            continue
    return entries, offset + cut + 1


def write_state(patch: dict) -> None:
    target = state_path()
    try:
        with _lock:
            try:
                current = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value
            current["t"] = time.time()
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(current), encoding="utf-8")
            os.replace(temp, target)
    except OSError:
        pass


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
