"""Saved project roots and path resolution.

A workspace is just a short alias for a directory on this machine. Nothing
requires one: every tool also accepts an absolute path, and `run` works with
no workspace at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import app_paths

CONFIG_FILE = app_paths.config_file()

WORKSPACES: dict[str, Path] = {}


def load() -> None:
    global WORKSPACES

    if not CONFIG_FILE.exists():
        return

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    WORKSPACES = {
        name: Path(path)
        for name, path in (data.get("workspaces") or {}).items()
    }


def save() -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(
            {"workspaces": {name: str(path) for name, path in WORKSPACES.items()}},
            indent=4,
        ),
        encoding="utf-8",
    )


def list_workspaces() -> dict[str, str]:
    return {name: str(path) for name, path in WORKSPACES.items()}


def add_workspace(name: str, path: str) -> Path:
    resolved = Path(path).expanduser().resolve()

    if not resolved.exists():
        raise FileNotFoundError(path)
    if not resolved.is_dir():
        raise NotADirectoryError(path)

    WORKSPACES[name] = resolved
    save()
    return resolved


def remove_workspace(name: str) -> None:
    if name not in WORKSPACES:
        raise ValueError(f"Workspace '{name}' not found")

    WORKSPACES.pop(name, None)
    save()


def get_workspace(name: str) -> Path:
    """Resolve an alias - or a plain directory path - to a directory."""
    if name in WORKSPACES:
        return WORKSPACES[name].expanduser().resolve()

    candidate = Path(name).expanduser()
    if candidate.is_dir():
        return candidate.resolve()

    raise ValueError(
        f"'{name}' is neither a registered workspace nor an existing directory. "
        f"Known workspaces: {', '.join(sorted(WORKSPACES)) or 'none'}"
    )


def resolve_path(workspace: str, path: str) -> Path:
    """Turn (workspace, path) into an absolute path.

    An absolute `path` is used as given. A relative one is joined to the
    workspace when there is one, and to the current directory when there is not.
    """
    target = Path(path or ".").expanduser()

    if target.is_absolute():
        return target.resolve()

    if workspace:
        return (get_workspace(workspace) / target).resolve()

    return target.resolve()


def resolve_dir(location: str) -> Path:
    """Directory for a command to run in. Empty means the current directory."""
    if not location:
        return Path.cwd()
    return get_workspace(location)


load()
