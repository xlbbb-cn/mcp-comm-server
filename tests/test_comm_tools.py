"""Tests for the MCP tool layer built on the t2 serial core.

No hardware is required: pyserial's ``loop://`` URL handler echoes everything
written back on the same port, so the whole stack can be exercised in CI.
"""

from __future__ import annotations

import pytest

serial = pytest.importorskip("serial")

from mcp_comm_server.tools import comm
from mcp_comm_server.utils.serial_utils import (
    decode_escapes,
    decode_text,
    encode_payload,
    hex_dump,
    make_json_safe,
    parse_bytesize,
    parse_parity,
    parse_stopbits,
)

LOOP = "loop://"


# --------------------------------------------------------------- utils layer
def test_parse_helpers_map_pyserial_constants():
    assert parse_bytesize(7) == serial.SEVENBITS
    assert parse_bytesize("8") == serial.EIGHTBITS
    assert parse_parity("even") == serial.PARITY_EVEN
    assert parse_parity("S") == serial.PARITY_SPACE
    assert parse_stopbits(1.5) == serial.STOPBITS_ONE_POINT_FIVE
    assert parse_stopbits("2") == serial.STOPBITS_TWO


def test_parse_helpers_fall_back_to_defaults():
    assert parse_bytesize(4) == serial.EIGHTBITS
    assert parse_parity("bogus") == serial.PARITY_NONE
    assert parse_stopbits(3) == serial.STOPBITS_ONE


def test_encode_payload_formats():
    assert encode_payload("AT\\r\\n") == b"AT\r\n"
    assert encode_payload("AT\\r\\n", "str") == b"AT\r\n"
    assert encode_payload("OK", "ascii") == b"OK"
    assert encode_payload("41 54 0d 0a", "hex") == b"AT\r\n"
    assert encode_payload("41540d0a", "hex") == b"AT\r\n"
    assert encode_payload("QVQNCg==", "base64") == b"AT\r\n"
    with pytest.raises(ValueError):
        encode_payload("zz", "hex")
    with pytest.raises(ValueError):
        encode_payload("héllo", "ascii")


def test_decode_escapes_and_hex_dump():
    assert decode_escapes("a\\r\\nb\\t") == "a\r\nb\t"
    assert decode_escapes("\\x41") == "A"
    assert decode_escapes("plain") == "plain"
    assert hex_dump(b"AT\r\n") == "41 54 0D 0A"


def test_decode_escapes_keeps_malformed_sequences_literal():
    assert decode_escapes("\\x4") == "\\x4"
    assert decode_escapes("\\xZZ") == "\\xZZ"
    assert decode_escapes("\\") == "\\"


def test_hex_escape_keeps_the_exact_byte():
    # \xFF is one raw byte, not the UTF-8 encoding of U+00FF (c3 bf).
    assert encode_payload("\\xff") == b"\xff"
    assert encode_payload("\\xff", "text", encoding="utf-8") == b"\xff"
    assert encode_payload("\\xff", "ascii") == b"\xff"
    assert encode_payload("\\x00\\x41") == b"\x00A"


def test_hex_parser_strips_0x_only_at_token_start():
    assert encode_payload("0x41 0x42", "hex") == b"AB"
    assert encode_payload("0X41,0X42", "hex") == b"AB"
    assert encode_payload("41-42:43_44", "hex") == b"ABCD"
    # A literal "0x" in the middle is invalid, not silently removed.
    with pytest.raises(ValueError):
        encode_payload("A0x1", "hex")
    with pytest.raises(ValueError):
        encode_payload("0x", "hex")


def test_base64_rejects_garbage_instead_of_returning_empty_bytes():
    assert encode_payload("QVQNCg==", "base64") == b"AT\r\n"
    assert encode_payload("QVQN Cg==", "base64") == b"AT\r\n"  # whitespace ignored
    for bad in ("!!!", "not base64", "QVQ"):
        with pytest.raises(ValueError):
            encode_payload(bad, "base64")


def test_decode_text_never_raises_for_unknown_encoding():
    assert decode_text(b"abc", "no-such-encoding") == "abc"
    assert decode_text(b"\xff", "no-such-encoding") == "\ufffd"
    assert decode_text(b"\xff", "utf-8") == "\ufffd"
    assert decode_text(b"", "no-such-encoding") == ""


def test_make_json_safe_handles_memoryview_and_non_finite_floats():
    assert make_json_safe(memoryview(b"\x01\xab")) == "01ab"
    assert make_json_safe(float("nan")) == "nan"
    assert make_json_safe(float("inf")) == "inf"
    assert make_json_safe({"x": b"\xff", "y": [float("-inf")]}) == {
        "x": "ff",
        "y": ["-inf"],
    }


# --------------------------------------------------------------- tools layer
def test_list_comm_returns_ports_with_device_and_description():
    result = comm.list_comm()
    assert result["status"] == "ok"
    assert result["ok"] is True
    assert result["count"] == len(result["ports"])
    for entry in result["ports"]:
        assert "device" in entry
        assert "description" in entry
        assert "is_open" in entry


def test_set_comm_opens_and_get_comm_reports_state():
    opened = comm.set_comm(port=LOOP, baudrate=115200, parity="N", stopbits=1, timeout=0.2)
    assert opened["status"] == "ok"
    assert opened["ok"] is True
    assert opened["is_open"] is True
    assert opened["config"]["baudrate"] == 115200

    state = comm.get_comm(LOOP)
    assert state["status"] == "ok"
    assert state["port"] == LOOP
    assert state["is_open"] is True
    assert state["source"] == "live"
    assert state["config"]["baudrate"] == 115200
    assert state["in_waiting"] == 0


def test_get_comm_without_port_returns_defaults_and_connections():
    comm.set_comm(port=LOOP, timeout=0.1)
    state = comm.get_comm()
    assert state["status"] == "ok"
    assert state["open_count"] == 1
    assert state["connections"][0]["port"] == LOOP
    assert "baudrate" in state["defaults"]


def test_set_comm_without_connect_only_stores_config():
    result = comm.set_comm(port=LOOP, baudrate=19200, connect=False)
    assert result["status"] == "ok"
    assert result["is_open"] is False
    assert result["config"]["baudrate"] == 19200
    assert result["changed"] == ["baudrate"]

    state = comm.get_comm(LOOP)
    assert state["is_open"] is False
    assert state["source"] == "stored"
    assert state["config"]["baudrate"] == 19200


def test_set_comm_close_keeps_configuration():
    comm.set_comm(port=LOOP, baudrate=9600, timeout=0.1)
    closed = comm.set_comm(port=LOOP, close=True)
    assert closed["status"] == "ok"
    assert closed["closed"] is True
    assert closed["is_open"] is False
    assert closed["config"]["baudrate"] == 9600


def test_write_read_data_comm_text_mode():
    comm.set_comm(port=LOOP, baudrate=9600, timeout=0.2)
    result = comm.write_read_data_comm(
        port=LOOP, data="AT", append_terminator="\\r\\n", read_timeout=0.2
    )
    assert result["status"] == "ok"
    assert result["bytes_written"] == 4
    assert result["bytes_read"] == 4
    assert result["response_length"] == 4
    assert result["written_hex"] == "41540d0a"
    assert result["written_dump"] == "41 54 0D 0A"
    assert result["data"] == "AT\\r\\n"
    assert result["data_hex"] == "41540d0a"
    assert result["response_text"] == "AT\\r\\n"
    assert result["response_hex"] == "41540d0a"


def test_write_read_data_comm_ascii_mode():
    result = comm.write_read_data_comm(
        port=LOOP,
        data="PING\\r\\n",
        data_format="ascii",
        expected_bytes=6,
        read_timeout=0.2,
    )
    assert result["status"] == "ok"
    assert result["data_format"] == "ascii"
    assert result["bytes_read"] == 6
    assert result["data_hex"] == b"PING\r\n".hex()


def test_write_read_data_comm_hex_mode_and_delimiter():
    result = comm.write_read_data_comm(
        port=LOOP,
        data="01 03 00 00 00 0A",
        data_format="hex",
        expected_bytes=6,
        read_timeout=0.2,
    )
    assert result["status"] == "ok"
    assert result["written_hex"] == "01030000000a"
    assert result["data_hex"] == "01030000000a"
    assert result["bytes_read"] == 6


def test_write_read_data_comm_response_format_hex_only():
    result = comm.write_read_data_comm(
        port=LOOP, data="AB", response_format="hex", expected_bytes=2, read_timeout=0.2
    )
    assert result["status"] == "ok"
    assert result["response_hex"] == "4142"
    assert "response_text" not in result


def test_write_read_data_comm_reopens_closed_port_with_stored_config():
    comm.set_comm(port=LOOP, baudrate=57600, timeout=0.2, connect=False)
    result = comm.write_read_data_comm(port=LOOP, data="HI", expected_bytes=2, read_timeout=0.2)
    assert result["status"] == "ok"
    assert result["bytes_read"] == 2

    state = comm.get_comm(LOOP)
    assert state["is_open"] is True
    assert state["config"]["baudrate"] == 57600


def test_write_read_data_comm_connect_if_closed_false_reports_code():
    result = comm.write_read_data_comm(port=LOOP, data="A", connect_if_closed=False)
    assert result["status"] == "error"
    assert result["code"] == "PORT_NOT_OPEN"
    assert result["message"]


def test_write_read_data_comm_invalid_port_has_code_and_message():
    result = comm.write_read_data_comm(port="", data="AT")
    assert result["status"] == "error"
    assert result["ok"] is False
    assert result["code"] == "INVALID_PORT"
    assert result["message"]
    assert result["next_step"]


def test_write_read_data_comm_bad_hex_has_code():
    result = comm.write_read_data_comm(port=LOOP, data="not-hex-z", data_format="hex")
    assert result["status"] == "error"
    assert result["code"] == "INVALID_DATA"
    assert "hex" in result["message"].lower()


def test_write_read_data_comm_rejects_unsupported_format():
    result = comm.write_read_data_comm(port=LOOP, data="A", data_format="binary")  # type: ignore[arg-type]
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMETER"


def test_write_read_data_comm_unopenable_port_has_recovery_hint():
    result = comm.write_read_data_comm(port="COM_NOT_A_PORT_999", data="AT", read_timeout=0.1)
    assert result["status"] == "error"
    assert result["code"] in {"PORT_NOT_FOUND", "PORT_BUSY", "OPEN_FAILED"}
    assert result["message"]
    assert result["next_step"]


def test_set_comm_invalid_parameter_has_code():
    result = comm.set_comm(port=LOOP, baudrate=-1)
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMETER"
    assert result["message"]


# ---------------------------------------------------------- envelope contract
def test_all_tool_envelopes_are_consistent_and_json_safe():
    import json

    results = [
        comm.list_comm(),
        comm.get_comm(),
        comm.get_comm(LOOP),
        comm.set_comm(port=LOOP, timeout=0.1),
        comm.write_read_data_comm(port=LOOP, data="A", expected_bytes=1, read_timeout=0.2),
        comm.set_comm(port="", baudrate=-1),  # invalid port error
        comm.write_read_data_comm(port=LOOP, data="zz", data_format="hex"),  # data error
        comm.write_read_data_comm(port=LOOP, data="A", response_format="bad"),  # param error
    ]

    for result in results:
        assert result["status"] in {"ok", "error"}
        assert result["ok"] is (result["status"] == "ok")
        assert isinstance(result["ok"], bool)
        if result["status"] == "error":
            assert isinstance(result["code"], str) and result["code"]
            assert isinstance(result["message"], str) and result["message"]
            assert isinstance(result["next_step"], str) and result["next_step"]
        else:
            assert "code" not in result
        # The MCP SDK serialises the result to JSON; bytes would break it.
        json.dumps(result)
        bytes_fields = [key for key, value in result.items() if isinstance(value, bytes)]
        assert bytes_fields == []
