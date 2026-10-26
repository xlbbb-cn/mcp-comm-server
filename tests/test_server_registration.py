"""Verify the MCP server registers exactly the four expected tools."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from mcp_comm_server.server import mcp

EXPECTED = {"list_comm", "get_comm", "set_comm", "write_read_data_comm"}


def test_four_tools_are_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED


def test_tool_descriptions_and_schemas():
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    for name in EXPECTED:
        assert by_name[name].description, f"{name} has no description"
        assert by_name[name].input_schema.get("type") == "object"
        assert by_name[name].output_schema, f"{name} has no output schema"

    set_props = by_name["set_comm"].input_schema["properties"]
    assert "port" in set_props
    assert {"baudrate", "parity", "connect", "close"} <= set(set_props)

    read_props = by_name["write_read_data_comm"].input_schema["properties"]
    assert "port" in read_props
    assert "data" in read_props
    assert read_props["data_format"]["enum"] == ["str", "text", "ascii", "hex", "base64"]

    output_props = by_name["write_read_data_comm"].output_schema["properties"]
    assert {
        "status",
        "ok",
        "code",
        "message",
        "data",
        "data_hex",
        "bytes_read",
        "response_length",
    } <= set(output_props)


def test_required_arguments_are_advertised():
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert by_name["set_comm"].input_schema.get("required") == ["port"]
    assert set(by_name["write_read_data_comm"].input_schema.get("required", [])) == {
        "port",
        "data",
    }
    assert not by_name["list_comm"].input_schema.get("required")
