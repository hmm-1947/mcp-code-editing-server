"""Command execution.

Shared destructive-command guard and output clipping for sh/proc.
"""

from __future__ import annotations

import re

#: Refused outright. Not a sandbox - a guard against catastrophic typos in a
#: loop that is allowed to run commands unattended.
DESTRUCTIVE_PATTERNS = (
    (r"\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+/(\s|$)", "recursive delete of /"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(~|\$HOME)(/\s*)?$", "recursive delete of home directory"),
    (r"\bmkfs(\.|\s)", "filesystem format"),
    (r"\bformat\s+[a-zA-Z]:", "drive format"),
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|disk)", "raw write to a block device"),
    (r">\s*/dev/(sd|nvme|hd|disk)", "raw write to a block device"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "host power state change"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bchmod\s+-R\s+777\s+/(\s|$)", "recursive permission wipe of /"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b", "piping a remote script into a shell"),
    (r"\bgit\s+push\b[^\n]*--force", "force push"),
    (r"\bgit\s+reset\s+--hard\b", "hard reset (discards uncommitted work)"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "git clean (discards untracked work)"),
)

MAX_OUTPUT_CHARS = 20000


def refusal_reason(command: str) -> str | None:
    for pattern, label in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, command):
            return label
    return None


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """Keep the head and (larger) tail - failures surface at the end."""
    if len(text) <= limit:
        return text, False
    head = int(limit * 0.35)
    tail = limit - head
    omitted = len(text) - limit
    return (
        f"{text[:head]}\n...[{omitted} chars omitted from the middle]...\n{text[-tail:]}",
        True,
    )
