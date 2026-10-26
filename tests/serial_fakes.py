"""Minimal pyserial-compatible fake devices for fault-injection tests.

The real ``loop://`` backend is used for happy-path integration tests; these
fakes exist to exercise error paths that a loopback cannot produce (close
failures, half-open handles, devices that never reply).
"""

from __future__ import annotations

import serial


class FakeSerial:
    """A tiny in-memory serial device with injectable failures."""

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
        **_: object,
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
        self.close_error: BaseException | None = None
        self.write_error: BaseException | None = None
        self.read_error: BaseException | None = None

    @property
    def in_waiting(self) -> int:
        return len(self.input)

    def read(self, size: int = 1) -> bytes:
        # Record the timeout pyserial would have used.  A None value here is
        # exactly what makes a real device block forever.
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


class HalfOpenSerial(FakeSerial):
    """A handle that is constructed but fails while opening."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.is_open = False

    def open(self) -> None:
        raise RuntimeError("open failed after construction")

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    """Callable factory compatible with ``SerialManager(serial_factory=...)``."""

    def __init__(self) -> None:
        self.created: list[FakeSerial] = []

    def __call__(self, port: str, **kwargs: object) -> FakeSerial:
        device = FakeSerial(port, **kwargs)  # type: ignore[arg-type]
        self.created.append(device)
        return device

    def last(self) -> FakeSerial:
        assert self.created, "no fake serial device was created"
        return self.created[-1]
