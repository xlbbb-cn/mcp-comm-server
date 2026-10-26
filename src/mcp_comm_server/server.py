"""MCP server exposing serial/COM communication tools over stdio.

The four tools are registered with the MCP SDK's ``@mcp.tool()`` decorator.
Their implementation lives in :mod:`mcp_comm_server.tools.comm`; the wrappers
below exist so registration, the published input schema and the output schema
(``CommResult``) all stay in one place.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Literal

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import PROG, ConfigError, ServerConfig, load_config
from .core.serial import get_serial_manager
from .schemas import CommResult
from .tools import comm as comm_tools

SERVER_NAME = "mcp-comm-server"

# Logging is configured in main() once --log-level / MCP_COMM_LOG_LEVEL is
# known.  Until then the MCP SDK's default handler is used; it already writes
# to stderr.  stdout must stay reserved for the stdio protocol.
logger = logging.getLogger("mcp_comm_server")

INSTRUCTIONS = """\
Tools for talking to devices over a serial (COM) port.

Typical flow:
1. list_comm  -> find the port name and description, e.g. "COM3"
2. set_comm   -> configure baudrate/parity/... and open the port
3. write_read_data_comm -> send a command and read the reply
4. set_comm(port=..., close=true) -> release the port

All tools return a JSON object with status "ok" or "error"; on error read
"code", "message" and "next_step". Payloads can be sent as str/text (escape
sequences such as \\r\\n are expanded), ascii, hex ("01 03 00 00 00 0A") or
base64; responses are returned as text and hex.
"""

mcp = MCPServer("mcp-comm-server", instructions=INSTRUCTIONS)


@mcp.tool()
def list_comm(include_open: bool = True) -> CommResult:
    """List the serial ports available on this machine.

    Returns the device name (``device``), human readable ``description`` and
    USB metadata for every port. Call this first to discover the ``port`` value
    required by the other tools.

    Args:
        include_open: include an ``is_open`` flag per port. Defaults to True.
    """
    return comm_tools.list_comm(include_open=include_open)


@mcp.tool()
def get_comm(port: str | None = None) -> CommResult:
    """Query a port's current configuration and open state.

    Without ``port``: returns the server defaults and every open connection.
    With ``port``: returns that port's effective settings (``config``), whether
    it is open, where the settings came from, and buffered byte count.

    Args:
        port: optional port name, e.g. "COM3" or "/dev/ttyUSB0".
    """
    return comm_tools.get_comm(port=port)


@mcp.tool()
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
) -> CommResult:
    """Set serial parameters and/or open or close a port.

    Only supplied parameters change; omitted ones keep the stored value for
    that port. Settings survive ``close``. Use ``connect=false`` to store
    settings without opening, or ``close=true`` to release the port.

    Args:
        port: port name from list_comm, e.g. "COM3".
        baudrate: baud rate, e.g. 9600, 115200.
        bytesize: data bits, 5/6/7/8.
        parity: N/E/O/M/S (or none/even/odd/mark/space).
        stopbits: 1, 1.5 or 2.
        timeout: default read timeout in seconds (0 = non-blocking).
        write_timeout: write timeout in seconds (omit for blocking writes).
        xonxoff: software flow control.
        rtscts: hardware RTS/CTS flow control.
        dsrdtr: hardware DSR/DTR flow control.
        connect: open the port after applying settings. Defaults to True.
        close: close the port instead; stored settings are kept.
    """
    return comm_tools.set_comm(
        port=port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
        timeout=timeout,
        write_timeout=write_timeout,
        xonxoff=xonxoff,
        rtscts=rtscts,
        dsrdtr=dsrdtr,
        connect=connect,
        close=close,
    )


@mcp.tool()
def write_read_data_comm(
    port: str,
    data: str,
    data_format: Literal["str", "text", "ascii", "hex", "base64"] = "str",
    encoding: str = "utf-8",
    expected_bytes: int | None = None,
    read_delimiter: str | None = None,
    read_timeout: float | None = None,
    write_timeout: float | None = None,
    append_terminator: str | None = None,
    delay_before_read: float = 0.0,
    flush_input: bool = True,
    response_format: Literal["text", "hex", "both"] = "both",
    baudrate: int | None = None,
    connect_if_closed: bool = True,
) -> CommResult:
    """Write data to a port, then read the device response.

    The connection opened by set_comm is reused; if it is closed it is opened
    with the stored configuration. Returns the response as text and exact hex,
    together with the number of bytes written and read.

    Args:
        port: port name from list_comm, e.g. "COM3".
        data: payload, interpreted according to data_format.
        data_format: str/text (escape sequences expanded), ascii, hex
            (e.g. "01 03 00 00 00 0A") or base64.
        encoding: text encoding for str/text and for decoding the response.
        expected_bytes: read exactly N bytes (bounded by read_timeout).
        read_delimiter: stop reading when this terminator appears, e.g. "\\r\\n"
            or "OK"; use hex syntax when data_format is hex.
        read_timeout: seconds to wait for the response.
        write_timeout: optional write timeout for this transaction.
        append_terminator: text appended before sending, e.g. "\\r\\n" for AT
            commands (str/text/ascii only).
        delay_before_read: seconds to wait between write and read.
        flush_input: discard stale buffered bytes before writing. Defaults True.
        response_format: return response_text, response_hex or both (default).
        baudrate: optional baud rate used only when opening a closed port.
        connect_if_closed: auto-open a closed port; False fails with
            PORT_NOT_OPEN instead.
    """
    return comm_tools.write_read_data_comm(
        port=port,
        data=data,
        data_format=data_format,
        encoding=encoding,
        expected_bytes=expected_bytes,
        read_delimiter=read_delimiter,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
        append_terminator=append_terminator,
        delay_before_read=delay_before_read,
        flush_input=flush_input,
        response_format=response_format,
        baudrate=baudrate,
        connect_if_closed=connect_if_closed,
    )


def configure_logging(level: str = "INFO") -> None:
    """Send every log line to stderr with a consistent format.

    ``force=True`` replaces handlers installed earlier (notably the MCP SDK's
    import-time ``basicConfig``), so ``--log-level`` really wins.  Logging to
    stdout would corrupt the stdio MCP protocol.
    """
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # The MCP SDK logger inherits the root handler, but may carry its own level.
    # Keep it aligned with the configured verbosity.
    logging.getLogger("mcp").setLevel(level)


def run_server(config: ServerConfig) -> None:
    """Run the MCP server over stdio (blocking).

    stdio is the only supported transport: MCP clients launch the process and
    exchange protocol frames on stdin/stdout.
    """
    # The SDK uses this setting for its internal log level.
    mcp.settings.log_level = config.log_level

    logger.info(
        "starting %s %s over stdio (stdout=MCP protocol, logs=stderr)",
        SERVER_NAME,
        __version__,
    )
    mcp.run(transport="stdio")


def _close_serial_ports() -> None:
    """Best-effort shutdown: release every port held by this process."""
    try:
        result = get_serial_manager().close()
    except Exception:  # pragma: no cover - shutdown must never mask the error
        logger.warning("failed to close serial ports during shutdown", exc_info=True)
        return
    closed = result.get("closed_count", 0)
    if closed:
        logger.info("closed %s serial port(s) during shutdown", closed)
    for failure in result.get("failed", []):
        logger.warning(
            "could not close %s during shutdown: %s",
            failure.get("port"),
            failure.get("message"),
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns a process exit code.

    Exit codes: ``0`` normal shutdown, ``1`` unexpected fatal error,
    ``2`` invalid command line / environment configuration, ``130`` Ctrl-C.
    """
    try:
        config = load_config(argv)
    except ConfigError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    logger.debug("configuration: %s", config.as_dict())

    exit_code = 0
    try:
        run_server(config)
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        logger.info("interrupted by user, shutting down")
        exit_code = 130
    except Exception:
        logger.exception("unexpected fatal error, shutting down")
        exit_code = 1
    finally:
        _close_serial_ports()

    if exit_code == 0:
        logger.info("%s stopped", SERVER_NAME)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
