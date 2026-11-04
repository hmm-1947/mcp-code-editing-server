import importlib.abc
import importlib.machinery
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


class Stub(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0].startswith("tree_sitter"):
            return importlib.machinery.ModuleSpec(name, self)

    def create_module(self, spec):
        return MagicMock()

    def exec_module(self, module):
        pass


try:
    import tree_sitter_python
    REAL = True
except ImportError:
    REAL = False
    sys.meta_path.insert(0, Stub())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cody_cli import code_cmds, main
from cody_cli.common import set_payloads

folder = tempfile.mkdtemp()
code_cmds._STATE_FILE = Path(folder) / "state.json"
failures = []


def run(*args):
    return main.dispatch(["--ws", folder] + list(args))


def batch(script):
    set_payloads(script)
    try:
        return main.dispatch(["--ws", folder, "batch"])
    finally:
        set_payloads(None)


def check(label, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else " " + str(detail)))
    if not condition:
        failures.append(label)


def write(name, text):
    open(Path(folder, name), "w", encoding="utf-8", newline="").write(text)


def content(name):
    return open(Path(folder, name), encoding="utf-8", newline="").read()


write("a.txt", "one\ntwo\ntwo\nthree\n")
run("read", "a.txt")
out = run("edit", "a.txt", "--lines", "1", "--old", "one", "--new", "x")
check("conflict lines+old", out.startswith("ERR") and "conflicting" in out, out)
check("conflict wrote nothing", content("a.txt") == "one\ntwo\ntwo\nthree\n")
out = run("edit", "a.txt", "--old", "two", "--new", "TWO")
check("old multi still errors", "-nth" in out, out)
out = run("edit", "a.txt", "--old", "two", "--nth", "2", "--new", "TWO")
check("old nth 2", content("a.txt") == "one\ntwo\nTWO\nthree\n", out)
out = run("edit", "a.txt", "--old", "two", "--nth", "3", "--new", "Z")
check("old nth beyond", out.startswith("ERR"), out)
out = run("edit", "a.txt", "--after", "e", "--new", "ins")
check("anchor multi errors", "-nth" in out, out)
out = run("edit", "a.txt", "--after", "e", "--nth", "1", "--new", "ins")
check("anchor nth 1", content("a.txt") == "one\nins\ntwo\nTWO\nthree\n", out + content("a.txt"))
out = run("edit", "a.txt", "--before", "e", "--nth", "9", "--new", "ins")
check("anchor nth beyond", out.startswith("ERR"), out)

write("b.txt", "p\\tq\n")
run("read", "b.txt")
out = run("edit", "b.txt", "--raw", "--old", "\\tq", "--new", "\\tr")
check("raw keeps backslash", content("b.txt") == "p\\tr\n", out + content("b.txt"))

write("c.txt", "first\nsecond\n")
out = batch("read c.txt\nedit c.txt --lines 1 --new 'a\nb'\nread c.txt --force")
check("batch multi-line quoted new", content("c.txt") == "a\nb\nsecond\n", out + repr(content("c.txt")))
out = batch("sh echo it's\nfind first --files")
check("batch sh not joined", "find first --files" in out and "== find" in out, out)

write("d.txt", "alpha\n")
run("read", "d.txt")
disk = json.loads(code_cmds._STATE_FILE.read_text(encoding="utf-8"))
check("state persisted", any(k.endswith("d.txt") for k in disk), disk)
code_cmds._STATE.clear()
code_cmds._DISK = None
write("d.txt", "beta\n")
out = run("edit", "d.txt", "--old", "beta", "--new", "gamma")
check("guard survives restart", out.startswith("ERR") and "changed since" in out, out)
out = run("edit", "d.txt", "--old", "beta", "--new", "gamma", "--force")
check("force bypasses", content("d.txt") == "gamma\n", out)
run("rm", "d.txt")
disk = json.loads(code_cmds._STATE_FILE.read_text(encoding="utf-8"))
check("rm forgets", not any(k.endswith("d.txt") for k in disk), disk)

def run_payload(body, *args):
    set_payloads(body)
    try:
        return main.dispatch(["--ws", folder] + list(args))
    finally:
        set_payloads(None)


def raw_bytes(name):
    return open(Path(folder, name), "rb").read()


write("e.txt", "a\nb\nc\n")
run("read", "e.txt")
out = run_payload('[{"old": "a", "new": "A"}, {"old": "c", "new": "C\\\\d"}]', "medit", "e.txt", "--content", "-")
check("medit applies all", content("e.txt") == "A\nb\nC\\d\n", out + repr(content("e.txt")))
out = run_payload('[{"old": "A", "new": "1"}, {"old": "zzz", "new": "2"}]', "medit", "e.txt", "--content", "-")
check("medit all or nothing", out.startswith("ERR") and content("e.txt") == "A\nb\nC\\d\n", out)
out = run("medit", "e.txt", "--content", "[]")
check("medit needs raw", out.startswith("ERR"), out)

Path(folder, "bom.txt").write_bytes(b"\xef\xbb\xbfhello\nworld\n")
run("read", "bom.txt")
out = run("edit", "bom.txt", "--old", "hello", "--new", "hi")
check("utf8 bom kept", raw_bytes("bom.txt") == b"\xef\xbb\xbfhi\nworld\n", out + repr(raw_bytes("bom.txt")))
out = run("read", "bom.txt")
check("bom hidden on read", "1 hi" in out, out)

Path(folder, "u16.txt").write_bytes("x\ny\n".encode("utf-16"))
head = raw_bytes("u16.txt")[:2]
out = run("read", "u16.txt")
check("utf16 read", "1 x" in out and "2 y" in out, out)
out = run("edit", "u16.txt", "--old", "y", "--new", "zz")
data = raw_bytes("u16.txt")
check("utf16 edit keeps encoding", data[:2] == head and data[2:].decode("utf-16-le" if head == b"\xff\xfe" else "utf-16-be") == "x\nzz\n", out + repr(data))

old_limit = code_cmds.EDIT_MAX_BYTES
code_cmds.EDIT_MAX_BYTES = 1000
write("big.txt", "".join(f"line {i}\n" for i in range(1, 501)))
out = run("read", "big.txt:5-7")
check("big read range", "5 line 5" in out and "7 line 7" in out and out.startswith("big.txt:5-7"), out)
out = run("read", "big.txt")
check("big read head", "1 line 1" in out and "# more: read big.txt:201-" in out, out[-120:])
out = run("read", "big.txt", "--around", "400", "--window", "2")
check("big read around", "398 line 398" in out and "402 line 402" in out, out)
out = run("read", "big.txt", "--outline")
check("big outline refused", out.startswith("ERR"), out)
out = run("edit", "big.txt", "--old", "line 1", "--nth", "1", "--new", "x")
check("big edit refused", out.startswith("ERR") and "limit" in out, out)
code_cmds.EDIT_MAX_BYTES = old_limit

write("wide.txt", "".join("w" * 5000 + "\n" for _ in range(30)))
out = run("read", "wide.txt")
check("long line clipped", "...[+" in out and len(out) < code_cmds.READ_MAX_CHARS + 1000, len(out))
check("char cap cut hint", "# more: read wide.txt:" in out, out[-100:])

write("fmt.py", "import os\n\n# top comment\nx = 1  # trailing\n\n\"\"\"doc # not comment\"\"\"\ns = \"# keep\"\ny = 2\n")
out = run("read", "fmt.py")
check("read header short", out.startswith("fmt.py:1-8\n"), out)
check("blank lines dropped", "\n2 \n" not in out and "\n5 " not in out and "\n2 " not in out, out)
check("no pipe prefix", "1 import os" in out and "|" not in out.replace("# keep", ""), out)
again = run("read", "fmt.py")
check("unchanged short", again == "fmt.py:1-8 unchanged; -force to resend", again)
forced = run("read", "fmt.py", "-force")
check("force resends", forced == out, forced)
if REAL:
    check("comment dropped", "top comment" not in out and "trailing" not in out, out)
    check("trailing trimmed", "\n4 x = 1\n" in out, repr(out))
    check("string hash kept", "# keep" in out and "doc # not comment" in out, out)
    check("comments flag", "top comment" in run("read", "fmt.py", "-comments", "-force"))
check("blanks flag", "\n2 \n" in run("read", "fmt.py", "-blanks", "-force"))
write("s.txt", "one\n")
run("read", "s.txt")
out = run("edit", "s.txt", "-old", "one", "-new", "ONE")
check("single dash flags", out.startswith("ok") and content("s.txt") == "ONE\n", out)
out = main.dispatch(["-ws", folder, "read", "s.txt"])
check("-ws alias", out.startswith("s.txt:1-1"), out)
out = batch("read s.txt\nfind ONE --files")
check("batch read no dup header", out.startswith("s.txt:1-1") and "== find ONE --files" in out and "== read" not in out, out)

write("g.txt", "a\nb\nc\nd\n")
run("read", "g.txt")
out = batch("edit g.txt --lines 2 --new B --show 0\nedit g.txt --insert-at 1 --new top --show 0\nedit g.txt --old d --new D --show 0\nsh echo hi")
check("batch edits grouped", out.startswith("E g.txt: 2, +1, ~5\nS echo hi\n"), out)

if os.name == "nt":
    out = run("read", "//server/share/x.txt")
    check("unc refused", out.startswith("ERR") and "UNC" in out, out)

print("FAILED: " + ", ".join(failures) if failures else "ALL PASS")
sys.exit(1 if failures else 0)
