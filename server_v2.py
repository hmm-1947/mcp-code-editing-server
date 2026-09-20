"""Cody MCP v2: a single `run` tool that drives the Cody CLI.

    AI client -> MCP -> run(command) -> cody CLI -> code ops | playwright-cli
"""

import argparse
import os
import re
import shlex

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from cody_cli.browser_cmds import screenshot_paths
from cody_cli.common import set_payloads
from cody_cli.main import HELP, dispatch, parse_line

MAX_INLINE_IMAGES = 3
MAX_INLINE_BYTES = 1_500_000                  # bigger screenshots stay path-only
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _with_images(output: str):
    """`b screenshot` saves a file on the server's disk, which a remote agent can't
    open. Attach it as MCP image content so the agent actually sees it; the path
    stays in the text as a fallback for clients that don't render images."""
    images = []
    for path in screenshot_paths(output)[:MAX_INLINE_IMAGES]:
        try:
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file() and path.stat().st_size <= MAX_INLINE_BYTES:
                images.append(Image(path=str(path)))
        except OSError:
            continue
    return [output, *images] if images else output


INSTRUCTIONS = (
    "One tool: run(command). The command is a Cody CLI line, e.g. "
    "`read src/app.py:40-80`, `find handleLogin --glob '*.dart'`, "
    "`edit src/app.py --lines 44-46 --new 'x = 1'`, `sh pytest -q`, `b open https://example.com`. "
    "Start with `help` for the full command list. Prefix `--ws NAME` to use a saved workspace. "
    "Multi-line code: put the text on the lines after the command and give a text flag a marker. "
    "`--new -` takes all the remaining text, e.g. `edit f.py --lines 4-6 --new -` then the new code below. "
    "For several payloads use `--old <<A --new <<B`: each runs up to a line that is exactly its tag "
    "(pick a tag that cannot appear alone on a line in the text). A count mismatch is an ERR and nothing is written."
)

mcp = FastMCP("Cody", instructions=INSTRUCTIONS)

# The single-tool schema tells an agent nothing, so the cheat sheet rides in the
# tool description itself: zero `help` calls per session. Derived from HELP so
# the two can never drift apart.
TOOL_DESCRIPTION = (
    "Run one Cody CLI command (code read/search/edit, shell, background processes, browser). "
    "Put the command in `command`; multi-line text goes on the following lines.\n\n" + HELP
)

RAW_HEADS = ("sh", "batch", "proc", "check")


def _prefix_argv(line: str) -> list[str] | None:
    """Fallback when shlex chokes (e.g. an apostrophe inside PowerShell text).
    sh/batch/proc read the raw line themselves, so only `[--ws NAME] <cmd>` has
    to tokenize. Returns None for any other command so it still errors loudly."""
    match = re.match(r"^(--ws\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s+)?(\w+)(?:\s|$)", line)
    if not match or match.group(2) not in RAW_HEADS:
        return None
    prefix = shlex.split(match.group(1)) if match.group(1) else []
    return prefix + [match.group(2)]


_WS_ALONE = re.compile(r"--ws\s+(?:\"[^\"]*\"|'[^']*'|\S+)")


@mcp.tool(description=TOOL_DESCRIPTION, output_schema=None)     # no schema: the reply may be text + image content
def run(command: str) -> str | list:
    text = (command or "").lstrip()
    line, _, payload = text.partition("\n")
    line = line.strip()
    if _WS_ALONE.fullmatch(line) and payload.strip():        # `--ws X` alone, command on the next line
        next_line, _, payload = payload.lstrip("\r\n").partition("\n")
        line = f"{line} {next_line.strip()}".strip()
    if not line:
        return HELP
    if line.startswith("cody "):
        line = line[5:]
    try:
        argv = parse_line(line)
    except ValueError as error:
        argv = _prefix_argv(line)
        if argv is None:
            return f"ERR could not parse command: {error}"
    print(f"[run] {line[:160]!r}" + (f" +payload {len(payload)}c" if payload else ""))
    saved = os.environ.get("CODY_WS")
    set_payloads(payload if payload.strip() else None)
    try:
        return _with_images(dispatch(argv, raw=line))
    finally:
        set_payloads(None)
        if saved is None:
            os.environ.pop("CODY_WS", None)
        else:
            os.environ["CODY_WS"] = saved


def main() -> None:
    parser = argparse.ArgumentParser(description="Cody MCP v2 (single run tool)")
    parser.add_argument("--transport", default="streamable-http",
                        choices=("streamable-http", "sse", "stdio"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    arguments = parser.parse_args()
    if arguments.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=arguments.transport, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
