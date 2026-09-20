"""Richer symbol outline built directly on tree-sitter, independent of
code_engine.finder (which server.py's tools depend on, so it stays untouched).

Per language it walks the top of the tree and up to MAX_DEPTH nested scopes and
emits: kind, name, start-end lines, nesting depth.
    35-297 fn mount
      41-45 fn drop          (arrow function assigned to a const)
    12-14 interface Block
    5-5 const TILE_COLORS
"""

from __future__ import annotations

from pathlib import Path

from code_engine.languages import LANGUAGES
from code_engine.parser import parse_file

MAX_DEPTH = 3

# node type -> label
DECLS = {
    "class_declaration": "class", "abstract_class_declaration": "class",
    "class_definition": "class", "mixin_declaration": "mixin",
    "extension_declaration": "extension",
    "interface_declaration": "interface", "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "function_declaration": "fn", "generator_function_declaration": "fn",
    "function_definition": "fn", "method_definition": "fn", "method_declaration": "fn",
    "function_signature": "fn", "getter_signature": "fn", "setter_signature": "fn",
    "constructor_signature": "fn", "factory_constructor_signature": "fn",
    "enum_constant": None,
}
# Dart: a signature node is followed by a SIBLING function_body that holds the real end line.
SIGNATURE_TYPES = {
    "function_signature", "getter_signature", "setter_signature",
    "constructor_signature", "factory_constructor_signature",
}
FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
WRAPPERS = {"export_statement", "decorated_definition"}
SCOPES = {"statement_block", "class_body", "block", "function_body", "enum_body"}
VAR_HOLDERS = {"lexical_declaration", "variable_declaration"}
SKIP_INNER_CONST = True     # const declarations inside functions are noise; top-level only


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _name(source: bytes, node) -> str | None:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(source, named)
    # Dart mixin/extension: name is a bare identifier child, not a `name` field.
    ident = next((c for c in node.children if c.type in ("identifier", "type_identifier")), None)
    return _text(source, ident) if ident is not None else None


def _end_node(node):
    """The node whose end line is the declaration's real end. For Dart signatures
    that is the following sibling function_body (walking out of method_signature)."""
    holder = node.parent if node.parent is not None and node.parent.type == "method_signature" else node
    sibling = holder.next_sibling
    if node.type in SIGNATURE_TYPES and sibling is not None and sibling.type == "function_body":
        return sibling
    return node


def _emit(rows: list, depth: int, kind: str, name: str, node) -> None:
    start = node.start_point[0] + 1
    end = max(start, _end_node(node).end_point[0] + 1)
    rows.append((start, end, depth, kind, name))


def _visit(node, source: bytes, depth: int, rows: list) -> None:
    if node.type in WRAPPERS:
        for child in node.children:
            if child.is_named:
                _visit(child, source, depth, rows)
        return

    if node.type == "method_signature":                     # Dart wrapper around a *_signature
        for child in node.children:
            if child.is_named:
                _visit(child, source, depth, rows)
        return

    kind = DECLS.get(node.type)
    if kind is not None:
        name = _name(source, node)
        if name:
            _emit(rows, depth, kind, name, node)
            if depth + 1 < MAX_DEPTH:
                _children(node, source, depth + 1, rows)
        return

    if node.type in VAR_HOLDERS:
        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            name = _name(source, declarator)
            value = declarator.child_by_field_name("value")
            if not name:
                continue
            if value is not None and value.type in FUNCTION_VALUES:
                _emit(rows, depth, "fn", name, declarator)
                if depth + 1 < MAX_DEPTH:
                    _children(value, source, depth + 1, rows)
            elif depth == 0:
                _emit(rows, depth, "const", name, declarator)
        return

    if node.type == "assignment" or node.type == "expression_statement":
        return


def _children(node, source: bytes, depth: int, rows: list) -> None:
    for child in node.children:
        if child.type in SCOPES:
            for inner in child.children:
                if inner.is_named:
                    _visit(inner, source, depth, rows)
        elif child.type in ("arrow_function", "function_expression"):
            _children(child, source, depth, rows)


def outline_rows(path: Path) -> list[tuple[int, int, int, str, str]] | None:
    """[(start, end, depth, kind, name)] in source order, or None if unsupported."""
    if path.suffix.lower() not in LANGUAGES:
        return None
    tree, source = parse_file(str(path))
    rows: list = []
    for top in tree.root_node.children:
        if top.is_named:
            _visit(top, source, 0, rows)
    rows.sort(key=lambda r: (r[0], -r[1]))
    return rows


def format_rows(rows: list, header: str | None = None) -> str:
    lines = [header] if header else []
    for start, end, depth, kind, name in rows:
        span = f"{start}" if start == end else f"{start}-{end}"
        lines.append(f"{'  ' * depth}{span} {kind} {name}")
    return "\n".join(lines)
