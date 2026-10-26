"""Output schemas for the four MCP communication tools.

Tools return plain dictionaries (easy to unit-test without the MCP runtime),
but are annotated with :class:`CommResult`.  The MCP SDK uses that annotation
to publish the ``outputSchema`` in ``tools/list`` and to validate/serialize the
structured result sent to the client.

Every result has the same envelope:

* success: ``{"status": "ok", "ok": true, ...tool fields}``
* failure: ``{"status": "error", "ok": false, "code": "...", "message": "...",
  "next_step": "...", "details": {...}}``
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CommResult"]


class CommResult(BaseModel):
    """Uniform JSON envelope returned by every ``*_comm`` tool."""

    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "error"] = Field(
        description="'ok' when the operation succeeded, otherwise 'error'."
    )
    ok: bool = Field(description="Boolean mirror of ``status`` for easy branching.")
    code: str | None = Field(
        default=None,
        description="Machine-readable error code (error results only), e.g. PORT_NOT_FOUND.",
    )
    message: str | None = Field(
        default=None, description="Human-readable error message (error results only)."
    )
    next_step: str | None = Field(
        default=None, description="Actionable recovery hint (error results only)."
    )
    details: dict[str, Any] | None = Field(
        default=None, description="Structured error context (error results only)."
    )

    # --- list_comm ---------------------------------------------------------
    count: int | None = Field(default=None, description="Number of ports listed.")
    ports: list[dict[str, Any]] | None = Field(
        default=None,
        description="Ports with device/name/description/hwid/vid/pid/serial_number/... and is_open.",
    )

    # --- get_comm ----------------------------------------------------------
    port: str | None = Field(default=None, description="Serial port name, e.g. 'COM3'.")
    is_open: bool | None = Field(default=None, description="Whether the port is open.")
    source: str | None = Field(
        default=None, description="Where the settings came from: live/stored/default."
    )
    config: dict[str, Any] | None = Field(
        default=None, description="Effective serial parameters for the port."
    )
    settings: dict[str, Any] | None = Field(
        default=None, description="Alias of ``config`` (effective serial parameters)."
    )
    params: dict[str, Any] | None = Field(
        default=None, description="Alias of ``config`` kept for t1/t2 compatibility."
    )
    defaults: dict[str, Any] | None = Field(
        default=None, description="Default parameters applied to newly opened ports."
    )
    connections: list[dict[str, Any]] | None = Field(
        default=None, description="Every currently open connection."
    )
    open_count: int | None = Field(default=None, description="Number of open connections.")
    in_waiting: int | None = Field(
        default=None, description="Bytes already buffered by the device/driver."
    )

    # --- set_comm ----------------------------------------------------------
    closed: bool | None = Field(
        default=None, description="True when close=true actually closed an open port."
    )
    reused: bool | None = Field(
        default=None, description="True when an already-open handle was reused."
    )
    applied: bool | None = Field(
        default=None, description="True when new settings were applied to a live handle."
    )
    changed: list[str] | None = Field(
        default=None, description="Names of the parameters that were changed."
    )

    # --- write_read_data_comm ---------------------------------------------
    bytes_written: int | None = Field(default=None, description="Bytes accepted by write().")
    bytes_read: int | None = Field(default=None, description="Bytes received in the response.")
    data: str | None = Field(
        default=None, description="Response as text (control characters escaped)."
    )
    data_text: str | None = Field(
        default=None, description="Alias of ``data``; response decoded with ``encoding``."
    )
    data_hex: str | None = Field(
        default=None, description="Response as lower-case hex without separators."
    )
    data_dump: str | None = Field(
        default=None, description="Response as space-separated upper-case hex."
    )
    written: str | None = Field(
        default=None, description="Written payload as escaped text."
    )
    written_text: str | None = Field(default=None, description="Alias of ``written``.")
    written_hex: str | None = Field(
        default=None, description="Written payload as lower-case hex."
    )
    response_text: str | None = Field(
        default=None, description="Response text (when response_format includes text)."
    )
    response_hex: str | None = Field(
        default=None, description="Response hex (when response_format includes hex)."
    )
    response_length: int | None = Field(
        default=None, description="Alias of ``bytes_read`` (response length in bytes)."
    )
    response_dump: str | None = Field(
        default=None, description="Response as space-separated upper-case hex."
    )
    written_dump: str | None = Field(
        default=None, description="Written payload as space-separated upper-case hex."
    )
    complete: bool | None = Field(
        default=None, description="False when the response was cut short by the timeout."
    )
    timed_out: bool | None = Field(
        default=None, description="True when the read stopped because of the timeout."
    )
    truncated: bool | None = Field(
        default=None, description="True when the read hit the maximum byte limit."
    )
    elapsed_ms: float | None = Field(
        default=None, description="Wall-clock duration of the write/read transaction."
    )
    read_timeout: float | None = Field(
        default=None, description="Read timeout used for the transaction, in seconds."
    )
    timeout: float | None = Field(
        default=None, description="Alias of ``read_timeout``."
    )
    expected_bytes: int | None = Field(
        default=None, description="Requested response size, when provided."
    )
    read_delimiter: str | None = Field(
        default=None, description="Terminator used to stop the read, when provided."
    )
    hint: str | None = Field(default=None, description="Extra recovery hint.")
