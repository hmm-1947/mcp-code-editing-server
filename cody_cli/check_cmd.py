"""check: autodetect the project's type/lint checker and run it.

    check                run the detected checker in the workspace
    check --what         only print what would run
    check --cmd "..."    override the command
    check --timeout S    default 180

Detection is by marker file, first match wins. Output is the checker's own,
capped by the same head+tail clipping as `sh`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .common import CliError, root_of, to_int
from .shell_cmds import cmd_sh


def _package_scripts(root: Path) -> dict:
    try:
        return json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {}) or {}
    except (OSError, ValueError):
        return {}


def detect(root: Path) -> tuple[str, str] | None:
    """Return (label, command) for the first checker that fits, else None."""
    if (root / "tsconfig.json").is_file():
        return "typescript", "npx tsc --noEmit"
    scripts = _package_scripts(root)
    for name in ("typecheck", "type-check", "check", "lint"):
        if name in scripts:
            return f"npm:{name}", f"npm run {name} --silent"
    if (root / "pubspec.yaml").is_file():
        return "dart", "dart analyze" if shutil.which("dart") else "flutter analyze"
    if (root / "Cargo.toml").is_file():
        return "rust", "cargo check --quiet"
    if (root / "go.mod").is_file():
        return "go", "go vet ./..."
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file() or any(root.glob("*.py")):
        if shutil.which("ruff"):
            return "ruff", "ruff check ."
        return "python", "python -m compileall -q ."
    return None


def cmd_check(raw: str) -> str:
    tokens = raw.split()
    what_only = "--what" in tokens
    override = None
    timeout = 180
    if "--cmd" in tokens:
        after = raw.split("--cmd", 1)[1].strip()
        override = after.strip("\"'") if after else None
    if "--timeout" in tokens:
        index = tokens.index("--timeout")
        if index + 1 < len(tokens):
            timeout = to_int(tokens[index + 1], "timeout")

    root = root_of(None)
    if override:
        label, command = "custom", override
    else:
        found = detect(root)
        if found is None:
            raise CliError("no checker detected (looked for tsconfig.json, package.json scripts, pubspec.yaml, Cargo.toml, go.mod, python). use: check --cmd \"...\"")
        label, command = found
    if what_only:
        return f"{label}: {command}"
    result = cmd_sh(f"--timeout {timeout} {command}")
    first, _, rest = result.partition("\n")
    if first == "exit 0":
        return f"ok {label} clean" + (f"\n{rest}" if rest and rest != "(no output)" else "")
    return f"FAIL {label}: {first}\n{rest}".rstrip()
