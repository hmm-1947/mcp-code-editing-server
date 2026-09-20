from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from code_engine.finder import find_symbols_in_file, list_classes, list_functions
from code_engine.languages import LANGUAGES
from code_engine.replace import build_function_replacement, fuzzy_find_text
from core import history, search as text_search, structure, terminal
from core.diff import unified
from core.syntax_check import check_python_syntax

from . import outline
from .common import CliError, Raw, current_workspace, path_of, root_of, split_flags, to_int, unescape_flag

MAX_READ_LINES = 400
READ_CAP = 200
OUTLINE_OVER = 120
FIND_TEXT = 110
FILES_SCAN = 2000
SH_CAP = 4000
SHOW_CAP = 30
DEFAULT_SHOW = 2
# Types whose edits are already validated on write (bracket balance / py compile), so a
# terse reply is enough. .json is bracket-only (no comma checks), so it gets context too.
SELF_CHECKED = (structure.STRUCTURAL_EXTENSIONS - {".json"}) | {".py"}


def _read_text(target: Path) -> str:
    if not target.exists():
        raise CliError(f"not found: {target}")
    if not target.is_file():
        raise CliError(f"not a file: {target}")
    if target.suffix.lower() in text_search.BINARY_SUFFIXES:
        raise CliError(f"binary file: {target.name}")
    try:
        with target.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError:
        raise CliError(f"not utf-8: {target.name}")


def _ending(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= (text.count("\n") - crlf) else "\n"


def _match_ending(existing: str, new_text: str) -> str:
    normalized = new_text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n") if _ending(existing) == "\r\n" else normalized


def _short(raw: str) -> str:
    """Path as the model typed it, but never a long absolute path: relative to
    the workspace when inside it, else just the last two segments."""
    target = path_of(raw)
    try:
        base = root_of(None)
        return target.relative_to(base).as_posix()
    except (ValueError, CliError):
        parts = target.parts
        return "/".join(parts[-2:]) if len(parts) > 1 else str(target)


_SENT: dict[tuple[str, int, int], str] = {}
_SENT_MAX = 300


def _already_sent(target: Path, start: int, end: int, body: str) -> bool:
    """True when this exact range with this exact content was already returned
    in this server session. Content-hashed, so any edit makes it read fresh."""
    if os.environ.get("CODY_NOCACHE") == "1":
        return False
    digest = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()
    key = (str(target), start, end)
    if _SENT.get(key) == digest:
        return True
    if len(_SENT) >= _SENT_MAX:
        _SENT.pop(next(iter(_SENT)))
    _SENT[key] = digest
    return False


def _numbered(lines: list[str], first: int, last: int) -> str:
    return "\n".join(f"{n}|{lines[n - 1]}" for n in range(first, last + 1))


def _commit(target: Path, proposed: str, existed: bool) -> None:
    problem = structure.validate_structure(target, proposed)
    if problem is not None:
        raise CliError(
            f"unbalanced brackets line {problem.get('line')}: "
            f"expected {problem.get('expected')} found {problem.get('found')}"
        )
    syntax = check_python_syntax(target, proposed)
    if syntax is not None:
        raise CliError(f"python syntax line {syntax['line']}: {syntax['detail']}")
    if existed:
        history.snapshot(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(proposed)


def _rows(target: Path):
    """Rich outline rows, or None when the language/file can't be parsed."""
    try:
        return outline.outline_rows(target)
    except Exception:
        return None


def _outline(target: Path, raw: str, total: int) -> str | None:
    rows = _rows(target)
    if rows is None:
        return None
    if not rows:
        return None
    header = f"# {raw} {total} lines - outline (read {raw}:A-B or --fn NAME for code)"
    return outline.format_rows(rows, header)


def _function_span(target: Path, name: str) -> tuple[int, int, str]:
    rows = _rows(target)
    if rows is None:
        raise CliError(f"cannot parse {target.name} (unsupported language or syntax error)")
    exact = [r for r in rows if r[4] == name and r[3] in ("fn", "class", "interface", "type", "enum", "mixin", "extension", "const")]
    if not exact:
        near = sorted({r[4] for r in rows if name.lower()[:3] in r[4].lower()} or {r[4] for r in rows})[:6]
        hint = f"; similar: {', '.join(near)}" if near else ""
        raise CliError(f"symbol '{name}' not found in {target.name}{hint}")
    if len(exact) > 1:
        spots = ", ".join(f"{r[0]}-{r[1]}" for r in exact[:6])
        raise CliError(f"'{name}' is defined {len(exact)}x at {spots}; read by range")
    start, end, _depth, kind, _name = exact[0]
    return start, end, kind


def _header_comment(lines: list[str], limit: int = 12) -> list[str]:
    """The leading comment/docstring block of a file (skipping blanks and a shebang),
    markers stripped, at most `limit` lines. [] when the file doesn't start with one."""
    lines = [lines[0].lstrip("\ufeff"), *lines[1:]] if lines else lines      # tolerate a UTF-8 BOM
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("#!")):
        i += 1
    if i >= len(lines):
        return []
    first = lines[i].strip()
    block: list[str] = []
    for quote in ('"""', "'''"):
        if first.startswith(quote):
            rest = first[3:]
            if quote in rest:
                block = [rest.split(quote)[0]]
            else:
                block = [rest]
                for line in lines[i + 1:]:
                    if quote in line:
                        block.append(line.split(quote)[0])
                        break
                    block.append(line)
            break
    else:
        if first.startswith("/*"):
            for line in lines[i:]:
                block.append(line)
                if "*/" in line:
                    break
            block = [re.sub(r"^\s*/?\*+/?|\*/\s*$", "", ln) for ln in block]
        else:
            marker = next((m for m in ("///", "//", "#", "--") if first.startswith(m)), None)
            if marker:
                for line in lines[i:]:
                    if not line.strip().startswith(marker):
                        break
                    block.append(line.strip()[len(marker):])
    cleaned = [ln.strip()[:160] for ln in block if ln.strip()]
    return cleaned[:limit]


def _summary(target: Path, raw: str, lines: list[str]) -> str:
    out = [f"# {raw} {len(lines)} lines - summary (read {raw}:A-B or --fn NAME for code)"]
    header = _header_comment(lines)
    if header:
        out.append("header:")
        out.extend(f"  | {ln}" for ln in header)
    rows = _rows(target)
    if rows:
        top = []
        for start, end, depth, kind, name in rows:
            if depth:
                continue
            exported = "export " if lines[start - 1].lstrip().startswith("export") else ""
            top.append(f"{start if start == end else f'{start}-{end}'} {exported}{kind} {name}")
        if top:
            out.append("top-level:")
            out.extend(f"  {row}" for row in top)
    if len(out) == 1:
        out.append("(no header comment and no parseable symbols)")
    return "\n".join(out)


def cmd_read(args: list[str]) -> str:
    positional, flags = split_flags(args, {"around", "window", "fn"}, {"full", "outline", "force", "no-ln", "summary"})
    if not positional:
        raise CliError("usage: read <path>[:A-B] [--fn NAME] [--around N --window N] [--outline] [--full]")
    if "fn" in flags and len(positional) > 1:
        raise CliError("--fn works on a single file")
    outputs: list[str] = []
    for spec in positional:
        raw, start, end = spec, None, None
        if ":" in spec[2:]:
            head, _, tail = spec.rpartition(":")
            if tail.replace("-", "").isdigit():
                raw = head
                if "-" in tail:
                    a, _, b = tail.partition("-")
                    start = int(a) if a else 1
                    end = int(b) if b else None
                else:
                    start = end = int(tail)
        target = path_of(raw)
        text = _read_text(target)
        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            outputs.append(f"# {raw} (empty)")
            continue
        if flags.get("summary"):
            outputs.append(_summary(target, raw, lines))
            continue
        explicit = start is not None or "around" in flags or "fn" in flags
        if "fn" in flags:
            start, end, _kind = _function_span(target, flags["fn"])
        if "around" in flags:
            centre = to_int(flags["around"], "around")
            span = to_int(flags.get("window", 15), "window")
            start, end = max(1, centre - span), centre + span
        if flags.get("outline") or (not explicit and total > OUTLINE_OVER and not flags.get("full")):
            outline = _outline(target, raw, total)
            if outline is not None:
                outputs.append(outline)
                continue
        if start is None:
            start = 1
            end = min(total, READ_CAP if not flags.get("full") else MAX_READ_LINES)
        end = min(total, end if end is not None else start + READ_CAP - 1)
        if not flags.get("full") and end - start + 1 > READ_CAP and "fn" not in flags:
            end = start + READ_CAP - 1
        if start > total:
            raise CliError(f"{raw}: start {start} past end ({total} lines)")
        header = f"# {raw} {start}-{end}/{total}"
        body = "\n".join(lines[start - 1:end]) if flags.get("no-ln") else _numbered(lines, start, end)
        sent_before = _already_sent(target, start, end, body)
        if sent_before and not flags.get("force"):
            outputs.append(f"{header} # unchanged since last read; already in your context (--force to resend)")
            continue
        has_more = end < total and any(line.strip() for line in lines[end:])     # a blank tail is not worth a hint
        more = f"\n# more: read {raw}:{end + 1}-" if has_more and "around" not in flags and "fn" not in flags else ""
        outputs.append(f"{header}\n{body}{more}")
    return "\n".join(outputs)


def cmd_find(args: list[str]) -> str:
    positional, flags = split_flags(
        args, {"in", "glob", "max", "ctx"}, {"regex", "i", "defs", "deps", "files"}
    )
    if not positional:
        raise CliError("usage: find <query> [--files] [--in DIR] [--glob '*.py'] [--regex] [--i] [--defs] [--max N] [--ctx N]")
    query = " ".join(positional)
    root = root_of(flags.get("in"))
    files_only = bool(flags.get("files"))
    limit = to_int(flags.get("max", FILES_SCAN if files_only else 30), "max")
    ctx = 0 if files_only else to_int(flags.get("ctx", 0), "ctx")
    regex = bool(flags.get("regex"))
    pattern = query if regex else r"\b" + "".join(f"\\{c}" if c in r".^$*+?()[]{}|\\" else c for c in query) + r"\b"
    found = text_search.search(
        root, pattern, regex=True, case_sensitive=not flags.get("i"),
        max_results=limit, context_lines=ctx,
        exclude_deps=not flags.get("deps"),
        globs=[flags["glob"]] if flags.get("glob") else None,
    )
    results = found["results"]
    if not results:
        return "0 matches"
    defs: set[tuple[str, int]] = set()
    if not regex:
        for name in list({r["file"] for r in results})[:40]:
            absolute = root / name
            if absolute.suffix.lower() in LANGUAGES and absolute.is_file():
                try:
                    for symbol in find_symbols_in_file(str(absolute), query):
                        if symbol["name"] == query:
                            defs.add((name, symbol["line"]))
                except Exception:
                    continue
    lines_out: list[str] = []
    if flags.get("defs"):
        results = [r for r in results if (r["file"], r["line"]) in defs]
        if not results:
            return "0 definitions"
    grouped: dict[str, list[dict]] = {}
    for hit in results:
        grouped.setdefault(hit["file"].replace(os.sep, "/"), []).append(hit)
    capped = len(found["results"]) >= limit
    if flags.get("files"):
        rows = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        body = "\n".join(f"{name} ({len(hits)})" for name, hits in rows)
        return f"{body}\n# {len(rows)} file(s), {len(results)} hit(s)" + (" (capped, raise --max)" if capped else "")
    for name, hits in grouped.items():
        if ctx > 0:
            lines_out.append(f"{name}")
            for hit in hits:
                mark = " [def]" if (hit["file"], hit["line"]) in defs else ""
                lines_out.append(f"  {hit['line']}{mark}\n{hit.get('context') or hit['text'].strip()}")
        elif len(hits) == 1:
            hit = hits[0]
            mark = " [def]" if (hit["file"], hit["line"]) in defs else ""
            lines_out.append(f"{name}:{hit['line']}{mark}: {hit['text'].strip()[:FIND_TEXT]}")
        else:
            lines_out.append(name)
            for hit in hits:
                mark = " [def]" if (hit["file"], hit["line"]) in defs else ""
                lines_out.append(f"  {hit['line']}{mark}: {hit['text'].strip()[:FIND_TEXT]}")
    if len(results) == 1 and not capped:          # one hit speaks for itself
        return "\n".join(lines_out)
    tail = f"\n# {len(grouped)} file(s), {len(results)} hit(s)" + (" (capped, narrow or raise --max; `find Q --files` lists files only)" if capped else "")
    return "\n".join(lines_out) + tail


def cmd_tree(args: list[str]) -> str:
    positional, flags = split_flags(args, {"depth", "max"}, {"deps"})
    root = root_of(positional[0] if positional else None)
    depth = to_int(flags.get("depth", 2), "depth")
    cap = to_int(flags.get("max", 150), "max")
    out = [root.name + "/"]
    count = 0

    def walk(folder: Path, level: int) -> None:
        nonlocal count
        if level >= depth or count >= cap:
            return
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for item in entries:
            if not flags.get("deps") and item.name in text_search.DEPENDENCY_DIRS:
                continue
            out.append(f"{'  ' * (level + 1)}{item.name}{'/' if item.is_dir() else ''}")
            count += 1
            if count >= cap:
                out.append("  ...capped")
                return
            if item.is_dir():
                walk(item, level + 1)

    walk(root, 0)
    return "\n".join(out)


def cmd_sym(args: list[str]) -> str:
    positional, flags = split_flags(args, {"max", "depth"}, set())
    if not positional:
        raise CliError("usage: sym <file-or-dir> [--depth N]   (file: nested outline with end lines; dir: top-level only)")
    target = path_of(positional[0])
    if not target.exists():
        raise CliError(f"not found: {target}")
    is_dir = target.is_dir()
    files = (
        [p for p in text_search.iter_files(target) if p.suffix.lower() in LANGUAGES]
        if is_dir
        else [target]
    )
    files = files[: to_int(flags.get("max", 60), "max")]
    root = target if is_dir else target.parent
    depth_cap = to_int(flags["depth"], "depth") if "depth" in flags else (1 if is_dir else 3)
    out: list[str] = []
    for file_path in files:
        rows = _rows(file_path)
        if not rows:
            continue
        rows = [r for r in rows if r[2] < depth_cap]
        rel = file_path.relative_to(root).as_posix() if is_dir else positional[0]
        out.append(outline.format_rows(rows, f"# {rel}"))
    return "\n".join(out) if out else "0 symbols"


def cmd_edit(args: list[str]) -> str:
    positional, flags = split_flags(
        args,
        {"lines", "old", "new", "fn", "expect", "show"},
        {"all", "dry", "stdin"},
    )
    if not positional:
        raise CliError("usage: edit <path> --lines A-B --new TEXT | --old TEXT --new TEXT [--all] | --fn NAME --new TEXT | --dry")
    target = path_of(positional[0])
    text = _read_text(target)
    new = unescape_flag(flags["new"]) if "new" in flags else None
    dry = bool(flags.get("dry"))

    if "fn" in flags:
        if new is None:
            raise CliError("--fn needs --new")
        try:
            proposed_bytes, first, last = build_function_replacement(str(target), flags["fn"], new)
        except ValueError as error:
            raise CliError(str(error))
        proposed = proposed_bytes.decode("utf-8")
        label = f"fn {flags['fn']} {first}-{last}"
        span = (first, first + len(new.splitlines()) - 1 if new.strip() else first)

    elif "lines" in flags:
        if new is None:
            raise CliError("--lines needs --new")
        a, _, b = flags["lines"].partition("-")
        start = to_int(a, "lines start")
        end = to_int(b or a, "lines end")
        lines = text.splitlines()
        if start < 1 or end < start or end > len(lines):
            raise CliError(f"bad range {start}-{end}; file has {len(lines)} lines")
        if "expect" in flags:
            current = "\n".join(lines[start - 1:end])
            if current.strip() != unescape_flag(flags["expect"]).strip():
                raise CliError(f"expect mismatch, stale lines. current:\n{_numbered(lines, start, end)}")
        newline = _ending(text)
        body = _match_ending(text, new).splitlines()
        proposed = newline.join(lines[:start - 1] + body + lines[end:]) + (newline if text.endswith("\n") else "")
        label = f"{start}-{end} -> {start}-{start + len(body) - 1}" if body else f"{start}-{end} deleted"
        span = (start, max(start, start + len(body) - 1))

    elif "old" in flags:
        if new is None:
            raise CliError("--old needs --new")
        old = _match_ending(text, unescape_flag(flags["old"]))
        count = text.count(old)
        body = _match_ending(text, new)
        if count == 0:
            fuzzy = fuzzy_find_text(text, old)
            if fuzzy is None:
                raise CliError("old text not found")
            proposed = text[:fuzzy[0]] + body + text[fuzzy[1]:]
            first_line = text.count("\n", 0, fuzzy[0]) + 1
            span = (first_line, first_line + max(0, len(body.splitlines()) - 1))
            label = f"1 replaced (fuzzy) at {span[0]}-{span[1]}"
        elif count > 1 and not flags.get("all"):
            hits = []
            index = 0
            while len(hits) < 8:
                found = text.find(old, index)
                if found < 0:
                    break
                hits.append(str(text.count("\n", 0, found) + 1))
                index = found + len(old)
            raise CliError(f"old text matches {count}x at lines {','.join(hits)}; extend it, use --lines, or --all")
        else:
            first_line = text.count("\n", 0, text.find(old)) + 1
            proposed = text.replace(old, body) if flags.get("all") else text.replace(old, body, 1)
            span = (first_line, first_line + max(0, len(body.splitlines()) - 1))
            if flags.get("all") and count > 1:
                label = f"{count} replaced (first at {span[0]})"
            else:
                label = f"1 replaced at {span[0]}-{span[1]}"
    else:
        raise CliError("nothing to do: give --lines, --old, or --fn")

    if dry:
        return unified(text, proposed, positional[0])
    _commit(target, proposed, existed=True)
    reply = f"ok {label} {_short(positional[0])}"
    show = to_int(flags["show"], "show") if "show" in flags else (0 if target.suffix.lower() in SELF_CHECKED else DEFAULT_SHOW)
    if show > 0:
        reply += "\n" + _edit_context(proposed, span, show)
    return reply


def _edit_context(proposed: str, span: tuple[int, int], pad: int) -> str:
    """The changed region +/- `pad` lines, numbered, so the edit can be verified
    without a second read call. Capped so --show can never become a full dump."""
    lines = proposed.splitlines()
    if not lines:
        return ""
    first = max(1, span[0] - pad)
    last = min(len(lines), span[1] + pad)
    last = min(last, first + SHOW_CAP - 1)
    return _numbered(lines, first, last)


def cmd_new(args: list[str]) -> str:
    positional, flags = split_flags(args, {"content"}, {"force"})
    if not positional or "content" not in flags:
        raise CliError("usage: new <path> --content TEXT [--force]")
    target = path_of(positional[0])
    if target.exists() and not flags.get("force"):
        raise CliError(f"exists: {positional[0]} (use --force to overwrite)")
    body = unescape_flag(flags["content"])
    if isinstance(flags["content"], Raw) and body and not body.endswith("\n"):
        body += "\n"
    existed = target.exists()
    _commit(target, body, existed=existed)
    return f"ok {'overwrote' if existed else 'new'} {len(body.splitlines())}L {_short(positional[0])}"


def cmd_append(args: list[str]) -> str:
    positional, flags = split_flags(args, {"content"}, set())
    if not positional or "content" not in flags:
        raise CliError("usage: append <path> --content TEXT")
    target = path_of(positional[0])
    text = _read_text(target)
    newline = _ending(text)
    body = _match_ending(text, unescape_flag(flags["content"]))
    if not body.endswith("\n"):
        body += newline
    head = text if (not text or text.endswith("\n")) else text + newline
    _commit(target, head + body, existed=True)
    first = len(head.splitlines()) + 1
    return f"ok +{first}-{first + len(body.splitlines()) - 1} {_short(positional[0])}"


def cmd_rm(args: list[str]) -> str:
    if not args:
        raise CliError("usage: rm <path>")
    target = path_of(args[0])
    if not target.exists():
        raise CliError(f"not found: {args[0]}")
    if not target.is_file():
        raise CliError("rm only deletes single files")
    history.snapshot(target)
    target.unlink()
    return f"ok deleted {_short(args[0])}"


def cmd_undo(args: list[str]) -> str:
    if not args:
        raise CliError("usage: undo <path>")
    target = path_of(args[0])
    restored = history.undo(target)
    if restored is None:
        raise CliError(f"no history for {args[0]}")
    return f"ok restored {_short(args[0])}"


SH_FLAGS = ("cwd", "timeout", "cap")


def _split_sh_args(args: list[str]) -> tuple[list[str], dict]:
    """Cody's own flags are only read BEFORE the command; everything from the
    first non-flag token on belongs to the shell (so `sh git reset --hard` and
    `sh pytest --tb=short` pass through untouched). A trailing `--cap N` is
    still honoured, since models habitually put it last."""
    flags: dict = {}
    index = 0
    while index + 1 < len(args) and args[index].startswith("--") and args[index][2:] in SH_FLAGS:
        flags[args[index][2:]] = args[index + 1]
        index += 2
    rest = args[index:]
    if len(rest) >= 3 and rest[-2] in ("--cap", "--timeout", "--cwd") and rest[-2][2:] not in flags:
        flags[rest[-2][2:]] = rest[-1]
        rest = rest[:-2]
    return rest, flags


def cmd_sh(args: list[str]) -> str:
    positional, flags = _split_sh_args(args)
    if not positional:
        raise CliError("usage: sh [--cwd DIR] [--timeout S] [--cap CHARS] <command...>")
    command = " ".join(positional)
    cwd_raw = flags.get("cwd") or current_workspace()
    cwd = path_of(cwd_raw) if flags.get("cwd") else (
        root_of(None) if not cwd_raw else path_of(".")
    )
    result = terminal.execute(command, cwd, timeout=max(1, to_int(flags.get("timeout", 300), "timeout")))
    if result.refused:
        return f"ERR refused: {result.refused}"
    limit = max(200, to_int(flags.get("cap", SH_CAP), "cap"))
    head = "exit 0" if result.ok else f"exit {result.exit_code}" + (" TIMEOUT" if result.timed_out else "") + f" {result.duration_ms}ms"
    parts = [head]
    out, out_clipped = terminal.clip(result.stdout.strip(), limit)
    err, err_clipped = terminal.clip(result.stderr.strip(), limit if not result.ok else limit // 2)
    if out:
        parts.append(out)
    if err:
        parts.append("[stderr]\n" + err)
    if out_clipped or err_clipped:
        parts.append("# clipped; filter with findstr/Select-String, or --cap N")
    return "\n".join(parts)


def cmd_ws(args: list[str]) -> str:
    from config import add_workspace, list_workspaces, remove_workspace
    action = args[0] if args else "list"
    if action == "list":
        return "\n".join(f"{k}={v}" for k, v in list_workspaces().items()) or "none"
    if action == "add" and len(args) >= 3:
        return f"ok {args[1]} -> {add_workspace(args[1], args[2])}"
    if action == "rm" and len(args) >= 2:
        remove_workspace(args[1])
        return f"ok removed {args[1]}"
    raise CliError("usage: ws [list] | ws add NAME PATH | ws rm NAME")
