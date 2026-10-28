from __future__ import annotations

import os
import re
import shlex
import sys
import time

from . import stats
from .browser_cmds import cmd_browser
from .code_cmds import (
    cmd_append, cmd_edit, cmd_find, cmd_glob, cmd_new, cmd_read, cmd_rm,
    cmd_medit, cmd_sym, cmd_tree, cmd_undo, cmd_ws,
)
from .batch_cmds import cmd_batch
from .android_cmds import cmd_android
from .check_cmd import cmd_check
from .err_cmds import cmd_err, record as record_failure

from .graph_cmds import cmd_graph
from .shell_cmds import cmd_proc, cmd_sh
from .common import CliError, check_payload_count, fail, internal_error, peek_payload, set_workspace, split_line

HELP = """cody <cmd>   (-ws NAME|PATH first, once; paths relative to workspace or absolute; flags are single-dash: -flag; --flag also works)
find  Q [-files|-list] [-in DIR -glob '*.py' -type py,ts -regex -i -multiline -defs -max N -skip N -ctx N -sort mtime|hits|name]
      grouped by file; -files = paths+hit counts, -list = paths only (sorted by -sort, default hits/name); -multiline lets a regex span lines; -type = file kinds
find  Q -first [-workers N=5 -timeout S=20]   N parallel rg workers on split folders; first hit wins, the rest are cancelled; `find -next` drops that file and continues (-next -unit drops its folder); scanned folders are not rescanned
glob  PATTERN [-in DIR -max N=100 -skip N -deps]   file paths matching a glob, newest first (e.g. glob "src/**/*.ts")
read  PATH [PATH2..]   big code file -> outline (A-B kind name); small -> code as `N text` lines. Blank and comment lines are dropped (py java js ts tsx dart); numbers are kept, so a gap = blank/comment
read  PATH:A-B | -fn NAME | -around N [-window N] | -outline | -no-ln (raw, keeps blanks+comments) | -blanks | -comments | -force
read  PATH [PATH2..] -summary   header comment + top-level symbols (export marked): learn a file's role/conventions without reading it
read  X.ipynb [-cell A-B] [-outputs] [-raw]   notebook as cells ([i] type source; -raw = the JSON)
read  X.pdf[:A-B] | -pages A-B   pdf text, 10 pages by default, 30 max; scanned pdfs have no text
read  IMAGE [-max-dim N=1024 -quality N=80]   image returned inline, resized when large
      cap 200 lines/call; -full lifts it to 400. Same range unchanged -> `path:A-B unchanged` only (-force resends)
sym   FILE|DIR         symbols: A-B kind name, nested by indent (A-B = size for read -fn); cheap way to learn a file's structure
tree  [DIR] [-depth N -max N -deps]   hides .git, node_modules, .cody_history, ...
edit  PATH -lines A-B -new T [-expect T] [-nocheck]   reply: ok A-B -> C-D path; -nocheck skips the syntax check (also on new/append)
edit  PATH -old T -new T [-all] | -fn NAME -new T   reply: ok 1 replaced at L1-L2 path; -dry = diff
      insert: -before A | -after A | -insert-at N with -new T (A = text unique in the file; adds lines, replaces nothing)
      -nth N picks the Nth match of -old/-before/-after; -raw keeps backslash escapes literal in inline text
      only one of -lines/-old/-fn/-before/-after/-insert-at per edit
      context: -show N adds N lines around the edit (-show 0 = none). Default 2 for json/md/html/css/yaml (not
      syntax-checked); none for code (checked on write, run `check` for types).
new PATH -content T [-force] (makes parent dirs) | append PATH -content T | rm PATH | undo PATH
medit PATH -content - (or <<X)   then a JSON list on the next lines: [{"old": "..", "new": "..", "all": false}, ..]  several edits, ONE write, all or nothing
      edits apply in order on the running text; -dry shows the diff. Files over 10 MB: read needs A-B/-around, edits refused. BOM / utf-16 files keep their encoding.
err   [show [A-B]] | edit A[-B] -new T | ins N -new T | rm A[-B] | sub -old T -new T [-all] | apply [-force -nocheck] | clear
      when edit/new/append/medit fails with ERR, the new text is saved to txt_err (one slot per session) with the command; fix only the bad lines
      (edit/ins/rm/sub work on txt_err, not the file), then `err apply` re-runs the original command with the fixed text and clears txt_err. A later failure replaces it.
sh    [-shell cmd|ps|bash] [-cwd DIR] [-timeout S=300] [-cap N=4000] CMD...   default shell: cmd.exe
      Cody flags go BEFORE CMD; CMD's own --flags, quotes and %VARS% pass through untouched.
      PowerShell / pipes / quotes: `sh -shell ps -script -` then the script on the next lines (no quoting layer).
      Empty success prints (no output). Refuses destructive commands (git reset --hard, rm -rf /, ...).
proc  start NAME [-ready TEXT] [-wait S=30] [-cwd D] [-shell ..] -- CMD  | logs NAME [-tail N] | stop NAME | list [-all]
      background dev servers; start returns when TEXT appears in the output, with the URL. Logs live outside the workspace.
      start notes when the server moved off a taken port (and who holds it). list -all also shows dev-port listeners
      NOT started via proc (e.g. a stale server from an earlier session).
check [-what] [-cmd C] [-timeout S]   autodetects tsc / npm script / dart / cargo / go / ruff; ok X clean | FAIL
batch  one command per line on the following lines, e.g. `batch` then `tree src -depth 3` / `read a.json b.json`.
      Runs in one workspace; max 100; `-stop` halts at the first error or failed sh/check/proc. b, check, sh and proc work
      inside a batch (screenshots too); multi-line text: -new <<X, text on the next lines, closing line X. -new - and sh -script cannot.
ws    [list] | add NAME PATH | rm NAME       (-ws also accepts a directory path)
graph build [-force] | update | status | tree | open [tree]   code graph (bundled graphify) in WS/graphify-out; build reuses an existing graph, update is incremental
graph query "Q" [-dfs -budget N] | path "A" "B" [-undirected] | explain "X" [-all]   compact text; use BEFORE reading files for architecture, call-flow, dependency questions
b     Playwright's own Chromium, NOT your installed browser (any playwright-cli command; each reply shows new console
      warnings/errors with their text). Batchable. Common ones:
        open URL | goto URL | reload | close | snapshot | find TEXT | click REF | fill REF TEXT | select REF V
        press KEY (ArrowLeft, Space, a) | keydown/keyup KEY | hover REF | mousemove X Y | mousedown/mouseup
        clickat X Y [-right] [-canvas [CSS]]   move+down+up in ONE call; -canvas = canvas pixel coords (like pixel)
        eval "() => expr"   run JS in the page: the way to read canvas/game state or any DOM value
        pixel X,Y [X,Y..] [CSS]   canvas pixel colours (canvas coords, default `canvas`), no screenshot needed
        screenshot -filename F   returned as an inline image (and its path)
      open/goto URL -errors: reply is just title + console errors/warnings (with text) instead of the snapshot.
      reload is errors-only by default (-full = snapshot). Refs (e5) reset on open/goto/reload: `b snapshot` again.
      b flags are single-dash too (-headed, -filename ...); cody turns them into playwright's --flags.
android  phone control via accessibility tree; `android help` lists everything. e.g. android status | android screen | android tap 5 -screen | android type text=hi submit=1 | android open app=Chrome
stats [N|reset]        output size per command
TEXT FLAGS (-new -old -content -expect): inline values treat \\n \\t \\\\ as escapes.
RAW MODE does NO escape processing (regexes and \\n in code are kept literally): put the text on the lines after
the command and use `-new -` (all remaining text) or `-old <<A -new <<B` (each ends at a line that is exactly
its tag). Mismatched payloads = ERR, nothing written. Or -new-file F / -old-file F / -content-file F.
Workflow: find -files -> read PATH (outline) -> read -fn NAME -> edit -> check."""

COMMANDS = {
    "read": cmd_read, "find": cmd_find, "glob": cmd_glob, "tree": cmd_tree, "sym": cmd_sym,
    "edit": cmd_edit, "medit": cmd_medit, "new": cmd_new, "append": cmd_append, "rm": cmd_rm,
    "undo": cmd_undo, "sh": cmd_sh, "proc": cmd_proc, "batch": cmd_batch, "check": cmd_check, "ws": cmd_ws, "b": cmd_browser, "graph": cmd_graph, "android": cmd_android, "err": cmd_err,
}


def cmd_stats(args: list[str]) -> str:
    if args and args[0] == "reset":
        return stats.reset()
    limit = int(args[0]) if args and args[0].isdigit() else 0
    return stats.summary(limit)


COMMANDS["stats"] = cmd_stats


RAW_COMMANDS = {"sh", "batch", "proc", "check"}     # take the raw line, not shlex tokens
NO_PAYLOAD_CHECK = {"sh", "batch", "check"}         # consume trailing text themselves


def _raw_after(raw: str | None, name: str, fallback: list[str]) -> str:
    """The text after the command word, verbatim. Falls back to re-joined tokens
    when called without a raw line (e.g. `python -m cody_cli ...`)."""
    if raw is None:
        return " ".join(fallback)
    text = raw.strip()
    if text.startswith("cody "):
        text = text[5:].lstrip()
    text = re.sub(r"^--?ws\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*", "", text)
    if text.startswith(name):
        text = text[len(name):]
    return text.strip()


def dispatch(argv: list[str], raw: str | None = None) -> str:
    args = list(argv)
    marker = "--ws" if "--ws" in args else ("-ws" if "-ws" in args else "")
    if marker:
        index = args.index(marker)
        if index + 1 >= len(args):
            return fail("-ws needs a name")
        set_workspace(args[index + 1])
        del args[index:index + 2]
        if not args:
            return fail("-ws NAME needs a command after it, e.g. `-ws NAME tree`")
    if not args or args[0] in ("help", "-h", "--help"):
        return HELP
    name, rest = args[0], args[1:]
    handler = COMMANDS.get(name)
    if handler is None:
        return fail(f"unknown command '{name}'. try: {' '.join(sorted(COMMANDS))} | help")
    started = time.perf_counter()
    snapshot = peek_payload()

    try:
        if name not in NO_PAYLOAD_CHECK:
            check_payload_count(rest)
        output = handler(_raw_after(raw, name, rest) if name in RAW_COMMANDS else rest)
    except CliError as error:
        output = fail(str(error))
    except Exception as error:
        output = internal_error(name, error)
    output = record_failure(name, rest, snapshot, output)
    if name != "stats":
        stats.record(name, rest, output, int((time.perf_counter() - started) * 1000))
    return output


def parse_line(line: str) -> list[str]:
    return split_line(line)


def main() -> None:
    output = dispatch(sys.argv[1:])
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(output)
    sys.exit(1 if output.startswith("ERR ") else 0)


if __name__ == "__main__":
    main()
