from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import app_paths

from code_engine.finder import find_symbols_in_file
from code_engine.languages import LANGUAGES
from code_engine.replace import build_function_replacement, fuzzy_find_text
from core import hunt as text_hunt, search as text_search, structure, uncomment
from core.diff import unified
from core import history
from core.syntax_check import check_python_syntax

from . import media_cmds as media, outline
from .common import CliError, Raw, current_session, current_workspace, path_of, root_of, split_flags, to_int, unescape_flag

MAX_READ_LINES = 400
EDIT_MAX_BYTES = 10 * 1024 * 1024
READ_MAX_CHARS = 30000
LINE_CAP = 1000
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


_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


def _encoding_of(target: Path) -> tuple[bytes, str]:
    try:
        with target.open("rb") as handle:
            head = handle.read(3)
    except OSError:
        return b"", "utf-8"
    for bom, codec in _BOMS:
        if head.startswith(bom):
            return bom, codec
    return b"", "utf-8"


def _read_text(target: Path) -> str:
    if not target.exists():
        raise CliError(f"not found: {target}{_similar(target)}")
    if not target.is_file():
        raise CliError(f"not a file: {target}")
    if target.suffix.lower() in text_search.BINARY_SUFFIXES:
        raise CliError(f"binary file: {target.name}")
    size = target.stat().st_size
    if size > EDIT_MAX_BYTES:
        raise CliError(f"{target.name} is {size // 1048576} MB, over the {EDIT_MAX_BYTES // 1048576} MB limit; use read with a line range, or sh")
    bom, codec = _encoding_of(target)
    try:
        with target.open("rb") as handle:
            return handle.read()[len(bom):].decode(codec)
    except UnicodeDecodeError:
        raise CliError(f"not {codec}: {target.name}")


def _similar(target: Path) -> str:
    import difflib
    try:
        names = [p.name for p in target.parent.iterdir() if p.is_file()]
    except OSError:
        return ""
    close = difflib.get_close_matches(target.name, names, n=1, cutoff=0.6)
    return f"; did you mean {close[0]}?" if close else ""


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


_SENT: dict[tuple[str, str, int, int], str] = {}
_SENT_MAX = 300


def _already_sent(target: Path, start: int, end: int, body: str) -> bool:
    """True when this exact range with this exact content was already returned
    in this server session. Content-hashed, so any edit makes it read fresh."""
    if os.environ.get("CODY_NOCACHE") == "1":
        return False
    digest = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()
    key = (current_session(), str(target), start, end)
    if _SENT.get(key) == digest:
        return True
    if len(_SENT) >= _SENT_MAX:
        _SENT.pop(next(iter(_SENT)))
    _SENT[key] = digest
    return False


_STATE: dict[tuple[str, str], str] = {}
_STATE_FILE = app_paths.logs_dir() / "read_state.json"
_STATE_MAX = 2000
_DISK: dict[str, str] | None = None


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _disk() -> dict[str, str]:
    global _DISK
    if _DISK is None:
        try:
            loaded = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            _DISK = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            _DISK = {}
    return _DISK


def _save_disk() -> None:
    temp = _STATE_FILE.with_suffix(".tmp")
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(_disk()), encoding="utf-8")
        os.replace(temp, _STATE_FILE)
    except OSError:
        temp.unlink(missing_ok=True)


def _remember(target: Path, digest: str) -> None:
    key = str(target)
    _STATE[(current_session(), key)] = digest
    disk = _disk()
    if disk.get(key) == digest:
        return
    disk.pop(key, None)
    disk[key] = digest
    while len(disk) > _STATE_MAX:
        disk.pop(next(iter(disk)))
    _save_disk()


def _forget(target: Path) -> None:
    key = str(target)
    _STATE.pop((current_session(), key), None)
    if _disk().pop(key, None) is not None:
        _save_disk()


def _nth(text: str, needle: str, n: int) -> int:
    if n < 1:
        raise CliError("-nth starts at 1")
    pos = -len(needle)
    for _ in range(n):
        pos = text.find(needle, pos + len(needle))
        if pos < 0:
            return -1
    return pos


def _guard(target: Path, text: str, force: bool) -> None:
    known = _STATE.get((current_session(), str(target)))
    if known is None:
        known = _disk().get(str(target))
    if known is not None and known != _digest(text) and not force:
        raise CliError(f"{target.name} changed since your last read or edit; read it again (or pass -force)")


def _split_lines(text: str) -> list[str]:
    """Lines split on \\n / \\r\\n only, so form feeds and U+2028 stay inside their line."""
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def _line_records(text: str) -> list[tuple[str, str]]:
    parts = text.split("\n")
    records: list[tuple[str, str]] = []
    last = len(parts) - 1
    for index, part in enumerate(parts):
        if index == last:
            if part == "":
                break
            if part.endswith("\r"):
                records.append((part[:-1], "\r"))
            else:
                records.append((part, ""))
        elif part.endswith("\r"):
            records.append((part[:-1], "\r\n"))
        else:
            records.append((part, "\n"))
    return records


_LINE_PREFIX = re.compile(r"^\s*\d+(?:\|\s?|\s)")


def _squash(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _expect_matches(current: str, raw) -> bool:
    wanted = _squash(current)
    candidates = {unescape_flag(raw), str(raw)}
    for candidate in list(candidates):
        rows = candidate.replace("\r\n", "\n").split("\n")
        if rows and all(_LINE_PREFIX.match(row) for row in rows if row.strip()):
            candidates.add("\n".join(_LINE_PREFIX.sub("", row, count=1) for row in rows))
    return any(_squash(candidate) == wanted for candidate in candidates)


def _clip(line: str) -> str:
    return line if len(line) <= LINE_CAP else f"{line[:LINE_CAP]}...[+{len(line) - LINE_CAP} chars]"


def _fit(rows: list[str]) -> int:
    used = 0
    for index, row in enumerate(rows):
        used += len(row) + 8
        if used > READ_MAX_CHARS and index > 0:
            return index
    return len(rows)


def _numbered(lines: list[str], first: int, last: int) -> str:
    return "\n".join(f"{n} {lines[n - 1]}" for n in range(first, last + 1))


def _commit(target: Path, proposed: str, existed: bool, nocheck: bool = False) -> None:
    if not nocheck:
        problem = structure.validate_syntax(target, proposed)
        if problem is not None and existed:
            try:
                if structure.validate_syntax(target, _read_text(target)) is not None:
                    problem = None
            except CliError:
                pass
        if problem is not None:
            if problem.get("message") == "Syntax error":
                raise CliError(
                    f"syntax error line {problem.get('line')} near '{problem.get('found')}' "
                    f"({problem.get('expected')}); nothing written, pass -nocheck to write anyway"
                )
            raise CliError(
                f"unbalanced brackets line {problem.get('line')}: "
                f"expected {problem.get('expected')} found {problem.get('found')}; "
                f"nothing written, pass -nocheck to write anyway"
            )
        syntax = check_python_syntax(target, proposed)
        if syntax is not None:
            raise CliError(f"python syntax line {syntax['line']}: {syntax['detail']}")
    if existed:
        history.snapshot(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".cody-tmp")
    try:
        bom, codec = _encoding_of(target) if existed and target.exists() else (b"", "utf-8")
        with temp.open("wb") as handle:
            handle.write(bom + proposed.encode(codec))
        if existed and target.exists():
            shutil.copymode(target, temp)
        os.replace(temp, target)
        _remember(target, _digest(proposed))
    finally:
        temp.unlink(missing_ok=True)


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
    header = f"# {raw} {total} lines - outline (read {raw}:A-B or -fn NAME for code)"
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
    out = [f"# {raw} {len(lines)} lines - summary (read {raw}:A-B or -fn NAME for code)"]
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


def _read_big(target: Path, raw: str, start: int | None, end: int | None, flags: dict) -> str:
    if "fn" in flags or flags.get("outline") or flags.get("summary"):
        raise CliError(f"{raw} is over {EDIT_MAX_BYTES // 1048576} MB; use read {raw}:A-B or -around N")
    if "around" in flags:
        centre = to_int(flags["around"], "around")
        span = to_int(flags.get("window", 15), "window")
        start, end = max(1, centre - span), centre + span
    start = start or 1
    cap = MAX_READ_LINES if flags.get("full") else READ_CAP
    end = start + cap - 1 if end is None else min(end, start + cap - 1)
    bom, codec = _encoding_of(target)
    stream = "utf-16" if codec.startswith("utf-16") else ("utf-8-sig" if bom else "utf-8")
    shown: list[str] = []
    more = False
    try:
        with target.open("r", encoding=stream, newline="") as handle:
            for number, line in enumerate(handle, 1):
                if number > end:
                    more = True
                    break
                if number >= start:
                    shown.append(_clip(line.rstrip("\r\n")))
    except UnicodeDecodeError:
        raise CliError(f"not {codec}: {target.name}")
    if not shown:
        raise CliError(f"{raw}: start {start} past end of file")
    fit = _fit(shown)
    if fit < len(shown):
        shown, more = shown[:fit], True
    last = start + len(shown) - 1
    body = "\n".join(shown) if flags.get("no-ln") else "\n".join(f"{start + i} {row}" for i, row in enumerate(shown))
    hint = f"\n# more: read {raw}:{last + 1}-" if more else ""
    return f"{raw}:{start}-{last}\n{body}{hint}"


def cmd_read(args: list[str]) -> str:
    positional, flags = split_flags(args, {"around", "window", "fn", "cell", "pages", "max-dim", "quality"}, {"full", "outline", "force", "no-ln", "summary", "outputs", "raw", "blanks", "comments"})
    if not positional:
        raise CliError("usage: read <path>[:A-B] [-fn NAME] [-around N -window N] [-outline] [-full] | x.ipynb [-cell A-B -outputs -raw] | x.pdf[:A-B] [-pages A-B] | image [-max-dim N -quality N]")
    if "fn" in flags and len(positional) > 1:
        raise CliError("-fn works on a single file")
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
        suffix = target.suffix.lower()
        if target.is_file() and suffix == ".pdf":
            outputs.append(media.read_pdf(target, raw, flags, start, end))
            continue
        if target.is_file() and suffix in media.IMAGE_SUFFIXES:
            outputs.append(media.read_image(target, raw, flags))
            continue
        if target.is_file() and target.stat().st_size > EDIT_MAX_BYTES:
            outputs.append(_read_big(target, raw, start, end, flags))
            continue
        text = _read_text(target)
        _remember(target, _digest(text))
        if target.suffix.lower() == ".ipynb" and not flags.get("raw"):
            outputs.append(media.read_notebook(text, raw, flags))
            continue
        lines = _split_lines(text)
        total = len(lines)
        if total == 0:
            outputs.append(f"{raw} (empty)")
            continue
        if flags.get("summary"):
            outputs.append(_summary(target, raw, lines))
            continue
        keep_blank = bool(flags.get("blanks") or flags.get("no-ln"))
        shown_lines = lines
        if not flags.get("comments") and not flags.get("no-ln"):
            blanked = uncomment.blank_comments(suffix, text)
            if blanked is not None:
                candidate = _split_lines(blanked)
                if len(candidate) == total:
                    shown_lines = [new.rstrip() if new != old else new for old, new in zip(lines, candidate)]
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
        pairs = [(n, _clip(shown_lines[n - 1])) for n in range(start, end + 1) if keep_blank or shown_lines[n - 1].strip()]
        fit = _fit([row for _, row in pairs])
        cut = fit < len(pairs)
        if cut:
            pairs = pairs[:fit]
            end = pairs[-1][0]
        header = f"{raw}:{start}-{end}"
        if not pairs:
            body = "(blank)"
        elif flags.get("no-ln"):
            body = "\n".join(row for _, row in pairs)
        else:
            body = "\n".join(f"{n} {row}" for n, row in pairs)
        sent_before = _already_sent(target, start, end, body)
        if sent_before and not flags.get("force"):
            outputs.append(f"{header} unchanged; -force to resend")
            continue
        has_more = end < total and any(line.strip() for line in shown_lines[end:])
        more = f"\n# more: read {raw}:{end + 1}-" if (has_more and "around" not in flags and "fn" not in flags) or cut else ""
        outputs.append(f"{header}\n{body}{more}")
    return "\n".join(outputs)


def cmd_find(args: list[str]) -> str:
    positional, flags = split_flags(
        args,
        {"in", "glob", "max", "ctx", "skip", "type", "sort", "workers", "timeout"},
        {"regex", "i", "defs", "deps", "files", "list", "multiline", "first", "next", "unit"},
    )
    if flags.get("next"):
        return _hunt_next(flags)
    if not positional:
        raise CliError("usage: find <query> [-files|-list] [-in DIR] [-glob G] [-type py] [-regex] [-i] [-multiline] [-defs] [-max N] [-ctx N] [-sort mtime] | find <query> -first [-workers N -timeout S], then find -next")
    query = " ".join(positional)
    scope = path_of(flags["in"]) if flags.get("in") else None
    target = scope if scope is not None and scope.is_file() else None
    root = root_of(flags.get("in"))
    files_only = bool(flags.get("files") or flags.get("list"))
    limit = to_int(flags.get("max", FILES_SCAN if files_only else 30), "max")
    skip = to_int(flags.get("skip", 0), "skip")
    ctx = 0 if files_only else to_int(flags.get("ctx", 0), "ctx")
    regex = bool(flags.get("regex"))
    pattern = query if regex else r"\b" + "".join(f"\\{c}" if c in r".^$*+?()[]{}|\\" else c for c in query) + r"\b"
    globs = [flags["glob"]] if flags.get("glob") else []
    if flags.get("type"):
        try:
            globs += text_search.type_globs(str(flags["type"]).split(","))
        except ValueError as error:
            raise CliError(str(error))
    if flags.get("first"):
        return _hunt_first(root, target, pattern, flags, globs)
    if files_only:
        return _find_files(root, pattern, flags, globs, target, limit, skip)
    found = text_search.search(
        root, pattern, regex=True, case_sensitive=not flags.get("i"),
        max_results=limit + skip, context_lines=ctx,
        exclude_deps=not flags.get("deps"),
        globs=globs or None,
        target=target,
        multiline=bool(flags.get("multiline")),
    )
    results = found["results"][skip:]
    if not results:
        return "0 matches" if not found.get("timed_out") else "ERR search timed out with no results; narrow with -in or -glob"
    defs: set[tuple[str, int]] = set()
    if not regex and not files_only:
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
    capped = len(found["results"]) >= limit + skip or bool(found.get("timed_out"))
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
    tail = f"\n# {len(grouped)} file(s), {len(results)} hit(s)" + (" (capped, narrow or raise -max; `find Q -files` lists files only)" if capped else "")
    return "\n".join(lines_out) + tail


def _find_files(root: Path, pattern: str, flags: dict, globs: list[str], target: Path | None, limit: int, skip: int) -> str:
    counts = not flags.get("list")
    found = text_search.search_files(
        root, pattern, regex=True, case_sensitive=not flags.get("i"), exclude_deps=not flags.get("deps"),
        globs=globs or None, target=target, multiline=bool(flags.get("multiline")), counts=counts, max_files=max(FILES_SCAN, limit + skip),
    )
    rows = found["files"]
    if not rows:
        return "0 matches" if not found.get("timed_out") else "ERR search timed out with no results; narrow with -in or -glob"
    order = str(flags.get("sort") or ("hits" if counts else "name"))

    def stamp(name: str) -> float:
        try:
            return (root / name).stat().st_mtime
        except OSError:
            return 0.0

    if order == "mtime":
        rows = sorted(rows, key=lambda row: (-stamp(row[0]), row[0]))
    elif order == "hits":
        rows = sorted(rows, key=lambda row: (-(row[1] or 0), row[0]))
    elif order == "name":
        rows = sorted(rows, key=lambda row: row[0])
    else:
        raise CliError("-sort is mtime, hits or name")
    page = rows[skip:skip + limit]
    names = [name.replace(os.sep, "/") for name, _ in page]
    body = "\n".join(f"{name} ({count})" for name, (_, count) in zip(names, page)) if counts else "\n".join(names)
    capped = bool(found.get("truncated") or found.get("timed_out") or len(rows) > skip + limit)
    tail = f"# {len(page)} file(s)" + (f", {sum(count for _, count in page)} hit(s)" if counts else "")
    tail += f", by {order}" if order == "mtime" else ""
    tail += " (capped, raise -max or narrow)" if capped else ""
    return f"{body}\n{tail}"


def _hunt_options(flags: dict) -> tuple[int, int]:
    workers = to_int(flags.get("workers", os.environ.get("CODY_FIND_WORKERS", 5)), "workers")
    timeout = to_int(flags.get("timeout", os.environ.get("CODY_FIND_TIMEOUT", 20)), "timeout")
    return max(1, min(workers, 16)), max(1, timeout)


def _hunt_report(result: dict) -> str:
    hit = result["hit"]
    stats = f"{result['clean']}/{result['total']} unit(s) clean, {result['left']} left"
    if result["timed_out"]:
        stats += f", {len(result['timed_out'])} timed out ({', '.join(result['timed_out'][:3])}; raise -timeout)"
    if hit is None:
        return f"0 more matches\n# hunt over: {stats}"
    name = hit["file"].replace(os.sep, "/")
    return (
        f"{name}:{hit['line']}: {hit['text'].strip()[:FIND_TEXT]}\n"
        f"# hunt: {stats}; `find -next` drops this file and goes on, `find -next -unit` skips its whole folder"
    )


def _hunt_first(root: Path, target: Path | None, pattern: str, flags: dict, globs: list[str]) -> str:
    workers, timeout = _hunt_options(flags)
    try:
        result = text_hunt.begin(
            current_session(), root, target, pattern, regex=True, case_sensitive=not flags.get("i"), globs=globs,
            exclude_deps=not flags.get("deps"), multiline=bool(flags.get("multiline")), workers=workers, timeout=timeout,
        )
    except ValueError as error:
        raise CliError(str(error))
    return _hunt_report(result)


def _hunt_next(flags: dict) -> str:
    try:
        return _hunt_report(text_hunt.resume(current_session(), bool(flags.get("unit"))))
    except ValueError as error:
        raise CliError(str(error))


def cmd_glob(args: list[str]) -> str:
    import subprocess
    positional, flags = split_flags(args, {"in", "max", "skip"}, {"deps"})
    if not positional:
        raise CliError("usage: glob <pattern> [-in DIR] [-max N] [-skip N] [-deps]")
    root = root_of(flags.get("in"))
    limit = to_int(flags.get("max", 100), "max")
    skip = to_int(flags.get("skip", 0), "skip")
    command = ["rg", "--files", "--follow", "--no-ignore", "--sortr=modified", "--glob", positional[0]]
    command += ["--hidden"] if flags.get("deps") else text_search.ripgrep_excludes()
    command += ["--", "."]
    try:
        process = subprocess.run(
            command, cwd=str(root), capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        raise CliError("ripgrep (rg) not found")
    except subprocess.TimeoutExpired:
        raise CliError("glob timed out; narrow with -in")
    files = [line.removeprefix("./").removeprefix(".\\").replace("\\", "/") for line in process.stdout.splitlines() if line]
    if not files:
        return "0 files"
    page = files[skip:skip + limit]
    more = len(files) > skip + limit
    tail = f"\n# {len(page)} of {len(files)} file(s), newest first" + (f" (more: -skip {skip + limit})" if more else "")
    return "\n".join(page) + tail


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
        raise CliError("usage: sym <file-or-dir> [-depth N]   (file: nested outline with end lines; dir: top-level only)")
    target = path_of(positional[0])
    if not target.exists():
        raise CliError(f"not found: {target}{_similar(target)}")
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
        {"lines", "old", "new", "fn", "expect", "show", "before", "after", "insert-at", "nth"},
        {"all", "dry", "stdin", "nocheck", "force", "raw"},
    )
    if not positional:
        raise CliError("usage: edit <path> -lines A-B -new TEXT | -old TEXT -new TEXT [-all] | -fn NAME -new TEXT | -dry")
    target = path_of(positional[0])
    text = _read_text(target)
    stamp = target.stat().st_mtime_ns
    _guard(target, text, bool(flags.get("force")))
    modes = [m for m in ("fn", "lines", "old", "before", "after", "insert-at") if m in flags]
    if len(modes) > 1:
        raise CliError("conflicting flags --" + ", --".join(modes) + "; give only one")
    un = str if flags.get("raw") else unescape_flag
    new = un(flags["new"]) if "new" in flags else None
    dry = bool(flags.get("dry"))

    if "fn" in flags:
        if new is None:
            raise CliError("-fn needs -new")
        if _encoding_of(target)[1] != "utf-8":
            raise CliError("-fn is not supported for utf-16 files; use -lines or -old")
        try:
            proposed_bytes, first, last = build_function_replacement(str(target), flags["fn"], _match_ending(text, new))
        except ValueError as error:
            raise CliError(str(error))
        proposed = proposed_bytes.decode("utf-8").lstrip("\ufeff")
        label = f"fn {flags['fn']} {first}-{last}"
        span = (first, first + len(new.splitlines()) - 1 if new.strip() else first)

    elif "lines" in flags:
        if new is None:
            raise CliError("-lines needs -new")
        a, _, b = flags["lines"].partition("-")
        start = to_int(a, "lines start")
        end = to_int(b or a, "lines end")
        lines = _split_lines(text)
        if start < 1 or end < start or end > len(lines):
            raise CliError(f"bad range {start}-{end}; file has {len(lines)} lines")
        if "expect" in flags and not _expect_matches("\n".join(lines[start - 1:end]), flags["expect"]):
            raise CliError(f"expect mismatch, stale lines. current:\n{_numbered(lines, start, end)}")
        records = _line_records(text)
        fallback = _ending(text)
        body = _split_lines(_match_ending(text, new))
        inner = records[start - 1][1] or fallback
        tail = records[end - 1][1]
        added = [(line, inner) for line in body]
        if added:
            added[-1] = (added[-1][0], tail)
        records[start - 1:end] = added
        proposed = "".join(content + ending for content, ending in records)
        label = f"{start}-{end} -> {start}-{start + len(body) - 1}" if body else f"{start}-{end} deleted"
        span = (start, max(start, start + len(body) - 1))

    elif "old" in flags:
        if new is None:
            raise CliError("-old needs -new")
        old = _match_ending(text, un(flags["old"]))
        count = text.count(old)
        body = _match_ending(text, new)
        if count == 0:
            fuzzy = fuzzy_find_text(text, old)
            if fuzzy is None:
                raise CliError("old text not found" + ("; new text already present, edit may be applied" if body.strip() and body in text else ""))
            proposed = text[:fuzzy[0]] + body + text[fuzzy[1]:]
            first_line = text.count("\n", 0, fuzzy[0]) + 1
            span = (first_line, first_line + max(0, len(body.splitlines()) - 1))
            label = f"1 replaced (fuzzy) at {span[0]}-{span[1]}"
        elif count > 1 and not flags.get("all") and "nth" not in flags:
            hits = []
            index = 0
            while len(hits) < 8:
                found = text.find(old, index)
                if found < 0:
                    break
                hits.append(str(text.count("\n", 0, found) + 1))
                index = found + len(old)
            raise CliError(f"old text matches {count}x at lines {','.join(hits)}; extend it, use -lines, -all, or -nth N")
        else:
            pos = _nth(text, old, to_int(flags["nth"], "nth")) if "nth" in flags else text.find(old)
            if pos < 0:
                raise CliError(f"-nth beyond the {count} match(es)")
            first_line = text.count("\n", 0, pos) + 1
            proposed = text.replace(old, body) if flags.get("all") else text[:pos] + body + text[pos + len(old):]
            span = (first_line, first_line + max(0, len(body.splitlines()) - 1))
            if flags.get("all") and count > 1:
                label = f"{count} replaced (first at {span[0]})"
            else:
                label = f"1 replaced at {span[0]}-{span[1]}"
    elif "before" in flags or "after" in flags or "insert-at" in flags:
        if new is None:
            raise CliError("-before/-after/-insert-at need -new")
        records = _line_records(text)
        fallback = _ending(text)
        body = _split_lines(_match_ending(text, new))
        if not body:
            raise CliError("nothing to insert")
        if "insert-at" in flags:
            position = to_int(flags["insert-at"], "insert-at")
            if position < 1 or position > len(records) + 1:
                raise CliError(f"bad -insert-at {position}; file has {len(records)} lines (use 1-{len(records) + 1})")
            at = position - 1
        else:
            key = "before" if "before" in flags else "after"
            anchor = _match_ending(text, un(flags[key]))
            count = text.count(anchor) if anchor else 0
            if count == 0:
                raise CliError(f"-{key} anchor not found")
            if count > 1 and "nth" not in flags:
                raise CliError(f"-{key} anchor matches {count}x; make it longer, use -nth N, or use -insert-at")
            pos = _nth(text, anchor, to_int(flags["nth"], "nth")) if "nth" in flags else text.find(anchor)
            if pos < 0:
                raise CliError(f"-{key} anchor has only {count} match(es)")
            first_line = text.count("\n", 0, pos) + 1
            at = first_line - 1 if key == "before" else first_line + anchor.count("\n")
        neighbor = records[at - 1][1] if at > 0 else (records[0][1] if records else "")
        glue = neighbor or fallback
        added = [(line, glue) for line in body]
        if at == len(records) and records and records[-1][1] == "":
            records[-1] = (records[-1][0], glue)
            added[-1] = (added[-1][0], "")
        records[at:at] = added
        proposed = "".join(content + ending for content, ending in records)
        label = f"inserted {at + 1}-{at + len(body)}"
        span = (at + 1, at + len(body))
    else:
        raise CliError("nothing to do: give -lines, -old, -fn, -before, -after or -insert-at")

    if dry:
        return unified(text, proposed, positional[0])
    if target.stat().st_mtime_ns != stamp:
        raise CliError("file changed while editing; read it again")
    _commit(target, proposed, existed=True, nocheck=bool(flags.get("nocheck")))
    reply = f"ok {label} {_short(positional[0])}"
    show = to_int(flags["show"], "show") if "show" in flags else (0 if target.suffix.lower() in SELF_CHECKED else DEFAULT_SHOW)
    if show > 0:
        reply += "\n" + _edit_context(proposed, span, show)
    return reply


def cmd_medit(args: list[str]) -> str:
    positional, flags = split_flags(args, {"content"}, {"dry", "nocheck", "force"})
    if not positional or "content" not in flags:
        raise CliError('usage: medit <path> -content - (or <<X), then a JSON list [{"old": "..", "new": "..", "all": false}, ..] on the next lines')
    if not isinstance(flags["content"], Raw):
        raise CliError("medit takes the JSON as a raw payload: -content - or -content <<X")
    try:
        items = json.loads(str(flags["content"]))
    except ValueError as error:
        raise CliError(f"bad JSON: {error}")
    if not isinstance(items, list) or not items:
        raise CliError("content must be a non-empty JSON list")
    target = path_of(positional[0])
    text = _read_text(target)
    stamp = target.stat().st_mtime_ns
    _guard(target, text, bool(flags.get("force")))
    current = text
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or not isinstance(item.get("old"), str) or not isinstance(item.get("new"), str) or not item["old"]:
            raise CliError(f"edit {number}: needs a non-empty string old and a string new; nothing written")
        old = _match_ending(text, item["old"])
        body = _match_ending(text, item["new"])
        count = current.count(old)
        if count == 0:
            fuzzy = fuzzy_find_text(current, old)
            if fuzzy is None:
                raise CliError(f"edit {number}: old text not found; nothing written")
            current = current[:fuzzy[0]] + body + current[fuzzy[1]:]
        elif count > 1 and not item.get("all"):
            raise CliError(f'edit {number}: old text matches {count}x; extend it or set "all": true; nothing written')
        else:
            current = current.replace(old, body) if item.get("all") else current.replace(old, body, 1)
    if flags.get("dry"):
        return unified(text, current, positional[0])
    if target.stat().st_mtime_ns != stamp:
        raise CliError("file changed while editing; read it again")
    _commit(target, current, existed=True, nocheck=bool(flags.get("nocheck")))
    return f"ok {len(items)} edits {_short(positional[0])}"


def _edit_context(proposed: str, span: tuple[int, int], pad: int) -> str:
    """The changed region +/- `pad` lines, numbered, so the edit can be verified
    without a second read call. Capped so -show can never become a full dump."""
    lines = _split_lines(proposed)
    if not lines:
        return ""
    first = max(1, span[0] - pad)
    last = min(len(lines), span[1] + pad)
    last = min(last, first + SHOW_CAP - 1)
    return _numbered(lines, first, last)


def cmd_new(args: list[str]) -> str:
    positional, flags = split_flags(args, {"content"}, {"force", "nocheck"})
    if not positional or "content" not in flags:
        raise CliError("usage: new <path> -content TEXT [-force]")
    target = path_of(positional[0])
    if target.exists() and not flags.get("force"):
        raise CliError(f"exists: {positional[0]} (use -force to overwrite)")
    body = unescape_flag(flags["content"])
    if isinstance(flags["content"], Raw) and body and not body.endswith("\n"):
        body += "\n"
    existed = target.exists()
    _commit(target, body, existed=existed, nocheck=bool(flags.get("nocheck")))
    return f"ok {'overwrote' if existed else 'new'} {len(body.splitlines())}L {_short(positional[0])}"


def cmd_append(args: list[str]) -> str:
    positional, flags = split_flags(args, {"content"}, {"nocheck"})
    if not positional or "content" not in flags:
        raise CliError("usage: append <path> -content TEXT")
    target = path_of(positional[0])
    text = _read_text(target)
    newline = _ending(text)
    body = _match_ending(text, unescape_flag(flags["content"]))
    if not body.endswith("\n"):
        body += newline
    head = text if (not text or text.endswith("\n")) else text + newline
    _commit(target, head + body, existed=True, nocheck=bool(flags.get("nocheck")))
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
    _forget(target)
    return f"ok deleted {_short(args[0])}"


def cmd_undo(args: list[str]) -> str:
    if not args:
        raise CliError("usage: undo <path>")
    target = path_of(args[0])
    restored = history.undo(target)
    if restored is None:
        raise CliError(f"no history for {args[0]}")
    _forget(target)
    return f"ok restored {_short(args[0])}"


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
