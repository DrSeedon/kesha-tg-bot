"""Stdio MCP facade for Kesha's in-process Telegram tools."""

import os
from typing import Any

import aiohttp
import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from kesha_tools import bridge_tools


SOCKET_ENV = "KESHA_BRIDGE_SOCKET"
TOKEN_ENV = "KESHA_BRIDGE_TOKEN"
SESSION_ENV = "KESHA_BRIDGE_SESSION"


def _json_type(value: type) -> dict[str, Any]:
    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        list: {"type": "array", "items": {}},
        dict: {"type": "object"},
    }.get(value, {})


def exposed_tools() -> list[Tool]:
    result = []
    for item in bridge_tools():
        if isinstance(item.input_schema, dict) and "type" in item.input_schema:
            schema = item.input_schema
        else:
            properties = {
                name: _json_type(value)
                for name, value in item.input_schema.items()
            }
            schema = {
                "type": "object",
                "properties": properties,
                "required": list(properties),
            }
        result.append(Tool(
            name=item.name,
            description=item.description,
            inputSchema=schema,
        ))
    return result


async def call_bridge(name: str, arguments: dict[str, Any]) -> CallToolResult:
    socket = os.environ.get(SOCKET_ENV, "")
    token = os.environ.get(TOKEN_ENV, "")
    session = os.environ.get(SESSION_ENV, "")
    if not socket or not token or not session:
        return CallToolResult(
            content=[TextContent(type="text", text="Kesha bridge is not configured")],
            isError=True,
        )

    connector = aiohttp.UnixConnector(path=socket)
    headers = {
        "X-Kesha-Bridge-Token": token,
        "X-Kesha-Bridge-Session": session,
    }
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as client:
            async with client.post(
                "http://localhost/tool",
                headers=headers,
                json={"tool": name, "args": arguments},
            ) as response:
                payload = await response.json()
                if response.status != 200:
                    return CallToolResult(
                        content=[TextContent(
                            type="text",
                            text=str(payload.get("error") or response.status),
                        )],
                        isError=True,
                    )
    except Exception as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Kesha bridge failed: {exc}")],
            isError=True,
        )

    result = payload.get("result") or {}
    content = [
        TextContent(type="text", text=str(item.get("text") or ""))
        for item in result.get("content") or []
        if item.get("type") == "text"
    ]
    if not content:
        content = [TextContent(type="text", text="Kesha tool returned no text")]
    return CallToolResult(content=content, isError=bool(result.get("is_error")))


server = Server("kesha", version="1.0.0")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return exposed_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    return await call_bridge(name, arguments)


async def run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(run)
