"""
MCP SSE client — connects to the deployed MCP server and exposes
its tools as OpenAI-compatible function definitions.

Opens a fresh connection per call to avoid managing a persistent
SSE stream across async turn boundaries.
"""

import json
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

MCP_SSE_URL = os.getenv(
    "MCP_SSE_URL",
    "https://agent-telemetry-mcp.delightfulwave-961fa824.eastus.azurecontainerapps.io/sse",
)


async def list_tools() -> list[dict]:
    """Return OpenAI-compatible tool definitions from the MCP server."""
    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema or {"type": "object", "properties": {}},
                    },
                }
                for t in result.tools
            ]


async def call_tool(name: str, arguments: dict) -> str:
    """Execute a named tool on the MCP server. Returns result as a JSON string."""
    async with sse_client(MCP_SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            parts = [c.text if hasattr(c, "text") else str(c) for c in result.content]
            return "\n".join(parts)
