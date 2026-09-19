"""Per-file edit history: snapshot before every write, undo restores the last one."""

from __future__ import annotations

import time
from pathlib import Path

HISTORY_DIR_NAME = ".cody_history"
MAX_SNAPSHOTS_PER_FILE = 20


def _history_dir(target: Path) -> Path:
    return target.parent / HISTORY_DIR_NAME / target.name


def snapshot(target: Path) -> Path | None:
    """Save the current on-disk contents of target before it gets overwritten.

    Returns the snapshot path, or None if the file doesn't exist yet (nothing
    to snapshot on create).
    """
    if not target.is_file():
        return None

    directory = _history_dir(target)
    directory.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap_path = directory / f"{stamp}.bak"
    counter = 1
    while snap_path.exists():
        snap_path = directory / f"{stamp}-{counter}.bak"
        counter += 1

    snap_path.write_bytes(target.read_bytes())
    _prune(directory)
    return snap_path


def _prune(directory: Path) -> None:
    snaps = sorted(directory.glob("*.bak"), key=lambda p: p.stat().st_mtime)
    excess = len(snaps) - MAX_SNAPSHOTS_PER_FILE
    for old in snaps[:max(0, excess)]:
        old.unlink(missing_ok=True)


def list_snapshots(target: Path) -> list[Path]:
    directory = _history_dir(target)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.bak"), key=lambda p: p.stat().st_mtime)


def undo(target: Path) -> Path | None:
    """Restore target from its most recent snapshot. Returns the snapshot used, or None."""
    snaps = list_snapshots(target)
    if not snaps:
        return None
    latest = snaps[-1]
    if target.is_file():
        directory = _history_dir(target)
        redo_stamp = time.strftime("%Y%m%d-%H%M%S")
        redo_path = directory / f"{redo_stamp}-preundo.bak"
        redo_path.write_bytes(target.read_bytes())
    target.write_bytes(latest.read_bytes())
    latest.unlink(missing_ok=True)
    return latest
