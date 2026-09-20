"""Single shared text-search entry point.

Uses ripgrep when it is installed and falls back to a pure-Python scan when it
is not, so exclusion rules and results are identical either way.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterator

DEPENDENCY_DIRS = frozenset({
    ".cody_history", ".dart_tool", ".git", ".idea", ".mypy_cache", ".next", ".pub-cache",
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


def iter_files(root: Path, exclude_deps: bool = True) -> Iterator[Path]:
    """Yield files below root, pruning dependency/build directories in place."""
    for directory, directories, filenames in os.walk(root):
        if exclude_deps:
            directories[:] = [name for name in directories if name not in DEPENDENCY_DIRS]
        for filename in filenames:
            yield Path(directory) / filename


def ripgrep_excludes() -> list[str]:
    return [flag for directory in sorted(DEPENDENCY_DIRS) for flag in ("-g", f"!**/{directory}/**")]


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
    timeout: int = 120,
) -> dict:
    """Run ripgrep and return {"results": [...], "engine": "rg"|"python"}.

    Always searches ignore-file-blind (--no-ignore): DEPENDENCY_DIRS is the
    single source of truth for what gets skipped, not whatever a project's
    own .gitignore/.ignore says. Respecting a repo's .gitignore here silently
    breaks search whenever it's broad (a bare "*", or something like
    "sites/"), which then wrongly leaves the tool reporting no matches from
    files that plainly exist.
    """
    command = ["rg", "--json", "--line-number", "--follow", "--no-ignore"]
    if not regex:
        command.append("--fixed-strings")
    if not case_sensitive:
        command.append("--ignore-case")
    if context_lines > 0:
        command.extend(["--context", str(context_lines)])
    if exclude_deps:
        command.extend(ripgrep_excludes())
    else:
        command.append("--hidden")
    for glob in globs or []:
        command.extend(["-g", glob])
    command.extend((pattern, str(root)))

    try:
        process = subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {
            "results": _fallback(root, pattern, regex, case_sensitive, max_results, context_lines, exclude_deps),
            "engine": "python",
        }

    if process.returncode not in (0, 1):
        return {
            "results": _fallback(root, pattern, regex, case_sensitive, max_results, context_lines, exclude_deps),
            "engine": "python",
            "rg_stderr": process.stderr.strip()[:500] or None,
        }

    results: list[dict] = []
    before: list[str] = []
    for line in process.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "begin":
            before = []
            continue
        if kind == "context":
            before.append(event["data"]["lines"]["text"].rstrip())
            before[:] = before[-max(context_lines, 1):]
            continue
        if kind != "match":
            continue
        data = event["data"]
        entry = {
            "file": os.path.relpath(data["path"]["text"], root),
            "line": data["line_number"],
            "text": data["lines"]["text"].rstrip()[:300],
        }
        if context_lines > 0:
            entry["context"] = "\n".join(before + [entry["text"]])
        before = []
        results.append(entry)
        if len(results) >= max_results:
            break
    return {"results": results, "engine": "rg"}


def _fallback(root, pattern, regex, case_sensitive, max_results, context_lines, exclude_deps) -> list[dict]:
    flags = 0 if case_sensitive else re.IGNORECASE
    matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    results: list[dict] = []
    for file_path in iter_files(root, exclude_deps):
        if file_path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="strict").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, start=1):
            if not matcher.search(line):
                continue
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
