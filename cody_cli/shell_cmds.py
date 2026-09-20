"""sh (one-shot commands) and proc (long-lived background processes).

Why this is separate from code_cmds: the old `sh` re-joined shlex tokens, which
stripped quotes (`dir "C:\\Program Files"` broke) and always ran under cmd.exe
(PowerShell syntax died). Here `sh` receives the RAW text after the word `sh`,
and can hand a script straight to PowerShell / cmd / bash with no quoting layer.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from core import terminal

from .common import CliError, current_workspace, path_of, root_of, take_script, to_int

SH_CAP = 4000
SHELLS = ("cmd", "ps", "bash")
_FLAG = re.compile(r"^--(cwd|timeout|cap|shell|script)\b\s*")
_NO_OUTPUT = "(no output)"
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def parse_sh(raw: str) -> tuple[str, dict]:
    """Split leading cody flags off the RAW text; the rest is the command verbatim.

    `--cwd D --shell ps git log --oneline` -> flags {cwd, shell}, command
    `git log --oneline`. Only recognised flags at the very start are consumed,
    so a command's own --flags always pass through untouched. A trailing
    `--cap N` / `--timeout N` is also honoured (agents habitually append it).
    """
    text = raw.strip()
    flags: dict = {}
    while True:
        match = _FLAG.match(text)
        if not match:
            break
        name = match.group(1)
        text = text[match.end():]
        if name == "script":
            flags["script"] = True
            continue
        value, text = _take_value(text)
        flags[name] = value
    tail = re.search(r"\s--(cap|timeout)\s+(\d+)\s*$", text)
    if tail and tail.group(1) not in flags:
        flags[tail.group(1)] = tail.group(2)
        text = text[:tail.start()]
    return text.strip(), flags


def _take_value(text: str) -> tuple[str, str]:
    text = text.lstrip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        end = text.find(quote, 1)
        if end > 0:
            return text[1:end], text[end + 1:].lstrip()
    head, _, rest = text.partition(" ")
    return head, rest.lstrip()


def _resolve_cwd(flags: dict) -> Path:
    if flags.get("cwd"):
        return path_of(flags["cwd"])
    ws = current_workspace()
    return path_of(".") if ws else root_of(None)


def _default_shell() -> str:
    return os.environ.get("CODY_SHELL", "cmd" if os.name == "nt" else "bash")


def _build_argv(shell: str, body: str) -> list[str] | str:
    """Return an argv list (no shell layer) or a string for the cmd/shell=True path."""
    if shell == "cmd":
        return body
    if shell == "ps":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            raise CliError("PowerShell not found on PATH")
        # Silence the progress stream at the source; it otherwise leaks a
        # `#< CLIXML` XML blob into stderr on every -EncodedCommand call.
        script = _PS_PREAMBLE + "\n" + body
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    if shell == "bash":
        exe = shutil.which("bash")
        if not exe:
            raise CliError("bash not found on PATH")
        return [exe, "-c", body]
    raise CliError(f"unknown shell '{shell}' (use {'|'.join(SHELLS)})")


def cmd_sh(raw: str) -> str:
    body, flags = parse_sh(raw)
    shell = flags.get("shell", _default_shell())
    if shell not in SHELLS:
        raise CliError(f"unknown shell '{shell}' (use {'|'.join(SHELLS)})")
    if flags.get("script") or (not body and "script" in flags):
        body = take_script("sh --script")
    if not body:
        raise CliError("usage: sh [--shell ps|cmd|bash] [--cwd DIR] [--timeout S] [--cap N] <command> | sh --shell ps --script -  (script on the following lines)")

    reason = terminal.refusal_reason(body)
    if reason:
        return f"ERR refused: {reason}"

    cwd = _resolve_cwd(flags)
    timeout = max(1, to_int(flags.get("timeout", 300), "timeout"))
    limit = max(200, to_int(flags.get("cap", SH_CAP), "cap"))
    prepared = _build_argv(shell, body)
    started = time.perf_counter()
    result = _run(prepared, cwd, timeout)
    ms = int((time.perf_counter() - started) * 1000)

    ok = result["code"] == 0 and not result["timeout"]
    head = "exit 0" if ok else f"exit {result['code']}" + (" TIMEOUT" if result["timeout"] else "") + f" {ms}ms"
    parts = [head]
    out, out_clipped = terminal.clip(result["out"].strip(), limit)
    err, err_clipped = terminal.clip(result["err"].strip(), limit if not ok else limit // 2)
    if out:
        parts.append(out)
    if err:
        parts.append("[stderr]\n" + err)
    if ok and not out and not err:
        parts.append(_NO_OUTPUT)
    if out_clipped or err_clipped:
        parts.append("# clipped; filter the output, or --cap N")
    return "\n".join(parts)


_CLIXML = re.compile(r"#< CLIXML\s*<Objs.*?</Objs>", re.S)
_CLIXML_TEXT = re.compile(r'<S S="Error">(.*?)</S>', re.S)
_PS_PREAMBLE = "$ProgressPreference='SilentlyContinue'"


def _clean_stderr(text: str) -> str:
    """PowerShell wraps stderr as CLIXML when driven non-interactively. Keep the
    human-readable error text, drop the XML envelope and our injected preamble
    (PowerShell echoes the failing statement, which would leak it)."""
    if "#< CLIXML" in text:
        errors = _CLIXML_TEXT.findall(text)
        if errors:
            joined = "".join(errors).replace("_x000D__x000A_", "\n").replace("_x000D_", "")
            text = joined.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
        else:
            text = _CLIXML.sub("", text)
    return text.replace(_PS_PREAMBLE + "\n", "").replace(_PS_PREAMBLE, "").strip()


def _run(prepared, cwd: Path, timeout: int) -> dict:
    if not cwd.is_dir():
        return {"code": 1, "out": "", "err": f"working directory does not exist: {cwd}", "timeout": False}
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    try:
        done = subprocess.run(
            prepared, shell=isinstance(prepared, str), cwd=str(cwd), capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL,
        )
        return {"code": done.returncode, "out": done.stdout or "", "err": _clean_stderr(done.stderr or ""), "timeout": False}
    except subprocess.TimeoutExpired as expired:
        return {"code": None, "out": _text(expired.stdout), "err": _text(expired.stderr), "timeout": True}
    except OSError as error:
        return {"code": 1, "out": "", "err": f"could not start: {error}", "timeout": False}


def _text(blob) -> str:
    if blob is None:
        return ""
    return blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else str(blob)


# --------------------------------------------------------------------------
# proc: long-lived background processes (dev servers, watchers)
# --------------------------------------------------------------------------
PROC_DIR = Path(__file__).resolve().parent.parent / "tray_logs" / "proc"
_URL = re.compile(r"https?://[^\s\"'<>\x1b]+")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _registry_path() -> Path:
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    return PROC_DIR / "registry.json"


def _load() -> dict:
    try:
        return json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    _registry_path().write_text(json.dumps(data, indent=1), encoding="utf-8")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        done = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
        )
        return f'"{pid}"' in done.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass


def _tail(path: Path, count: int) -> str:
    try:
        text = _ANSI.sub("", path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-count:])


def _clip_lines(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


# --- who is holding a port (Windows only; degrades to "no info" elsewhere) ---------
DEV_PORTS = (*range(3000, 3011), 4173, 4200, 5000, *range(5173, 5181), 8000, 8080, 8081, 8888, 9000)
_PORT_IN_USE = re.compile(r"[Pp]ort (\d+) is (?:already )?in use")     # Vite, Next, ...
_LISTEN = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.M)   # v4 and [::1] v6


def _listeners() -> dict[int, int]:
    """port -> pid of whoever is LISTENING on it. {} when unavailable."""
    if os.name != "nt":
        return {}
    try:
        done = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, int] = {}
    for port, pid in _LISTEN.findall(done.stdout):
        found.setdefault(int(port), int(pid))
    return found


def _process_table() -> dict[int, tuple[int, str, str]]:
    """pid -> (parent pid, exe name, command line). {} when unavailable."""
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if os.name != "nt" or not exe:
        return {}
    script = ("Get-CimInstance Win32_Process | ForEach-Object { [pscustomobject]@{p=$_.ProcessId;pp=$_.ParentProcessId;"
              "n=$_.Name;c=$_.CommandLine} } | ConvertTo-Json -Compress")
    try:
        done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=30, creationflags=_CREATE_NO_WINDOW)
        rows = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    rows = [rows] if isinstance(rows, dict) else rows
    return {int(r["p"]): (int(r["pp"] or 0), r.get("n") or "?", r.get("c") or "") for r in rows if r.get("p") is not None}


def _descendants(roots: set[int], table: dict) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (parent, _name, _cmd) in table.items():
        children.setdefault(parent, []).append(pid)
    seen, stack = set(roots), list(roots)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _managed_names(registry: dict, table: dict) -> dict[int, str]:
    """pid -> proc name, for every live proc-started process and its children."""
    owners: dict[int, str] = {}
    for name, entry in registry.items():
        for pid in _descendants({entry["pid"]}, table):
            owners.setdefault(pid, name)
    return owners


def _port_notes(log_text: str, urls: list[str], registry: dict) -> list[str]:
    """Dev servers (Vite) silently hop to the next free port when theirs is taken.
    Say so, and say who holds the original, so the caller can reuse or stop it."""
    taken = list(dict.fromkeys(int(p) for p in _PORT_IN_USE.findall(log_text)))
    if not taken:
        return []
    listeners = _listeners()
    table = _process_table() if listeners else {}
    managed = _managed_names(registry, table)
    landed = re.search(r":(\d+)", urls[0].split("//", 1)[-1]) if urls else None
    notes = []
    for port in taken:
        pid = listeners.get(port)
        if pid is None:
            who = "no listener found now"
        else:
            name = table.get(pid, (0, "process", ""))[1]
            who = f"{name} pid {pid}, " + (f"proc '{managed[pid]}'" if pid in managed else "not started via proc")
        notes.append(f"note: port {port} was in use ({who})" + (f"; server is on {landed.group(1)}" if landed else ""))
    return notes


def _unmanaged_listeners(registry: dict) -> list[str]:
    listeners = _listeners()
    if not listeners:
        return ["# port scan unavailable on this system"]
    table = _process_table()
    managed = _managed_names(registry, table)
    lines = []
    for port in sorted(p for p in listeners if p in DEV_PORTS):
        pid = listeners[port]
        if pid in managed:
            continue
        _parent, name, command = table.get(pid, (0, "?", ""))
        lines.append(f":{port} pid {pid} {name} {command[:80]}".rstrip())
    return ["# other listeners on common dev ports (not started via proc):", *lines] if lines else \
           ["# no other listeners on common dev ports"]


def _proc_start(raw: str) -> str:
    if " -- " not in f" {raw} ":
        raise CliError("usage: proc start NAME [--ready TEXT] [--wait S] [--cwd DIR] [--shell ps|cmd|bash] -- COMMAND")
    head, _, command = f" {raw} ".partition(" -- ")
    command = command.strip()
    head_args = head.split()
    if not head_args or not command:
        raise CliError("usage: proc start NAME [--ready TEXT] [--wait S] -- COMMAND")
    name = head_args[0]
    opts: dict = {}
    index = 1
    while index < len(head_args):
        flag = head_args[index]
        if flag in ("--ready", "--wait", "--cwd", "--shell") and index + 1 < len(head_args):
            value = head_args[index + 1]
            if flag == "--ready":                      # ready text may contain spaces
                pieces = []
                index += 1
                while index < len(head_args) and not head_args[index].startswith("--"):
                    pieces.append(head_args[index])
                    index += 1
                opts["ready"] = " ".join(pieces)
                continue
            opts[flag[2:]] = value
            index += 2
            continue
        raise CliError(f"unknown proc start option '{flag}'")
    if not re.fullmatch(r"[\w.-]+", name):
        raise CliError("process name: letters, digits, . _ - only")

    registry = _load()
    existing = registry.get(name)
    if existing and _alive(existing["pid"]):
        return f"ERR '{name}' already running (pid {existing['pid']}); `proc stop {name}` first"

    reason = terminal.refusal_reason(command)
    if reason:
        return f"ERR refused: {reason}"

    shell = opts.get("shell", _default_shell())
    prepared = _build_argv(shell, command)
    cwd = _resolve_cwd(opts)
    if not cwd.is_dir():
        raise CliError(f"working directory does not exist: {cwd}")
    log = PROC_DIR / f"{name}.log"
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"")
    wait = max(1, to_int(opts.get("wait", 30), "wait"))

    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    env["FORCE_COLOR"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    handle = open(log, "ab", buffering=0)
    flags_nt = (subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            prepared, shell=isinstance(prepared, str), cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            creationflags=flags_nt, start_new_session=(os.name != "nt"),
        )
    except OSError as error:
        handle.close()
        raise CliError(f"could not start: {error}")
    registry[name] = {"pid": process.pid, "command": command[:200], "cwd": str(cwd), "log": str(log), "started": int(time.time())}
    _save(registry)

    ready = opts.get("ready")
    deadline = time.time() + wait
    state = "started"
    while time.time() < deadline:
        time.sleep(0.25)
        text = _ANSI.sub("", log.read_text(encoding="utf-8", errors="replace"))
        if ready and ready.lower() in text.lower():
            state = "ready"
            break
        if process.poll() is not None:
            state = f"EXITED {process.returncode}"
            break
        if not ready and text.strip() and time.time() - registry[name]["started"] >= 2:
            state = "running"
            break
    else:
        state = "not ready yet" if ready else "running"

    text = _ANSI.sub("", log.read_text(encoding="utf-8", errors="replace"))
    urls = list(dict.fromkeys(u.rstrip(").,;]") for u in _URL.findall(text)))[:3]
    lines = [f"{state}: {name} pid {process.pid}" + (f"  {' '.join(urls)}" if urls else "")]
    lines.extend(_port_notes(text, urls, registry))
    if state.startswith("EXITED") or state == "not ready yet":
        lines.append(_clip_lines(_tail(log, 15)))
    return "\n".join(lines)


def _proc_stop(name: str) -> str:
    registry = _load()
    entry = registry.get(name)
    if not entry:
        raise CliError(f"no process '{name}'. `proc list` shows running ones")
    was_alive = _alive(entry["pid"])
    if was_alive:
        _kill_tree(entry["pid"])
        for _ in range(20):
            if not _alive(entry["pid"]):
                break
            time.sleep(0.1)
    registry.pop(name, None)
    _save(registry)
    return f"ok stopped {name}" if was_alive else f"ok {name} was already stopped"


def _proc_list(show_all: bool = False) -> str:
    registry = _load()
    rows = [f"{name} pid {entry['pid']} {'up' if _alive(entry['pid']) else 'DEAD'} {entry['command'][:60]}"
            for name, entry in registry.items()]
    if not rows:
        rows = ["no processes started via proc" + ("" if show_all else " (`proc list --all` also scans dev ports)")]
    if show_all:
        rows += _unmanaged_listeners(registry)
    return "\n".join(rows)


def _proc_logs(rest: str) -> str:
    parts = rest.split()
    if not parts:
        raise CliError("usage: proc logs NAME [--tail N]")
    name, tail = parts[0], 30
    if "--tail" in parts and parts.index("--tail") + 1 < len(parts):
        tail = max(1, to_int(parts[parts.index("--tail") + 1], "tail"))
    entry = _load().get(name)
    if not entry:
        raise CliError(f"no process '{name}'. `proc list` shows running ones")
    text = _tail(Path(entry["log"]), tail)
    return _clip_lines(text, 3000) if text else "(no output yet)"


def cmd_proc(raw: str) -> str:
    action, _, rest = raw.strip().partition(" ")
    rest = rest.strip()
    if action == "start":
        return _proc_start(rest)
    if action == "stop" and rest:
        return _proc_stop(rest.split()[0])
    if action == "logs":
        return _proc_logs(rest)
    if action in ("list", "ls", ""):
        return _proc_list(show_all="--all" in rest.split())
    raise CliError("usage: proc start NAME [--ready TEXT] -- CMD | proc logs NAME [--tail N] | proc stop NAME | proc list [--all]")
