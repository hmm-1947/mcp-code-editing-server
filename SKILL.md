---
name: local-code-editing
description: Use whenever the Cody MCP server is connected (a single tool, `run`, that takes a Cody CLI command line). Covers how to find, read and edit code with it, run shell commands and background servers, check a change, drive a browser, batch several commands into one call, and query a code graph (architecture, call flow, dependencies) before reading files. Applies to any codebase reached through this server.
---

# Cody: one tool, `run(command)`

There is exactly one tool: `run(command)`. The `command` is a Cody CLI line, for
example `read src/app.py:40-80` or `sh pytest -q`. The tool description already
carries the full command list, so `help` is rarely needed. Nothing is staged or
enforced: find, read, edit, report, and stop.

```
run("find handleLogin -glob '*.dart'")
run("read lib/auth.dart -fn login")
run("edit lib/auth.dart -lines 44-46 -new 'x = 1'")
run("sh flutter analyze")
```

## Rules

- Do not add comments to the code.
- When using this MCP tool, no need to tell the user what you are going to do or what you find while editing. If you want to say anything, say it in one line. Never give the user a long description of what is being done or found.
- If anything is left to do at the end, or the limits of the session are about to run out, say what is left in nice one-liners.
- Flags are single-dash: `-ws`, `-lines`, `-new`, `-files`. The double-dash form still works, but write single-dash. This includes `b` (cody converts them for playwright-cli) and `graph`. Only flags of programs that `sh` runs (`git`, `npm`) keep their own form.

## Workspace and paths

Paths are relative to the workspace, or absolute. Pick the workspace with `-ws`,
first on the line, with a saved alias or a plain directory path:

```
-ws myapp tree -depth 2
-ws D:/projects/myapp find login
```

`ws` lists saved aliases (`ws`), saves one (`ws add NAME PATH`) or removes one
(`ws rm NAME`). Without `-ws`, relative paths resolve against the server's own
working directory, so set it whenever you are working on a project. If `-ws NAME`
is on a line by itself, the command may follow on the next line.

### Path rules (Windows server)

- The server runs on the user's Windows machine, so paths are Windows paths: `D:/projects/myapp`. Never use `/home/...`, `/mnt/...` or `~` paths from your own sandbox.
- Forward slashes are safest: `D:/projects/myapp/src/a.py`. Backslashes also work (`D:\projects\myapp`), and they are never treated as escapes.
- Paths with spaces go in single quotes: `-ws 'D:/My Projects/app'`.
- Register a workspace once with its full absolute path, then reuse the alias:

```
ws add myapp D:/projects/myapp
-ws myapp tree -depth 2
```

- If the user gives a path, use it exactly as given (do not guess a drive or prefix). If unsure, run `ws` to see saved aliases, or `-ws D:/ tree -depth 1` to look around.
- A path error such as "neither a registered workspace nor an existing directory" means the path is wrong: run `ws`, fix the path, and retry. Do not keep retrying variations.
- Inside a workspace, use relative paths with forward slashes (`src/app.py`), never a leading `/`.

## Locate

```
find Q                        every occurrence, grouped by file
find Q -files                just paths and hit counts (cheapest first look)
find Q -defs                 only definitions (tree-sitter), one file:line each
find Q -in lib -glob '*.dart' -regex -i -ctx 2 -max 50
find Q -list                 paths only, no counts
find Q -files -sort mtime    newest files first (also hits, name); -skip N pages
find Q -type py,ts           file kinds instead of -glob
find Q -multiline -regex    pattern may span lines
find Q -first [-workers 5 -timeout 20]   parallel hunt: first hit wins, the other workers stop; `find -next` drops that file and goes on, `find -next -unit` drops its folder
glob '**/*.ts' [-max N -skip N]            paths by glob, newest first
tree [DIR] -depth 2          layout (hides .git, node_modules, ...)
sym FILE|DIR                  symbols as `A-B kind name`, nested by indent
```

`find -files` then `sym` or `read` is the cheap way in: learn the layout before
spending tokens on file bodies.

## Read

```
read PATH                     big code file -> outline; small file -> code as `N text` lines
read PATH:40-120              a range
read PATH -fn NAME           one function (the outline shows each size as A-B)
read PATH -around 88 -window 10
read PATH -summary           header comment + top-level symbols; a file's role without reading it
read A B C -summary          several files at once
```

Output is `path:A-B` then `N text` lines. Blank lines and comments (py, java, js, ts, tsx, dart)
are dropped but numbers are kept, so a gap in the numbers is a blank or comment line.
Other flags: `-outline`, `-no-ln` (raw: keeps blanks and comments), `-blanks`, `-comments`, `-force`.
A call returns at most 200 lines (`-full` lifts that to 400). Reading the same unchanged
range again returns `path:A-B unchanged`; use `-force` to resend it. Read the region you are
about to change: the line numbers are the ones `edit` expects. Every flag also works with one
dash (`-lines`, `-new`, `-ws`).
```
read X.ipynb [-cell 2-5] [-outputs] [-raw]   notebook as cells; -raw = the JSON
read X.pdf:3-5 | -pages 3-5                    pdf text, 10 pages by default, 30 max
read IMAGE [-max-dim 1024 -quality 80]        image sent inline, resized when large
```


## Edit

Preferred: replace a line range, so the old code never has to be reproduced.

```
edit PATH -lines 44-46 -new 'x = 1'          reply: ok 44-46 -> 44-44 path
edit PATH -lines 44-46 -new 'x = 1' -expect 'old text'
edit PATH -old 'foo(' -new 'bar(' [-all]    exact text (error if it matches more than once)
edit PATH -fn NAME -new 'def NAME(): ...'    replace a whole function
edit PATH -old T -new T -dry                show the diff, write nothing
medit PATH -content -                         then a JSON list on the next lines: [{"old": "..", "new": "..", "all": false}]; several edits, one write, all or nothing
new PATH -content T [-force]                 create a file (makes parent dirs)
append PATH -content T
rm PATH
undo PATH                                      restore the previous version of an edit
```

Inline text treats `\n`, `\t` and `\\` as escapes. For multi-line code, or anything
with regexes or backslashes, use raw payloads: put the text on the lines after the
command.

`-raw` keeps backslashes literal in inline text. In `batch`, a quoted inline value may span lines. An edit after the file changed outside Cody is refused, also across server restarts (`-force` bypasses).

```
edit lib/a.dart -lines 4-6 -new -
<new code, every remaining line>
```

For several payloads use heredoc-style tags: `-old <<A -new <<B`, each ending at a
line that is exactly its tag (pick a tag that never appears alone on a line). A count
mismatch is an `ERR` and nothing is written. `-new-file F`, `-old-file F` and
`-content-file F` read the text from a file.
`-before` and `-after` take `-` / `<<TAG` payloads too (a multi-line anchor).

Refusals worth respecting: giving two of `-lines/-old/-fn/-before/-after/-insert-at` is an error; a text match that occurs more than once is an error
(widen it, use `-lines`, `-all`, or `-nth N` to pick the Nth match, also for `-before/-after`); an `-expect` that no longer matches
means the lines moved, so re-read; and a write that leaves brackets unbalanced (C-family
and Dart files) or Python with a syntax error is rejected and nothing is written.
`-show N` adds N lines of context to the reply (default 2 for json/md/html/css/yaml,
which are not syntax-checked; none for code).

## Failed writes: txt_err

When `edit`, `new`, `append` or `medit` fails with `ERR`, the new text (`-new` / `-content`)
is saved to `txt_err` (one slot per session) together with the command, and the reply ends
with a `# saved to txt_err` line. Fix the saved text, not the whole edit; do not resend the code.

```
err show [A-B]                  numbered lines of txt_err, with the failed command and error
err edit 12-13 -new 'x = 1'    replace lines of txt_err
err ins 14 -new 'y = 2'        insert before line 14 (N = lines+1 appends)
err rm 3-4                      remove lines
err sub -old 'foo(' -new 'bar(' [-all]    replace text inside txt_err; must match once
err apply [-force] [-nocheck] re-run the original command with the fixed text; on success txt_err is cleared
err clear
```

`err apply` keeps txt_err when the command fails again, so keep fixing. A new failure replaces the slot.

## Shell and background processes

```
sh pytest -q                          default shell is cmd.exe
sh -cwd lib -timeout 60 -cap 4000 flutter analyze
sh -shell ps Get-ChildItem | Select-Object -First 5
```

Cody flags go before the command; the command's own `--flags`, quotes and `%VARS%`
pass through untouched. For PowerShell with pipes or quotes, use `sh -shell ps
-script -` and put the script on the following lines. Success with no output prints
`(no output)`. Destructive commands (`git reset --hard`, `rm -rf /`, ...) are
refused; ask the user to run those.

Dev servers and anything long-running go in the background:

```
proc start web -ready "ready in" -wait 30 -cwd app -- npm run dev
proc logs web -tail 40
proc list [-all]            -all also shows dev-port listeners not started via proc
proc stop web
```

`start` returns once the ready text appears, with the URL, and says when the server
moved off a taken port. Logs are kept outside the workspace.

## Check

```
check                  autodetects tsc / npm script / dart / cargo / go / ruff
check -what           show what it would run, without running it
check -cmd "pytest -q" [-timeout S]
```

Reply is `ok ...` when clean and `FAIL` plus the checker output otherwise. Run it
when asked, or when a change is large enough to be in doubt. A one-line edit needs
no check. Do not build a verification loop, and do not rewrite working code to get
around a missing tool; report the missing tool.

## Batch

One command per line after `batch`, in one workspace, at most 100 per call:

```
-ws myapp batch
tree src -depth 2
read a.json b.json
find TODO -files
```

`batch -stop` halts at the first error or failed `sh`/`check`/`proc`. `b`, `check`,
`sh` and `proc` work inside a batch; payload commands (`-new -`) and `sh -script`
do not.

Batch output is compact. Consecutive edits to the same file merge into one line:

```
E a.py: +160-183, 148, 23→23-28       + insert, N replace, A→B replace that changed size,
                                      xN delete, ~N old-text replace
S pytest -q                           sh line (was "== sh ...")
```

An edit that returns context lines (json, md, html, css, yaml, txt; or `-show N`) gets
its own `E file: span` header followed by the context, and does not join a group.
Pass `-show 0` to keep the group.

## Browser

`b <playwright-cli command>` drives Playwright's, Every reply lists new console warnings and errors.

```
b open http://localhost:3000 -errors      title + console errors only
b snapshot | b find TEXT | b click e5 | b fill e7 "text"
b eval "() => document.title"              read DOM, canvas or game state
b pixel 10,20 canvas                        canvas colours without a screenshot
b screenshot -filename shot.png            returned as an inline image
```

Refs (`e5`) reset on `open`, `goto` and `reload`: take a fresh `b snapshot` first.

## Graph

A code graph (files, symbols, classes, calls, imports) is built on demand and stored
in `<workspace>/graphify-out`. It is bundled with Cody; nothing to install.

Use it when:

- The codebase is large or unfamiliar (hundreds of files) and the question is
  architectural: "how does X work", call flow, dependencies. Run `graph query` BEFORE
  your first `find` or `read`, then `read` only the files and line ranges it names.
- You need relationships across many files: "what calls X", "what breaks if I change
  X", "how does A reach B" (`graph explain` / `graph path`).
- No graph yet and one of the above applies: `graph build` once, then query.

Do not use it when:

- The project is small (a few dozen files). `find` + `read` + `sym` are faster, exact
  and always current.
- You know the file or symbol. Just `read` or `sym` it.
- You are about to edit. The graph only says that things are related; read the real
  code before changing it.
- Only `graph explain` on one symbol is cheap; `graph query` on a broad name returns
  large, noisy replies (stdlib nodes, third-party code) that cost more than reading.

```
graph status                    is there a graph, how big, how old
graph build                     first build; reuses an existing graph without rebuilding
graph update                    incremental refresh after code changed (cached, fast)
graph query "how does auth work" [-dfs] [-budget N]   relevant nodes, edges and files
graph path "AuthController" "UserRepository"            shortest relationship chain
graph explain "AuthService"     one node and its neighbours
graph tree | graph open [tree]  html views for the user (open launches their browser)
```

`query`, `path` and `explain` never build: with no graph they reply `no graph ... run:
graph build`. Run `graph build` once per project (it can take a while on big repos),
`graph update` after you changed code, never both in a row. Replies are capped (`-cap N`
raises it).

Tips:

- Query with the concept or symbol names from the question, not whole sentences.
  Vague result: rephrase with a concrete class or function name, or use `-dfs` to
  follow one chain deeper. `-budget N` bounds the answer size.
- `path` and `explain` take node names as `graph query` printed them. Misspelled or
  ambiguous names are fuzzy matched; use the exact label if the wrong node comes back.
- Edges marked EXTRACTED come straight from the code; INFERRED are the graph's best
  guess, so confirm them with `read` before relying on them.
- A graph can be stale after edits. Run `graph update` after a batch of changes you
  want to query again, not after every edit.
- `graph open` and `graph tree` are for the user to look at. Never read the html,
  `graph.json` or `GRAPH_REPORT.md` into the conversation; ask `graph query` instead.

## Housekeeping

`stats [N|reset]` shows output size per command, useful for finding what is eating
tokens. Typical flow: `find -files` -> `read PATH` (outline) -> `read -fn NAME` ->
`edit` -> `check` (only if needed).