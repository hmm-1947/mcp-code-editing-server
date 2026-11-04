"""Who handles a task: Laya classifies it, then the mode, rate limits and machine state pick the model."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import openrouter, settings
from .laya_bridge import LAYA
from .lmstudio import LOCAL
from .openrouter import BackendError
from .ratelimit import RateLimiter

KINDS = {
    "explore": "locate or explain code: where something is defined or used, how files or modules fit together",
    "digest": "summarize or extract the key points from logs, command output, test results or error messages",
    "code": "write, rewrite or review a small piece of code, a test or a config file",
    "reason": "design, debug a hard problem, compare tradeoffs, analyze something in several steps",
    "simple": "reword, format, convert, or answer a short factual question",
}
COMPLEXITY = {
    "low": "one short step on a small input",
    "medium": "a few steps, or several files or a medium input",
    "high": "deep multi-step work or a very large input",
}
QUESTIONS = {
    "kind": {"type": "choice", "instructions": "What kind of task is this?", "criteria": KINDS},
    "complexity": {"type": "choice", "instructions": "How hard is this task?", "criteria": COMPLEXITY},
}

HEURISTICS = (
    ("digest", r"\b(log|logs|traceback|stack ?trace|stdout|output|pytest|test results?|failing|failed|error message)\b"),
    ("code", r"\b(write|implement|refactor|rewrite|generate|unit tests?|function|snippet|review)\b"),
    ("reason", r"\b(why|design|architecture|trade-?offs?|debug|root cause|compare|plan|strategy|analy[sz]e)\b"),
    ("explore", r"\b(find|where|locate|which file|how does|explain|callers?|usages?|defined|structure|overview)\b"),
    ("simple", r"\b(rephrase|reword|format|convert|translate|capitali[sz]e|what is)\b"),
)

LIMITER = RateLimiter(18)
HEALTH: dict[str, int] = {}
_LAST_KEY: dict = {"value": None}


@dataclass
class Decision:
    kind: str = "explore"
    complexity: str = "medium"
    source: str = "heuristic"
    confidence: float = 0.0
    ms: int = 0
    note: str = ""


@dataclass
class Candidate:
    backend: str
    model: str = ""
    why: str = ""


@dataclass
class Plan:
    chain: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def heuristic(task: str, context_chars: int = 0) -> Decision:
    text = task.lower()
    kind = "reason" if len(text) > 700 else "explore"
    for name, pattern in HEURISTICS:
        if re.search(pattern, text):
            kind = name
            break
    size = len(task) + context_chars
    complexity = "low" if size < 600 else ("medium" if size < 6000 else "high")
    if kind == "reason" and complexity == "low":
        complexity = "medium"
    return Decision(kind=kind, complexity=complexity, source="heuristic")


def decide(task: str, context_chars: int = 0, forced_kind: str = "") -> Decision:
    cfg = settings.load()
    base = heuristic(task, context_chars)
    if forced_kind in KINDS:
        base.kind, base.source = forced_kind, "forced"
        return base
    if not cfg["laya_enabled"]:
        base.note = "laya disabled"
        return base
    started = time.time()
    try:
        answers = LAYA.predict({"task": task[:1800], "input_chars": context_chars}, QUESTIONS)
    except BackendError as error:
        base.note = f"laya unavailable ({error}); heuristic used"
        base.ms = int((time.time() - started) * 1000)
        return base
    kind, complexity = answers.get("kind", {}), answers.get("complexity", {})
    confidence = float(kind.get("answer_confidence") or 0.0)
    decision = Decision(
        kind=kind.get("choice") if kind.get("choice") in KINDS else base.kind,
        complexity=complexity.get("choice") if complexity.get("choice") in COMPLEXITY else base.complexity,
        source="laya", confidence=confidence, ms=int((time.time() - started) * 1000))
    if confidence < cfg["laya_min_confidence"]:
        decision.kind, decision.source = base.kind, "heuristic"
        decision.note = f"laya unsure ({confidence:.2f}); heuristic kind"
    return decision


def openrouter_state() -> tuple[bool, str]:
    cfg = settings.load()
    LIMITER.configure(cfg["openrouter_rpm"])
    if _LAST_KEY["value"] != cfg["openrouter_api_key"]:
        if _LAST_KEY["value"] is not None:
            LIMITER.unblock()
            HEALTH.clear()
        _LAST_KEY["value"] = cfg["openrouter_api_key"]
    if not cfg["openrouter_api_key"]:
        return False, "no OpenRouter API key set"
    blocked = LIMITER.blocked_for()
    if blocked > 0:
        return False, f"OpenRouter blocked for {int(blocked)}s ({LIMITER.blocked_reason() or 'limit'})"
    wait = LIMITER.wait_needed()
    if wait > cfg["openrouter_max_wait"]:
        return False, f"OpenRouter rate limit reached ({LIMITER.used()}/{LIMITER.rpm} per minute)"
    return True, ""


def _score(model, decision: Decision, needed_tokens: int) -> float:
    score = 0.0
    kind, tags = decision.kind, model.tags
    if kind == "code" and "coder" in tags:
        score += 3
    if kind == "reason" and "reasoning" in tags:
        score += 3
    if kind in ("explore", "digest", "simple") and "reasoning" in tags:
        score -= 1
    if "general" in tags:
        score += 0.5
    size = min(model.size_b, 120.0)
    if decision.complexity == "low":
        score += 1.5 if 0 < size <= 32 else 0
        score -= size / 80.0
    elif decision.complexity == "high":
        score += size / 20.0
    else:
        score += min(size, 70.0) / 40.0
    if model.context and model.context < needed_tokens:
        score -= 100
    score -= 2 * HEALTH.get(model.id, 0)
    return score


def openrouter_candidates(decision: Decision, needed_tokens: int, limit: int = 3) -> list[Candidate]:
    cfg = settings.load()
    pinned = cfg["openrouter_models"]
    models = [openrouter.FreeModel(id=m, size_b=openrouter._size_b(m), tags=openrouter._tags(m, ""))
              for m in pinned] if pinned else openrouter.free_models()
    usable = [m for m in models if LIMITER.cooling(m.id) <= 0]
    ranked = sorted(usable, key=lambda m: (-_score(m, decision, needed_tokens), m.id))
    return [Candidate("openrouter", m.id, f"{m.size_b:g}b" if m.size_b else "") for m in ranked[:limit]]


def requesty_state() -> tuple[bool, str]:
    cfg = settings.load()
    if not cfg["requesty_enabled"]:
        return False, "Requesty is switched off"
    if not cfg["requesty_api_key"]:
        return False, "no Requesty API key set"
    if not cfg["requesty_models"]:
        return False, "no Requesty models listed"
    return True, ""


def requesty_candidates(limit: int = 3) -> list[Candidate]:
    cfg = settings.load()
    usable = [m for m in cfg["requesty_models"] if LIMITER.cooling("requesty:" + m) <= 0]
    return [Candidate("requesty", m, "requesty") for m in usable[:limit]]


def gemini_state() -> tuple[bool, str]:
    cfg = settings.load()
    if not cfg["gemini_enabled"]:
        return False, "Gemini is switched off"
    if not cfg["gemini_api_key"]:
        return False, "no Gemini API key set"
    if not cfg["gemini_models"]:
        return False, "no Gemini models listed"
    return True, ""


def gemini_candidates(limit: int = 3) -> list[Candidate]:
    cfg = settings.load()
    usable = [m for m in cfg["gemini_models"] if LIMITER.cooling("gemini:" + m) <= 0]
    return [Candidate("gemini", m, "gemini") for m in usable[:limit]]


def nvidia_state() -> tuple[bool, str]:
    cfg = settings.load()
    if not cfg["nvidia_enabled"]:
        return False, "NVIDIA NIM is switched off"
    if not cfg["nvidia_api_key"]:
        return False, "no NVIDIA NIM API key set"
    if not cfg["nvidia_models"]:
        return False, "no NVIDIA NIM models listed"
    return True, ""


def nvidia_candidates(limit: int = 3) -> list[Candidate]:
    cfg = settings.load()
    usable = [m for m in cfg["nvidia_models"] if LIMITER.cooling("nvidia:" + m) <= 0]
    return [Candidate("nvidia", m, "nvidia") for m in usable[:limit]]


def build_plan(decision: Decision, needed_tokens: int, forced_backend: str = "auto") -> Plan:
    cfg = settings.load()
    mode = cfg["mode"]
    plan = Plan()
    only = ""
    if forced_backend in ("openrouter", "requesty", "gemini", "nvidia", "local"):
        target = "local" if forced_backend == "local" else "openrouter"
        if mode in ("openrouter", "local") and target != mode:
            plan.notes.append(f"-backend {forced_backend} ignored: mode is {mode} only")
        else:
            mode = target
            only = forced_backend
    want_requesty = mode in ("openrouter", "both") and only in ("", "requesty")
    want_gemini = mode in ("openrouter", "both") and only in ("", "gemini")
    want_nvidia = mode in ("openrouter", "both") and only in ("", "nvidia")
    want_or = mode in ("openrouter", "both") and only in ("", "openrouter")
    want_local = mode in ("local", "both")
    if want_requesty and (only == "requesty" or cfg["requesty_enabled"]):
        usable, reason = requesty_state()
        if usable:
            chain = requesty_candidates()
            if not chain:
                plan.notes.append("all Requesty models are cooling down")
            plan.chain += chain
        else:
            plan.notes.append(reason)
    if want_gemini and (only == "gemini" or cfg["gemini_enabled"]):
        usable, reason = gemini_state()
        if usable:
            chain = gemini_candidates()
            if not chain:
                plan.notes.append("all Gemini models are cooling down")
            plan.chain += chain
        else:
            plan.notes.append(reason)
    if want_nvidia and (only == "nvidia" or cfg["nvidia_enabled"]):
        usable, reason = nvidia_state()
        if usable:
            chain = nvidia_candidates()
            if not chain:
                plan.notes.append("all NVIDIA NIM models are cooling down")
            plan.chain += chain
        else:
            plan.notes.append(reason)
    if want_or:
        usable, reason = openrouter_state()
        if usable:
            chain = openrouter_candidates(decision, needed_tokens)
            if not chain:
                plan.notes.append("no OpenRouter free model available")
            plan.chain += chain
        else:
            plan.notes.append(reason)
    if want_local:
        if mode == "both" and plan.chain:
            plan.notes.append("local is the fallback")
        plan.chain.append(Candidate("local", cfg["local_model"] or "auto", "fallback" if mode == "both" else ""))
    return plan


def record_result(candidate: Candidate, ok: bool) -> None:
    if candidate.backend in ("requesty", "gemini", "nvidia"):
        key = candidate.backend + ":" + candidate.model
        HEALTH[key] = 0 if ok else min(HEALTH.get(key, 0) + 1, 5)
        return
    if candidate.backend != "openrouter":
        return
    HEALTH[candidate.model] = 0 if ok else min(HEALTH.get(candidate.model, 0) + 1, 5)


def handle_error(candidate: Candidate, error: BackendError) -> None:
    record_result(candidate, False)
    if candidate.backend in ("requesty", "gemini", "nvidia"):
        key = candidate.backend + ":" + candidate.model
        if error.kind in ("auth", "credit"):
            LIMITER.cool(key, 600)
        elif error.kind == "gone":
            LIMITER.cool(key, 3600)
        elif error.kind in ("rate", "daily"):
            LIMITER.cool(key, max(20.0, error.retry_after))
        elif error.kind == "transient":
            LIMITER.cool(key, 45)
        else:
            LIMITER.cool(key, 180)
        return
    if candidate.backend != "openrouter":
        return
    if error.kind == "daily":
        LIMITER.block(error.retry_after or 3600, "daily free limit reached")
    elif error.kind == "rate":
        LIMITER.cool(candidate.model, max(20.0, error.retry_after))
    elif error.kind in ("auth", "credit"):
        LIMITER.block(600, "OpenRouter key rejected" if error.kind == "auth" else "OpenRouter credits")
    elif error.kind == "gone":
        LIMITER.cool(candidate.model, 3600)
    elif error.kind == "transient":
        LIMITER.cool(candidate.model, 45)
    else:
        LIMITER.cool(candidate.model, 180)
