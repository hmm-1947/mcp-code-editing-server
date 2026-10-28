"""Cody MCP: a single `run` tool that drives the Cody CLI.

    AI client -> MCP -> run(command) -> cody CLI -> code ops | playwright-cli
"""

import argparse
import multiprocessing
import os
import re
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--graphify-worker":
        from cody_cli.graph_worker import main as _graph_worker_main
        sys.exit(_graph_worker_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from cody_cli.main import dispatch as _cli_dispatch
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _cli_output = _cli_dispatch(sys.argv[2:])
        print(_cli_output)
        sys.exit(1 if _cli_output.startswith("ERR ") else 0)

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image

import app_paths
from cody_cli.browser_cmds import screenshot_paths
from cody_cli.common import (
    reset_session, reset_workspace, set_payloads, set_session, set_workspace, split_line,
)
from cody_cli.main import HELP, dispatch, parse_line
from android_use import bridge as android_bridge
from android_use import tools as android_tools

if app_paths.FROZEN:
    os.environ["PATH"] = str(app_paths.resource_dir() / "bin") + os.pathsep + os.environ.get("PATH", "")

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
    "One tool: run(command), a Cody CLI line, e.g. `read src/app.py:40-80`. "
    "Use `help` for the full command list. "
    "Android phone control is the `android` command (`android help`)."
)

mcp = FastMCP("Cody", instructions=INSTRUCTIONS)
android_tools.register(mcp)

TOOL_DESCRIPTION = (
    "Run one Cody CLI command: find glob read sym tree edit medit new append rm undo err sh proc check batch ws graph b android stats. "
    "Put `-ws NAME|PATH` first. Multi-line text goes on the lines after the command (`-new -`, or `<<TAG`). "
    "Use the local-code-editing skill to learn usage."
)

RAW_HEADS = ("sh", "batch", "proc", "check")


def _prefix_argv(line: str) -> list[str] | None:
    """Fallback when shlex chokes (e.g. an apostrophe inside PowerShell text).
    sh/batch/proc read the raw line themselves, so only `[--ws NAME] <cmd>` has
    to tokenize. Returns None for any other command so it still errors loudly."""
    match = re.match(r"^(--?ws\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s+)?(\w+)(?:\s|$)", line)
    if not match or match.group(2) not in RAW_HEADS:
        return None
    prefix = split_line(match.group(1)) if match.group(1) else []
    return prefix + [match.group(2)]


_WS_ALONE = re.compile(r"--?ws\s+(?:\"[^\"]*\"|'[^']*'|\S+)")


@mcp.tool(description=TOOL_DESCRIPTION, output_schema=None)     # no schema: the reply may be text + image content
def run(command: str, ctx: Context | None = None) -> str | list:
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
    print(f"[run] {line[:160]!r}" + (f" +payload {len(payload)}c" if payload else ""), file=sys.stderr)
    ws_token = set_workspace("")
    session_token = set_session(getattr(ctx, "session_id", "") if ctx is not None else "")
    set_payloads(payload if payload.strip() else None)
    try:
        return _with_images(dispatch(argv, raw=line))
    finally:
        set_payloads(None)
        reset_session(session_token)
        reset_workspace(ws_token)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cody MCP (single run tool)")
    parser.add_argument("--transport", default="streamable-http",
                        choices=("streamable-http", "sse", "stdio"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--android-port", type=int, default=8765)
    parser.add_argument("--android-pair", action="store_true")
    parser.add_argument("--path", default=os.environ.get("CODY_MCP_PATH", "/mcp"))
    arguments = parser.parse_args()
    android_bridge.PORT["value"] = arguments.port
    if arguments.android_pair:
        for url in android_bridge.pairing_urls():
            print(url)
            print(android_bridge.pairing_code(url))
        return
    print(f"[android] pairing code: {android_bridge.pairing_code(android_bridge.pairing_urls()[0])}", file=sys.stderr)
    if arguments.transport == "stdio":
        android_bridge.start_standalone(arguments.host, arguments.android_port)
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=arguments.transport, host=arguments.host, port=arguments.port, path=arguments.path)


if __name__ == "__main__":
    main()
