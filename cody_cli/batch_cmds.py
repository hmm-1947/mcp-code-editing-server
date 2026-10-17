"""batch: run several cody commands in one call.

    --ws jjgames batch
    tree src --depth 3
    read package.json vite.config.ts
    find registry --files

One command per line. Blank lines and lines starting with # are ignored.
Output is each command's result under a `== cmd` header. Everything runs in the
same workspace, so `--ws` is only needed once. Multi-line text works with heredoc
tags: put `<<TAG` after --new/--old/--content/--expect and the text on the
following lines, ending at a line that is exactly TAG:

    edit a.py --lines 4-6 --new <<X
    new code
    X

`--new -` (take everything) and `sh --script` can't be batched. A search tool such
as findstr/grep/rg that exits 1 (no match) is not a failure for `--stop`.
"""

from __future__ import annotations

import re

from .common import CliError, check_payload_count, internal_error, set_payloads, split_line, take_script

MAX_COMMANDS = 20
NOT_BATCHABLE = {"batch"}                   # nested batch
RAW_IN_BATCH = {"sh", "check", "proc"}      # take the raw line, not shlex tokens (b takes tokens, so it batches normally)
STOP_ON_ERROR_DEFAULT = False
NO_MATCH_TOOLS = frozenset({"findstr", "grep", "egrep", "fgrep", "rg", "diff", "fc", "cmp"})
_TAG_FLAG = re.compile(r"--(?:new|old|content|expect)\s+<<([A-Za-z_][A-Za-z0-9_]*)(?=\s|$)")


def _heredoc_tags(line: str) -> list[str]:
    try:
        tokens = split_line(line)
    except ValueError:
        return []
    return [
        tokens[i][2:]
        for i in range(1, len(tokens))
        if tokens[i - 1] in ("--new", "--old", "--content", "--expect") and re.fullmatch(r"<<[A-Za-z_][A-Za-z0-9_]*", tokens[i])
    ]


def _parse_script(script: str) -> list[tuple[str, str | None]]:
    """[(command line, heredoc payload text or None)] in order."""
    rows = script.split("\n")
    commands: list[tuple[str, str | None]] = []
    index = 0
    while index < len(rows):
        line = rows[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        payload = None
        tags = _heredoc_tags(line)
        if tags:
            payload = ""
            for tag in tags:
                chunk: list[str] = []
                while index < len(rows) and rows[index].rstrip("\r") != tag:
                    chunk.append(rows[index])
                    index += 1
                if index >= len(rows):
                    raise CliError(f"batch: closing line '{tag}' not found for `{line[:60]}`")
                index += 1
                payload += ("\n".join(chunk) + "\n" if chunk else "") + tag + "\n"
        commands.append((line, payload))
    return commands


def cmd_batch(raw: str) -> str:
    from .main import COMMANDS, fail          # late import: main imports this module

    stop = "--stop" in raw.split()[:2]
    script = take_script("batch")
    commands = _parse_script(script)
    if not commands:
        raise CliError("batch needs one command per line after the command line")
    if len(commands) > MAX_COMMANDS:
        raise CliError(f"batch takes at most {MAX_COMMANDS} commands (got {len(commands)})")

    sections: list[str] = []
    for line, payload in commands:
        try:
            argv = split_line(line)
        except ValueError as error:
            sections.append(f"== {line}\n{fail(f'could not parse: {error}')}")
            if stop:
                break
            continue
        name = argv[0]
        if name.startswith("--"):
            sections.append(f"== {line}\n{fail('put --ws before `batch`, once, not on each line')}")
            continue
        if name in NOT_BATCHABLE or name not in COMMANDS:
            reason = "cannot be batched" if name in NOT_BATCHABLE else "unknown command"
            sections.append(f"== {line}\n{fail(f'{name}: {reason}')}")
            if stop:
                break
            continue
        if name in RAW_IN_BATCH:
            section = _run_raw(name, line)
            sections.append(section)
            if stop and _failed(section):
                break
            continue
        rest = argv[1:]
        if any(tok == "-" for tok in rest):
            sections.append(f"== {line}\n{fail('`--new -` cannot be batched; use --new <<TAG with the text on the next lines')}")
            if stop:
                break
            continue
        set_payloads(payload)
        try:
            check_payload_count(rest)
            output = COMMANDS[name](rest)
        except CliError as error:
            output = fail(str(error))
        except Exception as error:
            output = internal_error(name, error, "batch mode")
        finally:
            set_payloads(None)
        sections.append(f"== {line}\n{output}")
        if stop and output.startswith("ERR"):
            break
    return "\n".join(sections)


def _sh_tool(line: str) -> str:
    """First word of the command an `sh` batch line runs, lower-cased, no path or .exe."""
    from .shell_cmds import parse_sh

    try:
        body, _ = parse_sh(line[2:] if line.startswith("sh") else line)
    except Exception:
        return ""
    words = body.split(None, 1)
    if not words:
        return ""
    return re.split(r"[\\/]", words[0].strip("\"'"))[-1].lower().removesuffix(".exe")


def _failed(section: str) -> bool:
    """Did a batch section fail? ERR (any command), `exit N` with N != 0 (sh), FAIL (check).
    Exit 1 from findstr/grep/rg/diff means "no match / differs", which is not an error."""
    head, _, body = section.partition("\n")
    if body.startswith(("ERR", "FAIL", "EXITED")):
        return True
    exit_code = re.match(r"exit ([1-9]\d*)\b", body)
    if exit_code is None:
        return False
    line = head[3:].strip()
    if exit_code.group(1) == "1" and line.startswith("sh") and "TIMEOUT" not in body.split("\n", 1)[0]:
        if _sh_tool(line) in NO_MATCH_TOOLS:
            return False
    return True


def _run_raw(name: str, line: str) -> str:
    """Run a raw-line command (sh/check) from inside a batch. The handler gets the
    text after the command word, exactly as it would standalone."""
    from .main import COMMANDS, fail

    rest = line[len(name):].strip() if line.startswith(name) else line
    try:
        return f"== {line}\n{COMMANDS[name](rest)}"
    except CliError as error:
        return f"== {line}\n{fail(str(error))}"
    except Exception as error:
        return f"== {line}\n{internal_error(name, error, 'batch mode')}"
