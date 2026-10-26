"""Serial core layer (``SerialManager`` and its settings/error types)."""

from .serial import (
    ErrorCode,
    SerialError,
    SerialManager,
    SerialManagerError,
    SerialSettings,
    get_serial_manager,
    reset_serial_manager,
)

__all__ = [
    "ErrorCode",
    "SerialError",
    "SerialManager",
    "SerialManagerError",
    "SerialSettings",
    "get_serial_manager",
    "reset_serial_manager",
]
