"""Single shared text-search entry point.

Uses ripgrep when it is installed and falls back to a pure-Python scan when it
is not, so exclusion rules and results are identical either way.
"""

from __future__ import annotations

import json
import fnmatch
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Iterator

def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass



DEPENDENCY_DIRS = frozenset({
    ".cody_history", ".dart_tool", ".git", "graphify-out", ".idea", ".mypy_cache", ".next", ".pub-cache",
    ".pytest_cache", ".ruff_cache", ".venv", ".vscode", "__pycache__",
    "build", "coverage", "dist", "env", "node_modules", "site-packages",
    "target", "venv",
})

BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".tar", ".so", ".dylib", ".dll", ".exe", ".class", ".jar", ".pyc", ".pyo",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".lock",
})


def is_dependency_path(path: Path) -> bool:
    return any(part in DEPENDENCY_DIRS for part in path.parts)


TYPE_GLOBS = {
    "py": ("*.py", "*.pyi"), "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"), "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "dart": ("*.dart",), "java": ("*.java",), "kt": ("*.kt", "*.kts"), "go": ("*.go",), "rs": ("*.rs",),
    "c": ("*.c", "*.h"), "cpp": ("*.cc", "*.cpp", "*.cxx", "*.hpp", "*.hh", "*.h"), "cs": ("*.cs",),
    "html": ("*.html", "*.htm"), "css": ("*.css", "*.scss", "*.less"), "json": ("*.json",),
    "md": ("*.md", "*.markdown"), "yaml": ("*.yaml", "*.yml"), "toml": ("*.toml",), "sh": ("*.sh", "*.bash"),
    "sql": ("*.sql",), "xml": ("*.xml",), "swift": ("*.swift",), "php": ("*.php",), "rb": ("*.rb",),
    "vue": ("*.vue",), "svelte": ("*.svelte",), "ipynb": ("*.ipynb",),
}
TYPE_ALIASES = {
    "python": "py", "javascript": "js", "typescript": "ts", "rust": "rs", "kotlin": "kt", "ruby": "rb",
    "markdown": "md", "yml": "yaml", "csharp": "cs",
}


def type_globs(names: list[str]) -> list[str]:
    found: list[str] = []
    for raw in names:
        name = raw.strip().lower()
        if not name:
            continue
        key = TYPE_ALIASES.get(name, name)
        if key not in TYPE_GLOBS:
            raise ValueError(f"unknown --type {raw}; known: {', '.join(sorted(TYPE_GLOBS))}")
        found.extend(TYPE_GLOBS[key])
    return found


def glob_allows(path: Path | str, root: Path, globs: list[str]) -> bool:
    rel = os.path.relpath(path, root).replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]

    def hit(pattern: str) -> bool:
        return (
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern)
            or (pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]))
        )

    positive = [item for item in globs if not item.startswith("!")]
    if positive and not any(hit(item) for item in positive):
        return False
    return not any(hit(item[1:]) for item in globs if item.startswith("!"))


def iter_files(root: Path, exclude_deps: bool = True) -> Iterator[Path]:
    """Yield files below root, pruning dependency/build directories in place."""
    for directory, directories, filenames in os.walk(root):
        if exclude_deps:
            directories[:] = [name for name in directories if name not in DEPENDENCY_DIRS]
        for filename in filenames:
            yield Path(directory) / filename


def ripgrep_excludes() -> list[str]:
    return [flag for directory in sorted(DEPENDENCY_DIRS) for flag in ("-g", f"!**/{directory}/**")]


_MATCH_LINE = re.compile(r"^(\d+)([:-])(.*)$", re.S)


def _rg_command(
    pattern, target, regex, case_sensitive, context_lines, exclude_deps, globs, single, mode="lines", multiline=False,
) -> list[str]:
    command = ["rg", "--with-filename", "--follow", "--no-ignore"]
    if mode == "lines":
        command.extend(["--line-number", "--null", "--no-heading", "--max-columns", "300", "--max-columns-preview"])
    elif mode == "count":
        command.extend(["--count", "--null"])
    else:
        command.append("--files-with-matches")
    if single:
        command.extend(["-j", "1"])
    if multiline:
        command.append("--multiline")
    if not regex:
        command.append("--fixed-strings")
    if not case_sensitive:
        command.append("--ignore-case")
    if mode == "lines" and context_lines > 0:
        command.extend(["--context", str(context_lines)])
    if exclude_deps:
        command.extend(ripgrep_excludes())
    else:
        command.append("--hidden")
    for glob in globs or []:
        command.extend(["-g", glob])
    targets = list(target) if isinstance(target, (list, tuple)) else [target]
    command.extend(["-e", pattern, "--", *[str(item) for item in targets]])
    return command



def _stream(
    command: list[str], root: Path, max_results: int, context_lines: int, timeout: int,
    cancel: threading.Event | None = None,
) -> dict | None:
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    errors: list[str] = []
    drain = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)
    drain.start()
    state = {"timed_out": False, "killed": False}

    def expire() -> None:
        state["timed_out"] = True
        _kill(process)

    def watch() -> None:
        cancel.wait()
        if process.poll() is None:
            state["killed"] = True
            _kill(process)

    timer = threading.Timer(timeout, expire)
    timer.start()
    if cancel is not None:
        threading.Thread(target=watch, daemon=True).start()
    results: list[dict] = []
    before: list[str] = []
    current = None
    try:
        for raw in process.stdout:
            name, sep, rest = raw.rstrip("\r\n").partition("\0")
            parsed = _MATCH_LINE.match(rest) if sep else None
            if parsed is None:
                continue
            if name != current:
                current = name
                before = []
            text = parsed.group(3).rstrip()
            if parsed.group(2) == "-":
                before.append(text)
                before[:] = before[-max(context_lines, 1):]
                continue
            entry = {"file": os.path.relpath(name, root), "line": int(parsed.group(1)), "text": text[:300]}
            if context_lines > 0:
                entry["context"] = "\n".join(before + [entry["text"]])
            before = []
            results.append(entry)
            if len(results) >= max_results:
                _kill(process)
                break
    finally:
        timer.cancel()
    process.wait()
    process.stdout.close()
    drain.join(1)
    return {
        "results": results, "code": process.returncode, "stderr": "".join(errors),
        "timed_out": state["timed_out"], "killed": state["killed"],
    }



def search(
    root: Path,
    pattern: str,
    *,
    regex: bool = True,
    case_sensitive: bool = True,
    max_results: int = 200,
    context_lines: int = 0,
    exclude_deps: bool = True,
    globs: list[str] | None = None,
    timeout: int = 30,
    target: Path | None = None,
    multiline: bool = False,
) -> dict:
    """Run ripgrep and return {"results": [...], "engine": "rg"|"python"}.

    Always searches ignore-file-blind (--no-ignore): DEPENDENCY_DIRS is the
    single source of truth for what gets skipped, not whatever a project's
    own .gitignore/.ignore says. Respecting a repo's .gitignore here silently
    breaks search whenever it's broad (a bare "*", or something like
    "sites/"), which then wrongly leaves the tool reporting no matches from
    files that plainly exist.
    """
    outcome = None
    for single in (False, True):
        outcome = _stream(
            _rg_command(pattern, target or root, regex, case_sensitive, context_lines, exclude_deps, globs, single, "lines", multiline),
            root, max_results, context_lines, timeout,
        )
        if (
            outcome is None or outcome["results"] or outcome["timed_out"] or outcome["code"] in (0, 1)
            or "temporarily unavailable" not in outcome["stderr"].lower()
        ):
            break
    if outcome is None:
        return {
            "results": _fallback(root, pattern, regex, case_sensitive, max_results, context_lines, exclude_deps, target, globs, multiline),
            "engine": "python",
        }
    if outcome["results"]:
        return {"results": outcome["results"], "engine": "rg", "timed_out": outcome["timed_out"]}
    if outcome["timed_out"]:
        return {"results": [], "engine": "rg", "timed_out": True}
    if outcome["code"] in (0, 1):
        return {"results": [], "engine": "rg"}
    return {
        "results": _fallback(root, pattern, regex, case_sensitive, max_results, context_lines, exclude_deps, target, globs, multiline),
        "engine": "python",
        "rg_stderr": outcome["stderr"].strip()[:500] or None,
    }



def _stream_files(command: list[str], root: Path, max_files: int, timeout: int, counted: bool) -> dict | None:
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    errors: list[str] = []
    drain = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)
    drain.start()
    state = {"timed_out": False}

    def expire() -> None:
        state["timed_out"] = True
        _kill(process)

    timer = threading.Timer(timeout, expire)
    timer.start()
    files: list[tuple[str, int | None]] = []
    truncated = False
    try:
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            if counted:
                name, sep, count = line.partition("\0")
                if not sep or not count.isdigit():
                    continue
                files.append((os.path.relpath(name, root), int(count)))
            else:
                files.append((os.path.relpath(line, root), None))
            if len(files) >= max_files:
                truncated = True
                _kill(process)
                break
    finally:
        timer.cancel()
    process.wait()
    process.stdout.close()
    drain.join(1)
    return {
        "files": files, "code": process.returncode, "stderr": "".join(errors),
        "timed_out": state["timed_out"], "truncated": truncated,
    }


def search_files(
    root: Path,
    pattern: str,
    *,
    regex: bool = True,
    case_sensitive: bool = True,
    exclude_deps: bool = True,
    globs: list[str] | None = None,
    timeout: int = 30,
    target: Path | None = None,
    multiline: bool = False,
    counts: bool = True,
    max_files: int = 2000,
) -> dict:
    outcome = None
    for single in (False, True):
        outcome = _stream_files(
            _rg_command(pattern, target or root, regex, case_sensitive, 0, exclude_deps, globs, single, "count" if counts else "list", multiline),
            root, max_files, timeout, counts,
        )
        if (
            outcome is None or outcome["files"] or outcome["timed_out"] or outcome["code"] in (0, 1)
            or "temporarily unavailable" not in outcome["stderr"].lower()
        ):
            break
    if outcome is not None and (outcome["files"] or outcome["timed_out"] or outcome["code"] in (0, 1)):
        return {
            "files": outcome["files"], "engine": "rg",
            "timed_out": outcome["timed_out"], "truncated": outcome["truncated"],
        }
    hits = _fallback(root, pattern, regex, case_sensitive, 10 ** 9, 0, exclude_deps, target, globs, multiline)
    tally: dict[str, int] = {}
    for hit in hits:
        tally[hit["file"]] = tally.get(hit["file"], 0) + 1
    rows = [(name, count if counts else None) for name, count in tally.items()]
    return {
        "files": rows[:max_files], "engine": "python", "timed_out": False, "truncated": len(rows) > max_files,
        "rg_stderr": (outcome["stderr"].strip()[:500] or None) if outcome is not None else None,
    }


def _fallback(
    root, pattern, regex, case_sensitive, max_results, context_lines, exclude_deps, target=None, globs=None, multiline=False,
) -> list[dict]:
    flags = re.MULTILINE if multiline else 0
    if not case_sensitive:
        flags |= re.IGNORECASE
    matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    results: list[dict] = []
    for file_path in ([target] if target is not None and target.is_file() else iter_files(root, exclude_deps)):
        if file_path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if globs and not glob_allows(file_path, root, globs):
            continue
        try:
            body = file_path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        lines = body.splitlines()
        if multiline:
            spots = [body.count("\n", 0, found.start()) + 1 for found in matcher.finditer(body)]
        else:
            spots = [number for number, line in enumerate(lines, start=1) if matcher.search(line)]
        for number in spots:
            line = lines[number - 1] if number <= len(lines) else ""
            entry = {
                "file": os.path.relpath(file_path, root),
                "line": number,
                "text": line.rstrip()[:300],
            }
            if context_lines > 0:
                start = max(1, number - context_lines)
                end = min(len(lines), number + context_lines)
                entry["context"] = "\n".join(
                    f"{n:>5} | {lines[n - 1].rstrip()}" for n in range(start, end + 1)
                )
            results.append(entry)
            if len(results) >= max_results:
                return results
    return results

