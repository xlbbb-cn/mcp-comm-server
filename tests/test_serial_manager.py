"""Unit tests for :mod:`mcp_comm_server.core.serial_manager`.

No hardware is required: the tests inject a fake pyserial-compatible device
through ``SerialManager(serial_factory=..., port_enumerator=...)``. One test
also exercises the real pyserial ``loop://`` URL handler end to end.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

serial = pytest.importorskip("serial")

from mcp_comm_server.core.serial import (
    DEFAULT_MAX_READ_BYTES,
    DEFAULT_MAX_READ_TIMEOUT,
    ErrorCode,
    SerialManager,
    get_serial_manager,
    reset_serial_manager,
)


# --------------------------------------------------------------------- fakes
class FakeSerial:
    """Minimal pyserial-compatible device used by the tests."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 9600,
        bytesize: int = serial.EIGHTBITS,
        parity: str = serial.PARITY_NONE,
        stopbits: float = serial.STOPBITS_ONE,
        timeout: float | None = 1.0,
        write_timeout: float | None = None,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        **kwargs: object,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.xonxoff = xonxoff
        self.rtscts = rtscts
        self.dsrdtr = dsrdtr
        self.is_open = True
        self.closed = False
        self.input = bytearray()
        self.written = bytearray()
        self.auto_reply: bytes | None = None
        self.read_timeouts: list[float | None] = []
        self.write_error: BaseException | None = None
        self.read_error: BaseException | None = None
        self.close_error: BaseException | None = None

    @property
    def in_waiting(self) -> int:
        return len(self.input)

    def read(self, size: int = 1) -> bytes:
        # Record the timeout pyserial would have used: None means "block
        # forever", which the manager must never pass through.
        self.read_timeouts.append(self.timeout)
        if self.read_error is not None:
            raise self.read_error
        if not self.input:
            return b""
        size = max(int(size), 1)
        chunk = bytes(self.input[:size])
        del self.input[:size]
        return chunk

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.written.extend(data)
        if self.auto_reply:
            self.input.extend(self.auto_reply)
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        self.input.clear()

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.is_open = False
        self.closed = True


class FakeSerialFactory:
    """Callable factory that creates :class:`FakeSerial` instances."""

    def __init__(self) -> None:
        self.created: list[FakeSerial] = []
        self.failures: dict[str, BaseException] = {}
        self.preload: dict[str, bytes] = {}
        self.auto_replies: dict[str, bytes] = {}

    def __call__(self, port: str, **kwargs: object) -> FakeSerial:
        if port in self.failures:
            raise self.failures[port]
        device = FakeSerial(port, **kwargs)  # type: ignore[arg-type]
        device.input.extend(self.preload.pop(port, b""))
        device.auto_reply = self.auto_replies.get(port)
        self.created.append(device)
        return device

    def last(self) -> FakeSerial:
        assert self.created, "no fake serial device was created"
        return self.created[-1]


def fake_ports() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            device="COM1",
            name="COM1",
            description="USB Serial Port",
            hwid="USB VID:PID=1234:5678",
            vid=0x1234,
            pid=0x5678,
            serial_number="SN1",
            manufacturer="Fake Inc",
            product="Fake USB Serial",
            interface=None,
            location="1-1",
        ),
        SimpleNamespace(
            device="COM2",
            name="COM2",
            description="Bluetooth Serial",
            hwid="BTHENUM",
            vid=None,
            pid=None,
            serial_number=None,
            manufacturer=None,
            product=None,
            interface=None,
            location=None,
        ),
    ]


@pytest.fixture
def factory() -> FakeSerialFactory:
    return FakeSerialFactory()


@pytest.fixture
def manager(factory: FakeSerialFactory) -> SerialManager:
    instance = SerialManager(serial_factory=factory, port_enumerator=fake_ports)
    yield instance
    instance.reset()


# ------------------------------------------------------------- enumeration
def test_list_ports_returns_plain_dicts(manager: SerialManager) -> None:
    result = manager.list_ports()

    assert result["status"] == "ok"
    assert result["ok"] is True
    assert result["count"] == 2
    ports = {item["device"]: item for item in result["ports"]}
    assert ports["COM1"]["description"] == "USB Serial Port"
    assert ports["COM1"]["vid"] == 0x1234
    assert ports["COM1"]["is_open"] is False


def test_list_ports_marks_open_ports(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")

    ports = {item["device"]: item for item in manager.list_ports()["ports"]}
    assert ports["COM1"]["is_open"] is True
    assert ports["COM2"]["is_open"] is False


def test_list_ports_can_omit_open_state(manager: SerialManager) -> None:
    result = manager.list_ports(include_open=False)

    assert result["status"] == "ok"
    assert all("is_open" not in item for item in result["ports"])


def test_list_ports_failure_is_structured(factory: FakeSerialFactory) -> None:
    def broken_enumerator() -> list[object]:
        raise OSError("driver exploded")

    manager = SerialManager(serial_factory=factory, port_enumerator=broken_enumerator)
    result = manager.list_ports()

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.LIST_PORTS_FAILED
    assert "driver exploded" in result["message"]
    assert "next_step" in result


# ------------------------------------------------------------------ opening
def test_open_read_params_and_close(manager: SerialManager, factory: FakeSerialFactory) -> None:
    opened = manager.open(
        "COM1",
        baudrate=115200,
        bytesize=7,
        parity="even",
        stopbits=1.5,
        timeout=0.25,
        write_timeout=0.5,
    )
    assert opened["status"] == "ok"
    assert opened["is_open"] is True
    assert opened["reused"] is False

    device = factory.last()
    assert device.baudrate == 115200
    assert device.bytesize == serial.SEVENBITS
    assert device.parity == serial.PARITY_EVEN
    assert device.stopbits == serial.STOPBITS_ONE_POINT_FIVE

    params = manager.get_params("COM1")
    assert params["status"] == "ok"
    assert params["is_open"] is True
    assert params["source"] == "live"
    assert params["settings"] == {
        "baudrate": 115200,
        "bytesize": 7,
        "parity": "E",
        "stopbits": 1.5,
        "timeout": 0.25,
        "write_timeout": 0.5,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }

    closed = manager.close("COM1")
    assert closed["status"] == "ok"
    assert closed["closed"] is True
    assert manager.is_open("COM1") is False
    assert device.closed is True

    # Parameters survive a close and are reused when the port is reopened.
    after_close = manager.get_params("COM1")
    assert after_close["is_open"] is False
    assert after_close["source"] == "stored"
    assert after_close["settings"]["baudrate"] == 115200


def test_open_reuses_existing_connection_and_applies_new_params(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    first = manager.open("COM1", baudrate=9600)
    second = manager.open("COM1", baudrate=57600)

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert second["reused"] is True
    assert len(factory.created) == 1
    assert factory.last().baudrate == 57600


def test_close_is_idempotent(manager: SerialManager) -> None:
    manager.open("COM1")
    assert manager.close("COM1")["closed"] is True
    again = manager.close("COM1")
    assert again["status"] == "ok"
    assert again["closed"] is False


def test_close_all(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    manager.open("COM2")

    result = manager.close_all()
    assert result["status"] == "ok"
    assert result["closed_count"] == 2
    assert set(result["closed"]) == {"COM1", "COM2"}
    assert result["failed"] == []
    assert all(device.closed for device in factory.created)
    assert manager.list_connections()["count"] == 0


def test_open_missing_port_returns_port_not_found(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    factory.failures["COM9"] = serial.SerialException(
        "could not open port 'COM9': FileNotFoundError(2, "
        "'The system cannot find the file specified.', None, 2)"
    )

    result = manager.open("COM9")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.PORT_NOT_FOUND
    assert result["details"]["port"] == "COM9"
    assert "does not exist" in result["message"]


def test_open_busy_port_returns_port_busy(manager: SerialManager, factory: FakeSerialFactory) -> None:
    factory.failures["COM3"] = serial.SerialException(
        "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"
    )

    result = manager.open("COM3")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.PORT_BUSY
    assert "busy or access is denied" in result["message"]


def test_open_generic_failure_returns_open_failed(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    factory.failures["COM4"] = serial.SerialException("weird driver error")

    result = manager.open("COM4")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.OPEN_FAILED


def test_open_requires_a_port(manager: SerialManager) -> None:
    result = manager.open("   ")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PORT
    assert result["details"]["port"] == "   "


@pytest.mark.parametrize(
    "params",
    [
        {"baudrate": 0},
        {"baudrate": -1},
        {"baudrate": "fast"},
        {"baudrate": True},
        {"bytesize": 4},
        {"bytesize": 9},
        {"parity": "Z"},
        {"stopbits": 3},
        {"stopbits": True},
        {"timeout": -1},
        {"timeout": float("nan")},
        {"xonxoff": "maybe"},
        {"not_a_setting": 1},
    ],
)
def test_open_rejects_invalid_parameters(
    manager: SerialManager, factory: FakeSerialFactory, params: dict[str, object]
) -> None:
    result = manager.open("COM1", **params)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PARAMETER
    assert factory.created == []
    assert manager.is_open("COM1") is False


# --------------------------------------------------------------- parameters
def test_get_params_for_closed_port_returns_defaults(manager: SerialManager) -> None:
    result = manager.get_params("COM7")

    assert result["status"] == "ok"
    assert result["is_open"] is False
    assert result["source"] == "default"
    assert result["settings"]["baudrate"] == 9600
    assert result["settings"]["timeout"] == 1.0


def test_set_params_while_closed_is_used_on_next_open(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    stored = manager.set_params("COM1", baudrate=57600, parity="E", bytesize=7)
    assert stored["status"] == "ok"
    assert stored["is_open"] is False
    assert stored["applied"] is False

    params = manager.get_params("COM1")
    assert params["source"] == "stored"
    assert params["settings"]["baudrate"] == 57600

    assert manager.open("COM1")["status"] == "ok"
    device = factory.last()
    assert device.baudrate == 57600
    assert device.parity == serial.PARITY_EVEN
    assert device.bytesize == serial.SEVENBITS


def test_set_params_while_open_applies_to_device(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1", baudrate=9600, parity="N")
    result = manager.set_params("COM1", baudrate=230400, parity="O", stopbits=2, timeout=0.1)

    assert result["status"] == "ok"
    assert result["applied"] is True
    assert result["changed"] == ["baudrate", "parity", "stopbits", "timeout"]
    device = factory.last()
    assert device.baudrate == 230400
    assert device.parity == serial.PARITY_ODD
    assert device.stopbits == serial.STOPBITS_TWO
    assert device.timeout == 0.1


def test_set_params_partial_update_preserves_other_values(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1", baudrate=19200, bytesize=7, parity="E", stopbits=1.5)

    manager.set_params("COM1", timeout=0.5)

    settings = manager.get_params("COM1")["settings"]
    assert settings["timeout"] == 0.5
    assert settings["baudrate"] == 19200
    assert settings["bytesize"] == 7
    assert settings["parity"] == "E"
    assert settings["stopbits"] == 1.5
    assert factory.last().baudrate == 19200


def test_set_params_invalid_value_leaves_state_unchanged(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1", baudrate=9600, parity="N")

    result = manager.set_params("COM1", baudrate=0, parity="E")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PARAMETER
    settings = manager.get_params("COM1")["settings"]
    assert settings["baudrate"] == 9600
    assert settings["parity"] == "N"
    assert factory.last().baudrate == 9600


def test_set_params_with_unknown_key_is_rejected(manager: SerialManager) -> None:
    result = manager.set_params("COM1", speed=9600)  # type: ignore[call-arg]

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PARAMETER
    assert result["details"]["unknown"] == ["speed"]


def test_friendly_parameter_aliases_are_accepted(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    result = manager.open("COM1", baud=57600, databits=7, stop_bits=2)

    assert result["status"] == "ok"
    settings = result["settings"]
    assert settings["baudrate"] == 57600
    assert settings["bytesize"] == 7
    assert settings["stopbits"] == 2.0
    assert factory.last().baudrate == 57600


def test_defaults_apply_to_new_ports(manager: SerialManager, factory: FakeSerialFactory) -> None:
    changed = manager.set_defaults(baudrate=57600, timeout=0.2)
    assert changed["status"] == "ok"
    assert manager.get_defaults()["settings"]["baudrate"] == 57600

    manager.open("COM2")
    device = factory.last()
    assert device.baudrate == 57600
    assert device.timeout == 0.2


# --------------------------------------------------------------------- I/O
def test_write_requires_open_port(manager: SerialManager) -> None:
    result = manager.write("COM1", b"PING")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.PORT_NOT_OPEN
    assert result["details"]["operation"] == "write"


def test_write_returns_count_and_payload(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")

    result = manager.write("COM1", "AT\r\n")

    assert result["status"] == "ok"
    assert result["bytes_written"] == 4
    assert result["data"] == b"AT\r\n"
    assert result["data_hex"] == "41540d0a"
    assert factory.last().written == b"AT\r\n"


def test_write_rejects_empty_payload(manager: SerialManager) -> None:
    manager.open("COM1")

    result = manager.write("COM1", b"")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_DATA


def test_write_rejects_unsupported_payload_type(manager: SerialManager) -> None:
    manager.open("COM1")

    result = manager.write("COM1", 123)  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_DATA


def test_write_timeout_is_structured(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().write_error = serial.SerialTimeoutException("timed out")

    result = manager.write("COM1", b"PING", timeout=0.01)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.WRITE_TIMEOUT
    assert result["details"]["bytes_attempted"] == 4


def test_write_failure_is_structured(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().write_error = serial.SerialException("cable unplugged")

    result = manager.write("COM1", b"PING")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.WRITE_FAILED
    assert "cable unplugged" in result["message"]


def test_read_by_size(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().input.extend(b"HELLO")

    result = manager.read("COM1", size=5, timeout=0.1)

    assert result["status"] == "ok"
    assert result["data"] == b"HELLO"
    assert result["bytes_read"] == 5
    assert result["data_text"] == "HELLO"
    assert result["complete"] is True
    assert result["timed_out"] is False


def test_read_stops_at_delimiter(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().input.extend(b"OK\r\nEXTRA")

    result = manager.read("COM1", delimiter="\r\n", timeout=0.1)

    assert result["status"] == "ok"
    assert result["data"] == b"OK\r\n"
    assert result["complete"] is True


def test_read_idle_mode_returns_available_bytes(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1")
    factory.last().input.extend(b"abc")

    result = manager.read("COM1", timeout=0.1)

    assert result["status"] == "ok"
    assert result["data"] == b"abc"
    assert result["complete"] is True
    assert result["timed_out"] is False


def test_read_timeout_when_no_data(manager: SerialManager) -> None:
    manager.open("COM1")

    result = manager.read("COM1", size=4, timeout=0.05)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.READ_TIMEOUT
    assert result["details"]["operation"] == "read"
    assert "next_step" in result


def test_read_partial_returns_received_data(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().input.extend(b"AB")

    result = manager.read("COM1", size=4, timeout=0.05)

    assert result["status"] == "ok"
    assert result["data"] == b"AB"
    assert result["complete"] is False
    assert result["timed_out"] is True


def test_read_requires_open_port(manager: SerialManager) -> None:
    result = manager.read("COM1", size=1)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.PORT_NOT_OPEN
    assert result["port"] == "COM1"


def test_read_respects_max_bytes(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().input.extend(b"abcdef")

    result = manager.read("COM1", max_bytes=3, timeout=0.05)

    assert result["status"] == "ok"
    assert result["data"] == b"abc"
    assert result["complete"] is False
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size": 0},
        {"size": -3},
        {"timeout": -1},
        {"delimiter": ""},
        {"size": 10, "max_bytes": 4},
    ],
)
def test_read_rejects_invalid_arguments(manager: SerialManager, kwargs: dict[str, object]) -> None:
    manager.open("COM1")

    result = manager.read("COM1", **kwargs)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_read_failure_is_structured(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().read_error = serial.SerialException("read exploded")

    result = manager.read("COM1", size=1)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.READ_FAILED


def test_write_read_roundtrip(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    factory.last().auto_reply = b"PONG"

    result = manager.write_read("COM1", b"PING", expected_bytes=4, read_timeout=0.1)

    assert result["status"] == "ok"
    assert result["bytes_written"] == 4
    assert result["written"] == b"PING"
    assert result["data"] == b"PONG"
    assert result["response"] == b"PONG"
    assert result["bytes_read"] == 4
    assert result["complete"] is True
    assert factory.last().written == b"PING"


def test_write_read_flushes_stale_input(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1")
    device = factory.last()
    device.input.extend(b"STALE")
    device.auto_reply = b"OK\r\n"

    result = manager.write_read("COM1", b"AT\r\n", delimiter="\r\n", read_timeout=0.1)

    assert result["status"] == "ok"
    assert result["response"] == b"OK\r\n"


def test_write_read_timeout_is_structured(manager: SerialManager) -> None:
    manager.open("COM1")

    result = manager.write_read("COM1", b"PING", expected_bytes=4, read_timeout=0.05)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.READ_TIMEOUT
    assert result["details"]["bytes_written"] == 4


def test_write_read_requires_open_port(manager: SerialManager) -> None:
    result = manager.write_read("COM1", b"PING")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.PORT_NOT_OPEN


# ---------------------------------------------------------- lifecycle / thread
def test_reset_closes_connections_and_clears_settings(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1", baudrate=57600)
    device = factory.last()

    manager.reset()

    assert device.closed is True
    assert manager.is_open("COM1") is False
    assert manager.get_params("COM1")["source"] == "default"
    assert manager.get_params("COM1")["settings"]["baudrate"] == 9600


def test_concurrent_opens_create_only_one_connection(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    results: list[dict[str, object]] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(manager.open("COM1", baudrate=115200))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 8
    assert all(result["status"] == "ok" for result in results)
    assert len(factory.created) == 1
    assert sum(1 for result in results if result["reused"]) == 7


def test_write_read_transactions_do_not_interleave(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1")
    factory.last().auto_reply = b"OK!!"
    results: list[dict[str, object]] = []

    def worker(index: int) -> None:
        results.append(
            manager.write_read(
                "COM1",
                f"REQ{index}",
                expected_bytes=4,
                read_timeout=0.1,
            )
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 10
    assert all(result["status"] == "ok" for result in results)
    assert all(result["response"] == b"OK!!" for result in results)


def test_global_manager_is_a_singleton() -> None:
    reset_serial_manager()
    try:
        assert get_serial_manager() is get_serial_manager()
    finally:
        reset_serial_manager()


# ------------------------------------------------------- regression / safety
class _HalfOpenSerial(FakeSerial):
    """Constructed but the explicit open() call fails."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.is_open = False
        self.closed = False

    def open(self) -> None:
        raise RuntimeError("open failed after construction")

    def close(self) -> None:
        self.closed = True


def test_failed_open_releases_the_partial_handle(manager: SerialManager) -> None:
    created: list[_HalfOpenSerial] = []

    def factory(port: str, **kwargs: object) -> _HalfOpenSerial:
        device = _HalfOpenSerial(port, **kwargs)  # type: ignore[arg-type]
        created.append(device)
        return device

    manager = SerialManager(serial_factory=factory)
    result = manager.open("COM1")

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.OPEN_FAILED
    assert created and created[0].closed is True


def test_close_failure_retains_the_handle_for_retry(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1")
    device = factory.last()
    device.close_error = RuntimeError("driver busy")

    failed = manager.close("COM1")

    assert failed["status"] == "error"
    assert failed["code"] == ErrorCode.CLOSE_FAILED
    assert failed["details"]["retained"] is True
    assert manager.is_open("COM1") is True
    assert device.is_open is True

    device.close_error = None
    assert manager.close("COM1")["closed"] is True
    assert device.closed is True


def test_read_caps_forever_timeout(manager: SerialManager, factory: FakeSerialFactory) -> None:
    manager.open("COM1", timeout=None)

    result = manager.read("COM1", size=4)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.READ_TIMEOUT
    assert result["details"]["timeout"] == DEFAULT_MAX_READ_TIMEOUT
    assert factory.last().read_timeouts == [DEFAULT_MAX_READ_TIMEOUT]


def test_write_read_caps_forever_timeout(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1", timeout=None)

    result = manager.write_read("COM1", b"PING", expected_bytes=4)

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.READ_TIMEOUT
    assert result["details"]["timeout"] == DEFAULT_MAX_READ_TIMEOUT
    assert factory.last().read_timeouts == [DEFAULT_MAX_READ_TIMEOUT]


def test_write_read_rejects_expected_bytes_above_the_hard_limit(
    manager: SerialManager, factory: FakeSerialFactory
) -> None:
    manager.open("COM1")

    result = manager.write_read(
        "COM1", b"PING", expected_bytes=DEFAULT_MAX_READ_BYTES + 1
    )

    assert result["status"] == "error"
    assert result["code"] == ErrorCode.INVALID_PARAMETER
    assert result["details"]["parameter"] == "expected_bytes"
    assert factory.last().written == b""


def test_reset_preserves_port_locks(manager: SerialManager) -> None:
    lock = manager._lock_for("COM1")

    manager.reset()

    # Replacing the lock while a thread might hold it would break the
    # per-port transaction guarantee.
    assert manager._lock_for("COM1") is lock


def test_loopback_with_real_pyserial() -> None:
    """Smoke test the real pyserial backend through the loop:// URL handler."""
    manager = SerialManager()
    try:
        opened = manager.open("loop://", baudrate=115200, timeout=0.2)
        assert opened["status"] == "ok", opened

        written = manager.write("loop://", b"PING")
        assert written["status"] == "ok", written
        assert written["bytes_written"] == 4

        read = manager.read("loop://", size=4, timeout=0.5)
        assert read["status"] == "ok", read
        assert read["data"] == b"PING"
    finally:
        manager.reset()
