"""Agent orchestration: hands tasks from the MCP client to OpenRouter and local models.

Everything here is inert unless settings.enabled() is true. Importing this package
pulls in nothing heavy; torch only ever runs inside the separate Laya worker process.
"""
from .settings import enabled, load as load_settings

__all__ = ["enabled", "load_settings"]
