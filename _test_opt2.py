"""Tests for the second round: sh rewrite, proc, batch, check, history relocation,
edit reply spans, read flags, tool-description cheat sheet.
Run:  venv\\Scripts\\python.exe _test_opt2.py
"""
import asyncio
import glob
import os
import time
import urllib.request

os.environ["CODY_STATS"] = "0"
os.environ["CODY_NOCACHE"] = "1"

from fastmcp import Client

from server_v2 import mcp

WS = "--ws joshua-mcp"
PASS = FAIL = 0


async def run(c, cmd):
    r = await c.call_tool("run", {"command": f"{WS} {cmd}"})
    return r.content[0].text


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {label}")
    else:
        FAIL += 1
        print(f"FAIL  {label}  {detail[:400]}")


async def main():
    async with Client(mcp) as c:
        print("=== tool description ===")
        tools = await c.list_tools()
        d = tools[0].description
        check("D1 cheat sheet embedded in tool description", "proc" in d and "batch" in d and "sh " in d and len(d) > 1500, str(len(d)))
        check("D2 documents raw mode has no escape processing", "NO escape processing" in d)
        check("D3 documents which browser b uses", "NOT your installed browser" in d)

        print("\n=== sh: quoting / shells ===")
        o = await run(c, 'sh dir "C:\\Program Files" /b')
        check("H1 quoted path with space survives", o.startswith("exit 0") and "Common Files" in o, o)
        o = await run(c, "sh echo it's fine")
        check("H2 apostrophe in cmd text does not break parsing", o == "exit 0\nit's fine", o)
        o = await run(c, "sh --shell ps Get-ChildItem core | Where-Object { $_.Name -like '*.py' } | Select-Object -First 1 -ExpandProperty Name")
        check("H3 PowerShell pipe works with --shell ps", o.startswith("exit 0") and ".py" in o and "not recognized" not in o, o)
        check("H3b no CLIXML noise", "CLIXML" not in o and "<Objs" not in o, o)
        o = await run(c, "sh --shell ps --script -\n$x = 'single ''quoted'' text'\nWrite-Output $x\nWrite-Output \"tab\\there\"")
        check("H4 heredoc script: quotes intact, backslash untouched", "single 'quoted' text" in o and "tab\\there" in o, o)
        o = await run(c, "sh --shell ps --script -\nWrite-Error 'boom'\nexit 4")
        check("H5 ps error: exit code + message, no preamble leak", o.startswith("exit 4") and "boom" in o and "ProgressPreference" not in o and "CLIXML" not in o, o)
        o = await run(c, "sh cmd /c exit 0")
        check("H6 empty output + exit 0 says (no output)", o == "exit 0\n(no output)", repr(o))
        o = await run(c, "sh --shell ps git reset --hard")
        check("H7 guard applies in ps mode too", o.startswith("ERR refused"), o)
        o = await run(c, "sh --shell nope echo x")
        check("H8 unknown shell rejected", o.startswith("ERR unknown shell"), o)
        o = await run(c, "sh")
        check("H9 bare sh -> usage", o.startswith("ERR usage"), o)

        print("\n=== proc ===")
        port = 8791
        server = (f"python -u -c \"import http.server,socketserver;print('Serving at http://localhost:{port}/',flush=True);"
                  f"socketserver.TCPServer(('127.0.0.1',{port}),http.server.SimpleHTTPRequestHandler).serve_forever()\"")
        await run(c, "proc stop t_srv")
        o = await run(c, f"proc start t_srv --ready Serving --wait 20 -- {server}")
        check("P1 start returns ready + pid + URL", o.startswith("ready: t_srv pid ") and f"http://localhost:{port}/" in o, o)
        try:
            status = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).status
        except Exception as error:
            status = str(error)
        check("P2 server really is serving", status == 200, str(status))
        o = await run(c, f"proc start t_srv -- {server}")
        check("P3 duplicate name refused", o.startswith("ERR") and "already running" in o, o)
        o = await run(c, "proc list")
        check("P4 list shows it up", "t_srv" in o and " up " in o, o)
        o = await run(c, "proc logs t_srv --tail 3")
        check("P5 logs return output", "Serving at" in o, o)
        o = await run(c, "proc stop t_srv")
        check("P6 stop ok", o == "ok stopped t_srv", o)
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            still = True
        except Exception:
            still = False
        check("P7 whole process tree killed (port closed)", not still)
        o = await run(c, 'proc start t_bad --wait 5 -- python -c "import sys;print(\'boom\');sys.exit(3)"')
        check("P8 crash reported with exit code + output", o.startswith("EXITED 3") and "boom" in o, o)
        await run(c, "proc stop t_bad")
        o = await run(c, "proc stop no_such_proc")
        check("P9 unknown name -> ERR", o.startswith("ERR no process"), o)
        check("P10 logs kept outside the workspace", not glob.glob("*.log") and os.path.isdir("tray_logs/proc"))

        print("\n=== batch ===")
        o = await run(c, "batch\ntree core --depth 1\nfind snapshot --files --in core\nsh echo hi\n# a comment\n")
        check("B1 runs several commands in one call", "== tree core --depth 1" in o and "== find snapshot" in o and "== sh echo hi" in o and "from" not in o.split("== sh echo hi")[0][-5:], o)
        check("B1b sh inside batch works", "== sh echo hi\nexit 0\nhi" in o, o)
        check("B1c comments skipped", "a comment" not in o, o)
        o = await run(c, "batch\nnope thing\ntree core --depth 1")
        check("B2 bad line does not stop the rest", "ERR nope: unknown command" in o and "== tree core" in o, o)
        o = await run(c, "batch --stop\nnope thing\ntree core --depth 1")
        check("B3 --stop halts at first error", "ERR nope" in o and "== tree core" not in o, o)
        o = await run(c, "batch\nproc list\nread core/diff.py --new -")
        check("B4 proc and payload commands refused inside batch", "cannot be batched" in o and o.count("ERR") == 2, o)
        o = await run(c, "batch\n")
        check("B5 empty batch -> ERR", o.startswith("ERR"), o)
        o = await run(c, "batch\n" + "\n".join(["tree core --depth 1"] * 25))
        check("B6 over 20 commands rejected", o.startswith("ERR") and "at most 20" in o, o)
        o = await run(c, "read core/diff.py:1-2 core/history.py:1-2")
        check("B7 multi-path read", "# core/diff.py 1-2/" in o and "# core/history.py 1-2/" in o, o)

        print("\n=== check ===")
        o = await run(c, "check --what")
        check("C1 --what shows detection without running", ":" in o and not o.startswith("ERR"), o)
        o = await run(c, "check --cmd python -c \"print('fine')\"")
        check("C2 pass -> ok ... clean", o.startswith("ok custom clean"), o)
        o = await run(c, "check --cmd python -c \"import sys; print('type error x.ts:3'); sys.exit(1)\"")
        check("C3 fail -> FAIL with the checker output", o.startswith("FAIL custom: exit 1") and "type error x.ts:3" in o, o)

        print("\n=== edit reply spans / read flags / history ===")
        d = os.path.join(os.environ["TEMP"], "cody_opt2").replace("\\", "/")
        os.makedirs(d, exist_ok=True)
        f = f"{d}/h.py"
        if os.path.exists(f):
            os.remove(f)
        await run(c, f"new {f} --content 'a = 1\\nb = 2\\nc = 3\\nb = 9'")
        o = await run(c, f"edit {f} --old 'c = 3' --new 'c = 30'")
        check("E1 --old reply says where", o.startswith("ok 1 replaced at 3-3 "), o)
        o = await run(c, f"edit {f} --old 'a = 1' --new 'a = 5\\nz = 0' --show 1")
        check("E2 multi-line --old reply spans + --show context", "replaced at 1-2 " in o and "\n1|a = 5" in o and "\n2|z = 0" in o, o)
        o = await run(c, f"edit {f} --old 'b = ' --new 'q = ' --all")
        check("E3 --all reply reports count", "2 replaced (first at " in o, o)
        check("E4 no .cody_history created in the project dir", not os.path.isdir(d + "/.cody_history"))
        check("E5 history stored centrally", len(glob.glob("tray_logs/history/*")) >= 1)
        o = await run(c, f"undo {f}")
        check("E6 undo works from the central store", o.startswith("ok restored "), o)
        o = await run(c, f"read {f} --no-ln")
        check("E7 --no-ln drops N| prefixes", "|" not in o.split("\n", 1)[1] and "a = 5" in o, o)
        os.environ["CODY_NOCACHE"] = "0"
        await run(c, f"read {f}")
        again = await run(c, f"read {f}")
        forced = await run(c, f"read {f} --force")
        os.environ["CODY_NOCACHE"] = "1"
        check("E8 repeat read -> unchanged, mentions --force", "unchanged" in again and "--force" in again, again)
        check("E9 --force resends the code", "1|" in forced and "unchanged" not in forced, forced)
        os.remove(f)

    print(f"\n{PASS} passed, {FAIL} failed")


asyncio.run(main())
