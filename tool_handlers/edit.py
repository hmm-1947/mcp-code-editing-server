"""Editing files.

Line endings, indentation and the trailing newline are preserved. Two cheap
guards remain, because they are the two ways an automated editor silently
damages a file: an `old_text` that matches in more than one place, and a
replacement that leaves brackets unbalanced.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP

from code_engine.replace import build_function_replacement, fuzzy_find_text
from config import resolve_path
from core import structure
from core import history
from core.diff import unified
from core.syntax_check import check_python_syntax
from ._common import window

CONTEXT_PADDING = 6


def register(mcp: FastMCP) -> None:
    @mcp.tool
    def edit(
        path: str,
        workspace: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        new_text: str | None = None,
        old_text: str | None = None,
        function_name: str | None = None,
        new_function: str | None = None,
        content: str | None = None,
        append_text: str | None = None,
        expect: str | None = None,
        replace_all: bool = False,
        preview: bool = False,
        dry_run: bool = False,
        undo: bool = False,
    ) -> dict:
        """Change a file. Pick one form:

          start_line + end_line + new_text   replace a line range (preferred -
                                             you never repeat the old code)
          old_text + new_text                replace exact text; whitespace
                                             tolerant, refuses if ambiguous
          function_name + new_function       replace a whole function
          content                            write the file / create a new one
          append_text                        add text after the last line,
                                             no line numbers needed
          undo=True                          restore the file's last snapshot,
                                             ignores every other argument

        expect, with start_line/end_line, aborts the edit if the current text
        at that range doesn't match - a guard against stale line numbers.
        preview=True with old_text lists every occurrence instead of editing.
        dry_run=True shows the result (with a diff) without writing.
        Every write is snapshotted first, so undo=True can always recover it.
        """
        target = resolve_path(workspace, path)
        return _apply_edit(
            target, path, start_line, end_line, new_text, old_text,
            function_name, new_function, content, append_text, expect,
            replace_all, preview, dry_run, undo,
        )

    @mcp.tool
    def batch_edit(edits: list[dict]) -> dict:
        """Apply several edits in one call, all-or-nothing.

        Each item in `edits` takes the same fields as `edit` (path and
        workspace plus one of the edit forms). If any edit fails - bad range,
        ambiguous old_text, unbalanced brackets, bad Python syntax - every
        edit already applied in this batch is rolled back via its snapshot,
        and no partial refactor is left on disk. Pass dry_run=True on an item
        to preview it within the batch without it counting toward rollback.
        """
        applied_targets: list[Path] = []
        results: list[dict] = []

        for index, item in enumerate(edits):
            path = item.get("path")
            if not path:
                results.append({"error": "Missing 'path'.", "index": index})
                _rollback(applied_targets)
                return {"applied": False, "results": results,
                        "error": f"Edit {index} had no path; batch rolled back."}

            target = resolve_path(item.get("workspace", ""), path)
            result = _apply_edit(
                target, path,
                item.get("start_line"), item.get("end_line"),
                item.get("new_text"), item.get("old_text"),
                item.get("function_name"), item.get("new_function"),
                item.get("content"), item.get("append_text"),
                item.get("expect"),
                item.get("replace_all", False),
                item.get("preview", False),
                item.get("dry_run", False),
                item.get("undo", False),
            )
            results.append({"index": index, **result})

            failed = "error" in result or result.get("syntax_error")
            if failed:
                _rollback(applied_targets)
                return {
                    "applied": False, "results": results,
                    "error": f"Edit {index} failed; batch rolled back.",
                }
            if result.get("applied"):
                applied_targets.append(target)

        return {"applied": True, "results": results,
                "message": f"Applied {len(applied_targets)} edit(s) across "
                           f"{len(set(applied_targets))} file(s)."}


def _rollback(applied_targets: list[Path]) -> None:
    for target in reversed(applied_targets):
        history.undo(target)


def _apply_edit(
    target: Path,
    path: str,
    start_line: int | None,
    end_line: int | None,
    new_text: str | None,
    old_text: str | None,
    function_name: str | None,
    new_function: str | None,
    content: str | None,
    append_text: str | None,
    expect: str | None,
    replace_all: bool,
    preview: bool,
    dry_run: bool,
    undo: bool,
) -> dict:
    key = target.as_posix()
    print(f"[edit] {key}")

    # ------------------------------------------------------------- undo
    if undo:
        restored = history.undo(target)
        if restored is None:
            return {"error": f"No snapshot history for {key}.", "path": key}
        return {"message": f"Restored {key} from {restored.name}",
                "path": key, "applied": True}

    # -------------------------------------------------------- new file
    if not target.is_file():
        body = content if content is not None else (new_text if new_text is not None else append_text)
        if body is None:
            return {"error": f"{key} does not exist.",
                    "fix": ["Pass content=<full file> to create it, or check the path."]}
        if dry_run:
            return {"message": f"Dry run: would create {key}", "path": key,
                    "applied": False}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return {"message": f"Created {key}", "path": key, "applied": True,
                "lines": body.count("\n") + 1}

    try:
        # newline="" keeps \r\n intact; read_text would normalize it away
        # and quietly rewrite a CRLF file as LF on save.
        with target.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except UnicodeDecodeError:
        return {"error": f"{key} is not UTF-8 text."}

    # ------------------------------------------------------ whole file
    if content is not None:
        return _write(target, key, content, 1, content.count("\n") + 1,
                      f"Rewrote {key}", dry_run)

    # -------------------------------------------------------- function
    if function_name:
        if new_function is None:
            raise ValueError("new_function is required with function_name")
        try:
            proposed, first, last = build_function_replacement(
                str(target), function_name, new_function,
            )
        except ValueError as error:
            return {"error": str(error), "path": key, "fix": [
                f"find(action='search', query='{function_name}', definitions_only=True) "
                "for its real location, then edit by line range.",
            ]}
        return _write(target, key, proposed.decode("utf-8"), first, last,
                      f"Replaced function '{function_name}'", dry_run,
                      _formatting_notes("", new_function))

    # ------------------------------------------------------------ append
    if append_text is not None:
        lines = text.splitlines()
        newline = _dominant_ending(text)
        body = _match_line_ending(text, append_text)
        if not body.endswith("\n") and not body.endswith("\r\n"):
            body += newline
        head = text if (not text or text.endswith("\n")) else text + newline
        proposed = head + body
        first = len(lines) + 1
        return _write(target, key, proposed, first,
                      first + max(0, body.count("\n")) - 1,
                      f"Appended to {key}", dry_run)

    # ------------------------------------------------------ line range
    if start_line is not None:
        if end_line is None or new_text is None:
            raise ValueError("end_line and new_text are required with start_line")
        lines = text.splitlines()
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            return {"error": f"Invalid range {start_line}-{end_line}; the file has "
                             f"{len(lines)} lines.", "path": key,
                    "fix": [f"read(paths=['{path}']) for current line numbers."]}

        replaced = "\n".join(lines[start_line - 1:end_line])
        if expect is not None and replaced.strip() != expect.strip():
            return {
                "error": "expect did not match the current content at that range - "
                         "line numbers are likely stale.",
                "path": key,
                "current": window(text, start_line, end_line, CONTEXT_PADDING),
                "fix": [f"read(paths=['{path}']) for current line numbers, or drop expect."],
            }
        body = _match_line_ending(text, new_text)
        newline = _dominant_ending(text)
        trailing = text.endswith("\n")
        proposed = newline.join(
            lines[:start_line - 1] + body.splitlines() + lines[end_line:]
        ) + (newline if trailing else "")
        return _write(target, key, proposed, start_line,
                      start_line + max(0, body.count("\n")),
                      f"Replaced lines {start_line}-{end_line} of {key}", dry_run,
                      _formatting_notes(replaced, new_text))

    # -------------------------------------------------------- old_text
    if old_text:
        old_text = _match_line_ending(text, old_text)
        count = text.count(old_text)

        if preview:
            if count:
                return {"path": key, "match_count": count,
                        "matches": _previews(text, old_text), "applied": False}
            span = fuzzy_find_text(text, old_text)
            if span is None:
                return {"path": key, "match_count": 0, "matches": [], "applied": False,
                        "note": "Text not found."}
            first = text.count("\n", 0, span[0]) + 1
            return {"path": key, "match_count": 1, "applied": False,
                    "matches": [{"start_line": first,
                                 "end_line": text.count("\n", 0, span[1]) + 1,
                                 "match": "whitespace-tolerant"}]}

        if new_text is None:
            raise ValueError("new_text is required with old_text")

        if count == 0:
            span = fuzzy_find_text(text, old_text)
            if span is None:
                return {"error": "old_text was not found.", "path": key, "fix": [
                    f"read(paths=['{path}']) and copy the exact current text, or",
                    "edit(preview=True, old_text=<shorter fragment>).",
                ]}
            body = _match_line_ending(text, new_text)
            proposed = text[:span[0]] + body + text[span[1]:]
            offset = span[0]
            message = "Replaced 1 occurrence (whitespace-tolerant match)"
        elif count > 1 and not replace_all:
            return {
                "error": f"old_text occurs {count} times in {key}.",
                "path": key, "match_count": count,
                "matches": _previews(text, old_text),
                "fix": [
                    "Extend old_text until it is unique,",
                    "or edit(start_line=, end_line=) using the line numbers above,",
                    "or replace_all=True if every occurrence should change.",
                ],
            }
        else:
            body = _match_line_ending(text, new_text)
            offset = text.index(old_text)
            proposed = (text.replace(old_text, body) if replace_all
                        else text.replace(old_text, body, 1))
            message = f"Replaced {count if replace_all else 1} occurrence(s) in {key}"

        first = text.count("\n", 0, offset) + 1
        return _write(target, key, proposed, first,
                      first + max(0, body.count("\n")), message, dry_run,
                      _formatting_notes(old_text, new_text))

    return {"error": "Nothing to do: no replacement was given.", "path": key, "fix": [
        "edit(start_line=, end_line=, new_text=) - preferred.",
        "edit(old_text=, new_text=) - when line numbers are unknown.",
        "edit(content=) - to create or rewrite the file.",
        "edit(append_text=) - to add to the end without knowing line numbers.",
    ]}


# --------------------------------------------------------------------- write

def _write(target: Path, key: str, proposed: str, first: int, last: int,
           message: str, dry_run: bool, formatting: list[str] | None = None) -> dict:
    problem = structure.validate_structure(target, proposed)
    if problem is not None:
        return {
            "error": "Not applied: the result would leave brackets unbalanced.",
            "path": key, "detail": problem,
            "fix": ["Include the matching closing bracket in your replacement."],
        }

    syntax_problem = check_python_syntax(target, proposed)

    payload = {
        "message": f"Dry run: {message}" if dry_run else message,
        "path": key,
        "applied": not dry_run,
        "lines": [first, last],
        "context": window(proposed, first, last, CONTEXT_PADDING),
    }
    if formatting:
        payload["formatting"] = formatting
    if syntax_problem:
        payload["syntax_error"] = syntax_problem
    if dry_run:
        try:
            with target.open("r", encoding="utf-8", newline="") as handle:
                before = handle.read()
        except (OSError, UnicodeDecodeError):
            before = ""
        payload["diff"] = unified(before, proposed, key)
        return payload

    history.snapshot(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" writes exactly the bytes we assembled, so the file keeps the
    # line endings it already had instead of the platform default.
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(proposed)
    return payload


# ------------------------------------------------------------------- helpers

def _previews(text: str, old_text: str) -> list[dict]:
    matches: list[dict] = []
    index = 0
    while True:
        found = text.find(old_text, index)
        if found < 0:
            return matches
        first = text.count("\n", 0, found) + 1
        matches.append({
            "start_line": first,
            "end_line": first + old_text.count("\n"),
            "context": window(text, first, first + old_text.count("\n"), 2),
        })
        index = found + len(old_text)


def _dominant_ending(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= (text.count("\n") - crlf) else "\n"


def _match_line_ending(existing: str, new_text: str) -> str:
    """Rewrite incoming text to the file's own line-ending convention."""
    normalized = new_text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n") if _dominant_ending(existing) == "\r\n" else normalized


def _indent_of(text: str) -> tuple[str, int] | None:
    for line in text.split("\n"):
        if not line.strip():
            continue
        stripped = line.lstrip(" \t")
        prefix = line[: len(line) - len(stripped)]
        if not prefix:
            return ("none", 0)
        return ("tab", prefix.count("\t")) if "\t" in prefix else ("space", len(prefix))
    return None


def _formatting_notes(old_segment: str, new_segment: str) -> list[str]:
    """Report formatting drift instead of silently normalizing the file."""
    notes: list[str] = []
    before = _indent_of(old_segment or "")
    after = _indent_of(new_segment or "")
    if before and after and before[0] != after[0] and "none" not in (before[0], after[0]):
        notes.append(f"indentation changed from {before[0]}s to {after[0]}s")
    elif before and after and before[0] == after[0] == "space" and before[1] != after[1]:
        notes.append(f"leading indent changed from {before[1]} to {after[1]} spaces")
    if "\t" in (new_segment or "") and "\t" not in (old_segment or ""):
        notes.append("tabs introduced into a region that used spaces")
    return notes
