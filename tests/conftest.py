"""Shared fixtures: every test starts and ends with no open serial ports."""

from __future__ import annotations

import pytest

from mcp_comm_server.core.serial import reset_serial_manager


@pytest.fixture(autouse=True)
def clean_serial_manager():
    """Close cached handles and forget stored settings between tests."""
    reset_serial_manager()
    yield
    reset_serial_manager()
