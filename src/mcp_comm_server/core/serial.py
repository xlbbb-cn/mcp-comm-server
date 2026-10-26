"""Stable import path for the serial core layer.

The thread-safe :class:`SerialManager` itself lives in
:mod:`mcp_comm_server.core.serial_manager`; the MCP tool layer imports from
here so the domain module can be reorganised without touching tools::

    from mcp_comm_server.core.serial import get_serial_manager

Every manager method returns a JSON-friendly envelope (``status``/``ok`` on
success, ``code``/``message``/``next_step`` on failure) instead of raising.
"""

from __future__ import annotations

from .serial_manager import (
    DEFAULT_MAX_READ_BYTES,
    DEFAULT_MAX_READ_TIMEOUT,
    ErrorCode,
    SerialManager,
    SerialManagerError,
    SerialSettings,
    get_serial_manager,
    reset_serial_manager,
)

# Backwards-compatible alias for callers that only need the exception type.
SerialError = SerialManagerError

__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_READ_TIMEOUT",
    "ErrorCode",
    "SerialError",
    "SerialManager",
    "SerialManagerError",
    "SerialSettings",
    "get_serial_manager",
    "reset_serial_manager",
]