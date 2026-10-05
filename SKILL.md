---
name: local-code-editing
description: Use whenever the Cody MCP server is connected (a single tool, `run`, that takes a Cody CLI command line). Covers how to find, read and edit code with it, run shell commands and background servers, check a change, drive a browser, and batch several commands into one call. Applies to any codebase reached through this server.
---

# Cody: one tool, `run(command)`

There is exactly one tool: `run(command)`. The `command` is a Cody CLI line, for
example `read src/app.py:40-80` or `sh pytest -q`. The tool description already
carries the full command list, so `help` is rarely needed. Nothing is staged or
enforced: find, read, edit, report, and stop.

```
run("find handleLogin --glob '*.dart'")
run("read lib/auth.dart --fn login")
run("edit lib/auth.dart --lines 44-46 --new 'x = 1'")
run("sh flutter analyze")
```

## Rules

- Do not add comments to the code.
- When using this MCP tool, no need to tell the user what you are going to do or what you find while editing. If you want to say anything, say it in one line. Never give the user a long description of what is being done or found.
- If anything is left to do at the end, or the limits of the session are about to run out, say what is left in nice one-liners.

## Workspace and paths

Paths are relative to the workspace, or absolute. Pick the workspace with `--ws`,
first on the line, with a saved alias or a plain directory path:

```
--ws myapp tree --depth 2
--ws D:/projects/myapp find login
```

`ws` lists saved aliases (`ws`), saves one (`ws add NAME PATH`) or removes one
(`ws rm NAME`). Without `--ws`, relative paths resolve against the server's own
working directory, so set it whenever you are working on a project. If `--ws NAME`
is on a line by itself, the command may follow on the next line.

### Path rules (Windows server)

- The server runs on the user's Windows machine, so paths are Windows paths: `D:/projects/myapp`. Never use `/home/...`, `/mnt/...` or `~` paths from your own sandbox.
- Forward slashes are safest: `D:/projects/myapp/src/a.py`. Backslashes also work (`D:\projects\myapp`), and they are never treated as escapes.
- Paths with spaces go in single quotes: `--ws 'D:/My Projects/app'`.
- Register a workspace once with its full absolute path, then reuse the alias:

```
ws add myapp D:/projects/myapp
--ws myapp tree --depth 2
```

- If the user gives a path, use it exactly as given (do not guess a drive or prefix). If unsure, run `ws` to see saved aliases, or `--ws D:/ tree --depth 1` to look around.
- A path error such as "neither a registered workspace nor an existing directory" means the path is wrong: run `ws`, fix the path, and retry. Do not keep retrying variations.
- Inside a workspace, use relative paths with forward slashes (`src/app.py`), never a leading `/`.

## Locate

```
find Q                        every occurrence, grouped by file
find Q --files                just paths and hit counts (cheapest first look)
find Q --defs                 only definitions (tree-sitter), one file:line each
find Q --in lib --glob '*.dart' --regex --i --ctx 2 --max 50
tree [DIR] --depth 2          layout (hides .git, node_modules, ...)
sym FILE|DIR                  symbols as `A-B kind name`, nested by indent
```

`find --files` then `sym` or `read` is the cheap way in: learn the layout before
spending tokens on file bodies.

## Read

```
read PATH                     big code file -> outline; small file -> code with N| line numbers
read PATH:40-120              a range
read PATH --fn NAME           one function (the outline shows each size as A-B)
read PATH --around 88 --window 10
read PATH --summary           header comment + top-level symbols; a file's role without reading it
read A B C --summary          several files at once
```

Other flags: `--outline`, `--no-ln`, `--force`. A call returns at most 200 lines
(`--full` lifts that to 400). Reading the same unchanged range again returns
"unchanged"; use `--force` to resend it. Read the region you are about to change:
the line numbers are the ones `edit` expects.

## Edit

Preferred: replace a line range, so the old code never has to be reproduced.

```
edit PATH --lines 44-46 --new 'x = 1'          reply: ok 44-46 -> 44-44 path
edit PATH --lines 44-46 --new 'x = 1' --expect 'old text'
edit PATH --old 'foo(' --new 'bar(' [--all]    exact text (error if it matches more than once)
edit PATH --fn NAME --new 'def NAME(): ...'    replace a whole function
edit PATH --old T --new T --dry                show the diff, write nothing
new PATH --content T [--force]                 create a file (makes parent dirs)
append PATH --content T
rm PATH
undo PATH                                      restore the previous version of an edit
```

Inline text treats `\n`, `\t` and `\\` as escapes. For multi-line code, or anything
with regexes or backslashes, use raw payloads: put the text on the lines after the
command.

```
edit lib/a.dart --lines 4-6 --new -
<new code, every remaining line>
```

For several payloads use heredoc-style tags: `--old <<A --new <<B`, each ending at a
line that is exactly its tag (pick a tag that never appears alone on a line). A count
mismatch is an `ERR` and nothing is written. `--new-file F`, `--old-file F` and
`--content-file F` read the text from a file.

Refusals worth respecting: a text match that occurs more than once is an error
(widen `--old`, use `--lines`, or pass `--all`); an `--expect` that no longer matches
means the lines moved, so re-read; and a write that leaves brackets unbalanced (C-family
and Dart files) or Python with a syntax error is rejected and nothing is written.
`--show N` adds N lines of context to the reply (default 2 for json/md/html/css/yaml,
which are not syntax-checked; none for code).

## Shell and background processes

```
sh pytest -q                          default shell is cmd.exe
sh --cwd lib --timeout 60 --cap 4000 flutter analyze
sh --shell ps Get-ChildItem | Select-Object -First 5
```

Cody flags go before the command; the command's own `--flags`, quotes and `%VARS%`
pass through untouched. For PowerShell with pipes or quotes, use `sh --shell ps
--script -` and put the script on the following lines. Success with no output prints
`(no output)`. Destructive commands (`git reset --hard`, `rm -rf /`, ...) are
refused; ask the user to run those.

Dev servers and anything long-running go in the background:

```
proc start web --ready "ready in" --wait 30 --cwd app -- npm run dev
proc logs web --tail 40
proc list [--all]            --all also shows dev-port listeners not started via proc
proc stop web
```

`start` returns once the ready text appears, with the URL, and says when the server
moved off a taken port. Logs are kept outside the workspace.

## Check

```
check                  autodetects tsc / npm script / dart / cargo / go / ruff
check --what           show what it would run, without running it
check --cmd "pytest -q" [--timeout S]
```

Reply is `ok ...` when clean and `FAIL` plus the checker output otherwise. Run it
when asked, or when a change is large enough to be in doubt. A one-line edit needs
no check. Do not build a verification loop, and do not rewrite working code to get
around a missing tool; report the missing tool.

## Batch

One command per line after `batch`, in one workspace, at most 20 per call:

```
--ws myapp batch
tree src --depth 2
read a.json b.json
find TODO --files
```

`batch --stop` halts at the first error or failed `sh`/`check`/`proc`. `b`, `check`,
`sh` and `proc` work inside a batch; payload commands (`--new -`) and `sh --script`
do not.

## Browser

`b <playwright-cli command>` drives Playwright's, Every reply lists new console warnings and errors.

```
b open http://localhost:3000 --errors      title + console errors only
b snapshot | b find TEXT | b click e5 | b fill e7 "text"
b eval "() => document.title"              read DOM, canvas or game state
b pixel 10,20 canvas                        canvas colours without a screenshot
b screenshot --filename shot.png            returned as an inline image
```

Refs (`e5`) reset on `open`, `goto` and `reload`: take a fresh `b snapshot` first.

## Housekeeping

`stats [N|reset]` shows output size per command, useful for finding what is eating
tokens. Typical flow: `find --files` -> `read PATH` (outline) -> `read --fn NAME` ->
`edit` -> `check` (only if needed).