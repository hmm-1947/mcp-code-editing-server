import importlib.abc, importlib.machinery, os, sys, tempfile, json
from unittest.mock import MagicMock

class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0].startswith("tree_sitter"):
            return importlib.machinery.ModuleSpec(name, self)
    def create_module(self, spec):
        m = MagicMock(); m.__name__ = spec.name; m.__path__ = []; return m
    def exec_module(self, module):
        pass

sys.meta_path.insert(0, Finder())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cody_cli import main, common

tmp = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp, "pkg"))
open(os.path.join(tmp, "a.py"), "w", newline="").write("def f():\n    return 1\n\ndef g():\n    return 2\n")
open(os.path.join(tmp, "pkg", "b.py"), "w").write("x = 1\nfoo\nbar\n")
open(os.path.join(tmp, "pkg", "c.ts"), "w").write("const x = 1\n")
nb = {"cells": [{"cell_type": "markdown", "source": ["# T\n", "hi"]}, {"cell_type": "code", "execution_count": 3, "source": "print(1)", "outputs": [{"output_type": "stream", "text": "1\n"}]}], "metadata": {"kernelspec": {"language": "python"}}}
json.dump(nb, open(os.path.join(tmp, "n.ipynb"), "w"))

fails = []
def run(line, body=None):
    common.set_payloads(body)
    try:
        argv = common.split_line(line)
        return main.dispatch(argv, raw=line)
    finally:
        common.set_payloads(None)

def check(name, ok, out=""):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)
        print("   ", str(out)[:600])

def a_py():
    return open(os.path.join(tmp, "a.py")).read()

W = "--ws " + tmp + " "
common.set_session("t1")

o = run(W + "edit a.py --lines 2-2 --new -", "    return (1\n")
check("E1 bad edit saved", o.startswith("ERR") and "saved to txt_err (1L)" in o, o)
o = run(W + "err show")
check("E2 show", "1|    return (1" in o and "edit a.py --lines 2-2" in o, o)
o = run(W + "err sub --old '(1' --new '1'")
check("E3 sub", o.startswith("ok txt_err 1 replaced"), o)
o = run(W + "err apply")
check("E4 apply ok + cleared", o.startswith("ok 2-2") and "txt_err cleared" in o, o)
check("E4b file content", a_py() == "def f():\n    return 1\n\ndef g():\n    return 2\n", a_py())
o = run(W + "err")
check("E5 empty after clear", o == "txt_err empty", o)

o = run(W + "edit a.py --old <<A --new <<B", "def g():\n    return 2\nA\ndef g():\n    return (2\n    pass\nB\n")
check("E6 old/new heredoc fail saved", o.startswith("ERR") and "saved to txt_err (3L)" in o, o)
o = run(W + "err rm 2")
check("E7 rm", o.startswith("ok txt_err removed 2-2, 2L left"), o)
o = run(W + "err ins 2 --new '    return 22'")
check("E8 ins", o.startswith("ok txt_err inserted 2-2, 3L total"), o)
o = run(W + "err edit 3 --new '    pass'")
check("E9 edit", "3|    pass" in o, o)
o = run(W + "err apply")
check("E10 apply old-file", o.startswith("ok") and "txt_err cleared" in o, o)
check("E10b content", "return 22" in a_py() and "return 1" in a_py(), a_py())

o = run(W + "new a.py --content 'z = 1'")
check("E11 new exists saved", o.startswith("ERR") and "saved to txt_err" in o, o)
o = run(W + "err apply --force")
check("E12 apply force", o.startswith("ok overwrote"), o)
o = run(W + "err apply --force")
check("E13 apply when empty", o.startswith("ERR txt_err is empty"), o)

o = run(W + "edit a.py --after <<A --new 'q = 3'", "z = 1\nA\n")
check("E14 anchor heredoc", o.startswith("ok inserted"), o)

o = run(W + "batch", "edit pkg/b.py --lines 9-9 --new 'x'\nerr show\nerr clear")
check("E15 batch record+show", "saved to txt_err (1L)" in o and "1|x" in o and "ok txt_err cleared" in o, o)

o = run(W + "find foo --files --sort mtime")
check("F1 files mtime", "pkg/b.py (1)" in o and "by mtime" in o, o)
o = run(W + "find x --list --type ts")
check("F2 list type", o.startswith("pkg/c.ts"), o)
o = run(W + "find 'foo\\nbar' --regex --multiline --files")
check("F3 multiline", "pkg/b.py" in o, o)
o = run(W + "find x --type nope")
check("F4 bad type", o.startswith("ERR unknown --type"), o)
o = run(W + "find foo --first --workers 3")
check("F5 first", "pkg/b.py:2" in o and "hunt:" in o, o)
o = run(W + "find --next")
check("F6 next", "0 more matches" in o, o)
o = run(W + "glob '**/*.py'")
check("F7 glob", "a.py" in o and "newest first" in o, o)

o = run(W + "read n.ipynb --outputs")
check("N1 notebook", "[0] markdown" in o and "[1] code (3)" in o and "out:" in o, o)
o = run(W + "read n.ipynb --cell 1")
check("N2 cell", "[1] code" in o and "[0]" not in o, o)

try:
    import pypdf
    have_pdf = True
except ImportError:
    have_pdf = False
try:
    import PIL
    have_pil = True
except ImportError:
    have_pil = False
print("pypdf", have_pdf, "PIL", have_pil)
if have_pil:
    from PIL import Image
    Image.new("RGB", (3000, 2000), (200, 30, 30)).save(os.path.join(tmp, "big.png"))
    o = run(W + "read big.png --max-dim 512")
    check("I1 image resize", "512x341" in o and "ok screenshot" in o, o)
o = run(W + "read missing.pdf")
check("P0 missing pdf", o.startswith("ERR"), o)
if have_pdf:
    w = pypdf.PdfWriter()
    for _ in range(3):
        w.add_blank_page(200, 200)
    w.write(open(os.path.join(tmp, "p.pdf"), "wb"))
    o = run(W + "read p.pdf:2-3")
    check("P1 pdf pages", "pages 2-3/3" in o, o)

run(W + "err clear")
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
