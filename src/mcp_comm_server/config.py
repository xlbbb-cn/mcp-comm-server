"""Startup configuration for the MCP server.

One place decides how the server is launched: command-line flags first, then
``MCP_COMM_*`` environment variables, then built-in defaults.

    --log-level / MCP_COMM_LOG_LEVEL   INFO

The server speaks MCP over **stdio only**: stdout carries the protocol and
diagnostics go to stderr, so there is no transport, host or port to configure.

The module deliberately does **not** read ``.env`` files: MCP clients pass
their environment explicitly in ``mcpServers.env``, and implicit files make
startup hard to reproduce.  `.env.example` is documentation only.

``load_config()`` is side-effect free apart from reading the environment, so it
can be unit-tested without starting a server; invalid values raise
:class:`ConfigError` with the offending variable/flag in the message.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from . import __version__

__all__ = [
    "DEFAULT_LOG_LEVEL",
    "ConfigError",
    "LogLevel",
    "ServerConfig",
    "build_parser",
    "load_config",
]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_LOG_LEVEL: LogLevel = "INFO"

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

ENV_LOG_LEVEL = "MCP_COMM_LOG_LEVEL"

PROG = "mcp-comm-server"


class ConfigError(ValueError):
    """Raised when an environment variable or CLI value is invalid."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Validated, immutable startup configuration."""

    log_level: LogLevel = DEFAULT_LOG_LEVEL

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly view, used for startup logging and tests."""
        return {"log_level": self.log_level}


def _env(name: str) -> str | None:
    """Return a stripped environment value; empty strings count as unset."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_log_level(value: object) -> LogLevel:
    text = str(value).strip().upper()
    if text not in LOG_LEVELS:
        raise ConfigError(
            f"invalid log level {value!r}; choose one of: {', '.join(LOG_LEVELS)}"
        )
    return text  # type: ignore[return-value]


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (kept separate so tests can inspect the flags)."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "MCP server for serial/COM communication. "
            "Speaks the MCP protocol over stdio only; logs go to stderr."
        ),
        epilog=(
            "The flag has an environment-variable equivalent "
            f"({ENV_LOG_LEVEL}). "
            "Command-line values win over the environment. "
            "Logs always go to stderr; stdout is reserved for the MCP protocol."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        type=str.upper,
        choices=LOG_LEVELS,
        default=None,
        help=(
            "stderr log verbosity: DEBUG, INFO, WARNING, ERROR or CRITICAL "
            f"(default {DEFAULT_LOG_LEVEL}); env {ENV_LOG_LEVEL}"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def load_config(argv: Sequence[str] | None = None) -> ServerConfig:
    """Resolve CLI arguments and environment into a validated config.

    Precedence: CLI flag > ``MCP_COMM_*`` environment variable > default.
    Raises :class:`ConfigError` for invalid values; ``argparse`` itself raises
    ``SystemExit`` for unknown flags and malformed CLI choices.
    """
    args = build_parser().parse_args(argv)

    log_level = _parse_log_level(
        args.log_level
        if args.log_level is not None
        else _env(ENV_LOG_LEVEL) or DEFAULT_LOG_LEVEL
    )

    return ServerConfig(log_level=log_level)
