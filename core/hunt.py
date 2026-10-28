from __future__ import annotations

import collections
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import search

CHUNK = 50
MAX_DEPTH = 3
_HUNTS: dict[str, "Hunt"] = {}
_LOCK = threading.Lock()


@dataclass
class Hunt:
    root: Path
    pattern: str
    regex: bool
    case_sensitive: bool
    globs: list[str]
    exclude_deps: bool
    multiline: bool
    workers: int
    timeout: int
    remaining: list[tuple[str, ...]]
    total: int
    excluded: list[str] = field(default_factory=list)
    clean: int = 0
    timed_out: list[str] = field(default_factory=list)
    current: tuple[tuple[str, ...], str] | None = None


def _is_dir_unit(unit: tuple[str, ...]) -> bool:
    return len(unit) == 1 and os.path.isdir(unit[0])


def _label(unit: tuple[str, ...], root: Path) -> str:
    first = os.path.relpath(unit[0], root).replace("\\", "/")
    return first if len(unit) == 1 else f"{first} (+{len(unit) - 1} files)"


def _escape(path: str) -> str:
    return "".join(f"[{char}]" if char in "[]{}*?!" else char for char in path)


def make_units(root: Path, target: Path | None, globs: list[str], exclude_deps: bool, want: int) -> list[tuple[str, ...]]:
    if target is not None and target.is_file():
        return [(str(target),)]
    dirs = [str(target or root)]
    files: list[str] = []
    for _ in range(MAX_DEPTH):
        if len(dirs) >= want:
            break
        expanded: list[str] = []
        grew = False
        for directory in dirs:
            try:
                with os.scandir(directory) as scan:
                    entries = sorted(scan, key=lambda entry: entry.name)
            except OSError:
                expanded.append(directory)
                continue
            for entry in entries:
                if exclude_deps and (entry.name in search.DEPENDENCY_DIRS or entry.name.startswith(".")):
                    continue
                try:
                    if entry.is_dir():
                        expanded.append(entry.path)
                        grew = True
                    elif entry.is_file() and (not globs or search.glob_allows(entry.path, root, globs)):
                        files.append(entry.path)
                except OSError:
                    continue
        dirs = expanded
        if not grew:
            break
    units = [tuple(files[index:index + CHUNK]) for index in range(0, len(files), CHUNK)]
    return units + [(directory,) for directory in dirs]


def _command(hunt: Hunt, unit: tuple[str, ...]) -> list[str]:
    globs = None
    if _is_dir_unit(unit):
        globs = hunt.globs + [f"!**/{_escape(rel)}" for rel in hunt.excluded]
    return search._rg_command(
        hunt.pattern, list(unit), hunt.regex, hunt.case_sensitive, 0, hunt.exclude_deps, globs, True, "lines", hunt.multiline,
    )


def _scan(hunt: Hunt) -> dict | None:
    cancel = threading.Event()
    lock = threading.Lock()
    queue = collections.deque(hunt.remaining)
    pending: list[tuple[str, ...]] = []
    timed: list[tuple[str, ...]] = []
    state: dict = {"winner": None, "error": None, "clean": 0}

    def worker() -> None:
        while True:
            with lock:
                if cancel.is_set() or not queue:
                    return
                unit = queue.popleft()
            outcome = search._stream(_command(hunt, unit), hunt.root, 1, 0, hunt.timeout, cancel)
            with lock:
                if outcome is None:
                    state["error"] = "ripgrep (rg) not found"
                    cancel.set()
                    return
                if outcome["results"]:
                    if state["winner"] is None and not cancel.is_set():
                        state["winner"] = (unit, outcome["results"][0])
                        cancel.set()
                    else:
                        pending.append(unit)
                elif outcome["timed_out"]:
                    timed.append(unit)
                elif outcome["killed"]:
                    pending.append(unit)
                elif outcome["code"] in (0, 1):
                    state["clean"] += 1
                else:
                    state["error"] = outcome["stderr"].strip()[:300] or "search failed"
                    cancel.set()
                    return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(hunt.workers, len(queue))))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    cancel.set()
    hunt.clean += state["clean"]
    hunt.timed_out += [_label(unit, hunt.root) for unit in timed]
    winner = state["winner"]
    hunt.remaining = pending + list(queue)
    if state["error"]:
        raise ValueError(state["error"])
    if winner is None:
        hunt.current = None
        return None
    unit, hit = winner
    hunt.current = (unit, hit["file"])
    return hit


def _advance(session: str, hunt: Hunt) -> dict:
    try:
        hit = _scan(hunt)
    except ValueError:
        with _LOCK:
            _HUNTS.pop(session, None)
        raise
    with _LOCK:
        if hit is None:
            _HUNTS.pop(session, None)
        else:
            _HUNTS[session] = hunt
    return {
        "hit": hit, "clean": hunt.clean, "total": hunt.total, "left": len(hunt.remaining),
        "timed_out": list(hunt.timed_out), "excluded": len(hunt.excluded),
    }


def begin(
    session: str, root: Path, target: Path | None, pattern: str, *, regex: bool, case_sensitive: bool,
    globs: list[str], exclude_deps: bool, multiline: bool, workers: int, timeout: int,
) -> dict:
    units = make_units(root, target, globs, exclude_deps, workers * 2)
    hunt = Hunt(
        root=root, pattern=pattern, regex=regex, case_sensitive=case_sensitive, globs=list(globs),
        exclude_deps=exclude_deps, multiline=multiline, workers=workers, timeout=timeout,
        remaining=units, total=len(units),
    )
    return _advance(session, hunt)


def resume(session: str, drop_unit: bool) -> dict:
    with _LOCK:
        hunt = _HUNTS.get(session)
    if hunt is None or hunt.current is None:
        raise ValueError("no hunt in progress; start one with: find QUERY --first")
    unit, rel = hunt.current
    hunt.current = None
    if not drop_unit:
        key = os.path.normcase(os.path.abspath(hunt.root / rel))
        explicit = not _is_dir_unit(unit) and any(os.path.normcase(os.path.abspath(path)) == key for path in unit)
        if explicit:
            rest = tuple(path for path in unit if os.path.normcase(os.path.abspath(path)) != key)
            if rest:
                hunt.remaining.insert(0, rest)
        else:
            hunt.excluded.append(rel.replace(os.sep, "/"))
            hunt.remaining.insert(0, unit)
    return _advance(session, hunt)
