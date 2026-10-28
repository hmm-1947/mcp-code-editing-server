from __future__ import annotations

import re

MAX_BYTES = 400_000
_ALIAS = {".js": ".jsx", ".mjs": ".jsx", ".cjs": ".jsx"}
_KEEP = re.compile(
    r"^(?:#!|#\s*(?:-\*-|type:|noqa|pylint|pragma|fmt:|isort:|mypy:|pyright:|ruff:)"
    r"|//\s*(?:@ts-|eslint|prettier|istanbul|ignore|ignore_for_file|coverage:)"
    r"|/\*\s*(?:eslint|global\s|istanbul|webpackChunkName)"
    r"|///\s*<reference)",
    re.IGNORECASE,
)
_NOT_NEWLINE = re.compile(rb"[^\r\n]")


def blank_comments(suffix: str, text: str) -> str | None:
    try:
        from code_engine.languages import LANGUAGES, get_parser

        key = _ALIAS.get(suffix, suffix)
        if key not in LANGUAGES:
            return None
        data = text.encode("utf-8")
        if len(data) > MAX_BYTES:
            return None
        tree = get_parser("x" + key).parse(data)
        spans: list[tuple[int, int]] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if "comment" in node.type:
                spans.append((node.start_byte, node.end_byte))
                continue
            stack.extend(node.children)
        if not spans:
            return text
        buffer = bytearray(data)
        for start, end in spans:
            if _KEEP.match(data[start:end].decode("utf-8", "replace")):
                continue
            buffer[start:end] = _NOT_NEWLINE.sub(b" ", data[start:end])
        return bytes(buffer).decode("utf-8", "replace")
    except Exception:
        return None
