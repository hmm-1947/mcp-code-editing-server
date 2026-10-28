"""Smoke test for the built app folder: talks MCP to the frozen cody-server.exe.

    venv\\Scripts\\python.exe packaging\\smoke_test.py            (after build.ps1 -SkipInstaller)

Exercises the parts that break when packaging goes wrong: tree-sitter grammars, the bundled
ripgrep, the edit-history store and the workspace config - and checks they all write into the
chosen data folder rather than beside the exe.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastmcp import Client

APP = Path(__file__).resolve().parent / "dist_app" / ("CodyMCPServer.app/Contents/MacOS" if sys.platform == "darwin" else "CodyMCPServer")
PORT = 8021


async def call(client, command):
    result = await client.call_tool("run", {"command": command})
    return result.content[0].text if result.content else ""


async def main() -> int:
    data = Path(tempfile.mkdtemp(prefix="cody_data_"))
    work = Path(tempfile.mkdtemp(prefix="cody_ws_"))
    (work / "sample.py").write_text("def hello():\n    return 1\n\nclass A:\n    pass\n", encoding="utf-8")

    env = {**os.environ, "CODY_DATA_DIR": str(data)}
    server = subprocess.Popen(
        [str(APP / ("cody-server.exe" if sys.platform == "win32" else "cody-server")), "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    failures = []

    def check(name, condition, detail=""):
        print(("PASS " if condition else "FAIL ") + name + ("" if condition else f"  -> {detail}"))
        if not condition:
            failures.append(name)

    try:
        time.sleep(6)
        async with Client(f"http://127.0.0.1:{PORT}/mcp") as client:
            ws = str(work).replace("\\", "/")
            out = await call(client, f"--ws {ws} sym sample.py")
            check("tree-sitter symbols", "hello" in out and "A" in out, out)

            out = await call(client, f"--ws {ws} find hello")
            check("search finds text", "sample.py" in out, out)

            out = await call(client, f"--ws {ws} edit sample.py --old 'return 1' --new 'return 2'")
            check("edit works", out.startswith("ok"), out)
            out = await call(client, f"--ws {ws} read sample.py")
            check("edit applied", "return 2" in out, out)

            out = await call(client, f"--ws {ws} undo sample.py")
            check("undo restores", not out.startswith("ERR"), out)

            out = await call(client, f"--ws {ws} ws add smoke {ws}")
            check("workspace saved", not out.startswith("ERR"), out)
    finally:
        server.terminate()
        try:
            server.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server.kill()

    check("history stored in data dir", (data / "history").is_dir() and any((data / "history").iterdir()),
          str(list(data.rglob("*"))))
    check("config.json in data dir", (data / "config.json").is_file() and "smoke" in (data / "config.json").read_text(),
          str(list(data.rglob("*"))))
    check("logs dir in data dir", (data / "logs").is_dir())
    check("nothing written beside the exe", not (APP / "config.json").exists() and not (APP / "tray_logs").exists())

    print(f"\ndata dir: {data}")
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
