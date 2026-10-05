import re

from .finder import find_function, _body_end_byte


def build_function_replacement(
    path: str,
    function_name: str,
    new_function: str,
) -> tuple[bytes, int, int]:
    """Build a function replacement without writing it to disk.

    Returns the proposed bytes plus the first and last affected line numbers.
    """
    node, source = find_function(path, function_name)

    if node is None:
        raise ValueError(f"Function '{function_name}' not found")

    end_byte = _body_end_byte(node)

    if end_byte <= node.start_byte:
        raise ValueError(
            f"Could not determine the full body span of '{function_name}' - refusing to edit to avoid corrupting the file"
        )

    new_source = (
        source[:node.start_byte]
        + new_function.encode("utf-8")
        + source[end_byte:]
    )
    start_line = source.count(b"\n", 0, node.start_byte) + 1
    end_line = start_line + new_function.count("\n")
    return new_source, start_line, end_line


def fuzzy_find_text(text: str, old_text: str):
    """Find old_text in text tolerating differences in leading whitespace
    and blank-line spacing. Returns (start_idx, end_idx) into text, or
    None if not found. Used as a fallback when an exact substring match
    fails, so the LLM doesn't have to reproduce whitespace perfectly.
    """

    # \r?\n so a CRLF file still matches an LF-separated snippet.
    pattern = r"\r?\n".join(
        r"[ \t]*" + re.escape(line.strip()) + r"[ \t]*"
        for line in old_text.replace("\r\n", "\n").split("\n")
    )
    match = re.search(pattern, text)
    return None if match is None else match.span()
