from __future__ import annotations

import json
import re

import app_paths

from .common import (
    TEXT_FLAGS, CliError, _load_text_file, _take_payload, current_session, is_payload_marker, root_of,
    set_payloads, split_flags, to_int, unescape, unescape_flag,
)

WRITE_TEXT = {"edit": "new", "new": "content", "append": "content", "medit": "content"}
APPLY_FLAGS = {"edit": {"force", "nocheck"}, "new": {"force", "nocheck"}, "append": {"nocheck"}, "medit": {"force", "nocheck"}}
SHOW_CAP = 200
CONTEXT_CAP = 12
USAGE = (
    "usage: err [show [A-B]] | edit A[-B] -new T | ins N -new T | rm A[-B] | "
    "sub -old T -new T [-all] | apply [-force -nocheck] | clear"
)


def _folder():
    folder = app_paths.logs_dir() / "txt_err"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _sid() -> str:
    return re.sub(r"[^\w.-]", "_", current_session())[:60] or "default"


def _paths():
    folder = _folder()
    sid = _sid()
    return folder / f"{sid}.json", folder / f"{sid}.txt"


def _split(text: str) -> list[str]:
    if not text:
        return []
    return (text[:-1] if text.endswith("\n") else text).split("\n")


def _join(rows: list[str], trail: bool) -> str:
    return "\n".join(rows) + ("\n" if trail and rows else "")


def _rows_of(text: str) -> list[str]:
    return _split(text.replace("\r\n", "\n"))


def _extract(name: str, args: list[str], body: str | None):
    target = WRITE_TEXT[name]
    raw_mode = name == "edit" and bool({"-raw", "--raw"} & set(args))
    set_payloads(body)
    kept: list[str] = []
    fixed: dict[str, str] = {}
    text = None
    index = 0
    try:
        while index < len(args):
            arg = args[index]
            head, eq, inline = arg.lstrip("-").partition("=") if re.fullmatch(r"--?[A-Za-z].*", arg) else ("", "", "")
            is_file = head.endswith("-file")
            base = head[:-5] if is_file else head
            if base not in TEXT_FLAGS or not (eq or index + 1 < len(args)):
                kept.append(arg)
                index += 1
                continue
            given = inline if eq else args[index + 1]
            index += 1 if eq else 2
            if is_file:
                value = str(_load_text_file(given))
            elif not eq and is_payload_marker(given):
                value = str(_take_payload(base, given))
            else:
                value = given if raw_mode else unescape(given)
            if base == target:
                text = value
            else:
                fixed[base] = value
    except CliError:
        return None
    finally:
        set_payloads(None)
    if text is None or not text.strip():
        return None
    return kept, fixed, text.replace("\r\n", "\n")


def _write_meta(meta: dict) -> None:
    meta_path, _ = _paths()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _write_rows(meta: dict, rows: list[str]) -> None:
    _, text_path = _paths()
    text_path.write_text(_join(rows, meta["trail"]), encoding="utf-8", newline="\n")
    _write_meta(meta)


def record(name: str, args: list[str], body: str | None, output: str) -> str:
    if name not in WRITE_TEXT or not output.startswith("ERR"):
        return output
    try:
        found = _extract(name, args, body)
        if found is None:
            return output
        kept, fixed, text = found
        try:
            root = str(root_of(None))
        except CliError:
            root = ""
        meta = {
            "cmd": name, "args": kept, "flag": WRITE_TEXT[name], "fixed": fixed, "root": root,
            "error": output[4:].strip()[:300], "trail": text.endswith("\n"),
        }
        _write_rows(meta, _split(text))
        count = len(_split(text))
    except OSError:
        return output
    return f"{output}\n# saved to txt_err ({count}L): err show | err edit A-B -new T | err sub -old T -new T | err ins N -new T | err rm A-B | err apply"


def _load() -> tuple[dict, list[str]]:
    meta_path, text_path = _paths()
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        text = text_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise CliError("txt_err is empty; it fills when edit/new/append/medit fails")
    return meta, _split(text)


def _span(value: str, total: int) -> tuple[int, int]:
    first, dash, last = value.partition("-")
    start = to_int(first, "line")
    end = to_int(last, "line") if dash and last else (total if dash else start)
    if start < 1 or end < start or end > total:
        raise CliError(f"bad range {value}; txt_err has {total} line(s)")
    return start, end


def _numbered(rows: list[str], first: int, last: int) -> str:
    return "\n".join(f"{number}|{rows[number - 1]}" for number in range(first, last + 1))


def _reply(head: str, rows: list[str], first: int, last: int) -> str:
    last = min(last, len(rows), first + CONTEXT_CAP - 1)
    if last < first:
        return head
    return f"{head}\n{_numbered(rows, first, last)}"


def _header(meta: dict, total: int) -> str:
    line = f"{meta['cmd']} {' '.join(meta['args'])}"
    extra = f" +{'/'.join(meta['fixed'])}" if meta["fixed"] else ""
    return f"# txt_err {line[:140]}{extra} --{meta['flag']} {total}L; error: {meta['error'][:160]}"


def _show(args: list[str]) -> str:
    meta, rows = _load()
    if not rows:
        return f"{_header(meta, 0)}\n(empty)"
    if args:
        first, last = _span(args[0], len(rows))
    else:
        first, last = 1, len(rows)
    capped = last - first + 1 > SHOW_CAP
    if capped:
        last = first + SHOW_CAP - 1
    more = f"\n# more: err show {last + 1}-" if capped else ""
    return f"{_header(meta, len(rows))}\n{_numbered(rows, first, last)}{more}"


def _edit(args: list[str]) -> str:
    positional, flags = split_flags(args, {"new"}, set())
    if len(positional) != 1 or "new" not in flags:
        raise CliError("usage: err edit A[-B] -new TEXT")
    meta, rows = _load()
    start, end = _span(positional[0], len(rows))
    added = _rows_of(unescape_flag(flags["new"]))
    rows[start - 1:end] = added
    _write_rows(meta, rows)
    return _reply(f"ok txt_err {start}-{end} -> {len(added)}L, {len(rows)}L total", rows, start, start + len(added) - 1)


def _ins(args: list[str]) -> str:
    positional, flags = split_flags(args, {"new"}, set())
    if len(positional) != 1 or "new" not in flags:
        raise CliError("usage: err ins N -new TEXT")
    meta, rows = _load()
    position = to_int(positional[0], "line")
    if position < 1 or position > len(rows) + 1:
        raise CliError(f"bad line {position}; txt_err has {len(rows)} line(s) (use 1-{len(rows) + 1})")
    added = _rows_of(unescape_flag(flags["new"]))
    if not added:
        raise CliError("nothing to insert")
    rows[position - 1:position - 1] = added
    _write_rows(meta, rows)
    return _reply(f"ok txt_err inserted {position}-{position + len(added) - 1}, {len(rows)}L total", rows, position, position + len(added) - 1)


def _rm(args: list[str]) -> str:
    positional, _ = split_flags(args, set(), set())
    if len(positional) != 1:
        raise CliError("usage: err rm A[-B]")
    meta, rows = _load()
    start, end = _span(positional[0], len(rows))
    del rows[start - 1:end]
    _write_rows(meta, rows)
    return f"ok txt_err removed {start}-{end}, {len(rows)}L left"


def _sub(args: list[str]) -> str:
    positional, flags = split_flags(args, {"old", "new"}, {"all"})
    if positional or "old" not in flags or "new" not in flags:
        raise CliError("usage: err sub -old TEXT -new TEXT [-all]")
    meta, rows = _load()
    text = _join(rows, meta["trail"])
    old = unescape_flag(flags["old"]).replace("\r\n", "\n")
    new = unescape_flag(flags["new"]).replace("\r\n", "\n")
    if not old:
        raise CliError("-old is empty")
    count = text.count(old)
    if count == 0:
        raise CliError("old text not found in txt_err")
    if count > 1 and not flags.get("all"):
        spots: list[str] = []
        index = 0
        while len(spots) < 8:
            found = text.find(old, index)
            if found < 0:
                break
            spots.append(str(text.count("\n", 0, found) + 1))
            index = found + len(old)
        raise CliError(f"old text matches {count}x at lines {','.join(spots)}; extend it or use -all")
    first = text.count("\n", 0, text.find(old)) + 1
    updated = text.replace(old, new) if flags.get("all") else text.replace(old, new, 1)
    rows = _split(updated)
    _write_rows(meta, rows)
    return _reply(f"ok txt_err {count if flags.get('all') else 1} replaced at {first}, {len(rows)}L total", rows, first, first + new.count("\n"))


def _clear(args: list[str] | None = None) -> str:
    removed = False
    for path in _paths():
        if path.exists():
            path.unlink(missing_ok=True)
            removed = True
    return "ok txt_err cleared" if removed else "txt_err already empty"


def _apply(args: list[str]) -> str:
    from .main import COMMANDS

    _, flags = split_flags(args, set(), {"force", "nocheck"})
    meta, rows = _load()
    if not rows:
        raise CliError("txt_err has no lines; add some with err ins 1 -new T, or err clear")
    try:
        root = str(root_of(None))
    except CliError:
        root = ""
    if meta.get("root") and root and meta["root"] != root:
        raise CliError(f"txt_err was saved in workspace {meta['root']}; run with that -ws")
    command = meta["cmd"]
    argv = list(meta["args"])
    for name in ("force", "nocheck"):
        if flags.get(name):
            if name not in APPLY_FLAGS[command]:
                raise CliError(f"{command} has no --{name}")
            if f"--{name}" not in argv:
                argv.append(f"--{name}")
    _, text_path = _paths()
    folder = _folder()
    temp = []
    try:
        for flag, value in meta["fixed"].items():
            path = folder / f"{_sid()}.{flag}.tmp"
            path.write_text(value, encoding="utf-8", newline="")
            temp.append(path)
            argv += [f"--{flag}-file", str(path)]
        argv += [f"--{meta['flag']}-file", str(text_path)]
        set_payloads(None)
        try:
            output = COMMANDS[command](argv)
        except CliError as error:
            meta["error"] = str(error)[:300]
            _write_meta(meta)
            raise CliError(f"{error}; txt_err kept")
    finally:
        for path in temp:
            path.unlink(missing_ok=True)
    _clear()
    return f"{output}\n# txt_err cleared"


ACTIONS = {"show": _show, "edit": _edit, "ins": _ins, "rm": _rm, "sub": _sub, "apply": _apply, "clear": _clear}


def cmd_err(args: list[str]) -> str:
    if not args:
        meta_path, _ = _paths()
        if not meta_path.exists():
            return "txt_err empty"
        return _show([])
    handler = ACTIONS.get(args[0])
    if handler is None:
        raise CliError(USAGE)
    return handler(args[1:])
