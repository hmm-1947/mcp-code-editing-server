import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["CODY_STATS"] = "0"
os.environ["CODY_NOCACHE"] = "1"

from cody_cli.main import dispatch, parse_line
from cody_cli import common

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "  " + str(detail)[:300]))


def run(line, payload=None):
    common.set_payloads(payload)
    try:
        return dispatch(parse_line(line), raw=line)
    finally:
        common.set_payloads(None)


tmp = Path(tempfile.mkdtemp(prefix="cody_repro_"))
(tmp / "sub").mkdir()
(tmp / "sub" / "a.txt").write_text("hello\n", encoding="utf-8")

win = str(tmp / "sub" / "a.txt")
tokens = parse_line(f"read {win}")
check("W1 backslash windows path survives parse_line", tokens[1] == win, tokens)
out = run(f"--ws {tmp} read sub\\a.txt")
check("W2 backslash relative path reads", "hello" in out, out)

crlf = tmp / "crlf.py"
crlf.write_bytes(b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n")
out = run(f"--ws {tmp} edit crlf.py --fn a --new 'def a():\\n    return 10'")
data = crlf.read_bytes()
check("E1 --fn edit keeps CRLF endings", data.count(b"\n") == data.count(b"\r\n"), data)

ff = tmp / "ff.txt"
ff.write_bytes("one\ntwo\x0c\nthree\n".encode("utf-8"))
out = run(f"--ws {tmp} edit ff.txt --lines 1-1 --new 'ONE'")
after = ff.read_bytes().decode("utf-8")
check("E2 line edit preserves form feed", "\x0c" in after, repr(after))

u2028 = tmp / "u.json"
u2028.write_bytes('{"a": "x\u2028y",\n "b": 1}\n'.encode("utf-8"))
out = run(f"--ws {tmp} edit u.json --lines 2-2 --new ' \"b\": 2}}'")
after = u2028.read_bytes().decode("utf-8")
check("E3 line edit preserves U+2028 inside a line", "x\u2028y" in after and after.count("\n") == 2, repr(after))

dir_a = tmp / "A"
dir_b = tmp / "B"
dir_a.mkdir()
dir_b.mkdir()
(dir_a / "marker_a.txt").write_text("A\n", encoding="utf-8")
(dir_b / "marker_b.txt").write_text("B\n", encoding="utf-8")
bad = []


def worker(ws, name, other):
    for _ in range(60):
        out = run(f"--ws {ws} tree")
        if name not in out or other in out:
            bad.append(out)
            return
        time.sleep(0.001)


t1 = threading.Thread(target=worker, args=(dir_a, "marker_a", "marker_b"))
t2 = threading.Thread(target=worker, args=(dir_b, "marker_b", "marker_a"))
t1.start(); t2.start(); t1.join(); t2.join()
check("C1 concurrent --ws calls do not cross workspaces", not bad, bad[:1])

import server
import io
import contextlib

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    server.run.fn(f"--ws {tmp} tree") if hasattr(server.run, "fn") else None
check("P1 run() writes nothing to stdout (stdio transport safe)", buf.getvalue() == "", repr(buf.getvalue()[:80]))

req = Path(__file__).with_name("requirements.txt").read_bytes()
check("R1 requirements.txt is not UTF-16", not req.startswith((b"\xff\xfe", b"\xfe\xff")), req[:4])

start = time.time()
out = run(f"--ws {tmp} sh --timeout 3 ping -n 20 127.0.0.1")
elapsed = time.time() - start
check("T1 sh timeout returns within ~timeout+3s", elapsed < 8 and "TIMEOUT" in out, f"{elapsed:.1f}s {out[:80]}")

import asyncio
from fastmcp import Client

os.environ["CODY_NOCACHE"] = "0"


async def sessions():
    async with Client(server.mcp) as one, Client(server.mcp) as two:
        cmd = f"--ws {tmp} read sub/a.txt"
        first = (await one.call_tool("run", {"command": cmd})).content[0].text
        again = (await one.call_tool("run", {"command": cmd})).content[0].text
        other = (await two.call_tool("run", {"command": cmd})).content[0].text
        return first, again, other


first, again, other = asyncio.run(sessions())
check("S1 same session repeat read is 'unchanged'", "unchanged" in again, again)
check("S2 a different session still gets the content", "hello" in other and "unchanged" not in other, other)
run(f"--ws {tmp} edit sub/a.txt --lines 1-1 --new 'bye'")
check("A1 atomic write leaves no temp file", not list(tmp.rglob("*.cody-tmp")), list(tmp.rglob("*.cody-tmp")))
check("A2 edit applied", (tmp / "sub" / "a.txt").read_text(encoding="utf-8").startswith("bye"))

n = sum(1 for r in RESULTS if r)
print(f"\n{n} passed, {len(RESULTS) - n} failed")
sys.exit(0 if n == len(RESULTS) else 1)
