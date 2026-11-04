"""Machine resource sampling for the GUI and for local-model admission checks."""
from __future__ import annotations

import shutil
import subprocess
import sys

try:
    import psutil
except ImportError:
    psutil = None

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
LMSTUDIO_NAMES = ("lm studio", "lm-studio", "llmster", "lms")
_procs: dict = {}


def _run(args: list[str], timeout: float = 4.0) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def gpu() -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits"])
    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None

    def number(text: str):
        try:
            return float(text)
        except ValueError:
            return None

    info = {
        "name": parts[0],
        "util": number(parts[1]),
        "used_mb": number(parts[2]),
        "total_mb": number(parts[3]),
        "temp_c": number(parts[4]) if len(parts) > 4 else None,
        "power_w": number(parts[5]) if len(parts) > 5 else None,
        "by_pid": {},
    }
    apps = _run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
    for row in apps.strip().splitlines():
        pid, _, mem = row.partition(",")
        if pid.strip().isdigit() and number(mem.strip()) is not None:
            info["by_pid"][int(pid.strip())] = number(mem.strip())
    return info


def gpu_free_mb() -> float | None:
    info = gpu()
    if not info or info["total_mb"] is None or info["used_mb"] is None:
        return None
    return info["total_mb"] - info["used_mb"]


def system() -> dict:
    if psutil is None:
        return {}
    memory = psutil.virtual_memory()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(logical=True),
        "ram_used_gb": (memory.total - memory.available) / 1e9,
        "ram_total_gb": memory.total / 1e9,
        "ram_available_gb": memory.available / 1e9,
        "ram_percent": memory.percent,
    }


def ram_available_gb() -> float | None:
    if psutil is None:
        return None
    return psutil.virtual_memory().available / 1e9


def _tracked(proc):
    cached = _procs.get(proc.pid)
    if cached is None or cached.create_time() != proc.create_time():
        _procs[proc.pid] = proc
        proc.cpu_percent(None)
        return proc
    return cached


def group(label: str, pids: list[int], gpu_by_pid: dict) -> dict | None:
    """CPU / RAM / VRAM summed over `pids` and their children."""
    if psutil is None:
        return None
    seen: dict[int, object] = {}
    for pid in pids:
        try:
            root = psutil.Process(pid)
            for proc in [root] + root.children(recursive=True):
                seen[proc.pid] = proc
        except psutil.Error:
            continue
    if not seen:
        return None
    cpu = rss = 0.0
    vram = 0.0
    have_vram = False
    for pid, proc in seen.items():
        try:
            tracked = _tracked(proc)
            cpu += tracked.cpu_percent(None)
            rss += tracked.memory_info().rss
        except psutil.Error:
            continue
        if pid in gpu_by_pid:
            vram += gpu_by_pid[pid]
            have_vram = True
    return {"label": label, "pids": sorted(seen), "cpu": cpu, "ram_mb": rss / 1e6,
            "vram_mb": vram if have_vram else None}


def lmstudio_pids() -> list[int]:
    if psutil is None:
        return []
    found = []
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name in LMSTUDIO_NAMES or name.startswith("lm studio"):
            found.append(proc.pid)
    return found


def snapshot(laya_pid: int | None = None) -> dict:
    gpu_info = gpu()
    by_pid = gpu_info["by_pid"] if gpu_info else {}
    result = {"gpu": gpu_info, "system": system(), "lmstudio": None, "laya": None}
    pids = lmstudio_pids()
    if pids:
        result["lmstudio"] = group("LM Studio", pids, by_pid)
    if laya_pid:
        result["laya"] = group("Laya worker", [laya_pid], by_pid)
    return result
