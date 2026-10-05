"""Support modules for the tools.

    terminal    the only place a shell command is executed
    search      the only place a text search is executed
    structure   cheap bracket-balance guard for edits
"""

from ..src.core import search, structure, terminal  # noqa: F401

__all__ = ["search", "structure", "terminal"]
