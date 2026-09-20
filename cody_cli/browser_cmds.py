from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import CliError

BROWSER_DIR = Path(tempfile.gettempdir()) / "cody_browser"
MAX_CHARS = 3500
TIMEOUT = 120
SNAPSHOT_CMDS = {"snapshot", "find"}

_CODE_BLOCK = re.compile(r"### Ran Playwright code\s*```js.*?```\s*", re.S)
_SNAPSHOT_LINK = re.compile(r"### Snapshot\s*\n- \[Snapshot\]\(([^)]+)\)")


_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+\.js)"')


def _command_prefix() -> list[str]:
    """How to launch playwright-cli. The npm `.cmd` shim runs through cmd.exe, which
    treats `& | < >` in an unquoted argument (any URL with a query string) as shell
    syntax and breaks the call. So when the shim's target script can be found, run
    it with node directly (no cmd.exe layer); otherwise fall back to the shim."""
    found = shutil.which("playwright-cli.cmd") or shutil.which("playwright-cli")
    if not found:
        raise CliError("playwright-cli not found on PATH (npm i -g @playwright/cli)")
    shim = Path(found)
    if shim.suffix.lower() == ".cmd":
        try:
            match = _SHIM_TARGET.search(shim.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            match = None
        if match:
            script = shim.parent / match.group(1)
            node = shim.parent / "node.exe"
            node_exe = str(node) if node.is_file() else shutil.which("node")
            if script.is_file() and node_exe:
                return [node_exe, str(script)]
    return [found]


def _tidy(text: str, keep_urls: bool = False) -> str:
    text = _CODE_BLOCK.sub("", text)
    text = re.sub(r"^### Page\s*\n", "", text, flags=re.M)
    text = re.sub(r"^### (Snapshot|Result|Events|Error)\s*\n", lambda m: f"[{m.group(1).lower()}]\n", text, flags=re.M)
    text = re.sub(r"```(?:yaml)?\n?", "", text)
    text = re.sub(r"^\[(?:snapshot|events)\]\s*\n(?=\[|\Z)", "", text, flags=re.M)   # empty section headers
    text = re.sub(r" \[cursor=pointer\]", "", text)
    if not keep_urls:
        text = re.sub(r"\n\s*- /url: [^\n]*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_CHARS:
        head = int(MAX_CHARS * 0.7)
        tail = MAX_CHARS - head
        text = f"{text[:head]}\n...[{len(text) - MAX_CHARS} chars cut; narrow with `b snapshot <ref>` or `b find <text>`]...\n{text[-tail:]}"
    return text


_CONSOLE_COUNT = re.compile(r"- Console: (\d+) errors?, (\d+) warnings?")
_CONSOLE_REF = re.compile(r"- New console entries: (\S+?\.log)(?:#L(\d+)(?:-L(\d+))?)?")
MAX_CONSOLE_LINES = 8


def _inline_console(text: str, cwd: Path) -> str:
    """The CLI reports `Console: 2 errors, 1 warnings` plus a path relative to ITS
    working dir, which an agent can't open. Replace the pointer with the actual
    warning/error text (bounded), and drop the line entirely when there are none."""
    count = _CONSOLE_COUNT.search(text)
    ref = _CONSOLE_REF.search(text)
    if not ref:
        return text
    entries: list[str] = []
    path = Path(ref.group(1))
    path = path if path.is_absolute() else cwd / path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        first = int(ref.group(2)) if ref.group(2) else 1
        last = int(ref.group(3)) if ref.group(3) else first
        for line in lines[first - 1:last]:
            if "[ERROR]" in line or "[WARNING]" in line:
                entries.append(re.sub(r"^\[\s*\d+ms\]\s*", "", line).replace("[WARNING]", "warn").replace("[ERROR]", "error"))
    except (OSError, ValueError):
        pass
    if entries:
        shown = entries[:MAX_CONSOLE_LINES]
        more = f"\n  ...+{len(entries) - MAX_CONSOLE_LINES} more" if len(entries) > MAX_CONSOLE_LINES else ""
        block = "[console]\n" + "\n".join(f"  {e[:200]}" for e in shown) + more
    else:
        block = ""
    text = _CONSOLE_REF.sub(lambda _m: block, text, count=1)
    if count and count.group(1) == "0" and count.group(2) == "0":
        text = _CONSOLE_COUNT.sub("", text, count=1)
    return re.sub(r"\n{3,}", "\n\n", text)


def _inline_snapshot_file(text: str, cwd: Path) -> str:
    match = _SNAPSHOT_LINK.search(text)
    if not match:
        return text
    file_path = Path(match.group(1))
    if not file_path.is_absolute():
        file_path = cwd / file_path
    try:
        body = file_path.read_text(encoding="utf-8")
    except OSError:
        return text
    replacement = f"[snapshot]\n{body.strip()}"
    return _SNAPSHOT_LINK.sub(lambda _m: replacement, text, count=1)


USAGE = "usage: b <playwright-cli command> [args]  e.g. b open URL | b snapshot | b click e6 | b fill e3 text | b press ArrowLeft | b eval \"() => document.title\" | b screenshot --filename x.png"
ERRORS_CMDS = {"open", "goto", "reload"}
_PAGE_URL = re.compile(r"^- Page URL: (.*)$", re.M)
_PAGE_TITLE = re.compile(r"^- Page Title: (.*)$", re.M)
_CONSOLE_BLOCK = re.compile(r"^\[console\]\n(?:  .*\n?)+", re.M)
_POINT = re.compile(r"^\d+,\d+$")

# Reads from a private offscreen copy: never touches the page's own context, works for
# WebGL canvases too, and avoids the browser's willReadFrequently console warning.
# Flattened to one line before use (no `//` comments in here).
_PIXEL_JS = """() => {
  const sel = %s, pts = %s;
  const el = document.querySelector(sel);
  if (!el) return 'no element matches ' + sel;
  try {
    const copy = document.createElement('canvas');
    copy.width = el.width; copy.height = el.height;
    const ctx = copy.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(el, 0, 0);
    return pts.map(p => { const d = ctx.getImageData(p[0], p[1], 1, 1).data;
      return p[0] + ',' + p[1] + '=rgba(' + d[0] + ',' + d[1] + ',' + d[2] + ',' + d[3] + ')'; }).join(' ');
  } catch (e) { return 'error: ' + e.message; }
}"""


def _pixel(args: list[str]) -> str:
    """b pixel X,Y [X,Y ...] [SELECTOR]: read canvas pixels without a screenshot.
    Coordinates are canvas pixels (not CSS pixels); selector defaults to `canvas`."""
    points = [[int(n) for n in a.split(",")] for a in args if _POINT.match(a)]
    selectors = [a for a in args if not _POINT.match(a)]
    if not points or len(selectors) > 1:
        raise CliError("usage: b pixel X,Y [X,Y ...] [CSS_SELECTOR]   (canvas pixel coords; selector defaults to `canvas`)")
    if len(points) > 50:
        raise CliError("b pixel takes at most 50 points; use `b eval` for bulk sampling")
    script = _PIXEL_JS % (json.dumps(selectors[0] if selectors else "canvas"), json.dumps(points))
    script = " ".join(line.strip() for line in script.splitlines())
    result = cmd_browser(["eval", script])
    if result.startswith("ERR") or "[result]" not in result:
        return result
    body = result.split("[result]", 1)[1].strip()
    try:
        value, _end = json.JSONDecoder().raw_decode(body)      # first JSON value only; page info may follow
    except ValueError:
        return result
    if not isinstance(value, str):
        return result
    console = _CONSOLE_BLOCK.search(result)       # keep page errors visible; drop the page-info noise
    return f"{value}\n{console.group(0).rstrip()}" if console else value


def _errors_only(output: str) -> str:
    """For `open/goto/reload --errors`: one status line plus the console warnings and
    errors (with text), instead of the whole page snapshot."""
    url = _PAGE_URL.search(output)
    title = _PAGE_TITLE.search(output)
    head = "ok"
    if url:
        shown = url.group(1)
        head += " " + (shown if len(shown) <= 100 else shown[:97] + "...")
    if title and title.group(1).strip():
        head += f' "{title.group(1).strip()[:60]}"'
    block = _CONSOLE_BLOCK.search(output)
    if block:
        return f"{head}\n{block.group(0).rstrip()}"
    count = re.search(r"^- Console: .*$", output, re.M)
    if count:                                    # entries exist but their text was unreadable
        return f"{head}\n{count.group(0)[2:]} (text unavailable)"
    return f"{head} - console clean"


def screenshot_paths(text: str) -> list[Path]:
    """Files named by `ok screenshot <path>` lines (also inside batch output)."""
    return [Path(m.strip()) for m in re.findall(r"^ok screenshot (.+)$", text, flags=re.M)]


_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_MAP_JS = ("() => { const sel = %s; const el = document.querySelector(sel); "
           "if (!el) return 'no element matches ' + sel; const r = el.getBoundingClientRect(); "
           "return (r.left + %s * r.width / (el.width || r.width)) + ',' + (r.top + %s * r.height / (el.height || r.height)); }")
CLICKAT_USAGE = "usage: b clickat X Y [--right] [--canvas [CSS_SELECTOR]]   (X,Y = page px; with --canvas = canvas px, selector defaults to `canvas`)"


def _clickat(args: list[str]) -> str:
    """One call instead of mousemove + mousedown + mouseup."""
    tokens = [t for t in args if t != "--right"]
    right = len(tokens) != len(args)
    selector = None
    if "--canvas" in tokens:
        at = tokens.index("--canvas")
        tokens.pop(at)
        selector = tokens.pop(at) if at < len(tokens) and not _NUMBER.match(tokens[at]) else "canvas"
    if len(tokens) != 2 or not all(_NUMBER.match(t) for t in tokens):
        raise CliError(CLICKAT_USAGE)
    x, y = tokens
    if selector is not None:
        script = _MAP_JS % (json.dumps(selector), x, y)
        mapped = cmd_browser(["eval", script])
        body = mapped.split("[result]", 1)[1].strip() if "[result]" in mapped else ""
        value = ""
        try:
            value, _end = json.JSONDecoder().raw_decode(body)
            x, y = (f"{float(n):.1f}" for n in str(value).split(","))
        except (ValueError, TypeError):
            return mapped if mapped.startswith("ERR") else f"ERR clickat: {value or mapped}"
    button = ["right"] if right else []
    console: list[str] = []
    for step in (["mousemove", x, y], ["mousedown", *button], ["mouseup", *button]):
        out = cmd_browser(step)
        if out.startswith("ERR"):
            return f"{out}\n(clickat stopped at {step[0]})"
        block = _CONSOLE_BLOCK.search(out)
        if block:
            console += [ln for ln in block.group(0).splitlines()[1:] if ln not in console]
    head = f"ok clickat {x},{y}" + (" right" if right else "")
    return head + ("\n[console]\n" + "\n".join(console) if console else "")


def cmd_browser(args: list[str]) -> str:
    if not args:
        raise CliError(USAGE)
    sub = args[0]
    if sub == "pixel":
        return _pixel(args[1:])
    if sub == "clickat":
        return _clickat(args[1:])
    errors_only = "--errors" in args
    if errors_only:
        if sub not in ERRORS_CMDS:
            raise CliError(f"--errors works with: {', '.join(sorted(ERRORS_CMDS))}")
    full = sub == "reload" and "--full" in args
    args = [a for a in args if a != "--errors" and not (full and a == "--full")]
    if sub == "reload" and not full:          # a reload is almost always "did my change break anything?"
        errors_only = True
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    command = [*_command_prefix(), *args]
    try:
        process = subprocess.run(
            command,
            cwd=str(BROWSER_DIR),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise CliError(f"browser command timed out after {TIMEOUT}s")
    except OSError as error:
        raise CliError(f"could not start playwright-cli: {error}")

    output = (process.stdout or "") + (("\n" + process.stderr) if process.stderr.strip() else "")
    output = _inline_snapshot_file(output, BROWSER_DIR)
    output = _inline_console(output, BROWSER_DIR)
    output = _tidy(output, keep_urls=sub in SNAPSHOT_CMDS)

    if errors_only and process.returncode == 0:
        return _errors_only(output)

    if sub in ("screenshot", "pdf"):
        saved = re.search(r"\]\(([^)]+\.(?:png|jpe?g|webp|pdf))\)", output)
        if saved:
            path = Path(saved.group(1))
            if not path.is_absolute():
                path = BROWSER_DIR / path
            return f"ok screenshot {path}"

    if process.returncode != 0:
        stale = re.search(r"Ref (\S+) not found", output)
        if stale:
            return f"ERR ref {stale.group(1)} not found: refs reset on open/goto/reload; run `b snapshot` for fresh refs"
        return f"ERR exit {process.returncode}\n{output}".strip()
    return output or "ok"
