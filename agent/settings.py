"""Agent settings: one JSON file in the app data dir, shared by the GUI and the server.

The GUI writes it, the server re-reads it whenever the file changes, so toggling agents
or switching mode takes effect on the next call without restarting anything.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import app_paths

MODES = ("openrouter", "local", "both")

DEFAULTS = {
    "enabled": False,
    "mode": "both",
    "openrouter_api_key": "",
    "openrouter_rpm": 18,
    "openrouter_models": [],
    "openrouter_free_only": True,
    "openrouter_max_wait": 15,
    "openrouter_timeout": 90,
    "requesty_enabled": False,
    "requesty_api_key": "",
    "requesty_base_url": "https://router.requesty.ai/v1",
    "requesty_models": [],
    "requesty_free_only": True,
    "requesty_timeout": 90,
    "gemini_enabled": False,
    "gemini_api_key": "",
    "gemini_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "gemini_models": [],
    "gemini_free_only": True,
    "gemini_timeout": 90,
    "nvidia_enabled": False,
    "nvidia_api_key": "",
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1",
    "nvidia_models": [],
    "nvidia_free_only": True,
    "nvidia_timeout": 240,
    "lmstudio_url": "http://127.0.0.1:1234",
    "lms_path": "",
    "local_model": "",
    "local_context": 4096,
    "local_idle_seconds": 120,
    "local_max_tokens": 700,
    "local_timeout": 300,
    "local_min_free_ram_gb": 2.0,
    "laya_enabled": True,
    "laya_python": "",
    "laya_checkpoint": "convaiinnovations/laya",
    "laya_allow_download": False,
    "laya_idle_seconds": 120,
    "laya_threads": 4,
    "laya_min_confidence": 0.35,
    "max_steps": 5,
    "max_parallel": 3,
}

_lock = threading.Lock()
_cache: dict = {"stamp": None, "data": None}


def path() -> Path:
    return app_paths.data_dir() / "agent_settings.json"


def _coerce(data: dict) -> dict:
    merged = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    if merged["mode"] not in MODES:
        merged["mode"] = "both"
    merged["enabled"] = bool(merged["enabled"])
    merged["requesty_enabled"] = bool(merged["requesty_enabled"])
    merged["gemini_enabled"] = bool(merged["gemini_enabled"])
    merged["nvidia_enabled"] = bool(merged["nvidia_enabled"])
    for provider in ("openrouter", "requesty", "gemini", "nvidia"):
        merged[f"{provider}_free_only"] = bool(merged[f"{provider}_free_only"])

    for key in ("openrouter_rpm", "openrouter_max_wait", "openrouter_timeout", "requesty_timeout", "gemini_timeout", "nvidia_timeout", "local_context",
                "local_idle_seconds", "local_max_tokens", "local_timeout", "laya_idle_seconds",
                "laya_threads", "max_steps", "max_parallel"):
        try:
            merged[key] = max(1, int(merged[key]))
        except (TypeError, ValueError):
            merged[key] = DEFAULTS[key]
    for key in ("local_min_free_ram_gb", "laya_min_confidence"):
        try:
            merged[key] = float(merged[key])
        except (TypeError, ValueError):
            merged[key] = DEFAULTS[key]
    if not isinstance(merged["openrouter_models"], list):
        merged["openrouter_models"] = []
    if not isinstance(merged["requesty_models"], list):
        merged["requesty_models"] = []
    merged["requesty_models"] = [str(m).strip() for m in merged["requesty_models"] if str(m).strip()]
    if not isinstance(merged["gemini_models"], list):
        merged["gemini_models"] = []
    merged["gemini_models"] = [str(m).strip() for m in merged["gemini_models"] if str(m).strip()]
    if not isinstance(merged["nvidia_models"], list):
        merged["nvidia_models"] = []
    merged["nvidia_models"] = [str(m).strip() for m in merged["nvidia_models"] if str(m).strip()]
    merged["nvidia_api_key"] = str(merged["nvidia_api_key"] or "").strip()
    merged["nvidia_base_url"] = (str(merged["nvidia_base_url"] or "").strip().rstrip("/")
                                 or DEFAULTS["nvidia_base_url"])
    merged["gemini_api_key"] = str(merged["gemini_api_key"] or "").strip()
    merged["gemini_base_url"] = (str(merged["gemini_base_url"] or "").strip().rstrip("/")
                                 or DEFAULTS["gemini_base_url"])
    merged["requesty_api_key"] = str(merged["requesty_api_key"] or "").strip()
    merged["requesty_base_url"] = (str(merged["requesty_base_url"] or "").strip().rstrip("/")
                                   or DEFAULTS["requesty_base_url"])
    merged["openrouter_api_key"] = str(merged["openrouter_api_key"] or "").strip()
    return merged


def load() -> dict:
    target = path()
    try:
        stamp = (target.stat().st_mtime_ns, target.stat().st_size)
    except OSError:
        stamp = None
    with _lock:
        if _cache["data"] is not None and _cache["stamp"] == stamp:
            return dict(_cache["data"])
        data: dict = {}
        if stamp is not None:
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        merged = _coerce(data)
        _cache["stamp"], _cache["data"] = stamp, merged
        return dict(merged)


def save(changes: dict) -> dict:
    current = load()
    current.update({k: v for k, v in changes.items() if k in DEFAULTS})
    merged = _coerce(current)
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    os.replace(temp, target)
    with _lock:
        _cache["stamp"] = None
    return merged


def enabled() -> bool:
    try:
        return bool(load()["enabled"])
    except Exception:
        return False


def mask(secret: str) -> str:
    secret = secret or ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "..." + secret[-4:]
