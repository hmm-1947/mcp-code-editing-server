"""graph: code graph of the workspace, powered by the bundled graphify.

    graph build [--force]            first graph, or report the existing one
    graph update [--force]           incremental refresh (unchanged files are cached)
    graph query "Q" [--dfs] [--budget N]
    graph path A B [--undirected]
    graph explain X [--all]
    graph tree                       collapsible file/symbol tree html
    graph open [tree] [--no-launch]  interactive html (works offline)
    graph status [--json]

Output lives in <workspace>/graphify-out. graphify runs in a child process so the
server stays light and its exit codes and cwd never leak into it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import app_paths

from .common import CliError, root_of, split_flags, to_int

OUT_DIR = "graphify-out"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
MAX_STATS_BYTES = 40 * 1024 * 1024
DEFAULT_CAP = 8000
BUILD_TIMEOUT = 900
QUERY_TIMEOUT = 120

ASSET_SCRIPTS = (
    ("graph.html", "graph.offline.html", re.compile(r"<script src=\"https://unpkg\.com/vis-network[^>]*>\s*</script>"), "vis-network.min.js"),
    ("GRAPH_TREE.html", "GRAPH_TREE.offline.html", re.compile(r"<script src=\"https://d3js\.org/d3\.v7\.min\.js\">\s*</script>"), "d3.v7.min.js"),
)


def _worker_argv(args: list[str]) -> list[str]:
    if app_paths.FROZEN:
        return [sys.executable, "--graphify-worker", *args]
    return [sys.executable, str(Path(__file__).resolve().parent / "graph_worker.py"), *args]


def _run(root: Path, args: list[str], timeout: int) -> tuple[int, str]:
    env = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONUTF8": "1", "NO_COLOR": "1"}
    try:
        done = subprocess.run(
            _worker_argv(args), cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise CliError(f"graph {args[0]} timed out after {timeout}s")
    except OSError as error:
        raise CliError(f"cannot start graph engine: {error}")
    text = (done.stdout + b"\n" + done.stderr).decode("utf-8", errors="replace")
    cleaned = ANSI.sub("", text).replace("\r", "")
    return done.returncode, "\n".join(line.rstrip() for line in cleaned.split("\n") if line.strip())


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = text[: cap * 2 // 3]
    tail = text[-(cap // 3):]
    return f"{head}\n... [{len(text) - cap} chars clipped, narrow the question or raise --cap] ...\n{tail}"


def _out(root: Path) -> Path:
    return root / OUT_DIR


def _graph_file(root: Path) -> Path:
    return _out(root) / "graph.json"


def stats(root: Path) -> dict:
    graph = _graph_file(root)
    info = {"root": str(root), "out": str(_out(root)), "exists": graph.is_file()}
    if not info["exists"]:
        return info
    st = graph.stat()
    info.update(size=st.st_size, built=time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
    info["html"] = (_out(root) / "graph.html").is_file()
    info["tree"] = (_out(root) / "GRAPH_TREE.html").is_file()
    if st.st_size <= MAX_STATS_BYTES:
        try:
            data = json.loads(graph.read_text(encoding="utf-8"))
            nodes = data.get("nodes") or []
            info["nodes"] = len(nodes)
            info["edges"] = len(data.get("links") or data.get("edges") or [])
            info["communities"] = len({n.get("community") for n in nodes if n.get("community") is not None})
        except (OSError, ValueError):
            info["corrupt"] = True
    return info


def _status_line(info: dict) -> str:
    if not info["exists"]:
        return "no graph yet. run: graph build"
    if info.get("corrupt"):
        return f"graph.json unreadable (built {info['built']}). run: graph build --force"
    counts = f"{info['nodes']} nodes {info['edges']} edges {info['communities']} communities" if "nodes" in info else f"{info['size'] // 1024} KB"
    pages = "html" if info["html"] else "no html (too large: use graph tree)"
    return f"graph ok | built {info['built']} | {counts} | {pages} | {info['out']}"


def _need_graph(root: Path) -> None:
    if not _graph_file(root).is_file():
        raise CliError(f"no graph for {root}. run: graph build")


def _failure(action: str, code: int, text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()][-12:]
    return f"ERR graph {action} failed (exit {code})\n" + "\n".join(lines)


def _refresh(root: Path, force: bool, label: str) -> str:
    args = ["update", str(root)] + (["--force"] if force else [])
    before = _graph_file(root).stat().st_mtime if _graph_file(root).is_file() else 0
    code, text = _run(root, args, BUILD_TIMEOUT)
    if code != 0 or not _graph_file(root).is_file():
        return _failure(label, code, text)
    if not before:
        verdict = "built"
    else:
        verdict = "unchanged" if "No code-graph topology changes" in text else "updated"
    return f"ok {verdict} | {_status_line(stats(root))}"


def _build(root: Path, force: bool) -> str:
    if _graph_file(root).is_file() and not force:
        return f"ok reused existing graph (no rebuild) | {_status_line(stats(root))}\nuse `graph update` after code changes"
    return _refresh(root, force, "build")


def _update(root: Path, force: bool) -> str:
    return _refresh(root, force, "update")


def _passthrough(root: Path, args: list[str], cap: int, timeout: int = QUERY_TIMEOUT) -> str:
    _need_graph(root)
    code, text = _run(root, args, timeout)
    if code != 0 and not text:
        return f"ERR graph {args[0]} failed (exit {code})"
    if code != 0:
        first = text.splitlines()[0] if text.splitlines() else text
        if first.startswith("error:"):
            return "ERR " + first[6:].strip()
    return _clip(text, cap)


def _query(root: Path, positional: list[str], flags: dict, cap: int) -> str:
    if not positional:
        raise CliError('usage: graph query "question" [--dfs] [--budget N]')
    args = ["query", " ".join(positional)]
    if flags.get("dfs"):
        args.append("--dfs")
    if flags.get("budget"):
        args += ["--budget", str(to_int(flags["budget"], "--budget"))]
    return _passthrough(root, args, cap)


def _path(root: Path, positional: list[str], flags: dict, cap: int) -> str:
    if len(positional) != 2:
        raise CliError('usage: graph path "A" "B" [--undirected]')
    args = ["path", positional[0], positional[1]]
    if flags.get("undirected"):
        args.append("--undirected")
    return _passthrough(root, args, cap)


def _explain(root: Path, positional: list[str], flags: dict, cap: int) -> str:
    if not positional:
        raise CliError('usage: graph explain "name" [--all]')
    args = ["explain", " ".join(positional)]
    if flags.get("all"):
        args.append("--all")
    return _passthrough(root, args, cap)


def _tree(root: Path) -> str:
    _need_graph(root)
    code, text = _run(root, ["tree", "--label", root.name], BUILD_TIMEOUT)
    target = _out(root) / "GRAPH_TREE.html"
    if code != 0 or not target.is_file():
        return _failure("tree", code, text)
    return f"ok {target} ({target.stat().st_size // 1024} KB). view: graph open tree"


def _offline_copy(root: Path, kind: str) -> Path:
    for source_name, offline_name, pattern, asset in ASSET_SCRIPTS:
        if (kind == "tree") != (source_name == "GRAPH_TREE.html"):
            continue
        source = _out(root) / source_name
        if not source.is_file():
            raise CliError(f"{source_name} not found. run: graph {'tree' if kind == 'tree' else 'build'}")
        target = _out(root) / offline_name
        if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
            return target
        library = app_paths.resource_dir() / "third_party" / "graphify" / "assets" / asset
        if not library.is_file():
            raise CliError(f"bundled graph viewer script missing: {library}")
        html = source.read_text(encoding="utf-8")
        inline = "<script>" + library.read_text(encoding="utf-8") + "</script>"
        patched, count = pattern.subn(lambda _match: inline, html, count=1)
        target.write_text(patched if count else html, encoding="utf-8")
        return target
    raise CliError("unknown view")


def _launch(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))
    elif not webbrowser.open(path.as_uri()):
        raise CliError(f"could not open a browser for {path}")


def view_path(root: Path, kind: str = "graph") -> Path:
    _need_graph(root)
    if kind == "tree" and not (_out(root) / "GRAPH_TREE.html").is_file():
        _tree(root)
    return _offline_copy(root, kind)


def _open(root: Path, positional: list[str], flags: dict) -> str:
    kind = "tree" if positional and positional[0] == "tree" else "graph"
    page = view_path(root, kind)
    if flags.get("no-launch"):
        return f"ok {page}"
    _launch(page)
    return f"ok opened {page}"


def _status(root: Path, flags: dict) -> str:
    info = stats(root)
    return json.dumps(info) if flags.get("json") else _status_line(info)


def cmd_graph(args: list[str]) -> str:
    if not args:
        raise CliError("usage: graph build|update|query|path|explain|tree|open|status")
    action, rest = args[0], args[1:]
    positional, flags = split_flags(
        rest,
        value_flags={"budget", "cap", "ws-path"},
        bool_flags={"dfs", "undirected", "all", "force", "json", "no-launch"},
    )
    root = root_of(flags.get("ws-path"))
    cap = to_int(flags["cap"], "--cap") if flags.get("cap") else DEFAULT_CAP
    force = bool(flags.get("force"))
    if action == "build":
        return _build(root, force)
    if action == "update":
        return _update(root, force)
    if action == "query":
        return _query(root, positional, flags, cap)
    if action == "path":
        return _path(root, positional, flags, cap)
    if action == "explain":
        return _explain(root, positional, flags, cap)
    if action == "tree":
        return _tree(root)
    if action == "open":
        return _open(root, positional, flags)
    if action == "status":
        return _status(root, flags)
    raise CliError(f"unknown graph command '{action}'. try: build update query path explain tree open status")
