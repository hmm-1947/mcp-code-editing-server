from collections.abc import Iterator

from tree_sitter import Node

from .parser import parse_file


BODY_TYPES = frozenset({"block", "statement_block", "class_body", "function_body"})
FUNCTION_TYPES = frozenset({
    "function_definition", "method_definition", "method_declaration",
    "function_declaration", "generator_function_declaration",
    "function_signature", "method_signature", "getter_signature",
    "setter_signature", "constructor_signature", "factory_constructor_signature",
    "operator_signature",
})
CLASS_TYPES = frozenset({
    "class_definition", "class_declaration", "mixin_declaration",
    "extension_declaration", "interface_declaration",
})
DECLARATION_TYPES = FUNCTION_TYPES | CLASS_TYPES
NAME_TYPES = frozenset({"identifier", "type_identifier", "property_identifier"})


def walk(node: Node) -> Iterator[Node]:
    """Yield every syntax-tree node in preorder without recursion."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _name_node(node: Node) -> Node | None:
    return node.child_by_field_name("name") or next(
        (child for child in node.children if child.type in NAME_TYPES), None
    )


def _kind(node: Node) -> str | None:
    if node.type in FUNCTION_TYPES:
        return "function"
    if node.type in CLASS_TYPES:
        return "class"
    return None


def _iter_definitions(tree_root: Node) -> Iterator[tuple[Node, Node, str]]:
    """Yield each function/class definition once, including decorated Python code."""
    for node in walk(tree_root):
        if node.type == "decorated_definition":
            declaration = next((child for child in node.children if child.type in DECLARATION_TYPES), None)
            if declaration is not None:
                name = _name_node(declaration)
                kind = _kind(declaration)
                if name is not None and kind is not None:
                    yield declaration, name, kind
            continue

        # Decorated declarations are emitted by their wrapper above.
        if node.parent is not None and node.parent.type == "decorated_definition":
            continue

        name = _name_node(node)
        kind = _kind(node)
        if name is not None and kind is not None:
            yield node, name, kind


def _body_end_byte(node: Node) -> int:
    """Return the byte offset immediately after a declaration's full body."""
    if any(child.type in BODY_TYPES for child in node.children):
        return node.end_byte

    sibling = node.next_sibling
    while sibling is not None:
        if sibling.type in BODY_TYPES or sibling.type == ";":
            return sibling.end_byte
        if sibling.type in DECLARATION_TYPES or sibling.type == "decorated_definition":
            break
        sibling = sibling.next_sibling
    return node.end_byte


def _name(source: bytes, name_node: Node) -> str:
    return source[name_node.start_byte:name_node.end_byte].decode("utf-8")


def _signature(node: Node, source: bytes) -> str:
    body = next((child for child in node.children if child.type in BODY_TYPES), None)
    end = body.start_byte if body is not None else node.end_byte
    return source[node.start_byte:end].decode("utf-8").strip().rstrip("{").rstrip(":").strip()


def _find_definition(path: str, target_kind: str, target_name: str):
    tree, source = parse_file(path)
    for node, name_node, kind in _iter_definitions(tree.root_node):
        if kind == target_kind and _name(source, name_node) == target_name:
            return node, source
    return None, None


def find_function(path: str, function_name: str):
    """Return a function node and its source bytes, or (None, None)."""
    return _find_definition(path, "function", function_name)


def _find_symbols(path: str, query: str, include_bodies: bool) -> list[dict]:
    tree, source = parse_file(path)
    query = query.casefold()
    matches = []

    for node, name_node, kind in _iter_definitions(tree.root_node):
        name = _name(source, name_node)
        if query not in name.casefold():
            continue

        end_byte = _body_end_byte(node)
        match = {
            "name": name,
            "type": kind,
            "line": name_node.start_point[0] + 1,
            "end_line": source.count(b"\n", 0, end_byte) + 1,
        }
        if include_bodies:
            match["body"] = source[node.start_byte:end_byte].decode("utf-8")
        matches.append(match)

    return matches


def find_symbols_in_file(path: str, query: str) -> list[dict]:
    """Return matching functions/classes without their source bodies."""
    return _find_symbols(path, query, include_bodies=False)
