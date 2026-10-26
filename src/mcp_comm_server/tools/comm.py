"""MCP tools for serial (COM) communication.

The tools are thin, synchronous adapters over the thread-safe
:class:`~mcp_comm_server.core.serial_manager.SerialManager` core.  They own only the MCP
contract: argument validation, payload/format conversion, a uniform JSON
envelope and registration-friendly docstrings.

Tools
-----
list_comm             enumerate serial ports (device name + description)
get_comm              query a port's configuration and open state
set_comm              change parameters and/or open/close a port
write_read_data_comm  write bytes (str/ascii/hex/base64) then read the response

Every function returns a plain, JSON-serialisable ``dict``:

* success: ``{"status": "ok", "ok": true, ...}``
* failure: ``{"status": "error", "ok": false, "code": "...", "message": "...",
  "next_step": "...", "details": {...}}``

Functions never raise, so the MCP client always receives a structured result.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Literal

from ..core.serial import (
    DEFAULT_MAX_READ_BYTES,
    ErrorCode,
    SerialManagerError,
    get_serial_manager,
)
from ..utils.serial_utils import (
    PayloadError,
    decode_payload_preview,
    encode_escaped_text,
    encode_payload,
    hex_dump,
    make_json_safe,
)

logger = logging.getLogger(__name__)

DataFormat = Literal["str", "text", "ascii", "hex", "base64"]
ResponseFormat = Literal["text", "hex", "both"]

_TEXT_FORMATS = {"str", "text", "ascii"}


# --------------------------------------------------------------------- envelope
def _ok(**payload: Any) -> dict[str, Any]:
    """Build a success envelope with the t2/t1-compatible status fields."""
    result: dict[str, Any] = {"status": "ok", "ok": True}
    result.update(payload)
    return make_json_safe(result)


def _error(
    code: ErrorCode | str,
    message: str,
    *,
    port: str | None = None,
    next_step: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the uniform error envelope: ``code`` + ``message`` are required."""
    result: dict[str, Any] = {
        "status": "error",
        "ok": False,
        "code": str(code),
        "message": message,
    }
    if port is not None:
        result["port"] = str(port)
    elif details and details.get("port") is not None:
        result["port"] = str(details["port"])
    if next_step:
        result["next_step"] = next_step
    if details:
        result["details"] = make_json_safe(details)
    return result


def _passthrough(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize a t2 manager envelope and make every field JSON-safe."""
    normalized = dict(result)
    status = normalized.get("status")
    if status not in {"ok", "error"}:
        status = "ok" if normalized.get("ok", True) else "error"
    normalized["status"] = status
    normalized["ok"] = status == "ok"
    if normalized.get("port") is not None and not isinstance(normalized["port"], str):
        # t2 echoes the rejected value (which may be any type) in INVALID_PORT.
        normalized["port"] = str(normalized["port"])
    if status == "error":
        normalized.setdefault("code", ErrorCode.INTERNAL_ERROR.value)
        normalized.setdefault("message", "serial operation failed")
    return make_json_safe(normalized)


def _internal_error(operation: str, exc: Exception, port: str | None = None) -> dict[str, Any]:
    logger.exception("unexpected error during %s", operation)
    return _error(
        ErrorCode.INTERNAL_ERROR,
        f"unexpected error during {operation}: {exc}",
        port=port,
        details={"operation": operation, "exception": type(exc).__name__},
    )


def _invalid_argument(name: str, value: Any, reason: str, port: str | None = None) -> dict[str, Any]:
    """Uniform INVALID_PARAMETER envelope for tool-level validation."""
    return _error(
        ErrorCode.INVALID_PARAMETER,
        f"invalid value for {name}: {reason}",
        port=port,
        next_step="check the tool arguments; omitted numeric arguments must be finite and >= 0",
        details={"parameter": name, "value": value, "reason": reason},
    )


def _validate_seconds(value: Any, name: str, *, port: str) -> dict[str, Any] | None:
    """Return an error envelope for an optional, finite, non-negative duration."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _invalid_argument(name, value, "must be a number >= 0", port)
    if math.isnan(value) or math.isinf(value) or value < 0:
        return _invalid_argument(name, value, "must be a finite number >= 0", port)
    return None


# ------------------------------------------------------------------- list_comm
def list_comm(include_open: bool = True) -> dict[str, Any]:
    """List the serial ports available on this machine.

    Use this first: it tells you which port names (e.g. ``"COM3"``) can be
    passed to :func:`set_comm`, :func:`get_comm` and
    :func:`write_read_data_comm`.

    Args:
        include_open: include an ``is_open`` flag showing which ports already
            have an open connection in this server process. Defaults to True.

    Returns:
        ``{"status": "ok", "ok": true, "count": int, "ports": [...]}`` where
        every port has ``device``, ``name``, ``description``, ``hwid``,
        ``vid``, ``pid``, ``serial_number``, ``manufacturer``, ``product``,
        ``interface``, ``location`` and (optionally) ``is_open``.
    """
    manager = get_serial_manager()
    try:
        result = manager.list_ports(include_open=include_open)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive, t2 returns envelopes
        return _internal_error("list_comm", exc)
    return _passthrough(result)


# -------------------------------------------------------------------- get_comm
def get_comm(port: str | None = None) -> dict[str, Any]:
    """Query the current serial configuration and connection state.

    Args:
        port: port name from :func:`list_comm`, e.g. ``"COM3"`` or
            ``"/dev/ttyUSB0"``. When omitted, the server defaults and every
            open connection are returned instead.

    Returns:
        With ``port``: ``{"status": "ok", "port": "COM3", "is_open": bool,
        "source": "live"|"stored"|"default", "config": {...},
        "settings": {...}, "in_waiting": int}``.

        Without ``port``: ``{"status": "ok", "defaults": {...},
        "connections": [...], "open_count": int}``.
    """
    manager = get_serial_manager()

    if port is not None:
        try:
            state = manager.get_params(port)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
            return _internal_error("get_comm", exc, port)
        return _passthrough(state)

    try:
        defaults = manager.get_defaults()
        connections = manager.list_connections()
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
        return _internal_error("get_comm", exc)

    if defaults.get("status") == "error":
        return _passthrough(defaults)
    if connections.get("status") == "error":
        return _passthrough(connections)

    items = connections.get("connections", [])
    return _ok(
        defaults=defaults.get("defaults", defaults.get("settings", {})),
        connections=items,
        open_count=len(items),
    )


# -------------------------------------------------------------------- set_comm
def set_comm(
    port: str,
    baudrate: int | None = None,
    bytesize: int | None = None,
    parity: str | None = None,
    stopbits: float | None = None,
    timeout: float | None = None,
    write_timeout: float | None = None,
    xonxoff: bool | None = None,
    rtscts: bool | None = None,
    dsrdtr: bool | None = None,
    connect: bool = True,
    close: bool = False,
) -> dict[str, Any]:
    """Configure a serial port and/or open or close it.

    Only the parameters you pass are changed; omitted parameters keep the
    values previously stored for that port (or the server defaults). Settings
    survive a ``close`` and are reused by the next
    :func:`write_read_data_comm`.

    Args:
        port: port name from :func:`list_comm`, e.g. ``"COM3"``,
            ``"/dev/ttyUSB0"``, or a pyserial URL such as ``"loop://"``.
        baudrate: baud rate, e.g. 9600, 57600, 115200, 921600.
        bytesize: data bits, 5 / 6 / 7 / 8.
        parity: ``"N"`` none, ``"E"`` even, ``"O"`` odd, ``"M"`` mark,
            ``"S"`` space (long names such as ``"even"`` are accepted).
        stopbits: 1, 1.5 or 2.
        timeout: default read timeout in seconds (0 = non-blocking).
        write_timeout: write timeout in seconds (omit for blocking writes).
        xonxoff: software flow control.
        rtscts: hardware (RTS/CTS) flow control.
        dsrdtr: hardware (DSR/DTR) flow control.
        connect: open the port after applying the configuration. Set False to
            only store the configuration without opening.
        close: close the port instead of opening it. Takes precedence over
            ``connect``; the stored configuration is kept.

    Returns:
        ``{"status": "ok", "port": "COM3", "is_open": bool, "config": {...},
        "settings": {...}, "changed": [...]}``; for ``close=True`` also
        ``closed: bool``. Errors carry ``code`` and ``message``.
    """
    manager = get_serial_manager()

    overrides: dict[str, Any] = {
        key: value
        for key, value in {
            "baudrate": baudrate,
            "bytesize": bytesize,
            "parity": parity,
            "stopbits": stopbits,
            "timeout": timeout,
            "write_timeout": write_timeout,
            "xonxoff": xonxoff,
            "rtscts": rtscts,
            "dsrdtr": dsrdtr,
        }.items()
        if value is not None
    }

    try:
        if close:
            # ``close`` wins over ``connect``, but parameters must not be
            # silently dropped: store/validate them first so the next open
            # reuses the requested configuration.
            if overrides:
                stored = manager.set_params(port, **overrides)
                if stored.get("status") == "error":
                    return _passthrough(stored)
            closed = manager.close(port)
            if closed.get("status") == "error":
                return _passthrough(closed)
            state = manager.get_params(port)
            if state.get("status") == "error":
                return _passthrough(state)
            settings = state.get("settings", state.get("config", {}))
            return _ok(
                port=state.get("port", port),
                closed=bool(closed.get("closed", False)),
                is_open=False,
                source=state.get("source"),
                config=settings,
                settings=settings,
                params=settings,
                changed=sorted(overrides),
            )

        if connect:
            result = manager.open(port, **overrides)
        else:
            result = manager.set_params(port, **overrides)
    except SerialManagerError as exc:
        return _passthrough(exc.to_result())
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
        return _internal_error("set_comm", exc, port)

    return _passthrough(result)


# ---------------------------------------------------------- write_read_data_comm
def _encode_delimiter(
    delimiter: str,
    *,
    data_format: str,
    encoding: str,
) -> bytes:
    """Encode the read terminator using the same grammar as the payload."""
    if data_format == "hex":
        return encode_payload(delimiter, data_format="hex", encoding=encoding)
    if data_format == "base64":
        return encode_payload(delimiter, data_format="base64", encoding=encoding)
    if data_format == "ascii":
        return encode_escaped_text(delimiter, "ascii")
    return encode_escaped_text(delimiter, encoding)


def write_read_data_comm(
    port: str,
    data: str,
    data_format: DataFormat = "str",
    encoding: str = "utf-8",
    expected_bytes: int | None = None,
    read_delimiter: str | None = None,
    read_timeout: float | None = None,
    write_timeout: float | None = None,
    append_terminator: str | None = None,
    delay_before_read: float = 0.0,
    flush_input: bool = True,
    response_format: ResponseFormat = "both",
    baudrate: int | None = None,
    connect_if_closed: bool = True,
) -> dict[str, Any]:
    """Write data to a serial port and read back the device response.

    The connection opened by :func:`set_comm` is reused when possible;
    otherwise it is opened with the stored configuration (plus the optional
    ``baudrate`` override).

    Args:
        port: port name, e.g. ``"COM3"`` (see :func:`list_comm`).
        data: payload to send, interpreted according to ``data_format``.
        data_format:
            ``"str"`` / ``"text"`` (default) plain text, escape sequences such
            as ``"\\r\\n"`` and ``"\\x41"`` are expanded;
            ``"ascii"`` plain ASCII text, escape sequences expanded;
            ``"hex"`` hexadecimal bytes, e.g. ``"01 03 00 00 00 0A"``;
            separators (space, comma, ``-``, ``:``, ``_``) are ignored and a
            ``0x`` prefix is accepted at the start of each token;
            ``"base64"`` base64-encoded bytes.
        encoding: text encoding for str/text payloads and for decoding the
            response. Defaults to utf-8.
        expected_bytes: read exactly this many bytes (bounded by
            ``read_timeout``). Omit to read until the port goes idle.
        read_delimiter: stop reading as soon as this terminator appears, e.g.
            ``"\\r\\n"`` or ``"OK"``. Use hex syntax when
            ``data_format="hex"``. The read ends at whichever comes first:
            ``expected_bytes`` or this delimiter.
        read_timeout: seconds to wait for the response; defaults to the port's
            configured timeout.
        write_timeout: optional write timeout for this transaction.
        append_terminator: text appended to the payload before writing, e.g.
            ``"\\r\\n"`` for AT commands. Applies to str/text/ascii formats;
            include it in ``data`` for hex/base64 payloads.
        delay_before_read: seconds to wait between write and read, for slow
            devices.
        flush_input: discard bytes already buffered before writing (clears
            stale echoes). Defaults to True.
        response_format: include ``response_text``, ``response_hex`` or both
            (default). ``data``/``data_hex`` are always returned.
        baudrate: optional baud rate used only when the port must be opened.
        connect_if_closed: open the port automatically when it is not open;
            set False to fail with ``PORT_NOT_OPEN`` instead.

    Returns:
        ``{"status": "ok", "port": "COM3", "bytes_written": int,
        "bytes_read": int, "data": str, "data_hex": str, "data_dump": str,
        "complete": bool, "timed_out": bool, "elapsed_ms": float,
        "read_timeout": float, "response_text": str, "response_hex": str}``.

        On failure: ``{"status": "error", "code": "...", "message": "...",
        "next_step": "...", "details": {...}}``.

        A read that receives no bytes is **not** an error: the envelope is
        ``status="ok"`` with ``bytes_read=0``, ``empty_response=true``,
        ``timed_out=true`` and an actionable ``hint``.
    """
    manager = get_serial_manager()
    if not isinstance(port, str) or not port.strip():
        return _error(
            ErrorCode.INVALID_PORT,
            "port must be a non-empty string (for example 'COM3' or '/dev/ttyUSB0')",
            next_step="use list_comm to discover valid port names, then pass one of them",
            details={"port": port},
        )
    fmt = (data_format or "str").lower()
    if fmt not in _TEXT_FORMATS | {"hex", "base64"}:
        return _error(
            ErrorCode.INVALID_PARAMETER,
            f"unsupported data_format {data_format!r}",
            port=port,
            next_step="use data_format=str, text, ascii, hex or base64",
            details={"allowed": ["str", "text", "ascii", "hex", "base64"]},
        )

    resp_fmt = (response_format or "both").lower()
    if resp_fmt not in {"text", "hex", "both"}:
        return _error(
            ErrorCode.INVALID_PARAMETER,
            f"unsupported response_format {response_format!r}",
            port=port,
            next_step="use response_format=text, hex or both",
            details={"allowed": ["text", "hex", "both"]},
        )

    # Validate every numeric argument before touching the port: an invalid
    # read_timeout must not open a connection as a side effect.
    for name, value in (
        ("read_timeout", read_timeout),
        ("write_timeout", write_timeout),
        ("delay_before_read", delay_before_read),
    ):
        error = _validate_seconds(value, name, port=port)
        if error is not None:
            return error
    if expected_bytes is not None:
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes <= 0
        ):
            return _invalid_argument(
                "expected_bytes", expected_bytes, "must be a positive integer", port
            )
        if expected_bytes > DEFAULT_MAX_READ_BYTES:
            return _invalid_argument(
                "expected_bytes",
                expected_bytes,
                f"must be <= {DEFAULT_MAX_READ_BYTES}",
                port,
            )
    if baudrate is not None and (
        isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0
    ):
        return _invalid_argument("baudrate", baudrate, "must be a positive integer", port)

    try:
        payload = encode_payload(data, data_format=fmt, encoding=encoding)  # type: ignore[arg-type]
        if not payload:
            return _error(
                ErrorCode.INVALID_DATA,
                "nothing to write: payload is empty",
                port=port,
                next_step="pass a non-empty data payload",
            )

        if append_terminator and fmt in _TEXT_FORMATS:
            payload += encode_escaped_text(
                append_terminator, "ascii" if fmt == "ascii" else encoding
            )

        delimiter_bytes = (
            _encode_delimiter(read_delimiter, data_format=fmt, encoding=encoding)
            if read_delimiter
            else None
        )
    except PayloadError as exc:
        return _error(
            ErrorCode.INVALID_DATA,
            str(exc),
            port=port,
            next_step="check the payload: str/text, ascii, hex (e.g. '01 03') or base64",
        )
    except (LookupError, UnicodeEncodeError) as exc:
        return _error(
            ErrorCode.INVALID_DATA,
            f"cannot encode payload: {exc}",
            port=port,
            next_step="use a valid encoding (e.g. utf-8, latin-1) or data_format=hex",
            details={"encoding": encoding, "data_format": fmt},
        )

    try:
        if not manager.is_open(port):
            if not connect_if_closed:
                return _error(
                    ErrorCode.PORT_NOT_OPEN,
                    f"serial port {port!r} is not open",
                    port=port,
                    next_step="call set_comm(port=...) first, or set connect_if_closed=true",
                    details={"operation": "write_read"},
                )
            open_params: dict[str, Any] = {}
            if baudrate is not None:
                open_params["baudrate"] = baudrate
            opened = manager.open(port, **open_params)
            if opened.get("status") == "error":
                return _passthrough(opened)

        result = manager.write_read(
            port,
            payload,
            # The tool already produced raw bytes; the encoding only affects
            # how the *response* text is decoded (the core does not need it).
            expected_bytes=expected_bytes,
            delimiter=delimiter_bytes,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            delay_before_read=delay_before_read,
            flush_input=flush_input,
        )
    except SerialManagerError as exc:
        return _passthrough(exc.to_result())
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
        return _internal_error("write_read_data_comm", exc, port)

    if result.get("status") == "error":
        if result.get("code") == ErrorCode.READ_TIMEOUT.value:
            # Architecture contract: an empty response is data, not a failure.
            return _empty_read_result(
                port=result.get("port", port),
                written=payload,
                data_format=fmt,
                response_format=resp_fmt,
                encoding=encoding,
                expected_bytes=expected_bytes,
                read_delimiter=read_delimiter,
                details=result.get("details") or {},
            )
        return _passthrough(result)

    response: bytes = result.get("data", b"")
    written: bytes = result.get("written", b"")
    response_text = decode_payload_preview(response, encoding=encoding)
    bytes_read = int(result.get("bytes_read", len(response)))

    output = _ok(
        port=result.get("port", port),
        bytes_written=int(result.get("bytes_written", len(written))),
        bytes_read=bytes_read,
        data=response_text,
        data_text=response_text,
        data_hex=response.hex(),
        data_dump=hex_dump(response),
        written=decode_payload_preview(written, encoding=encoding),
        written_text=decode_payload_preview(written, encoding=encoding),
        written_hex=written.hex(),
        written_dump=hex_dump(written),
        complete=result.get("complete"),
        timed_out=result.get("timed_out"),
        truncated=result.get("truncated"),
        elapsed_ms=result.get("elapsed_ms"),
        read_timeout=result.get("timeout"),
        timeout=result.get("timeout"),
        expected_bytes=result.get("expected_bytes"),
        read_delimiter=read_delimiter,
        data_format=fmt,
        encoding=encoding,
    )
    _add_response_fields(output, response, resp_fmt, encoding=encoding)
    output["response_length"] = bytes_read
    return output


def _add_response_fields(
    output: dict[str, Any],
    response: bytes,
    response_format: str,
    encoding: str = "utf-8",
) -> None:
    if response_format in {"text", "both"}:
        output["response_text"] = decode_payload_preview(response, encoding=encoding)
    if response_format in {"hex", "both"}:
        output["response_hex"] = response.hex()
        output["response_dump"] = hex_dump(response)


def _empty_read_result(
    *,
    port: Any,
    written: bytes,
    data_format: str,
    response_format: str,
    encoding: str,
    expected_bytes: int | None,
    read_delimiter: str | None,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Build the success envelope for a write that received no reply bytes."""
    hint = (
        "no bytes were received within the timeout: check device power, wiring "
        "(TX/RX), baudrate/parity, or increase read_timeout / expected_bytes"
    )
    output = _ok(
        port=port,
        bytes_written=int(details.get("bytes_written", len(written))),
        bytes_read=0,
        data="",
        data_text="",
        data_hex="",
        data_dump="",
        written=decode_payload_preview(written, encoding=encoding),
        written_text=decode_payload_preview(written, encoding=encoding),
        written_hex=written.hex(),
        written_dump=hex_dump(written),
        complete=False,
        timed_out=True,
        truncated=False,
        elapsed_ms=details.get("elapsed_ms"),
        read_timeout=details.get("timeout"),
        timeout=details.get("timeout"),
        expected_bytes=expected_bytes,
        read_delimiter=read_delimiter,
        data_format=data_format,
        encoding=encoding,
        empty_response=True,
        hint=hint,
    )
    _add_response_fields(output, b"", response_format, encoding=encoding)
    # response_text/hex are empty strings, not missing: keep the shape stable.
    output["response_length"] = 0
    return output
