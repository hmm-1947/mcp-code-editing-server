"""Entry point.

The short usage contract in `instructions` is sent to MCP clients during
initialization, so a connected model knows how to use the tools before its
first call.
"""

import argparse

from fastmcp import FastMCP

from core.instructions import SERVER_INSTRUCTIONS
from tools import register_tools

mcp = FastMCP(
    "MCP Local Code Editing Server",
    instructions=SERVER_INSTRUCTIONS,
)

register_tools(mcp)


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP local code editing server")
    parser.add_argument("--transport", default="streamable-http",
                        choices=("streamable-http", "sse", "stdio"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    arguments = parser.parse_args()


    if arguments.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=arguments.transport, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()

