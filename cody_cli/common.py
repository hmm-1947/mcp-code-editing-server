from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_workspace, resolve_path


class CliError(Exception):
    pass


def ok(message: str = "ok") -> str:
    return message


def fail(message: str) -> str:
    return f"ERR {message}"


ERROR_LOG = ROOT / "tray_logs" / "cody_errors.log"


def internal_error(name: str, error: BaseException, context: str = "") -> str:
    """Reply for an unexpected exception: short, with the cause (the agent can act on
    "invalid literal for int(): 'x'" but not on "see the server log"). The full
    traceback is appended to tray_logs/cody_errors.log for the developer."""
    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a", encoding="utf-8") as log:
            log.write(f"--- {name} {context}\n{text}\n")
    except OSError:
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
    where = f" in {context}" if context else ""
    detail = " ".join(str(error).split())[:120]
    return f"ERR {name}: internal error{where} ({type(error).__name__}{': ' + detail if detail else ''})"


def unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


FILE_SUFFIX = "-file"
TEXT_FLAGS = {"new", "old", "content", "expect"}


class Raw(str):
    pass


def unescape_flag(value) -> str:
    return str(value) if isinstance(value, Raw) else unescape(value)


def _load_text_file(raw: str) -> Raw:
    source = path_of(raw)
    if not source.is_file():
        raise CliError(f"not found: {raw}")
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            return Raw(handle.read())
    except UnicodeDecodeError:
        raise CliError(f"not utf-8: {raw}")


BODY: str | None = None
_HEREDOC = re.compile(r"^<<([A-Za-z_][A-Za-z0-9_]*)$")


def set_payloads(body: str | None) -> None:
    global BODY
    BODY = None if body is None or not body.strip() else body


def _take_payload(name: str, marker: str) -> Raw:
    global BODY
    if BODY is None:
        raise CliError(f"--{name} {marker} needs text on the lines after the command")
    if marker == "-":
        text, BODY = BODY, None
        return Raw(text)
    tag = marker[2:]
    lines = BODY.split("\n")
    for index, line in enumerate(lines):
        if line.rstrip("\r") == tag:
            rest = "\n".join(lines[index + 1:])
            BODY = rest if rest.strip() else None
            return Raw("\n".join(lines[:index]) + "\n")
    raise CliError(f"--{name} {marker}: closing line '{tag}' not found")


def take_script(label: str) -> str:
    """Consume the whole text after the command line as a raw script (no escape
    processing). Used by `sh --script` and `batch`."""
    global BODY
    if BODY is None:
        raise CliError(f"{label} needs the script on the lines after the command")
    text, BODY = BODY, None
    return text.strip("\n")


def _markers(args: list[str]) -> list[str]:
    found = []
    for index in range(1, len(args)):
        previous = args[index - 1]
        if previous.startswith("--") and previous[2:] in TEXT_FLAGS and is_payload_marker(args[index]):
            found.append(args[index])
    return found


def check_payload_count(args: list[str]) -> None:
    markers = _markers(args)
    if BODY is None:
        if markers:
            raise CliError(f"{len(markers)} payload marker(s) given but no text after the command")
        return
    if not markers:
        raise CliError("text after the command but no `--flag -` or `--flag <<TAG` to receive it")
    if "-" in markers and len(markers) > 1:
        raise CliError("`-` takes all remaining text; use <<TAG for several payloads")
    remaining = BODY
    for marker in markers:
        if marker == "-":
            remaining = ""
            continue
        tag = marker[2:]
        lines = remaining.split("\n")
        for index, line in enumerate(lines):
            if line.rstrip("\r") == tag:
                remaining = "\n".join(lines[index + 1:])
                break
        else:
            raise CliError(f"closing line '{tag}' not found")
    if remaining.strip():
        raise CliError("extra text after the last payload; nothing was applied")


def is_payload_marker(value: str) -> bool:
    return value == "-" or bool(_HEREDOC.match(value))


def split_flags(args: list[str], value_flags: set[str], bool_flags: set[str]) -> tuple[list[str], dict]:
    file_flags = {f"{name}{FILE_SUFFIX}": name for name in value_flags if name in TEXT_FLAGS}
    value_flags = value_flags | set(file_flags)
    positional: list[str] = []
    flags: dict = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--") and len(arg) > 2:
            name = arg[2:]
            if "=" in name:
                name, val = name.split("=", 1)
                flags[name] = val
            elif name in bool_flags:
                flags[name] = True
            elif name in value_flags:
                if index + 1 >= len(args):
                    raise CliError(f"--{name} needs a value")
                index += 1
                if name in file_flags:
                    flags[file_flags[name]] = _load_text_file(args[index])
                elif name in TEXT_FLAGS and is_payload_marker(args[index]):
                    flags[name] = _take_payload(name, args[index])
                else:
                    flags[name] = args[index]
            else:
                raise CliError(f"unknown flag --{name}")
        else:
            positional.append(arg)
        index += 1
    return positional, flags


def current_workspace() -> str:
    return os.environ.get("CODY_WS", "")


def path_of(raw: str) -> Path:
    return resolve_path(current_workspace(), raw)


def root_of(raw: str | None) -> Path:
    if raw:
        base = path_of(raw)
    else:
        ws = current_workspace()
        base = get_workspace(ws) if ws else Path.cwd()
    if not base.exists():
        raise CliError(f"not found: {base}")
    return base if base.is_dir() else base.parent


def to_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise CliError(f"{name} must be an integer")
