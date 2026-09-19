from pathlib import Path


STRUCTURAL_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cxx", ".dart", ".h", ".hpp", ".java",
    ".js", ".jsx", ".json", ".kt", ".swift", ".ts", ".tsx",
})
REGEX_CAPABLE_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx"})
OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
CLOSING = frozenset(OPEN_TO_CLOSE.values())

# Characters/keywords after which a bare `/` starts a regex literal rather
# than meaning division. This is a heuristic (real JS lexing needs a parser)
# but it covers the common cases: after an operator, opening bracket,
# comma/semicolon/colon, or certain keywords, `/` opens a regex.
REGEX_PRECEDING_PUNCT = set("([{,;:=!&|?+-*%^~<>")
REGEX_PRECEDING_KEYWORDS = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete",
    "void", "throw", "case", "do", "else", "yield", "await",
})


def _last_significant_token(content: str, index: int) -> str:
    """The last non-whitespace char, or trailing keyword, before index."""
    cursor = index - 1
    while cursor >= 0 and content[cursor] in " \t\n\r":
        cursor -= 1
    if cursor < 0:
        return ""
    if content[cursor].isalnum() or content[cursor] == "_":
        end = cursor + 1
        start = cursor
        while start >= 0 and (content[start].isalnum() or content[start] == "_"):
            start -= 1
        return content[start + 1:end]
    return content[cursor]


def _regex_starts_here(content: str, index: int) -> bool:
    token = _last_significant_token(content, index)
    if token == "":
        return True  # start of file
    if len(token) == 1 and token in REGEX_PRECEDING_PUNCT:
        return True
    if token in REGEX_PRECEDING_KEYWORDS:
        return True
    return False


def validate_structure(path: str | Path, content: str) -> dict | None:
    """Check bracket balance outside string literals, comments, and (for
    JS-family files) regex literals.

    Returns None when validation is not applicable or succeeds; otherwise a
    small error payload suitable for returning directly from the edit tool.
    """
    suffix = Path(path).suffix.lower()
    if suffix not in STRUCTURAL_EXTENSIONS:
        return None
    regex_capable = suffix in REGEX_CAPABLE_EXTENSIONS

    stack: list[tuple[str, int]] = []
    line = 1
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    in_regex = False
    in_char_class = False  # inside a regex [...] where / doesn't end the regex

    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""

        if line_comment:
            if char == "\n":
                line += 1
                line_comment = False
            index += 1
            continue

        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            if char == "\n":
                line += 1
            index += 1
            continue

        if in_regex:
            if char == "\\":
                index += 2
                continue
            if char == "[":
                in_char_class = True
            elif char == "]":
                in_char_class = False
            elif char == "/" and not in_char_class:
                in_regex = False
            elif char == "\n":
                # Unterminated regex literal - bail out of regex mode rather
                # than swallowing the rest of the file.
                in_regex = False
                line += 1
                continue
            index += 1
            continue

        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            elif char == "\n":
                line += 1
            index += 1
            continue

        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if regex_capable and char == "/" and next_char != "/" and _regex_starts_here(content, index):
            in_regex = True
            in_char_class = False
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char in OPEN_TO_CLOSE:
            stack.append((char, line))
        elif char in CLOSING:
            if not stack:
                return {
                    "message": "Structural validation failed",
                    "line": line,
                    "expected": None,
                    "found": char,
                }
            opening, opening_line = stack.pop()
            expected = OPEN_TO_CLOSE[opening]
            if char != expected:
                return {
                    "message": "Structural validation failed",
                    "line": line,
                    "expected": expected,
                    "found": char,
                    "opened_at_line": opening_line,
                }
        if char == "\n":
            line += 1
        index += 1

    if stack:
        opening, opening_line = stack[-1]
        return {
            "message": "Structural validation failed",
            "line": opening_line,
            "expected": OPEN_TO_CLOSE[opening],
            "found": "end of file",
        }
    return None
