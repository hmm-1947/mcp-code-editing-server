import asyncio
import os

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
        print(f"FAIL  {label}  {detail[:300]}")


def size(text):
    return f"{len(text)}c ~{len(text) // 4}tok"


async def main():
    async with Client(mcp) as c:
        print("=== SIZES (baseline: read edit.py 18973c, read structure.py 6351c, find path 2318c) ===")
        o = await run(c, "read tool_handlers/edit.py")
        print("read edit.py (bare):", size(o))
        check("R1 big code file -> outline", o.startswith("# tool_handlers/edit.py") and "outline" in o and "fn edit" in o, o[:200])
        check("R1 outline has nested indent", any(l.startswith("  ") and "fn edit" in l for l in o.splitlines()), o[:400])
        check("R1 outline much smaller than 18973c", len(o) < 1500, str(len(o)))

        o = await run(c, "read core/structure.py")
        print("read structure.py (bare):", size(o))
        check("R2 171-line file -> outline", "outline" in o)

        o = await run(c, "read core/diff.py")
        check("R3 small file (17 lines) -> full code", "outline" not in o and "1|" in o, o[:120])

        o = await run(c, "read tool_handlers/edit.py --fn _dominant_ending")
        print("read --fn:", size(o))
        check("R4 --fn returns just the function", "_dominant_ending" in o and len(o.splitlines()) < 12, o)
        check("R4 --fn no 'more' hint", "# more" not in o)

        o = await run(c, "read tool_handlers/edit.py --fn nope_missing")
        check("R5 --fn missing -> ERR with similar names", o.startswith("ERR") and "not found" in o, o)

        o = await run(c, "read tool_handlers/edit.py:100-110")
        check("R6 range read", "# tool_handlers/edit.py 100-110/" in o and len(o.splitlines()) == 13 and o.splitlines()[-1].startswith("# more: read"), o[:100])

        o = await run(c, "read tool_handlers/edit.py:1-999")
        check("R7 range >200 is capped with more hint", "1-200/" in o and "# more: read" in o, o[:80])

        o = await run(c, "read tool_handlers/edit.py --full")
        check("R8 --full bypasses outline", "outline" not in o and o.count("\n") > 300, str(o.count("\n")))

        o = await run(c, "read README.MD")
        check("R9 non-code big file falls back to plain lines", "outline" not in o and o.startswith("# README.MD 1-"), o[:80])

        o = await run(c, "read tool_handlers/edit.py --around 100 --window 5")
        check("R10 --around window 5 -> 11 lines", "95-105/" in o and len(o.splitlines()) == 12, o[:80])

        o = await run(c, "read core/structure.py --outline")
        check("R11 --outline on demand", "outline" in o)

        print()
        o = await run(c, "find path --max 30")
        print("find path (grouped):", size(o), "(baseline 2318c)")
        check("F1 grouped: file header lines present", any(not l.startswith(" ") and not l.startswith("#") and ":" not in l.split(" ")[0] for l in o.splitlines()) or "file(s)" in o, o[:300])
        check("F1 footer has file+hit counts", "file(s)" in o and "hit(s)" in o.splitlines()[-1], o.splitlines()[-1])
        check("F1 smaller than baseline 2318c", len(o) < 2318, str(len(o)))

        o = await run(c, "find path --files")
        print("find --files:", size(o))
        check("F2 --files lists path (count)", all(("(" in l) or l.startswith("#") for l in o.splitlines()), o[:300])
        check("F2 --files has no code text", "def " not in o and "|" not in o, o[:200])

        o = await run(c, "find execute --defs --in core")
        check("F3 --defs single file:line form", "terminal.py:88 [def]" in o, o)

        nothing = "zz" + "zz_no_such" + "_token_qq"   # built at runtime so this file never contains it
        o = await run(c, f"find {nothing}")
        check("F4 no hits", o == "0 matches", o)

        o = await run(c, "find snapshot --in core --ctx 1")
        check("F5 --ctx shows context under file", "history.py" in o and "\n" in o, o[:200])

        o = await run(c, "find snapshot --files --in core")
        counts = {l.split(" (")[0]: int(l.split(" (")[1].rstrip(")")) for l in o.splitlines() if " (" in l and not l.startswith("#")}
        ground = 0
        with open("core/history.py", encoding="utf-8") as h:
            import re
            ground = len([1 for line in h if re.search(r"\bsnapshot\b", line)])
        check("F6 --files count matches ground truth", counts.get("history.py") == ground, f"{counts} vs {ground}")

        print()
        d = os.path.join(os.environ["TEMP"], "cody_opt").replace("\\", "/")
        os.makedirs(d, exist_ok=True)
        f = f"{d}/t.py"
        if os.path.exists(f):
            os.remove(f)
        o = await run(c, f"new {f} --content 'a = 1\\nb = 2\\nc = 3'")
        check("E1 new reply short", o.startswith("ok new 3L "), o)
        o = await run(c, f"edit {f} --lines 2-2 --new 'b = 20'")
        check("E2 lines reply shows range", o.startswith("ok 2-2 -> 2-2 "), o)
        o = await run(c, f"edit {f} --lines 2-2 --new 'x = 1\\ny = 2'")
        check("E3 grows: 2-2 -> 2-3", o.startswith("ok 2-2 -> 2-3 "), o)
        o = await run(c, f"edit {f} --lines 2-3 --new -\n")
        check("E4 empty payload rejected (no text)", o.startswith("ERR"), o)
        o = await run(c, f"append {f} --content 'z = 9'")
        check("E5 append reply reports added span (file has 4 lines after E3)", o.startswith("ok +5-5 "), o)
        check("E5b reply never echoes the long absolute temp path", "AppData" not in o and "Users" not in o, o)
        o = await run(c, f"edit {f} --lines 2-2 --new 'q = 7' --show 1")
        check("E5c --show N returns numbered context", o.startswith("ok 2-2 -> 2-2 ") and "\n1|" in o and "\n2|q = 7" in o and "\n3|" in o, o)
        o = await run(c, f"edit {f} --lines 2-2 --new 'q = 8'")
        check("E5d no --show -> single line reply", "\n" not in o, o)
        o = await run(c, f"edit {f} --old 'a = 1' --new 'a = 5'")
        check("E6 old reply", o.startswith("ok 1 replaced "), o)
        o = await run(c, f"new {f} --content 'q' --force")
        check("E7 overwrite flagged", o.startswith("ok overwrote 1L "), o)
        o = await run(c, f"rm {f}")
        check("E8 rm reply", o.startswith("ok deleted "), o)
        check("E9 rm reply under 40 chars (short path)", len(o) < 40, f"{len(o)}c {o}")

        print()
        o = await run(c, "sh echo hi")
        check("S1 success has no timing", o == "exit 0\nhi", repr(o))
        o = await run(c, "sh exit 3")
        check("S2 failure shows code+timing", o.startswith("exit 3 ") and o.endswith("ms"), repr(o))
        o = await run(c, "sh dir /s core")
        print("sh dir /s core:", size(o), "(baseline 2715c)")
        o = await run(c, 'sh dir /s /b "venv\\\\Lib"')
        print("sh big listing:", size(o))
        check("S3 big output capped near 4000", len(o) < 4400 and "clipped" in o, str(len(o)))
        check("S3 keeps head and tail", "chars omitted from the middle" in o)
        o = await run(c, 'sh --cap 800 dir /s /b "venv\\\\Lib"')
        check("S4 --cap before the command honoured", len(o) < 1300, str(len(o)))
        o = await run(c, 'sh dir /s /b "venv\\\\Lib" --cap 800')
        check("S4b trailing --cap still honoured", len(o) < 1300, str(len(o)))
        o = await run(c, "sh git reset --hard")
        check("S5 refused stays refused (--hard now reaches the guard)", o.startswith("ERR refused"), o)
        o = await run(c, "sh git log --oneline -n 1")
        check("S6 command's own --flags pass through", o.startswith("exit 0") and "unknown flag" not in o, o)
        o = await run(c, "sh --timeout 30 echo hi")
        check("S7 leading cody flag + command", o == "exit 0\nhi", repr(o))

        print()
        os.environ["CODY_NOCACHE"] = "0"
        first = await run(c, "read core/diff.py")
        again = await run(c, "read core/diff.py")
        print("repeat read:", size(first), "->", size(again))
        check("U1 first read sends the code", "1|" in first and "unchanged" not in first, first[:100])
        check("U2 identical repeat read -> tiny 'unchanged'", "unchanged" in again and "1|" not in again and len(again) < 120, again)
        part = await run(c, "read core/diff.py:1-3")
        check("U3 different range is not suppressed", "1|" in part, part)
        os.environ["CODY_NOCACHE"] = "1"

        os.environ["CODY_STATS"] = "1"
        from cody_cli import stats
        stats.reset()
        await run(c, "read core/diff.py")
        await run(c, "find snapshot --in core --files")
        await run(c, "read nope_file.py")
        s = await run(c, "stats")
        print("\n--- stats output ---\n" + s)
        check("T1 stats counts 3 calls", s.startswith("3 calls"), s)
        check("T1 stats lists per-command rows", "read" in s and "find" in s and "largest:" in s, s)
        check("T2 stats never records itself", "stats " not in s.split("\n", 1)[1])
        check("T3 errors recorded as err", any('"err": true' in l for l in open(stats.LOG, encoding="utf-8")))
        stats.reset()
        check("T4 reset clears", (await run(c, "stats")) == "no stats yet")

    print(f"\n{PASS} passed, {FAIL} failed")


asyncio.run(main())
