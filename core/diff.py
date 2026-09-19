"""Small diff helper shared by dry_run and preview."""

from __future__ import annotations

import difflib


def unified(before: str, after: str, path: str, context: int = 3) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}",
        n=context,
    )
    text = "".join(diff)
    return text if text else "(no textual difference)"
