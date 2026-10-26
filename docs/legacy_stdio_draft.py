#!/usr/bin/env python3
"""
DEPRECATED DRAFT — not used and not importable with mcp 2.x.

Superseded by ``src/mcp_comm_server/`` (``python -m mcp_comm_server``).  Kept
only as a historical reference: the tools below are stubs that return empty
text, and they use the mcp 1.x low-level ``Server`` API.

MCP Server for serial communication.

Tools:
- get_comm
- set_comm
- list_comm
- write_read_data_comm
"""

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import serial.tools.list_ports

app = Server("mcp-comm-server")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_comm",
            description="Get current serial communication configuration",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="set_comm",
            description="Set serial communication configuration",
            inputSchema={
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Serial port name"},
                    "baudrate": {"type": "integer", "description": "Baud rate"},
                    "timeout": {"type": "number", "description": "Timeout in seconds"},
                },
                "required": ["port"],
            },
        ),
        Tool(
            name="list_comm",
            description="List available serial ports",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="write_read_data_comm",
            description="Write data to serial port and read response",
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "Data to write"},
                    "read_timeout": {"type": "number", "description": "Read timeout"},
                },
                "required": ["data"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "get_comm":
        return [TextContent(type="text", text="{}")]
    elif name == "set_comm":
        return [TextContent(type="text", text="OK")]
    elif name == "list_comm":
        ports = [port.device for port in serial.tools.list_ports.comports()]
        return [TextContent(type="text", text="\n".join(ports) if ports else "No ports found")]
    elif name == "write_read_data_comm":
        return [TextContent(type="text", text="")]
    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
