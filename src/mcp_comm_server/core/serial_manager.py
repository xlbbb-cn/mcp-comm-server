"""Thread-safe serial (COM / tty) core layer built on pyserial.

The module follows the t1 architecture rules:

* it owns serial domain logic only and never imports an MCP/transport layer;
* every public method returns a plain, JSON-friendly dict instead of raising;
* successes use ``{"status": "ok", "ok": True, ...}``;
* failures use ``{"status": "error", "ok": False, "code": ..., "message": ...,
  "next_step": ..., "details": {...}}`` so callers can react programmatically;
* connections are cached per port name and guarded by a registry lock plus one
  transaction lock per port.

Port names may be real device names (``COM3``, ``/dev/ttyUSB0``) or any
pyserial URL (``loop://``, ``socket://host:port``, ``spy://COM3?file=log``).
The ``serial_factory`` / ``port_enumerator`` constructor arguments make the
whole layer testable without hardware.

Typical use::

    from mcp_comm_server.core.serial import get_serial_manager

    manager = get_serial_manager()
    manager.list_ports()
    manager.open("COM3", baudrate=115200, timeout=0.5)
    manager.write("COM3", b"PING\\r\\n")
    manager.read("COM3", size=4)
    manager.set_params("COM3", parity="E", stopbits=2)
    manager.close("COM3")
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_READ_TIMEOUT",
    "ErrorCode",
    "SerialManager",
    "SerialManagerError",
    "SerialSettings",
    "get_serial_manager",
    "reset_serial_manager",
]

#: Hard upper bound for a single read, to avoid unbounded buffering.
DEFAULT_MAX_READ_BYTES = 1024 * 1024

#: Hard upper bound for a single read wait.  ``timeout=None`` in pyserial
#: means "block forever"; a tool call must never do that while holding the
#: per-port transaction lock, so every read is capped at this many seconds.
DEFAULT_MAX_READ_TIMEOUT = 30.0

_BYTESIZE_VALUES = (5, 6, 7, 8)
_STOPBITS_VALUES = (1.0, 1.5, 2.0)

_BYTESIZE_TO_SERIAL = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}

_PARITY_TO_SERIAL = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}

_STOPBITS_TO_SERIAL = {
    1.0: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2.0: serial.STOPBITS_TWO,
}

_PARITY_ALIASES = {
    "N": "N",
    "NONE": "N",
    "NO": "N",
    "E": "E",
    "EVEN": "E",
    "O": "O",
    "ODD": "O",
    "M": "M",
    "MARK": "M",
    "S": "S",
    "SPACE": "S",
}

_SETTING_NAMES = (
    "baudrate",
    "bytesize",
    "parity",
    "stopbits",
    "timeout",
    "write_timeout",
    "xonxoff",
    "rtscts",
    "dsrdtr",
)

# Friendly aliases accepted in addition to the canonical pyserial names.
_SETTING_ALIASES = {
    "baud": "baudrate",
    "baud_rate": "baudrate",
    "databits": "bytesize",
    "data_bits": "bytesize",
    "stop_bits": "stopbits",
    "read_timeout": "timeout",
}


class ErrorCode(StrEnum):
    """Machine readable error codes returned in the error envelope."""

    INVALID_PORT = "INVALID_PORT"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    INVALID_DATA = "INVALID_DATA"
    PORT_NOT_FOUND = "PORT_NOT_FOUND"
    PORT_BUSY = "PORT_BUSY"
    PORT_NOT_OPEN = "PORT_NOT_OPEN"
    OPEN_FAILED = "OPEN_FAILED"
    CLOSE_FAILED = "CLOSE_FAILED"
    APPLY_FAILED = "APPLY_FAILED"
    LIST_PORTS_FAILED = "LIST_PORTS_FAILED"
    READ_TIMEOUT = "READ_TIMEOUT"
    WRITE_TIMEOUT = "WRITE_TIMEOUT"
    READ_FAILED = "READ_FAILED"
    WRITE_FAILED = "WRITE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_NEXT_STEPS: dict[str, str] = {
    ErrorCode.INVALID_PORT.value: (
        "pass a non-empty port name such as 'COM3' or '/dev/ttyUSB0'; "
        "use list_ports() to discover valid names"
    ),
    ErrorCode.INVALID_PARAMETER.value: (
        "use baudrate > 0, bytesize in 5/6/7/8, parity N/E/O/M/S, "
        "stopbits 1/1.5/2 and timeout >= 0"
    ),
    ErrorCode.INVALID_DATA.value: (
        "pass bytes/bytearray or a str that can be encoded with the requested encoding"
    ),
    ErrorCode.PORT_NOT_FOUND.value: (
        "check the port name with list_ports() and verify the USB/serial device is connected"
    ),
    ErrorCode.PORT_BUSY.value: (
        "close the other program that holds the port (or the previous connection) and retry"
    ),
    ErrorCode.PORT_NOT_OPEN.value: "call open(port, ...) first",
    ErrorCode.OPEN_FAILED.value: (
        "verify the port name, driver installation and USB/serial cabling, then retry"
    ),
    ErrorCode.CLOSE_FAILED.value: "retry close(); if it keeps failing, restart the process",
    ErrorCode.APPLY_FAILED.value: (
        "re-open the port so the new parameters are applied to a fresh handle"
    ),
    ErrorCode.LIST_PORTS_FAILED.value: "check the serial driver installation and retry",
    ErrorCode.READ_TIMEOUT.value: (
        "increase timeout, verify wiring (TX/RX) and baudrate/parity, "
        "or make sure the device is powered and actually replies"
    ),
    ErrorCode.WRITE_TIMEOUT.value: (
        "the device is not draining its input; check flow control (rtscts/xonxoff) "
        "or increase write_timeout"
    ),
    ErrorCode.READ_FAILED.value: "the port may have been unplugged; close it and re-open it",
    ErrorCode.WRITE_FAILED.value: "the port may have been unplugged; close it and re-open it",
    ErrorCode.INTERNAL_ERROR.value: "retry once; if it persists check the application logs",
}


class SerialManagerError(Exception):
    """Internal exception carrying a structured error payload.

    Public methods catch it and convert it to a result dict, so callers never
    have to handle pyserial exceptions themselves.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        next_step: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code) if not isinstance(code, ErrorCode) else code
        self.message = message
        self.details = dict(details or {})
        self.next_step = next_step

    def to_result(self) -> dict[str, Any]:
        return _error(
            self.code,
            self.message,
            details=self.details,
            next_step=self.next_step,
        )


@dataclass(frozen=True, slots=True)
class SerialSettings:
    """Validated serial parameters.

    ``timeout=None`` means "block forever", exactly like pyserial.
    """

    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    timeout: float | None = 1.0
    write_timeout: float | None = None
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": self.stopbits,
            "timeout": self.timeout,
            "write_timeout": self.write_timeout,
            "xonxoff": self.xonxoff,
            "rtscts": self.rtscts,
            "dsrdtr": self.dsrdtr,
        }

    def to_serial_kwargs(self) -> dict[str, Any]:
        return {
            "baudrate": self.baudrate,
            "bytesize": _BYTESIZE_TO_SERIAL[self.bytesize],
            "parity": _PARITY_TO_SERIAL[self.parity],
            "stopbits": _STOPBITS_TO_SERIAL[self.stopbits],
            "timeout": self.timeout,
            "write_timeout": self.write_timeout,
            "xonxoff": self.xonxoff,
            "rtscts": self.rtscts,
            "dsrdtr": self.dsrdtr,
        }


@dataclass(slots=True)
class _ReadOutcome:
    data: bytes
    timed_out: bool = False
    complete: bool = True
    truncated: bool = False
    timeout_used: float | None = None


# --------------------------------------------------------------------- helpers
def _ok(**payload: Any) -> dict[str, Any]:
    return {"status": "ok", "ok": True, **payload}


def _error(
    code: ErrorCode | str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    next_step: str | None = None,
) -> dict[str, Any]:
    code_text = str(code)
    result: dict[str, Any] = {
        "status": "error",
        "ok": False,
        "code": code_text,
        "message": message,
    }
    hint = next_step or _NEXT_STEPS.get(code_text)
    if hint:
        result["next_step"] = hint
    if details:
        result["details"] = dict(details)
        # The t1 contract also exposes the port at the top level of an error.
        if details.get("port") is not None:
            result["port"] = details["port"]
    return result


def _normalize_port(port: Any) -> str:
    if not isinstance(port, str) or not port.strip():
        raise SerialManagerError(
            ErrorCode.INVALID_PORT,
            "port must be a non-empty string (for example 'COM3' or '/dev/ttyUSB0')",
            details={"port": port},
        )
    return port.strip()


def _invalid_parameter(name: str, value: Any, reason: str) -> SerialManagerError:
    return SerialManagerError(
        ErrorCode.INVALID_PARAMETER,
        f"invalid value for {name}: {reason}",
        details={"parameter": name, "value": value, "reason": reason},
    )


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise _invalid_parameter(name, value, "must be a positive integer")
    if isinstance(value, float) and not value.is_integer():
        raise _invalid_parameter(name, value, "must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_parameter(name, value, "must be a positive integer") from exc
    if number <= 0:
        raise _invalid_parameter(name, value, "must be greater than 0")
    return number


def _normalize_parity(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid_parameter("parity", value, "must be one of N/E/O/M/S")
    parity = _PARITY_ALIASES.get(value.strip().upper())
    if parity is None:
        raise _invalid_parameter("parity", value, "must be one of N/E/O/M/S")
    return parity


def _normalize_stopbits(value: Any) -> float:
    if isinstance(value, bool):
        raise _invalid_parameter("stopbits", value, "must be 1, 1.5 or 2")
    try:
        stopbits = float(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_parameter("stopbits", value, "must be 1, 1.5 or 2") from exc
    if stopbits not in _STOPBITS_VALUES:
        raise _invalid_parameter("stopbits", value, "must be 1, 1.5 or 2")
    return stopbits


def _normalize_timeout(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _invalid_parameter(name, value, "must be None or a number >= 0")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_parameter(name, value, "must be None or a number >= 0") from exc
    if math.isnan(timeout) or math.isinf(timeout) or timeout < 0:
        raise _invalid_parameter(name, value, "must be None or a finite number >= 0")
    return timeout


def _positive_seconds(value: Any, name: str) -> float:
    """Validate a strictly positive, finite duration (used for caps)."""
    if value is None or isinstance(value, bool):
        raise _invalid_parameter(name, value, "must be a finite number > 0")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_parameter(name, value, "must be a finite number > 0") from exc
    if math.isnan(seconds) or math.isinf(seconds) or seconds <= 0:
        raise _invalid_parameter(name, value, "must be a finite number > 0")
    return seconds


def _normalize_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise _invalid_parameter(name, value, "must be a boolean")


def _validate_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize overrides; reject unknown keys.

    Only the keys present in ``params`` are returned, so callers can merge a
    partial update onto existing settings.
    """
    if not isinstance(params, Mapping):
        raise SerialManagerError(
            ErrorCode.INVALID_PARAMETER,
            "serial parameters must be passed as keyword arguments",
        )

    canonical: dict[str, Any] = {}
    for key, value in params.items():
        name = _SETTING_ALIASES.get(key, key)
        if name in canonical:
            raise SerialManagerError(
                ErrorCode.INVALID_PARAMETER,
                f"parameter {name!r} was supplied more than once",
                details={"parameter": name, "aliases": sorted(_SETTING_ALIASES)},
            )
        canonical[name] = value

    unknown = sorted(set(canonical) - set(_SETTING_NAMES))
    if unknown:
        raise SerialManagerError(
            ErrorCode.INVALID_PARAMETER,
            f"unknown serial parameter(s): {', '.join(unknown)}",
            details={"unknown": unknown, "allowed": list(_SETTING_NAMES)},
        )

    cleaned: dict[str, Any] = {}
    for name, value in canonical.items():
        if name == "baudrate":
            cleaned[name] = _positive_int(value, name)
        elif name == "bytesize":
            bytesize = _positive_int(value, name)
            if bytesize not in _BYTESIZE_VALUES:
                raise _invalid_parameter(name, value, "must be one of 5, 6, 7, 8")
            cleaned[name] = bytesize
        elif name == "parity":
            cleaned[name] = _normalize_parity(value)
        elif name == "stopbits":
            cleaned[name] = _normalize_stopbits(value)
        elif name in {"timeout", "write_timeout"}:
            cleaned[name] = _normalize_timeout(value, name)
        else:  # xonxoff / rtscts / dsrdtr
            cleaned[name] = _normalize_bool(value, name)
    return cleaned


def _encode_payload(data: Any, encoding: str) -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        try:
            return data.encode(encoding)
        except (LookupError, UnicodeEncodeError) as exc:
            raise SerialManagerError(
                ErrorCode.INVALID_DATA,
                f"cannot encode payload with {encoding!r}: {exc}",
                details={"encoding": encoding},
            ) from exc
    raise SerialManagerError(
        ErrorCode.INVALID_DATA,
        f"payload must be str or bytes, got {type(data).__name__}",
        details={"type": type(data).__name__},
    )


def _encode_delimiter(delimiter: str | bytes | bytearray | None, encoding: str) -> bytes | None:
    if delimiter is None:
        return None
    if isinstance(delimiter, str):
        try:
            encoded = delimiter.encode(encoding)
        except (LookupError, UnicodeEncodeError) as exc:
            raise SerialManagerError(
                ErrorCode.INVALID_PARAMETER,
                f"cannot encode delimiter with {encoding!r}: {exc}",
                details={"delimiter": delimiter, "encoding": encoding},
            ) from exc
    elif isinstance(delimiter, (bytes, bytearray)):
        encoded = bytes(delimiter)
    else:
        raise SerialManagerError(
            ErrorCode.INVALID_PARAMETER,
            "delimiter must be str or bytes",
            details={"type": type(delimiter).__name__},
        )
    if not encoded:
        raise SerialManagerError(
            ErrorCode.INVALID_PARAMETER,
            "delimiter must not be empty",
            details={"delimiter": delimiter},
        )
    return encoded


def _positive_size(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise _invalid_parameter(name, value, "must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_parameter(name, value, "must be a positive integer") from exc
    if number <= 0:
        raise _invalid_parameter(name, value, "must be greater than 0")
    return number


def _port_info(item: Any) -> dict[str, Any]:
    """Convert a ListPortInfo (or a mapping in tests) into a plain dict."""
    if isinstance(item, Mapping):
        get = item.get
    else:

        def get(name: str, default: Any = None) -> Any:
            return getattr(item, name, default)

    device = get("device") or get("name")
    return {
        "device": device,
        "name": get("name") or device,
        "description": get("description"),
        "hwid": get("hwid"),
        "vid": get("vid"),
        "pid": get("pid"),
        "serial_number": get("serial_number"),
        "manufacturer": get("manufacturer"),
        "product": get("product"),
        "interface": get("interface"),
        "location": get("location"),
    }


def _is_connection_open(conn: Any) -> bool:
    """Best-effort open check that also works with minimal mock devices."""
    if conn is None:
        return False
    try:
        value = getattr(conn, "is_open", None)
        return True if value is None else bool(value)
    except Exception:  # noqa: BLE001 - a broken backend must not crash the manager
        return False


def _parity_letter(value: Any) -> str:
    if isinstance(value, str):
        parity = _PARITY_ALIASES.get(value.strip().upper())
        if parity:
            return parity
    return "N"


def _default_serial_factory(port: str, **kwargs: Any) -> Any:
    if "://" in port:
        return serial.serial_for_url(port, **kwargs)
    return serial.Serial(port=port, **kwargs)


def _map_open_error(exc: Exception, port: str) -> dict[str, Any]:
    """Translate pyserial open failures into PORT_NOT_FOUND / PORT_BUSY / OPEN_FAILED."""
    text = str(exc)
    lowered = text.lower()
    details = {
        "port": port,
        "operation": "open",
        "exception": type(exc).__name__,
        "reason": text,
    }

    not_found_markers = (
        "filenotfounderror",
        "no such file or directory",
        "cannot find the file",
        "系统找不到指定的文件",
        "could not find",
    )
    busy_markers = (
        "permissionerror",
        "permission denied",
        "access is denied",
        "access denied",
        "拒绝访问",
        "being used by another",
        "already open",
        "in use",
        "busy",
    )
    if isinstance(exc, FileNotFoundError) or any(marker in lowered for marker in not_found_markers):
        return _error(
            ErrorCode.PORT_NOT_FOUND,
            f"serial port {port!r} does not exist: {text}",
            details=details,
        )
    if isinstance(exc, PermissionError) or any(marker in lowered for marker in busy_markers):
        return _error(
            ErrorCode.PORT_BUSY,
            f"serial port {port!r} is busy or access is denied: {text}",
            details=details,
        )
    return _error(
        ErrorCode.OPEN_FAILED,
        f"cannot open serial port {port!r}: {text}",
        details=details,
    )


# --------------------------------------------------------------------- manager
class SerialManager:
    """Thread-safe registry of serial connections keyed by port name.

    A single instance can be shared by every thread in the process. Registry
    state is protected by ``_registry_lock``; each port additionally has an
    ``RLock`` so a write/read transaction cannot interleave with another
    transaction or with ``close()`` on the same port.

    Parameters
    ----------
    serial_factory:
        Callable ``(port, **pyserial_kwargs) -> serial-like object``. Defaults
        to :func:`serial.Serial` (or :func:`serial.serial_for_url` for URL
        ports). Tests inject a fake.
    port_enumerator:
        Callable returning an iterable of ListPortInfo-like objects. Defaults
        to :func:`serial.tools.list_ports.comports`.
    """

    def __init__(
        self,
        serial_factory: Callable[..., Any] | None = None,
        *,
        port_enumerator: Callable[[], Iterable[Any]] | None = None,
    ) -> None:
        self._registry_lock = threading.RLock()
        self._connections: dict[str, Any] = {}
        self._settings: dict[str, SerialSettings] = {}
        self._port_locks: dict[str, threading.RLock] = {}
        self._default_settings = SerialSettings()
        self._serial_factory = serial_factory or _default_serial_factory
        self._port_enumerator = port_enumerator

    # ------------------------------------------------------------- enumeration
    def list_ports(self, include_open: bool = True) -> dict[str, Any]:
        """Enumerate the serial ports reported by the operating system.

        Returns the t1 envelope with ``count`` and ``ports``. Each entry has
        ``device``, ``description``, ``hwid``, ``vid``, ``pid``,
        ``serial_number``, ``manufacturer``, ``product``, ``interface``,
        ``location`` and (when ``include_open``) ``is_open``.
        """
        try:
            enumerator = self._port_enumerator or serial.tools.list_ports.comports
            raw_ports = list(enumerator())
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.exception("failed to enumerate serial ports")
            return _error(
                ErrorCode.LIST_PORTS_FAILED,
                f"failed to enumerate serial ports: {exc}",
                details={"exception": type(exc).__name__, "reason": str(exc)},
            )

        ports: list[dict[str, Any]] = []
        for item in raw_ports:
            info = _port_info(item)
            if include_open:
                info["is_open"] = self.is_open(info.get("device") or "")
            ports.append(info)
        return _ok(count=len(ports), ports=ports)

    # ------------------------------------------------------------- connections
    def is_open(self, port: str) -> bool:
        """Return whether ``port`` currently has a cached open connection."""
        try:
            name = _normalize_port(port)
        except SerialManagerError:
            return False
        with self._registry_lock:
            conn = self._connections.get(name)
            if conn is None:
                return False
            if _is_connection_open(conn):
                return True
            self._connections.pop(name, None)
            return False

    def get_connection(self, port: str) -> Any | None:
        """Return the live serial handle for ``port`` (advanced use / tests)."""
        try:
            name = _normalize_port(port)
        except SerialManagerError:
            return None
        with self._registry_lock:
            conn = self._connections.get(name)
            if conn is not None and _is_connection_open(conn):
                return conn
            if conn is not None:
                self._connections.pop(name, None)
            return None

    def list_connections(self) -> dict[str, Any]:
        """List every currently open connection with live parameters."""
        with self._registry_lock:
            ports = list(self._connections)
        items: list[dict[str, Any]] = []
        for port in ports:
            state = self.get_params(port)
            if state["status"] == "ok" and state.get("is_open"):
                items.append(
                    {
                        "port": port,
                        "settings": state["settings"],
                        "in_waiting": state.get("in_waiting", 0),
                    }
                )
        return _ok(count=len(items), connections=items)

    def open(self, port: str, **params: Any) -> dict[str, Any]:
        """Open ``port`` (or reuse it) and apply the given parameters.

        Parameters not supplied fall back to that port's stored settings, then
        to the manager defaults. Opening an already-open port applies the new
        settings to the existing handle and reports ``reused=True``.
        """
        try:
            name = _normalize_port(port)
            overrides = _validate_settings(params)
        except SerialManagerError as exc:
            return exc.to_result()

        try:
            with self._lock_for(name):
                with self._registry_lock:
                    conn = self._connections.get(name)
                    if conn is not None and not _is_connection_open(conn):
                        self._connections.pop(name, None)
                        conn = None
                    base = self._settings.get(name, self._default_settings)
                    settings = replace(base, **overrides)

                if conn is not None:
                    previous = self._settings.get(name)
                    try:
                        self._apply_settings(conn, settings)
                    except SerialManagerError as exc:
                        with self._registry_lock:
                            if previous is None:
                                self._settings.pop(name, None)
                            else:
                                self._settings[name] = previous
                        return exc.to_result()
                    with self._registry_lock:
                        self._settings[name] = settings
                    logger.debug("reused open serial connection on %s", name)
                    return self._open_result(name, conn, settings, reused=True)

                conn = None
                try:
                    conn = self._serial_factory(name, **settings.to_serial_kwargs())
                    if conn is None:
                        raise SerialManagerError(
                            ErrorCode.OPEN_FAILED,
                            f"serial factory returned no connection for {name!r}",
                            details={"port": name, "operation": "open"},
                        )
                    if not _is_connection_open(conn):
                        opener = getattr(conn, "open", None)
                        if callable(opener):
                            opener()
                    if not _is_connection_open(conn):
                        raise SerialManagerError(
                            ErrorCode.OPEN_FAILED,
                            f"serial port {name!r} did not open",
                            details={"port": name, "operation": "open"},
                        )
                except SerialManagerError as exc:
                    # The factory may have created a handle that is partially
                    # open; release it or the OS descriptor leaks.
                    self._close_quietly(conn, name)
                    return exc.to_result()
                except Exception as exc:  # noqa: BLE001 - pyserial raises many types
                    logger.warning("cannot open serial port %s: %s", name, exc)
                    self._close_quietly(conn, name)
                    return _map_open_error(exc, name)

                with self._registry_lock:
                    self._connections[name] = conn
                    self._settings[name] = settings
                logger.info("opened serial port %s with %s", name, settings.as_dict())
                return self._open_result(name, conn, settings, reused=False)
        except SerialManagerError as exc:
            return exc.to_result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected error while opening %s", name)
            return _error(
                ErrorCode.INTERNAL_ERROR,
                f"unexpected error while opening {name}: {exc}",
                details={"port": name, "operation": "open", "exception": type(exc).__name__},
            )

    def close(self, port: str | None = None) -> dict[str, Any]:
        """Close one port, or every open port when ``port`` is ``None``.

        Closing a port that is not open is idempotent and returns
        ``closed=False``. Stored parameters are kept.
        """
        if port is None:
            return self.close_all()

        try:
            name = _normalize_port(port)
        except SerialManagerError as exc:
            return exc.to_result()

        try:
            with self._lock_for(name):
                with self._registry_lock:
                    conn = self._connections.get(name)
                if conn is None:
                    return _ok(port=name, closed=False, is_open=False, message="port was not open")

                try:
                    conn.close()
                except Exception as exc:  # noqa: BLE001 - backend may raise anything
                    logger.warning("error while closing serial port %s: %s", name, exc)
                    # Keep the handle cached so a retry can still close it.
                    # Popping it before close() would leak the OS handle and
                    # make a second close() report "was not open".
                    retained = _is_connection_open(conn)
                    if not retained:
                        with self._registry_lock:
                            if self._connections.get(name) is conn:
                                self._connections.pop(name, None)
                    return _error(
                        ErrorCode.CLOSE_FAILED,
                        f"failed to close serial port {name!r}: {exc}",
                        details={
                            "port": name,
                            "operation": "close",
                            "exception": type(exc).__name__,
                            "reason": str(exc),
                            "retained": retained,
                        },
                    )

                # Only forget the handle once the backend really closed it.
                with self._registry_lock:
                    if self._connections.get(name) is conn:
                        self._connections.pop(name, None)
                logger.info("closed serial port %s", name)
                return _ok(port=name, closed=True, is_open=False)
        except SerialManagerError as exc:
            return exc.to_result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected error while closing %s", name)
            return _error(
                ErrorCode.INTERNAL_ERROR,
                f"unexpected error while closing {name}: {exc}",
                details={"port": name, "operation": "close", "exception": type(exc).__name__},
            )

    def close_all(self) -> dict[str, Any]:
        """Close every open connection; return what was closed and what failed."""
        with self._registry_lock:
            ports = list(self._connections)
        closed: list[str] = []
        failed: list[dict[str, Any]] = []
        for port in ports:
            result = self.close(port)
            if result["status"] == "ok" and result.get("closed"):
                closed.append(port)
            else:
                failed.append(
                    {
                        "port": port,
                        "code": result.get("code", ErrorCode.CLOSE_FAILED.value),
                        "message": result.get("message", "unknown error"),
                    }
                )
        return _ok(closed=closed, closed_count=len(closed), failed=failed, is_open=False)

    # ---------------------------------------------------------------- settings
    def get_params(self, port: str) -> dict[str, Any]:
        """Read the current parameters for ``port``.

        When the port is open the values are read from the live handle
        (``source="live"``); otherwise stored settings (``source="stored"``) or
        manager defaults (``source="default"``) are returned.
        """
        try:
            name = _normalize_port(port)
            with self._lock_for(name):
                conn = self.get_connection(name)
                if conn is not None:
                    live = self._read_settings(conn)
                    if live is not None:
                        values = live.as_dict()
                        return _ok(
                            port=name,
                            is_open=True,
                            source="live",
                            settings=values,
                            params=values,
                            config=values,
                            in_waiting=self._safe_in_waiting(conn),
                        )
                    logger.warning("could not read live settings from %s, using stored values", name)

                with self._registry_lock:
                    if name in self._settings:
                        stored = self._settings[name]
                        source = "stored"
                    else:
                        stored = self._default_settings
                        source = "default"
                values = stored.as_dict()
                return _ok(
                    port=name,
                    is_open=conn is not None,
                    source=source,
                    settings=values,
                    params=values,
                    config=values,
                    in_waiting=self._safe_in_waiting(conn) if conn is not None else 0,
                )
        except SerialManagerError as exc:
            return exc.to_result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected error while reading parameters for %s", port)
            return _error(
                ErrorCode.INTERNAL_ERROR,
                f"unexpected error while reading parameters for {port!r}: {exc}",
                details={"port": port, "operation": "get_params", "exception": type(exc).__name__},
            )

    # t1-compatible aliases
    def read_params(self, port: str) -> dict[str, Any]:
        """Alias of :meth:`get_params`."""
        return self.get_params(port)

    def describe(self, port: str) -> dict[str, Any]:
        """Alias of :meth:`get_params`."""
        return self.get_params(port)

    def set_params(self, port: str, **params: Any) -> dict[str, Any]:
        """Modify baudrate / bytesize / parity / stopbits / timeouts (and flow control).

        The update is validated as a whole before anything is changed. If the
        port is open the new parameters are applied to the live handle;
        otherwise they are stored and used the next time the port is opened.
        """
        try:
            name = _normalize_port(port)
            overrides = _validate_settings(params)
        except SerialManagerError as exc:
            return exc.to_result()

        try:
            with self._lock_for(name):
                conn = self.get_connection(name)
                with self._registry_lock:
                    base = self._settings.get(name, self._default_settings)
                if conn is not None:
                    live = self._read_settings(conn)
                    if live is not None:
                        base = live
                updated = replace(base, **overrides)

                if conn is not None:
                    try:
                        self._apply_settings(conn, updated)
                    except SerialManagerError as exc:
                        return exc.to_result()

                with self._registry_lock:
                    self._settings[name] = updated
                logger.info("updated serial parameters for %s: %s", name, overrides)
                values = updated.as_dict()
                return _ok(
                    port=name,
                    is_open=conn is not None,
                    applied=conn is not None,
                    settings=values,
                    params=values,
                    config=values,
                    changed=sorted(overrides),
                )
        except SerialManagerError as exc:
            return exc.to_result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected error while setting parameters for %s", name)
            return _error(
                ErrorCode.INTERNAL_ERROR,
                f"unexpected error while setting parameters for {name}: {exc}",
                details={"port": name, "operation": "set_params", "exception": type(exc).__name__},
            )

    # t1-compatible alias
    def save_config(self, port: str, **params: Any) -> dict[str, Any]:
        """Alias of :meth:`set_params` (stores settings without opening)."""
        return self.set_params(port, **params)

    def get_defaults(self) -> dict[str, Any]:
        """Return the parameters applied to newly opened ports."""
        with self._registry_lock:
            settings = self._default_settings
        values = settings.as_dict()
        return _ok(settings=values, defaults=values, config=values)

    def set_defaults(self, **params: Any) -> dict[str, Any]:
        """Update the defaults used for newly opened ports."""
        try:
            overrides = _validate_settings(params)
            with self._registry_lock:
                self._default_settings = replace(self._default_settings, **overrides)
                settings = self._default_settings
            values = settings.as_dict()
            return _ok(settings=values, defaults=values, config=values, changed=sorted(overrides))
        except SerialManagerError as exc:
            return exc.to_result()

    # ---------------------------------------------------------------- data I/O
    def write(
        self,
        port: str,
        data: str | bytes | bytearray | memoryview,
        *,
        encoding: str = "utf-8",
        flush: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Write bytes to an already-open port.

        ``timeout`` temporarily overrides the port's ``write_timeout`` for this
        call. Returns ``bytes_written`` plus the exact payload and hex form.
        """
        try:
            name = _normalize_port(port)
            payload = _encode_payload(data, encoding)
            if not payload:
                raise SerialManagerError(
                    ErrorCode.INVALID_DATA,
                    "nothing to write: payload is empty",
                    details={"port": name, "operation": "write"},
                )
            write_timeout = _normalize_timeout(timeout, "timeout") if timeout is not None else None
        except SerialManagerError as exc:
            return exc.to_result()

        try:
            with self._lock_for(name):
                conn = self.get_connection(name)
                if conn is None:
                    return self._not_open(name, "write")
                started = time.monotonic()
                written = self._write_payload(conn, payload, timeout=write_timeout, flush=flush)
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)
                return _ok(
                    port=name,
                    bytes_written=written,
                    data=payload,
                    data_hex=payload.hex(),
                    elapsed_ms=elapsed_ms,
                )
        except SerialManagerError as exc:
            exc.details.setdefault("port", name)
            exc.details.setdefault("operation", "write")
            return exc.to_result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected error while writing to %s", name)
            return _error(
                ErrorCode.INTERNAL_ERROR,
                f"unexpected error while writing to {name}: {exc}",
                details={"port": name, "operation": "write", "exception": type(exc).__name__},
            )

    def read(
        self,
        port: str,
        *,
        size: int | None = None,
        timeout: float | None = None,
        delimiter: str | bytes | bytearray | None = None,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
        max_read_timeout: float = DEFAULT_MAX_READ_TIMEOUT,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Read bytes from an already-open port.

        ``size`` reads up to N bytes, ``delimiter`` stops after the terminator
        (inclusive), and with neither the read ends when the line goes idle for
        ``timeout`` seconds. ``timeout`` defaults to the port's own timeout.

        Every read is bounded by ``max_read_timeout`` (default 30 s), including
        the case where the port was configured with ``timeout=None`` (pyserial's
        "block forever").  This prevents a tool call from holding the port lock
        indefinitely.

        A full/terminated read returns ``status="ok"``. A partial read returns
        the bytes received with ``timed_out=True`` and ``complete=False``. A
        read that receives nothing returns ``code="READ_TIMEOUT"``.
        """
        try:
            name = _normalize_port(port)
            if size is not None:
                size = _positive_size(size, "size")
            wait = _normalize_timeout(timeout, "timeout")
            delimiter_bytes = _encode_delimiter(delimiter, encoding)
            max_bytes_value = _positive_size(max_bytes, "max_bytes")
            wait_cap = _positive_seconds(max_read_timeout, "max_read_timeout")
            if size is not None and size > max_bytes_value:
                raise _invalid_parameter(
                    "size",
                    size,
                    f"must be <= max_bytes ({max_bytes_value})",
                )
        except SerialManagerError as exc:
            return exc.to_result()

        try:
            with self._lock_for(name):
                conn = self.get_connection(name)
                if conn is None:
                    return self._not_open(name, "read")
                started = time.monotonic()
                outcome = self._read_from_connection(
                    conn,
                    size=size,
                    timeout=wait,
                    delimiter=delimiter_bytes,
                    max_bytes=max_bytes_value,
                    max_read_timeout=wait_cap,
                )
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)
                return self._read_result(
                    name,
                    outcome,
                    elapsed_ms=elapsed_ms,
                    encoding=encoding,
                    requested_size=size,
                    delimiter=delimiter_bytes,
                    timeout=outcome.timeout_used,
                )
        except SerialManagerError as exc:
            exc.details.setdefault("port", name)
            exc.details.setdefault("operation", "read")
            return exc.to_result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected error while reading from %s", name)
            return _error(
                ErrorCode.INTERNAL_ERROR,
                f"unexpected error while reading from {name}: {exc}",
                details={"port": name, "operation": "read", "exception": type(exc).__name__},
            )

    def write_read(
        self,
        port: str,
        data: str | bytes | bytearray | memoryview,
        *,
        encoding: str = "utf-8",
        expected_bytes: int | None = None,
        delimiter: str | bytes | bytearray | None = None,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        delay_before_read: float = 0.0,
        flush_input: bool = True,
        max_read_timeout: float = DEFAULT_MAX_READ_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
    ) -> dict[str, Any]:
        """Write ``data`` and read the response as one atomic transaction.

        The per-port lock is held for the whole write/read cycle, so other
        threads cannot inject bytes between the request and the response.
        The read wait is capped by ``max_read_timeout`` even when the port was
        configured with pyserial's "block forever" timeout; ``max_bytes`` (and
        ``expected_bytes``) are bounded to prevent unbounded buffering.
        """
        try:
            name = _normalize_port(port)
            payload = _encode_payload(data, encoding)
            if not payload:
                raise SerialManagerError(
                    ErrorCode.INVALID_DATA,
                    "nothing to write: payload is empty",
                    details={"port": name, "operation": "write_read"},
                )
            if expected_bytes is not None:
                expected_bytes = _positive_size(expected_bytes, "expected_bytes")
            delimiter_bytes = _encode_delimiter(delimiter, encoding)
            wait = _normalize_timeout(read_timeout, "read_timeout")
            write_wait = _normalize_timeout(write_timeout, "write_timeout")
            delay = _normalize_timeout(delay_before_read, "delay_before_read") or 0.0
            wait_cap = _positive_seconds(max_read_timeout, "max_read_timeout")
            max_bytes_value = _positive_size(max_bytes, "max_bytes")
            if expected_bytes is not None and expected_bytes > max_bytes_value:
                raise _invalid_parameter(
                    "expected_bytes",
                    expected_bytes,
                    f"must be <= max_bytes ({max_bytes_value})",
                )
        except SerialManagerError as exc:
            return exc.to_result()

        try:
            with self._lock_for(name):
                conn = self.get_connection(name)
                if conn is None:
                    return self._not_open(name, "write_read")

                started = time.monotonic()
                if flush_input:
                    try:
                        conn.reset_input_buffer()
                    except Exception:  # noqa: BLE001  # pragma: no cover - backend dependent
                        logger.debug("reset_input_buffer unsupported on %s", name)

                written = self._write_payload(conn, payload, timeout=write_wait, flush=True)
                if delay > 0:
                    time.sleep(delay)

                outcome = self._read_from_connection(
                    conn,
                    size=expected_bytes,
                    timeout=wait,
                    delimiter=delimiter_bytes,
                    max_bytes=max_bytes_value,
                    max_read_timeout=wait_cap,
                )
                elapsed_ms = round((time.monotonic() - started) * 1000, 3)

                if not outcome.data:
                    return _error(
                        ErrorCode.READ_TIMEOUT,
                        f"no data received from {name!r} within the read timeout",
                        details={
                            "port": name,
                            "operation": "write_read",
                            "bytes_written": written,
                            "timeout": outcome.timeout_used,
                            "expected_bytes": expected_bytes,
                            "delimiter": delimiter_bytes,
                            "elapsed_ms": elapsed_ms,
                        },
                    )

                read_result = self._read_result(
                    name,
                    outcome,
                    elapsed_ms=elapsed_ms,
                    encoding=encoding,
                    requested_size=expected_bytes,
                    delimiter=delimiter_bytes,
                    timeout=outcome.timeout_used,
                )
                read_result["bytes_written"] = written
                read_result["written"] = payload
                read_result["written_hex"] = payload.hex()
                # A response alias keeps the API friendly for exchange-style callers.
                read_result["response"] = read_result["data"]
                return read_result
        except SerialManagerError as exc:
            exc.details.setdefault("port", name)
            exc.details.setdefault("operation", "write_read")
            return exc.to_result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected error during write_read on %s", name)
            return _error(
                ErrorCode.INTERNAL_ERROR,
                f"unexpected error during write_read on {name}: {exc}",
                details={"port": name, "operation": "write_read", "exception": type(exc).__name__},
            )

    # t1-compatible alias
    def write_and_read(self, port: str, data: Any, **kwargs: Any) -> dict[str, Any]:
        """Alias of :meth:`write_read`."""
        return self.write_read(port, data, **kwargs)

    def reset(self) -> None:
        """Close everything and forget all stored settings (tests / shutdown)."""
        self.close_all()
        # ``close_all`` can report failures; force-release whatever is left so
        # reset() never leaves an OS handle behind.  Port locks are deliberately
        # kept: dropping a lock another thread already holds would let a second
        # thread create a new lock for the same port and break mutual exclusion.
        with self._registry_lock:
            leftovers = list(self._connections.values())
            self._connections.clear()
        for conn in leftovers:
            self._close_quietly(conn, getattr(conn, "port", "?"))
        with self._registry_lock:
            self._settings.clear()
            self._default_settings = SerialSettings()

    # ---------------------------------------------------------------- internals
    def _lock_for(self, port: str) -> threading.RLock:
        with self._registry_lock:
            lock = self._port_locks.get(port)
            if lock is None:
                lock = threading.RLock()
                self._port_locks[port] = lock
            return lock

    def _open_result(
        self,
        port: str,
        conn: Any,
        settings: SerialSettings,
        *,
        reused: bool,
    ) -> dict[str, Any]:
        live = self._read_settings(conn) or settings
        values = live.as_dict()
        return _ok(
            port=port,
            is_open=True,
            reused=reused,
            settings=values,
            params=values,
            config=values,
            in_waiting=self._safe_in_waiting(conn),
        )

    def _not_open(self, port: str, operation: str) -> dict[str, Any]:
        return _error(
            ErrorCode.PORT_NOT_OPEN,
            f"serial port {port!r} is not open",
            details={"port": port, "operation": operation},
        )

    def _write_payload(
        self,
        conn: Any,
        payload: bytes,
        *,
        timeout: float | None,
        flush: bool,
    ) -> int:
        previous_timeout = getattr(conn, "write_timeout", None)
        if timeout is not None:
            try:
                conn.write_timeout = timeout
            except Exception:  # noqa: BLE001  # pragma: no cover - backend dependent
                logger.debug("could not set write_timeout on %s", getattr(conn, "port", "?"))
        try:
            written = conn.write(payload)
            if flush:
                flusher = getattr(conn, "flush", None)
                if callable(flusher):
                    flusher()
            return int(written if written is not None else len(payload))
        except serial.SerialTimeoutException as exc:
            raise SerialManagerError(
                ErrorCode.WRITE_TIMEOUT,
                f"write timed out after {timeout if timeout is not None else previous_timeout}s: {exc}",
                details={
                    "timeout": timeout if timeout is not None else previous_timeout,
                    "bytes_attempted": len(payload),
                },
            ) from exc
        except Exception as exc:
            self._forget_if_closed(conn)
            raise SerialManagerError(
                ErrorCode.WRITE_FAILED,
                f"write failed: {exc}",
                details={
                    "exception": type(exc).__name__,
                    "reason": str(exc),
                    "bytes_attempted": len(payload),
                },
            ) from exc
        finally:
            if timeout is not None:
                try:
                    conn.write_timeout = previous_timeout
                except Exception:  # noqa: BLE001  # pragma: no cover - backend dependent
                    logger.debug("could not restore write_timeout on %s", getattr(conn, "port", "?"))

    def _read_from_connection(
        self,
        conn: Any,
        *,
        size: int | None,
        timeout: float | None,
        delimiter: bytes | None,
        max_bytes: int,
        max_read_timeout: float = DEFAULT_MAX_READ_TIMEOUT,
    ) -> _ReadOutcome:
        # ``timeout=None`` means "use the port's own timeout"; port timeout None
        # means block forever in pyserial.  Cap the effective wait so a stalled
        # device can never hold the transaction lock indefinitely.
        if timeout is None:
            configured = getattr(conn, "timeout", None)
            wait = None if configured is None else float(configured)
        else:
            wait = timeout
        if wait is None:
            wait = max_read_timeout
        else:
            wait = min(wait, max_read_timeout)

        deadline = time.monotonic() + max(wait, 0.0)
        buffer = bytearray()
        previous_timeout = getattr(conn, "timeout", None)
        timed_out = False
        truncated = False
        idle_mode = size is None and delimiter is None

        try:
            while len(buffer) < max_bytes:
                if size is not None and len(buffer) >= size:
                    break
                if delimiter is not None and delimiter in buffer:
                    position = buffer.find(delimiter)
                    del buffer[position + len(delimiter) :]
                    break

                remaining = max_bytes - len(buffer)
                if size is not None:
                    wanted = min(size - len(buffer), remaining)
                else:
                    wanted = remaining
                available = self._safe_in_waiting(conn)
                if available > 0:
                    wanted = min(wanted, available)
                wanted = max(wanted, 1)

                left = deadline - time.monotonic()
                if left <= 0 and available <= 0:
                    timed_out = True
                    break
                # Keep a non-negative slice: with timeout=0 and buffered bytes
                # this still performs one non-blocking read.
                slice_timeout: float | None = max(left, 0.0)

                try:
                    conn.timeout = slice_timeout
                except Exception:  # noqa: BLE001  # pragma: no cover - backend dependent
                    logger.debug("could not set read timeout on %s", getattr(conn, "port", "?"))

                try:
                    chunk = conn.read(wanted)
                except serial.SerialTimeoutException:
                    timed_out = True
                    break
                except Exception as exc:
                    self._forget_if_closed(conn)
                    raise SerialManagerError(
                        ErrorCode.READ_FAILED,
                        f"read failed: {exc}",
                        details={"exception": type(exc).__name__, "reason": str(exc)},
                    ) from exc

                if not chunk:
                    timed_out = True
                    break
                buffer.extend(chunk)

        finally:
            try:
                conn.timeout = previous_timeout
            except Exception:  # noqa: BLE001  # pragma: no cover - backend dependent
                logger.debug("could not restore read timeout on %s", getattr(conn, "port", "?"))

        data = bytes(buffer)
        if size is not None and delimiter is not None:
            complete = len(data) >= size or delimiter in data
        elif size is not None:
            complete = len(data) >= size
        elif delimiter is not None:
            complete = delimiter in data
        else:
            complete = True
        if len(buffer) >= max_bytes and (idle_mode or not complete):
            truncated = True
            timed_out = True
        if idle_mode:
            # An idle-mode read ends when the line goes quiet; that is success,
            # not a timeout, as long as some bytes arrived.
            if data and not truncated:
                timed_out = False
            complete = not truncated
        if truncated:
            complete = False
        return _ReadOutcome(
            data=data,
            timed_out=timed_out,
            complete=complete,
            truncated=truncated,
            timeout_used=wait,
        )

    def _read_result(
        self,
        port: str,
        outcome: _ReadOutcome,
        *,
        elapsed_ms: float,
        encoding: str,
        requested_size: int | None,
        delimiter: bytes | None,
        timeout: float | None,
    ) -> dict[str, Any]:
        if not outcome.data:
            return _error(
                ErrorCode.READ_TIMEOUT,
                f"no data received from {port!r} within the read timeout",
                details={
                    "port": port,
                    "operation": "read",
                    "timeout": timeout,
                    "expected_bytes": requested_size,
                    "delimiter": delimiter,
                },
            )
        try:
            text = outcome.data.decode(encoding)
        except LookupError:
            text = outcome.data.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            text = outcome.data.decode(encoding, errors="replace")
        return _ok(
            port=port,
            data=outcome.data,
            data_hex=outcome.data.hex(),
            data_text=text,
            bytes_read=len(outcome.data),
            complete=outcome.complete,
            timed_out=outcome.timed_out,
            truncated=outcome.truncated,
            elapsed_ms=elapsed_ms,
            timeout=timeout,
            expected_bytes=requested_size,
            delimiter=delimiter,
        )

    def _apply_settings(self, conn: Any, settings: SerialSettings) -> None:
        try:
            conn.baudrate = settings.baudrate
            conn.bytesize = _BYTESIZE_TO_SERIAL[settings.bytesize]
            conn.parity = _PARITY_TO_SERIAL[settings.parity]
            conn.stopbits = _STOPBITS_TO_SERIAL[settings.stopbits]
            conn.timeout = settings.timeout
            conn.write_timeout = settings.write_timeout
            conn.xonxoff = settings.xonxoff
            conn.rtscts = settings.rtscts
            conn.dsrdtr = settings.dsrdtr
        except Exception as exc:
            raise SerialManagerError(
                ErrorCode.APPLY_FAILED,
                f"cannot apply settings to {getattr(conn, 'port', '?')!r}: {exc}",
                details={
                    "port": getattr(conn, "port", None),
                    "settings": settings.as_dict(),
                    "exception": type(exc).__name__,
                    "reason": str(exc),
                },
            ) from exc

    def _read_settings(self, conn: Any) -> SerialSettings | None:
        try:
            bytesize = int(conn.bytesize)
            if bytesize not in _BYTESIZE_VALUES:
                return None
            stopbits = float(conn.stopbits)
            if stopbits not in _STOPBITS_VALUES:
                return None
            timeout = conn.timeout
            if timeout is not None:
                timeout = float(timeout)
                if math.isnan(timeout) or math.isinf(timeout) or timeout < 0:
                    return None
            write_timeout = getattr(conn, "write_timeout", None)
            if write_timeout is not None:
                write_timeout = float(write_timeout)
                if math.isnan(write_timeout) or math.isinf(write_timeout) or write_timeout < 0:
                    return None
            return SerialSettings(
                baudrate=int(conn.baudrate),
                bytesize=bytesize,
                parity=_parity_letter(conn.parity),
                stopbits=stopbits,
                timeout=timeout,
                write_timeout=write_timeout,
                xonxoff=bool(getattr(conn, "xonxoff", False)),
                rtscts=bool(getattr(conn, "rtscts", False)),
                dsrdtr=bool(getattr(conn, "dsrdtr", False)),
            )
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _safe_in_waiting(conn: Any) -> int:
        try:
            return int(conn.in_waiting or 0)
        except Exception:  # noqa: BLE001  # pragma: no cover - platform dependent
            return 0

    def _forget_if_closed(self, conn: Any) -> None:
        if _is_connection_open(conn):
            return
        with self._registry_lock:
            for port, cached in list(self._connections.items()):
                if cached is conn:
                    self._connections.pop(port, None)
                    break

    @staticmethod
    def _close_quietly(conn: Any, port: str) -> None:
        """Best-effort close used on paths that must not raise (open/reset)."""
        if conn is None:
            return
        closer = getattr(conn, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception:  # never mask the original error
            logger.warning("failed to release serial handle for %s", port, exc_info=True)


# ----------------------------------------------------------------- singleton
_MANAGER: SerialManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_serial_manager() -> SerialManager:
    """Return the process-wide, lazily created :class:`SerialManager`."""
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = SerialManager()
    return _MANAGER


def reset_serial_manager() -> None:
    """Close and drop the process-wide manager (useful in tests / shutdown)."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.reset()
        _MANAGER = None
