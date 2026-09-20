"""batch: run several cody commands in one call.

    --ws jjgames batch
    tree src --depth 3
    read package.json vite.config.ts
    find registry --files

One command per line. Blank lines and lines starting with # are ignored.
Output is each command's result under a `== cmd` header. Everything runs in the
same workspace, so `--ws` is only needed once. Commands that take a raw text
payload (`--new -`, `--old <<TAG`, `sh --script`) can't be batched: run them alone.
"""

from __future__ import annotations

import re
import shlex

from .common import CliError, internal_error, take_script

MAX_COMMANDS = 20
NOT_BATCHABLE = {"batch"}                   # nested batch
RAW_IN_BATCH = {"sh", "check", "proc"}      # take the raw line, not shlex tokens (b takes tokens, so it batches normally)
STOP_ON_ERROR_DEFAULT = False


def cmd_batch(raw: str) -> str:
    from .main import COMMANDS, fail          # late import: main imports this module

    stop = "--stop" in raw.split()[:2]
    script = take_script("batch")
    lines = [ln.strip() for ln in script.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise CliError("batch needs one command per line after the command line")
    if len(lines) > MAX_COMMANDS:
        raise CliError(f"batch takes at most {MAX_COMMANDS} commands (got {len(lines)})")

    sections: list[str] = []
    for line in lines:
        try:
            argv = shlex.split(line, posix=True)
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
        if any(tok == "-" or tok.startswith("<<") for tok in rest):
            sections.append(f"== {line}\n{fail('payload commands (--new -, <<TAG) cannot be batched; run alone')}")
            if stop:
                break
            continue
        try:
            output = COMMANDS[name](rest)
        except CliError as error:
            output = fail(str(error))
        except Exception as error:
            output = internal_error(name, error, "batch mode")
        sections.append(f"== {line}\n{output}")
        if stop and output.startswith("ERR"):
            break
    return "\n".join(sections)


def _failed(section: str) -> bool:
    """Did a batch section fail? ERR (any command), `exit N` with N != 0 (sh), FAIL (check)."""
    body = section.split("\n", 1)[-1]
    return body.startswith(("ERR", "FAIL", "EXITED")) or re.match(r"exit [1-9]\d*\b", body) is not None


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
