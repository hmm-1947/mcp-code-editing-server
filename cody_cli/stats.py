from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "tray_logs" / "cody_stats.jsonl"


def enabled() -> bool:
    return os.environ.get("CODY_STATS", "1") != "0"


def record(command: str, argv: list[str], output: str, ms: int) -> None:
    if not enabled():
        return
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "t": int(time.time()),
            "cmd": command,
            "chars": len(output),
            "lines": output.count("\n") + 1,
            "ms": ms,
            "err": output.startswith("ERR"),
            "arg": " ".join(argv)[:120],
        }
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def summary(limit: int = 0) -> str:
    if not LOG.is_file():
        return "no stats yet"
    rows = []
    with LOG.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if limit:
        rows = rows[-limit:]
    if not rows:
        return "no stats yet"
    by: dict[str, list[int]] = {}
    for row in rows:
        by.setdefault(row["cmd"], []).append(row["chars"])
    total = sum(sum(v) for v in by.values())
    out = [f"{len(rows)} calls, {total} chars (~{total // 4} tok)"]
    for name, sizes in sorted(by.items(), key=lambda kv: -sum(kv[1])):
        out.append(
            f"{name:<8} n={len(sizes):<4} total={sum(sizes):<7} avg={sum(sizes) // len(sizes):<6} max={max(sizes)}"
        )
    worst = sorted(rows, key=lambda r: -r["chars"])[:3]
    out.append("largest: " + " | ".join(f"{r['cmd']} {r['chars']}c {r['arg'][:40]}" for r in worst))
    return "\n".join(out)


def reset() -> str:
    try:
        LOG.unlink(missing_ok=True)
    except OSError as error:
        return f"ERR {error}"
    return "ok stats cleared"
