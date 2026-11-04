"""OpenRouter client (stdlib only): free-model discovery and chat completions."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import app_paths

BASE = "https://openrouter.ai/api/v1"
USER_AGENT = "CodyMCP/1.0 (+https://github.com/)"
MODELS_TTL = 6 * 3600
SKIP_WORDS = ("embed", "guard", "safety", "moderation", "audio", "image", "lyria", "tts", "whisper", "ocr", "rerank")


class BackendError(Exception):
    """kind: rate | daily | auth | credit | gone | transient | empty | bad"""

    def __init__(self, kind: str, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


@dataclass
class FreeModel:
    id: str
    context: int = 0
    size_b: float = 0.0
    tags: set = field(default_factory=set)


def _cache_file() -> Path:
    return app_paths.logs_dir() / "openrouter_free_models.json"


def _request(method: str, url: str, key: str = "", body: dict | None = None, timeout: float = 30.0):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if key:
        headers["Authorization"] = "Bearer " + key
        headers["HTTP-Referer"] = "https://localhost/cody-mcp"
        headers["X-Title"] = "Cody MCP"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace")), dict(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        raise _classify(error.code, raw, dict(error.headers)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BackendError("transient", f"network: {getattr(error, 'reason', error)}") from None
    except ValueError:
        raise BackendError("transient", "invalid JSON from OpenRouter") from None


def _classify(status: int, raw: str, headers: dict) -> BackendError:
    message = raw[:300]
    metadata: dict = {}
    try:
        parsed = json.loads(raw)
        error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
        message = str(error.get("message") or message)[:300]
        metadata = error.get("metadata") or {}
    except ValueError:
        pass
    retry = 0.0
    try:
        retry = float(headers.get("Retry-After", 0) or 0)
    except ValueError:
        retry = 0.0
    if status == 429:
        reset_header = (metadata.get("headers") or {}).get("X-RateLimit-Reset") or headers.get("X-RateLimit-Reset")
        if "per-day" in message or "per day" in message.lower():
            wait = 3600.0
            try:
                wait = max(60.0, float(reset_header) / 1000.0 - time.time())
            except (TypeError, ValueError):
                pass
            return BackendError("daily", message, min(wait, 86400.0))
        return BackendError("rate", message, retry or 30.0)
    if status in (401, 403):
        return BackendError("auth", message or "invalid API key")
    if status == 402:
        return BackendError("credit", message or "insufficient credits")
    if status == 404:
        return BackendError("gone", message or "model not found")
    if status in (408, 409, 425) or status >= 500:
        return BackendError("transient", f"HTTP {status}: {message}", retry)
    return BackendError("bad", f"HTTP {status}: {message}")


def _tags(model_id: str, description: str) -> set:
    text = (model_id + " " + description).lower()
    tags = set()
    if re.search(r"code|coder|devstral|codestral|starcoder", text):
        tags.add("coder")
    if re.search(r"r1|reason|thinking|qwq|think|magistral|deepseek-v3|gpt-oss|glm", text):
        tags.add("reasoning")
    if re.search(r"instruct|chat|-it\b|gemma|llama|mistral|qwen|nemotron|hermes", text):
        tags.add("general")
    return tags


def _size_b(model_id: str) -> float:
    sizes = [float(m) for m in re.findall(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b\b", model_id.lower())]
    return max(sizes) if sizes else 0.0


def parse_models(payload: dict) -> list[FreeModel]:
    found: list[FreeModel] = []
    for item in payload.get("data", []):
        model_id = str(item.get("id", ""))
        pricing = item.get("pricing") or {}
        if not model_id.endswith(":free"):
            continue
        if str(pricing.get("prompt", "1")) not in ("0", "0.0") or str(pricing.get("completion", "1")) not in ("0", "0.0"):
            continue
        if model_id.startswith("openrouter/") or any(word in model_id.lower() for word in SKIP_WORDS):
            continue
        arch = item.get("architecture") or {}
        if "text" not in (arch.get("input_modalities") or ["text"]) or (arch.get("output_modalities") or ["text"]) != ["text"]:
            continue
        found.append(FreeModel(
            id=model_id,
            context=int(item.get("context_length") or 0),
            size_b=_size_b(model_id),
            tags=_tags(model_id, str(item.get("description", ""))[:400]),
        ))
    return found


def free_models(force: bool = False) -> list[FreeModel]:
    cache = _cache_file()
    if not force:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("t", 0)) < MODELS_TTL:
                return parse_models(cached["payload"])
        except (OSError, ValueError, KeyError):
            pass
    try:
        payload, _ = _request("GET", BASE + "/models", timeout=20.0)
    except BackendError:
        try:
            return parse_models(json.loads(cache.read_text(encoding="utf-8"))["payload"])
        except (OSError, ValueError, KeyError):
            return []
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"t": time.time(), "payload": payload}), encoding="utf-8")
    except OSError:
        pass
    return parse_models(payload)


def fetch_free_ids(free_only: bool = True) -> list[str]:
    payload, _ = _request("GET", BASE + "/models", timeout=20.0)
    if not free_only:
        return sorted({str(i.get("id", "")) for i in payload.get("data", []) if i.get("id")})
    return sorted({m.id for m in parse_models(payload)})


def chat(key: str, model: str, messages: list[dict], max_tokens: int = 900, timeout: float = 90.0,
         base: str = BASE) -> dict:
    payload, _ = _request("POST", base.rstrip("/") + "/chat/completions", key, {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }, timeout)
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        status = code if isinstance(code, int) else 500
        raise _classify(status, json.dumps(payload), {})
    try:
        choice = payload["choices"][0]
        text = (choice["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise BackendError("empty", "response had no choices") from None
    if not text:
        raise BackendError("empty", "model returned empty content")
    usage = payload.get("usage") or {}
    return {
        "text": text,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "model": str(payload.get("model") or model),
    }


def key_info(key: str) -> dict:
    payload, _ = _request("GET", BASE + "/key", key, timeout=15.0)
    return payload.get("data", payload) if isinstance(payload, dict) else {}


def model_ids(base: str, key: str) -> list[str]:
    payload, _ = _request("GET", base.rstrip("/") + "/models", key, timeout=20.0)
    items = payload.get("data", []) if isinstance(payload, dict) else []
    return sorted({str(item.get("id", "")) for item in items if isinstance(item, dict) and item.get("id")})
EXTRA_SKIP = ("live", "veo", "imagen", "aqa", "robotics", "computer-use", "rerank", "retriev", "parse", "clip", "reward")
PRICE_KEYS = ("prompt", "completion", "input", "output", "input_price", "output_price", "prompt_price", "completion_price")


def _prices(item: dict) -> list[float]:
    found = []
    for source, keys in ((item.get("pricing"), PRICE_KEYS), (item, PRICE_KEYS[4:])):
        if not isinstance(source, dict):
            continue
        for key in keys:
            try:
                found.append(float(source[key]))
            except (KeyError, TypeError, ValueError):
                pass
    return found


def free_model_ids(which: str, base: str, key: str, free_only: bool = True) -> list[str]:
    payload, _ = _request("GET", base.rstrip("/") + "/models", key, timeout=20.0)
    items = payload.get("data", []) if isinstance(payload, dict) else []
    ids = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        model_id = str(item["id"])
        if which == "gemini" and model_id.startswith("models/"):
            model_id = model_id[7:]
        lowered = model_id.lower()
        if not free_only:
            ids.add(model_id)
            continue
        if any(word in lowered for word in SKIP_WORDS + EXTRA_SKIP):
            continue
        if which == "gemini" and not re.search(r"flash|gemma", lowered):
            continue
        if which == "requesty":
            prices = _prices(item)
            if not lowered.endswith(":free") and (not prices or any(p > 0 for p in prices)):
                continue
        ids.add(model_id)
    return sorted(ids)
