"""Integration / fault-injection tests for the serial stack and MCP tools.

Coverage targets
----------------
* handle lifecycle: failed open releases a half-open handle; failed close keeps
  the handle retry-able instead of leaking it; open/close cycles do not grow
  the connection registry.
* timeouts: a port configured with pyserial's "block forever" timeout is still
  bounded, and oversized reads are rejected.
* concurrency: parallel tool calls on one port never interleave responses.
* tool contract: empty responses, invalid parameters and response formats all
  use one envelope and never leave a port open as a side effect.
"""

from __future__ import annotations

import threading

import pytest

serial = pytest.importorskip("serial")

from serial_fakes import FakeFactory, HalfOpenSerial

from mcp_comm_server.core.serial import (
    DEFAULT_MAX_READ_BYTES,
    DEFAULT_MAX_READ_TIMEOUT,
    ErrorCode,
    SerialManager,
)
from mcp_comm_server.tools import comm

LOOP = "loop://"


def _telemetry_manager(factory: FakeFactory) -> SerialManager:
    return SerialManager(serial_factory=factory)


# ------------------------------------------------------------ handle lifecycle
def test_failed_open_releases_the_partial_handle():
    created: list[HalfOpenSerial] = []

    def factory(port: str, **kwargs: object) -> HalfOpenSerial:
        device = HalfOpenSerial(port, **kwargs)  # type: ignore[arg-type]
        created.append(device)
        return device

    manager = SerialManager(serial_factory=factory)
    result = manager.open("COM1", timeout=0.1)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.OPEN_FAILED
    assert created and created[0].closed is True
    assert manager.is_open("COM1") is False


def test_close_failure_keeps_the_handle_for_a_retry():
    factory = FakeFactory()
    manager = SerialManager(serial_factory=factory)
    manager.open("COM1")
    device = factory.last()
    device.close_error = RuntimeError("driver busy")

    failed = manager.close("COM1")

    assert failed["status"] == "error"
    assert failed["code"] == ErrorCode.CLOSE_FAILED
    assert failed["details"]["retained"] is True
    # The handle is still cached and still open: not leaked.
    assert manager.is_open("COM1") is True
    assert device.is_open is True

    device.close_error = None
    retried = manager.close("COM1")
    assert retried["status"] == "ok"
    assert retried["closed"] is True
    assert device.closed is True
    assert manager.is_open("COM1") is False


def test_reset_keeps_port_locks_stable():
    """Dropping a lock another thread holds would break mutual exclusion."""
    manager = SerialManager(serial_factory=FakeFactory())
    lock = manager._lock_for("COM1")

    manager.reset()

    assert manager._lock_for("COM1") is lock


def test_many_open_close_cycles_do_not_grow_the_registry():
    comm.set_comm(port=LOOP, timeout=0.05)
    manager = comm.get_serial_manager()

    for _ in range(15):
        assert comm.set_comm(port=LOOP, timeout=0.05)["status"] == "ok"
        assert comm.set_comm(port=LOOP, close=True)["closed"] is True

    assert manager.list_connections()["count"] == 0
    assert manager._connections == {}


# -------------------------------------------------------------------- timeouts
def test_read_caps_pyserial_block_forever_timeout():
    factory = FakeFactory()
    manager = SerialManager(serial_factory=factory)
    manager.open("COM1", timeout=None)
    device = factory.last()

    result = manager.read("COM1", size=4)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.READ_TIMEOUT
    assert result["details"]["timeout"] == DEFAULT_MAX_READ_TIMEOUT
    assert device.read_timeouts == [DEFAULT_MAX_READ_TIMEOUT]


def test_read_rejects_expected_bytes_over_the_hard_limit():
    factory = FakeFactory()
    manager = SerialManager(serial_factory=factory)
    manager.open("COM1")

    result = manager.write_read(
        "COM1", b"PING", expected_bytes=DEFAULT_MAX_READ_BYTES + 1, read_timeout=0.01
    )

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PARAMETER
    assert result["details"]["parameter"] == "expected_bytes"
    # Nothing was written: validation happens before I/O.
    assert factory.last().written == b""


def test_write_read_timeout_uses_the_effective_cap():
    factory = FakeFactory()
    manager = SerialManager(serial_factory=factory)
    manager.open("COM1", timeout=None)

    result = manager.write_read("COM1", b"PING", expected_bytes=4)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.READ_TIMEOUT
    assert result["details"]["timeout"] == DEFAULT_MAX_READ_TIMEOUT
    assert factory.last().read_timeouts == [DEFAULT_MAX_READ_TIMEOUT]


# --------------------------------------------------------------- tool contract
def test_empty_response_is_a_success_envelope(monkeypatch):
    factory = FakeFactory()
    manager = _telemetry_manager(factory)
    monkeypatch.setattr(comm, "get_serial_manager", lambda: manager)

    result = comm.write_read_data_comm(
        port="COM1", data="PING", expected_bytes=4, read_timeout=0.05
    )

    assert result["status"] == "ok"
    assert result["ok"] is True
    assert "code" not in result
    assert result["empty_response"] is True
    assert result["bytes_written"] == 4
    assert result["bytes_read"] == 0
    assert result["response_length"] == 0
    assert result["response_text"] == ""
    assert result["response_hex"] == ""
    assert result["written_hex"] == b"PING".hex()
    assert result["complete"] is False
    assert result["timed_out"] is True
    assert result["read_timeout"] == pytest.approx(0.05)
    assert result["hint"]


def test_empty_response_honours_response_format(monkeypatch):
    manager = _telemetry_manager(FakeFactory())
    monkeypatch.setattr(comm, "get_serial_manager", lambda: manager)

    result = comm.write_read_data_comm(
        port="COM1",
        data="PING",
        expected_bytes=4,
        read_timeout=0.05,
        response_format="hex",
    )

    assert result["status"] == "ok"
    assert result["empty_response"] is True
    assert result["response_hex"] == ""
    assert "response_text" not in result


@pytest.mark.parametrize(
    "kwargs",
    [
        {"response_format": "bogus"},
        {"data_format": "binary"},
        {"expected_bytes": 0},
        {"expected_bytes": -1},
        {"expected_bytes": DEFAULT_MAX_READ_BYTES + 1},
        {"read_timeout": -0.1},
        {"read_timeout": float("nan")},
        {"write_timeout": float("inf")},
        {"delay_before_read": -1},
        {"baudrate": 0},
    ],
)
def test_invalid_arguments_are_rejected_before_opening(kwargs):
    result = comm.write_read_data_comm(port=LOOP, data="A", **kwargs)

    assert result["status"] == "error"
    assert result["ok"] is False
    assert result["code"] == ErrorCode.INVALID_PARAMETER
    assert result["message"]
    assert result["next_step"]
    # The port must not have been opened as a side effect.
    state = comm.get_comm(LOOP)
    assert state["is_open"] is False
    assert state["source"] == "default"


def test_invalid_port_is_reported_even_when_connect_is_disabled():
    result = comm.write_read_data_comm(port="  ", data="A", connect_if_closed=False)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PORT
    assert result["port"] == "  "


@pytest.mark.parametrize(
    "payload,fmt",
    [("!!!", "base64"), ("A0x1", "hex"), ("0x", "hex"), ("zz", "hex")],
)
def test_malformed_payloads_are_rejected(payload, fmt):
    result = comm.write_read_data_comm(port=LOOP, data=payload, data_format=fmt)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_DATA
    assert result["message"]


def test_hex_prefix_per_token_still_works():
    result = comm.write_read_data_comm(
        port=LOOP,
        data="0x41 0x42",
        data_format="hex",
        expected_bytes=2,
        read_timeout=0.3,
    )

    assert result["status"] == "ok"
    assert result["written_hex"] == "4142"
    assert result["data_hex"] == "4142"


def test_set_comm_close_stores_validated_parameters():
    stored = comm.set_comm(port=LOOP, baudrate=19200, connect=False)
    assert stored["status"] == "ok"

    closed = comm.set_comm(port=LOOP, baudrate=57600, close=True)

    assert closed["status"] == "ok"
    assert closed["closed"] is False  # it was never open
    assert closed["changed"] == ["baudrate"]
    assert closed["config"]["baudrate"] == 57600
    assert comm.get_comm(LOOP)["config"]["baudrate"] == 57600


def test_set_comm_close_rejects_invalid_parameters():
    result = comm.set_comm(port=LOOP, baudrate=-1, close=True)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PARAMETER
    state = comm.get_comm(LOOP)
    assert state["is_open"] is False
    assert state["source"] == "default"


# ----------------------------------------------------------------- concurrency
def test_parallel_tool_calls_do_not_interleave_responses():
    assert comm.set_comm(port=LOOP, timeout=0.2)["status"] == "ok"
    results: dict[int, dict[str, object]] = {}

    def worker(index: int) -> None:
        results[index] = comm.write_read_data_comm(
            port=LOOP,
            data=f"REQ{index:02d}",
            expected_bytes=5,
            read_timeout=0.5,
            flush_input=True,
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 12
    for index, result in results.items():
        expected = f"REQ{index:02d}"
        assert result["status"] == "ok", result
        assert result["response_text"] == expected
        assert result["data_hex"] == expected.encode().hex()
