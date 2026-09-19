"""Post-write sanity checks beyond bracket balance."""

from __future__ import annotations

from pathlib import Path


def check_python_syntax(path: str | Path, content: str) -> dict | None:
    """Compile-check a .py file's proposed content. Returns an error payload or None."""
    if Path(path).suffix.lower() != ".py":
        return None
    try:
        compile(content, str(path), "exec")
    except SyntaxError as error:
        return {
            "message": "Python syntax error in the result",
            "line": error.lineno,
            "column": error.offset,
            "detail": error.msg,
            "text": (error.text or "").rstrip("\n"),
        }
    return None
